"""Windows audio capture through WASAPI loopback.

AppLoopbackSource     just Discord's audio, using the per-app loopback added in Windows 10
                      2004 (what OBS's "Application Audio Capture" uses). It captures the
                      whole Discord process tree, so it doesn't matter which helper
                      process plays the voices.
DeviceLoopbackSource  everything playing on the default output device.
"""

import ctypes
import os
import sys
import threading
import time
from collections import namedtuple
from ctypes import POINTER, Structure, byref, c_int, c_longlong, c_uint32, c_uint64, c_void_p, wintypes

import comtypes
import numpy as np
import psutil
from comtypes import COMMETHOD, GUID, HRESULT, COMObject, IUnknown

from audio import CHUNK, SAMPLE_RATE, Resampler, to_mono

CLSCTX_ALL = 23
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
WAVE_FORMAT_PCM = 1
WAVE_FORMAT_IEEE_FLOAT = 3
WAVE_FORMAT_EXTENSIBLE = 0xFFFE
KSDATAFORMAT_SUBTYPE_IEEE_FLOAT = GUID("{00000003-0000-0010-8000-00AA00389B71}")
VT_BLOB = 65
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"
E_RENDER, E_CONSOLE = 0, 0
BUFFER_DURATION = 2_000_000  # 200 ms, in 100 ns units

DISCORD_APPS = {
    "discord.exe": "Discord", "discordptb.exe": "Discord PTB", "discordcanary.exe": "Discord Canary",
    "vesktop.exe": "Vesktop", "legcord.exe": "Legcord", "equibop.exe": "Equibop",
}

Format = namedtuple("Format", "rate channels sample block_align")


class WAVEFORMATEX(Structure):
    _pack_ = 1
    _fields_ = [("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD),
                ("nSamplesPerSec", wintypes.DWORD), ("nAvgBytesPerSec", wintypes.DWORD),
                ("nBlockAlign", wintypes.WORD), ("wBitsPerSample", wintypes.WORD), ("cbSize", wintypes.WORD)]


class WAVEFORMATEXTENSIBLE(Structure):
    _pack_ = 1
    _fields_ = [("Format", WAVEFORMATEX), ("wValidBitsPerSample", wintypes.WORD),
                ("dwChannelMask", wintypes.DWORD), ("SubFormat", GUID)]


class AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(Structure):
    _fields_ = [("TargetProcessId", wintypes.DWORD), ("ProcessLoopbackMode", c_int)]


class AUDIOCLIENT_ACTIVATION_PARAMS(Structure):
    _fields_ = [("ActivationType", c_int), ("ProcessLoopbackParams", AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS)]


class BLOB(Structure):
    _fields_ = [("cbSize", wintypes.ULONG), ("pBlobData", c_void_p)]


class PROPVARIANT(Structure):  # only the VT_BLOB arm is needed
    _fields_ = [("vt", wintypes.USHORT), ("wReserved1", wintypes.WORD), ("wReserved2", wintypes.WORD),
                ("wReserved3", wintypes.WORD), ("blob", BLOB)]


class IAudioCaptureClient(IUnknown):
    _iid_ = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetBuffer",
                  (["out"], POINTER(c_void_p), "ppData"),
                  (["out"], POINTER(c_uint32), "pNumFramesToRead"),
                  (["out"], POINTER(wintypes.DWORD), "pdwFlags"),
                  (["out"], POINTER(c_uint64), "pu64DevicePosition"),
                  (["out"], POINTER(c_uint64), "pu64QPCPosition")),
        COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], c_uint32, "NumFramesRead")),
        COMMETHOD([], HRESULT, "GetNextPacketSize", (["out"], POINTER(c_uint32), "pNumFramesInNextPacket")),
    ]


class IAudioClient(IUnknown):
    _iid_ = GUID("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
    _methods_ = [
        COMMETHOD([], HRESULT, "Initialize",
                  (["in"], c_int, "ShareMode"),
                  (["in"], wintypes.DWORD, "StreamFlags"),
                  (["in"], c_longlong, "hnsBufferDuration"),
                  (["in"], c_longlong, "hnsPeriodicity"),
                  (["in"], POINTER(WAVEFORMATEX), "pFormat"),
                  (["in"], POINTER(GUID), "AudioSessionGuid")),
        COMMETHOD([], HRESULT, "GetBufferSize", (["out"], POINTER(c_uint32), "pNumBufferFrames")),
        COMMETHOD([], HRESULT, "GetStreamLatency", (["out"], POINTER(c_longlong), "phnsLatency")),
        COMMETHOD([], HRESULT, "GetCurrentPadding", (["out"], POINTER(c_uint32), "pNumPaddingFrames")),
        COMMETHOD([], HRESULT, "IsFormatSupported",
                  (["in"], c_int, "ShareMode"),
                  (["in"], POINTER(WAVEFORMATEX), "pFormat"),
                  (["out"], POINTER(POINTER(WAVEFORMATEX)), "ppClosestMatch")),
        COMMETHOD([], HRESULT, "GetMixFormat", (["out"], POINTER(POINTER(WAVEFORMATEX)), "ppDeviceFormat")),
        COMMETHOD([], HRESULT, "GetDevicePeriod",
                  (["out"], POINTER(c_longlong), "phnsDefaultDevicePeriod"),
                  (["out"], POINTER(c_longlong), "phnsMinimumDevicePeriod")),
        COMMETHOD([], HRESULT, "Start"),
        COMMETHOD([], HRESULT, "Stop"),
        COMMETHOD([], HRESULT, "Reset"),
        COMMETHOD([], HRESULT, "SetEventHandle", (["in"], wintypes.HANDLE, "eventHandle")),
        COMMETHOD([], HRESULT, "GetService",
                  (["in"], POINTER(GUID), "riid"),
                  (["out"], POINTER(POINTER(IUnknown)), "ppv")),
    ]


class IMMDevice(IUnknown):
    _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
    _methods_ = [
        COMMETHOD([], HRESULT, "Activate",
                  (["in"], POINTER(GUID), "iid"),
                  (["in"], wintypes.DWORD, "dwClsCtx"),
                  (["in"], c_void_p, "pActivationParams"),
                  (["out"], POINTER(POINTER(IUnknown)), "ppInterface")),
        COMMETHOD([], HRESULT, "OpenPropertyStore",
                  (["in"], wintypes.DWORD, "stgmAccess"),
                  (["out"], POINTER(c_void_p), "ppProperties")),
        COMMETHOD([], HRESULT, "GetId", (["out"], POINTER(c_void_p), "ppstrId")),
        COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(wintypes.DWORD), "pdwState")),
    ]


class IMMDeviceEnumerator(IUnknown):
    _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
    _methods_ = [
        COMMETHOD([], HRESULT, "EnumAudioEndpoints",
                  (["in"], c_int, "dataFlow"),
                  (["in"], wintypes.DWORD, "dwStateMask"),
                  (["out"], POINTER(c_void_p), "ppDevices")),
        COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint",
                  (["in"], c_int, "dataFlow"),
                  (["in"], c_int, "role"),
                  (["out"], POINTER(POINTER(IMMDevice)), "ppEndpoint")),
    ]


CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")


class IActivateAudioInterfaceAsyncOperation(IUnknown):
    _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetActivateResult",
                  (["out"], POINTER(ctypes.c_long), "activateResult"),
                  (["out"], POINTER(POINTER(IUnknown)), "activatedInterface")),
    ]


class IActivateAudioInterfaceCompletionHandler(IUnknown):
    _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
    _methods_ = [
        COMMETHOD([], HRESULT, "ActivateCompleted",
                  (["in"], POINTER(IActivateAudioInterfaceAsyncOperation), "activateOperation")),
    ]


class IAgileObject(IUnknown):  # marker: safe to call from any thread
    _iid_ = GUID("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
    _methods_ = []


class _ActivationDone(COMObject):
    _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler, IAgileObject]

    def __init__(self):
        super().__init__()
        self.event = threading.Event()

    def ActivateCompleted(self, activateOperation):
        self.event.set()


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateEventW.restype = wintypes.HANDLE
_kernel32.CreateEventW.argtypes = [c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_ole32 = ctypes.WinDLL("ole32")
_ole32.CoTaskMemFree.restype = None
_ole32.CoTaskMemFree.argtypes = [c_void_p]
_ActivateAudioInterfaceAsync = ctypes.WinDLL("mmdevapi").ActivateAudioInterfaceAsync
_ActivateAudioInterfaceAsync.restype = HRESULT
_ActivateAudioInterfaceAsync.argtypes = [
    wintypes.LPCWSTR, POINTER(GUID), POINTER(PROPVARIANT),
    POINTER(IActivateAudioInterfaceCompletionHandler), POINTER(POINTER(IActivateAudioInterfaceAsyncOperation))]


def _pcm16(rate, channels):
    return WAVEFORMATEX(WAVE_FORMAT_PCM, channels, rate, rate * channels * 2, channels * 2, 16, 0)


def _describe(mix):
    """Format of a WAVEFORMATEX(-TENSIBLE) pointer returned by GetMixFormat."""
    wf = mix.contents
    tag = wf.wFormatTag
    if tag == WAVE_FORMAT_EXTENSIBLE:
        sub = ctypes.cast(mix, POINTER(WAVEFORMATEXTENSIBLE)).contents.SubFormat
        tag = WAVE_FORMAT_IEEE_FLOAT if sub == KSDATAFORMAT_SUBTYPE_IEEE_FLOAT else WAVE_FORMAT_PCM
    if tag == WAVE_FORMAT_IEEE_FLOAT and wf.wBitsPerSample == 32:
        sample = "float32"
    elif tag == WAVE_FORMAT_PCM and wf.wBitsPerSample in (16, 32):
        sample = f"int{wf.wBitsPerSample}"
    else:
        raise ValueError(f"unsupported mix format (tag {tag}, {wf.wBitsPerSample} bit)")
    return Format(wf.nSamplesPerSec, wf.nChannels, sample, wf.nBlockAlign)


def activate_process_loopback(pid):
    """An uninitialized IAudioClient that captures what process `pid` (and its children) play."""
    params = AUDIOCLIENT_ACTIVATION_PARAMS(
        AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK,
        AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(pid, PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE))
    variant = PROPVARIANT(vt=VT_BLOB)
    variant.blob.cbSize = ctypes.sizeof(params)
    variant.blob.pBlobData = ctypes.addressof(params)
    handler = _ActivationDone()
    operation = POINTER(IActivateAudioInterfaceAsyncOperation)()
    _ActivateAudioInterfaceAsync(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, byref(IAudioClient._iid_), byref(variant),
                                 handler.QueryInterface(IActivateAudioInterfaceCompletionHandler), byref(operation))
    if not handler.event.wait(5):
        raise TimeoutError("Windows didn't finish setting up app audio capture")
    hr, unknown = operation.GetActivateResult()
    if hr < 0:
        raise OSError(hr & 0xFFFFFFFF, f"app audio capture failed (0x{hr & 0xFFFFFFFF:08X})")
    return unknown.QueryInterface(IAudioClient)


def find_discord():
    """(pid, name) of the oldest top-level Discord process, or None."""
    procs = {}
    for p in psutil.process_iter(["name", "ppid", "create_time"]):
        if (p.info["name"] or "").lower() in DISCORD_APPS:
            procs[p.pid] = p.info
    roots = [(info["create_time"] or 0, pid, DISCORD_APPS[info["name"].lower()])
             for pid, info in procs.items() if info["ppid"] not in procs]
    if not roots:
        return None
    _, pid, name = min(roots)
    return pid, name


class _WasapiSource:
    """Capture loop shared by both sources: subclasses say what to capture."""

    event_driven = False

    def __init__(self, on_chunk, on_status):
        self.on_chunk = on_chunk
        self.on_status = on_status
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stop.set()

    # Subclasses implement these.
    def find_target(self):
        raise NotImplementedError

    def open(self, target):
        """Return (initialized IAudioClient, Format)."""
        raise NotImplementedError

    def label(self, target):
        raise NotImplementedError

    def waiting_message(self):
        return "Waiting for audio…"

    def _run(self):
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        try:
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
                except Exception as e:
                    print(f"Audio capture error: {e}", file=sys.stderr)
                    self.on_status("Audio capture stopped — retrying…")
                    self._stop.wait(2.0)
        finally:
            self._release()
            comtypes.CoUninitialize()

    def _release(self):
        pass

    def _capture(self, target):
        client, fmt = self.open(target)
        event = _kernel32.CreateEventW(None, False, False, None)
        try:
            if self.event_driven:
                client.SetEventHandle(event)
            capture = client.GetService(byref(IAudioCaptureClient._iid_)).QueryInterface(IAudioCaptureClient)
            resampler = Resampler(fmt.rate)
            pending = np.zeros(0, np.float32)
            client.Start()
            self.on_status(f"Listening to {self.label(target)}")
            last_data = last_check = time.monotonic()
            while not self._stop.is_set():
                _kernel32.WaitForSingleObject(event, 100 if self.event_driven else 10)
                got_data = False
                while capture.GetNextPacketSize() > 0:
                    data, frames, flags, _, _ = capture.GetBuffer()
                    if flags & AUDCLNT_BUFFERFLAGS_SILENT or not data:
                        samples = np.zeros(frames, np.float32)
                    else:
                        samples = to_mono(ctypes.string_at(data, frames * fmt.block_align), fmt.sample, fmt.channels)
                    capture.ReleaseBuffer(frames)
                    pending = self._emit(np.concatenate([pending, resampler.process(samples)]))
                    got_data = True
                now = time.monotonic()
                if got_data:
                    last_data = now
                elif now - last_data >= 0.1:
                    # Windows sends nothing while nothing is playing. Keep time moving so the
                    # translator still notices when someone stops talking.
                    pending = self._emit(np.concatenate([pending, np.zeros(SAMPLE_RATE // 10, np.float32)]))
                    last_data += 0.1
                if now - last_check >= 3.0:
                    last_check = now
                    if self.find_target() != target:
                        return  # app restarted or default device changed: start over
        finally:
            try:
                client.Stop()
            except Exception:
                pass
            _kernel32.CloseHandle(event)

    def _emit(self, samples):
        whole = len(samples) // CHUNK * CHUNK
        for i in range(0, whole, CHUNK):
            self.on_chunk(samples[i:i + CHUNK].copy())
        return samples[whole:]


class AppLoopbackSource(_WasapiSource):
    event_driven = True

    def find_target(self):
        return find_discord()

    def label(self, target):
        return target[1]

    def waiting_message(self):
        return "Waiting for Discord…"

    def open(self, target):
        error = None
        # Ask Windows to convert to 16 kHz mono for us; fall back to a common format.
        for rate, channels in ((SAMPLE_RATE, 1), (48000, 2)):
            client = activate_process_loopback(target[0])
            fmt = _pcm16(rate, channels)
            try:
                client.Initialize(AUDCLNT_SHAREMODE_SHARED,
                                  AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
                                  | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY,
                                  BUFFER_DURATION, 0, byref(fmt), None)
                return client, Format(rate, channels, "int16", channels * 2)
            except comtypes.COMError as e:
                error = e
        raise error


class DeviceLoopbackSource(_WasapiSource):
    _enumerator = None

    def _devices(self):
        if self._enumerator is None:
            self._enumerator = comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, CLSCTX_ALL)
        return self._enumerator

    def _release(self):
        self._enumerator = None

    def find_target(self):
        try:
            device = self._devices().GetDefaultAudioEndpoint(E_RENDER, E_CONSOLE)
        except comtypes.COMError:
            return None  # no output device
        address = device.GetId()
        try:
            return ctypes.wstring_at(address)
        finally:
            _ole32.CoTaskMemFree(address)

    def label(self, target):
        return "everything you hear"

    def waiting_message(self):
        return "Waiting for an output device…"

    def open(self, target):
        device = self._devices().GetDefaultAudioEndpoint(E_RENDER, E_CONSOLE)

        def activate():
            return device.Activate(byref(IAudioClient._iid_), CLSCTX_ALL, None).QueryInterface(IAudioClient)

        client = activate()
        fmt = _pcm16(SAMPLE_RATE, 1)
        try:
            client.Initialize(AUDCLNT_SHAREMODE_SHARED,
                              AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM
                              | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY,
                              BUFFER_DURATION, 0, byref(fmt), None)
            return client, Format(SAMPLE_RATE, 1, "int16", 2)
        except comtypes.COMError:
            pass
        # Older setups: capture in the device's own format and convert ourselves.
        client = activate()
        mix = client.GetMixFormat()
        try:
            fmt = _describe(mix)
            client.Initialize(AUDCLNT_SHAREMODE_SHARED, AUDCLNT_STREAMFLAGS_LOOPBACK, BUFFER_DURATION, 0, mix, None)
        finally:
            _ole32.CoTaskMemFree(ctypes.cast(mix, c_void_p))
        return client, fmt


def selftest():
    """Exercise the COM plumbing without needing anything to be playing. Returns a report."""
    report = []

    def run():
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        try:
            try:
                client = activate_process_loopback(os.getpid())
                fmt = _pcm16(SAMPLE_RATE, 1)
                client.Initialize(AUDCLNT_SHAREMODE_SHARED,
                                  AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
                                  | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM, BUFFER_DURATION, 0, byref(fmt), None)
                client.GetService(byref(IAudioCaptureClient._iid_)).QueryInterface(IAudioCaptureClient)
                report.append("app loopback ok")
                del client
            except Exception as e:
                report.append(f"app loopback unavailable here: {e!r}")
            source = DeviceLoopbackSource(lambda chunk: None, lambda text: None)
            try:
                target = source.find_target()
                if target is None:
                    report.append("no output device")
                else:
                    client, fmt = source.open(target)
                    report.append(f"device loopback ok ({fmt.rate} Hz, {fmt.channels} ch, {fmt.sample})")
                    del client
            except Exception as e:
                report.append(f"device loopback unavailable here: {e!r}")
            finally:
                source._release()
        finally:
            comtypes.CoUninitialize()

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(20)
    return "; ".join(report) or "timed out"
