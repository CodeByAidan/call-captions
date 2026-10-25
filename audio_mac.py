"""macOS audio capture through Core Audio process taps (macOS 14.2 and newer).

AppTapSource     just Discord's audio: a tap on Discord's processes, including the helper
                 processes Electron plays voices from (matched by bundle ID).
SystemTapSource  everything you hear: a tap on every process.

A tap only delivers audio through an aggregate device, so each capture builds a private
one around its tap. The first time, macOS asks to allow "System Audio Recording".
The flow follows Chromium's media/audio/mac/catap_audio_input_stream.mm.
"""

import ctypes
import os
import platform
import queue
import sys
import threading
import time
from ctypes import CFUNCTYPE, POINTER, Structure, byref, c_double, c_int32, c_uint32, c_void_p, sizeof

import numpy as np
import objc
from Foundation import NSArray, NSDictionary, NSNumber, NSUUID

from audio import CHUNK, SAMPLE_RATE, Resampler, to_mono


def _fourcc(code):
    return int.from_bytes(code.encode("ascii"), "big")


SYSTEM_OBJECT = 1
SCOPE_GLOBAL = _fourcc("glob")
ELEMENT_MAIN = 0
PROCESS_OBJECT_LIST = _fourcc("prs#")
PROCESS_BUNDLE_ID = _fourcc("pbid")
TAP_FORMAT = _fourcc("tfmt")
TAP_DESCRIPTION = _fourcc("tdsc")
NOMINAL_SAMPLE_RATE = _fourcc("nsrt")
FORMAT_IS_FLOAT = 1

# Bundle IDs of Discord clients. Their helper processes use sub-IDs ("com.hnc.Discord.helper").
DISCORD_BUNDLES = {
    "com.hnc.Discord": "Discord", "com.hnc.DiscordPTB": "Discord PTB",
    "com.hnc.DiscordCanary": "Discord Canary", "dev.vencord.vesktop": "Vesktop",
}
PERMISSION_HELP = ("Allow System Audio Recording for Call Captions in System Settings → "
                   "Privacy & Security, then restart it")


class PropertyAddress(Structure):
    _fields_ = [("mSelector", c_uint32), ("mScope", c_uint32), ("mElement", c_uint32)]


class StreamDescription(Structure):
    _fields_ = [("mSampleRate", c_double), ("mFormatID", c_uint32), ("mFormatFlags", c_uint32),
                ("mBytesPerPacket", c_uint32), ("mFramesPerPacket", c_uint32), ("mBytesPerFrame", c_uint32),
                ("mChannelsPerFrame", c_uint32), ("mBitsPerChannel", c_uint32), ("mReserved", c_uint32)]


class AudioBuffer(Structure):
    _fields_ = [("mNumberChannels", c_uint32), ("mDataByteSize", c_uint32), ("mData", c_void_p)]


class AudioBufferList(Structure):
    _fields_ = [("mNumberBuffers", c_uint32), ("mBuffers", AudioBuffer * 1)]


IOProc = CFUNCTYPE(c_int32, c_uint32, c_void_p, POINTER(AudioBufferList), c_void_p, c_void_p, c_void_p, c_void_p)

# Loading CoreAudio also registers its Objective-C classes (CATapDescription).
_core_audio = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
_core_foundation = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
_core_foundation.CFRelease.argtypes = [c_void_p]
_core_foundation.CFRelease.restype = None


def _function(name, *argtypes):
    try:
        f = getattr(_core_audio, name)
    except AttributeError:  # older macOS
        return None
    f.restype = c_int32
    f.argtypes = argtypes
    return f


_GetPropertyDataSize = _function("AudioObjectGetPropertyDataSize", c_uint32, POINTER(PropertyAddress),
                                 c_uint32, c_void_p, POINTER(c_uint32))
_GetPropertyData = _function("AudioObjectGetPropertyData", c_uint32, POINTER(PropertyAddress),
                             c_uint32, c_void_p, POINTER(c_uint32), c_void_p)
_SetPropertyData = _function("AudioObjectSetPropertyData", c_uint32, POINTER(PropertyAddress),
                             c_uint32, c_void_p, c_uint32, c_void_p)
_CreateProcessTap = _function("AudioHardwareCreateProcessTap", c_void_p, POINTER(c_uint32))
_DestroyProcessTap = _function("AudioHardwareDestroyProcessTap", c_uint32)
_CreateAggregateDevice = _function("AudioHardwareCreateAggregateDevice", c_void_p, POINTER(c_uint32))
_DestroyAggregateDevice = _function("AudioHardwareDestroyAggregateDevice", c_uint32)
_CreateIOProcID = _function("AudioDeviceCreateIOProcID", c_uint32, IOProc, c_void_p, POINTER(c_void_p))
_DestroyIOProcID = _function("AudioDeviceDestroyIOProcID", c_uint32, c_void_p)
_DeviceStart = _function("AudioDeviceStart", c_uint32, c_void_p)
_DeviceStop = _function("AudioDeviceStop", c_uint32, c_void_p)


def supported():
    version = tuple(int(part) for part in (platform.mac_ver()[0] or "0").split(".")[:2])
    return version >= (14, 2) and _CreateProcessTap is not None


def _address(selector):
    return PropertyAddress(selector, SCOPE_GLOBAL, ELEMENT_MAIN)


def _check(status, doing):
    if status:
        raise OSError(status, f"{doing} failed (OSStatus {status})")


def _get(obj, selector, value):
    size = c_uint32(sizeof(value))
    _check(_GetPropertyData(obj, byref(_address(selector)), 0, None, byref(size), byref(value)),
           f"reading property {selector:#x}")
    return value


def _get_string(obj, selector):
    ref = c_void_p()
    size = c_uint32(sizeof(ref))
    if _GetPropertyData(obj, byref(_address(selector)), 0, None, byref(size), byref(ref)) or not ref.value:
        return None
    try:
        return str(objc.objc_object(c_void_p=ref))
    finally:
        _core_foundation.CFRelease(ref)


def _process_objects():
    """Core Audio's objects for every process that's using audio."""
    address = _address(PROCESS_OBJECT_LIST)
    size = c_uint32()
    _check(_GetPropertyDataSize(SYSTEM_OBJECT, byref(address), 0, None, byref(size)), "listing audio processes")
    ids = (c_uint32 * (size.value // 4))()
    _check(_GetPropertyData(SYSTEM_OBJECT, byref(address), 0, None, byref(size), ids), "listing audio processes")
    return list(ids[:size.value // 4])


_bundle_ids = {}


def find_discord():
    """(process objects, client name) for Discord's audio processes, or None."""
    found, name = [], None
    objects = _process_objects()
    # Forget processes that have gone, so a reused object ID gets looked up again.
    for gone in set(_bundle_ids) - set(objects):
        del _bundle_ids[gone]
    for obj in objects:
        if obj not in _bundle_ids:
            _bundle_ids[obj] = _get_string(obj, PROCESS_BUNDLE_ID) or ""
        bundle = _bundle_ids[obj]
        for main, label in DISCORD_BUNDLES.items():
            if bundle == main or bundle.startswith(main + "."):
                found.append(obj)
                name = label
    return (tuple(sorted(found)), name) if found else None


def _has_permission(tap):
    # Chromium's probe: without permission, writing a tap's own description back fails.
    ref = c_void_p()
    size = c_uint32(sizeof(ref))
    if _GetPropertyData(tap, byref(_address(TAP_DESCRIPTION)), 0, None, byref(size), byref(ref)) or not ref.value:
        return False
    try:
        return _SetPropertyData(tap, byref(_address(TAP_DESCRIPTION)), 0, None, sizeof(ref), byref(ref)) == 0
    finally:
        _core_foundation.CFRelease(ref)


class _Tap:
    """A mono tap on some processes (or all of them), wrapped in a private aggregate device."""

    def __init__(self, processes=None):
        self.tap, self.device, self.proc_id = c_uint32(0), c_uint32(0), c_void_p()
        TapDescription = objc.lookUpClass("CATapDescription")
        if processes is None:
            description = TapDescription.alloc().initStereoGlobalTapButExcludeProcesses_(NSArray.array())
        else:
            ids = NSArray.arrayWithArray_([NSNumber.numberWithUnsignedInt_(p) for p in processes])
            description = TapDescription.alloc().initStereoMixdownOfProcesses_(ids)
        description.setMono_(True)
        description.setPrivate_(True)
        description.setName_("Call Captions")
        self.description = description

        self.tap = c_uint32()
        _check(_CreateProcessTap(objc.pyobjc_id(description), byref(self.tap)), "creating the audio tap")
        try:
            fmt = _get(self.tap.value, TAP_FORMAT, StreamDescription())
            if not fmt.mFormatFlags & FORMAT_IS_FLOAT or fmt.mBitsPerChannel != 32:
                raise OSError(0, f"unexpected tap format (flags {fmt.mFormatFlags:#x}, {fmt.mBitsPerChannel} bit)")
            self.channels = max(1, fmt.mChannelsPerFrame)
            properties = NSDictionary.dictionaryWithDictionary_({
                "name": "Call Captions", "uid": NSUUID.UUID().UUIDString(),
                "taps": [{"uid": description.UUID().UUIDString(), "drift": True}],
                "tapautostart": False, "private": True,
            })
            self.device = c_uint32()
            _check(_CreateAggregateDevice(objc.pyobjc_id(properties), byref(self.device)),
                   "creating the capture device")
            try:
                self.rate = _get(self.device.value, NOMINAL_SAMPLE_RATE, c_double()).value
            except OSError:
                self.rate = fmt.mSampleRate
        except Exception:
            self.close()
            raise

    def has_permission(self):
        return _has_permission(self.tap.value)

    def start(self, callback):
        """Starts calling callback(bytes) from Core Audio's thread. The first time, this
        is when macOS asks for permission, and it can block until the user answers."""
        def io_proc(device, now, input_data, input_time, output_data, output_time, client):
            try:
                buffers = input_data.contents
                if buffers.mNumberBuffers:
                    buffer = buffers.mBuffers[0]
                    if buffer.mData and buffer.mDataByteSize:
                        callback(ctypes.string_at(buffer.mData, buffer.mDataByteSize))
            except Exception:
                pass
            return 0

        self._io_proc = IOProc(io_proc)  # must outlive the device
        _check(_CreateIOProcID(self.device.value, self._io_proc, None, byref(self.proc_id)),
               "connecting to the audio tap")
        _check(_DeviceStart(self.device.value, self.proc_id), "starting audio capture")

    def close(self):
        if self.proc_id.value:
            _DeviceStop(self.device.value, self.proc_id)
            _DestroyIOProcID(self.device.value, self.proc_id)
            self.proc_id = c_void_p()
        if self.device.value:
            _DestroyAggregateDevice(self.device.value)
            self.device = c_uint32(0)
        if self.tap.value:
            _DestroyProcessTap(self.tap.value)
            self.tap = c_uint32(0)


class _TapSource:
    """Capture loop shared by both sources: subclasses say what to tap."""

    asked_permission = False

    def __init__(self, on_chunk, on_status):
        self.on_chunk = on_chunk
        self.on_status = on_status
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stop.set()

    def find_target(self):
        """Something that identifies what to tap, or None if there's nothing yet."""
        raise NotImplementedError

    def open(self, target):
        raise NotImplementedError

    def label(self, target):
        raise NotImplementedError

    def waiting_message(self):
        return "Waiting for audio…"

    def _run(self):
        if not supported():
            self.on_status("Capturing audio needs macOS 14.2 or newer", True)
            return
        waiting_shown = False
        while not self._stop.is_set():
            try:
                target = self.find_target()
            except Exception:
                target = None
            if target is None:
                if not waiting_shown:
                    self.on_status(self.waiting_message())
                    waiting_shown = True
                self._stop.wait(2.0)
                continue
            waiting_shown = False
            try:
                self._capture(target)
            except PermissionError:
                self.on_status(PERMISSION_HELP, True)
                self._stop.wait(30.0)
            except Exception as e:
                print(f"Audio capture error: {e}", file=sys.stderr)
                self.on_status("Audio capture stopped — retrying…")
                self._stop.wait(2.0)

    def _capture(self, target):
        tap = self.open(target)
        incoming = queue.Queue()
        try:
            if not _TapSource.asked_permission:
                self.on_status("If macOS asks, allow Call Captions to record audio", True)
                _TapSource.asked_permission = True
            tap.start(incoming.put)
            if not tap.has_permission():
                raise PermissionError
            self.on_status(f"Listening to {self.label(target)}")
            resampler = Resampler(round(tap.rate))
            pending = np.zeros(0, np.float32)
            last_data = last_check = time.monotonic()
            while not self._stop.is_set():
                try:
                    data = incoming.get(timeout=0.1)
                except queue.Empty:
                    data = None
                now = time.monotonic()
                if data:
                    samples = to_mono(data, "float32", tap.channels)
                    pending = self._emit(np.concatenate([pending, resampler.process(samples)]))
                    last_data = now
                elif now - last_data >= 0.1:
                    # Keep time moving if the device stalls, so the translator still
                    # notices when someone stops talking.
                    pending = self._emit(np.concatenate([pending, np.zeros(SAMPLE_RATE // 10, np.float32)]))
                    last_data += 0.1
                if now - last_check >= 3.0:
                    last_check = now
                    if self.find_target() != target:
                        return  # Discord restarted or started a new audio process: start over
        finally:
            tap.close()

    def _emit(self, samples):
        whole = len(samples) // CHUNK * CHUNK
        for i in range(0, whole, CHUNK):
            self.on_chunk(samples[i:i + CHUNK].copy())
        return samples[whole:]


class AppTapSource(_TapSource):
    def find_target(self):
        return find_discord()

    def open(self, target):
        return _Tap(target[0])

    def label(self, target):
        return target[1]

    def waiting_message(self):
        return "Waiting for Discord…"


class SystemTapSource(_TapSource):
    def find_target(self):
        return "everything"

    def open(self, target):
        return _Tap(None)

    def label(self, target):
        return "everything you hear"


def selftest():
    """Builds a tap and its capture device without starting it (starting it would ask for
    permission). Returns a report; raises if the plumbing is broken. Missing permission
    isn't a failure: a build machine can't grant it."""
    if not supported():
        raise RuntimeError(f"needs macOS 14.2 or newer (this is {platform.mac_ver()[0]})")
    report, errors = [], []

    def run():
        try:
            tap = _Tap(None)
            try:
                report.append(f"tap ok ({tap.rate:.0f} Hz, {tap.channels} ch)")
                report.append("permission granted" if tap.has_permission() else "no recording permission yet")
            finally:
                tap.close()
            discord = find_discord()
            report.append(f"{len(_process_objects())} audio processes, Discord {'found' if discord else 'not running'}")
        except Exception as e:
            errors.append(e)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(30)
    if thread.is_alive():
        raise TimeoutError("Core Audio didn't answer within 30 s")
    if errors:
        raise RuntimeError("; ".join(report)) from errors[0]
    return "; ".join(report)
