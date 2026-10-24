"""Audio sources, all delivering 16 kHz mono float32 chunks of CHUNK samples.

make_source() picks the right one for the platform:
  Linux    DiscordSource (just Discord, via `parec --monitor-stream`) or
           DeviceSource (everything on an output device, via its monitor)
  Windows  see audio_windows.py
FileSource plays an audio file in real time, for testing.
"""

import json
import subprocess
import sys
import threading
import time

import numpy as np

SAMPLE_RATE = 16000
CHUNK = 800  # 50 ms

DISCORD_CLIENTS = ("vesktop", "discord", "webcord", "armcord", "legcord", "equibop")


def make_source(kind, on_chunk, on_status):
    """kind: "discord", "all", or (Linux only) part of an output device's name."""
    if sys.platform == "win32":
        from audio_windows import AppLoopbackSource, DeviceLoopbackSource

        return AppLoopbackSource(on_chunk, on_status) if kind == "discord" else DeviceLoopbackSource(on_chunk, on_status)
    if kind == "discord":
        return DiscordSource(on_chunk, on_status)
    return DeviceSource(None if kind == "all" else kind, on_chunk, on_status)


def to_mono(data, sample_format, channels):
    """Raw interleaved samples ("int16", "int32" or "float32") to mono float32."""
    if sample_format == "int16":
        x = np.frombuffer(data, np.int16).astype(np.float32) / 32768
    elif sample_format == "int32":
        x = np.frombuffer(data, np.int32).astype(np.float32) / 2**31
    else:
        x = np.frombuffer(data, np.float32)
    if channels > 1:
        x = x.reshape(-1, channels).mean(axis=1)
    return x.astype(np.float32, copy=False)


class Resampler:
    """Streaming conversion to SAMPLE_RATE: low-pass filter, then linear interpolation."""

    TAPS = 63

    def __init__(self, rate):
        self.step = rate / SAMPLE_RATE
        if self.step == 1:
            return
        cutoff = 0.45 * SAMPLE_RATE / rate  # just under the new Nyquist, as a fraction of `rate`
        n = np.arange(self.TAPS) - (self.TAPS - 1) / 2
        taps = 2 * cutoff * np.sinc(2 * cutoff * n) * np.hanning(self.TAPS)
        self.taps = (taps / taps.sum()).astype(np.float32)
        self.history = np.zeros(self.TAPS - 1, np.float32)
        self.filtered = np.zeros(1, np.float32)  # last filtered sample, for interpolating across blocks
        self.position = 0.0                      # next output position, in filtered samples

    def process(self, x):
        if self.step == 1:
            return x
        padded = np.concatenate([self.history, x])
        self.history = padded[len(padded) - (self.TAPS - 1):]
        y = np.concatenate([self.filtered, np.convolve(padded, self.taps, mode="valid")])
        last = len(y) - 1
        count = int((last - self.position) // self.step) + 1 if last >= self.position else 0
        at = self.position + self.step * np.arange(count)
        i = at.astype(np.int64)
        frac = (at - i).astype(np.float32)
        out = y[i] * (1 - frac) + y[np.minimum(i + 1, last)] * frac
        self.position += self.step * count - last
        self.filtered = y[last:]
        return out.astype(np.float32)


# -- Linux (PipeWire / PulseAudio) -------------------------------------------------

def _pactl_json(*args):
    out = subprocess.run(["pactl", "-f", "json", *args], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def find_discord_stream():
    """Return (sink_input_index, app_name) of the Discord client's playback stream, or None."""
    found = []
    for s in _pactl_json("list", "sink-inputs"):
        p = s.get("properties", {})
        hay = " ".join(str(p.get(k, "")) for k in
                       ("application.name", "application.process.binary", "node.name")).lower()
        if any(name in hay for name in DISCORD_CLIENTS):
            found.append((s.get("corked", False), s["index"], p.get("application.name") or "Discord"))
    if not found:
        return None
    found.sort()  # prefer streams that are actually playing
    return found[0][1], found[0][2]


def find_sink_monitor(match=None):
    """Monitor source of the output device whose name/description contains `match`
    (the default output device if None)."""
    sinks = _pactl_json("list", "sinks")
    if match is None:
        default = subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True).stdout.strip()
        sinks = [s for s in sinks if s["name"] == default]
    else:
        sinks = [s for s in sinks if match.lower() in f"{s['name']} {s.get('description', '')}".lower()]
    if not sinks:
        return None
    s = sinks[0]
    return s.get("monitor_source") or f"{s['name']}.monitor", s.get("description") or s["name"]


class _ParecSource:
    """Runs parec against a target, restarting it whenever the target changes or disappears."""

    def __init__(self, on_chunk, on_status):
        self.on_chunk = on_chunk
        self.on_status = on_status
        self._stop = threading.Event()
        self._proc = None
        self._target = None

    def resolve(self):
        """Return (parec_args, target_key, human_label) or None if nothing to capture yet."""
        raise NotImplementedError

    def waiting_message(self):
        return "Waiting for audio…"

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()
        threading.Thread(target=self._watchdog, daemon=True).start()

    def stop(self):
        self._stop.set()
        if self._proc:
            self._proc.kill()

    def _run(self):
        waiting_shown = False
        while not self._stop.is_set():
            try:
                resolved = self.resolve()
            except Exception:
                resolved = None
            if not resolved:
                if not waiting_shown:
                    self.on_status(self.waiting_message())
                    waiting_shown = True
                time.sleep(1.0)
                continue
            waiting_shown = False
            args, self._target, label = resolved
            self.on_status(f"Listening to {label}")
            self._proc = subprocess.Popen(
                ["parec", *args, "--format=float32le", f"--rate={SAMPLE_RATE}", "--channels=1",
                 "--raw", "--latency-msec=30"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            nbytes = CHUNK * 4
            while not self._stop.is_set():
                data = self._proc.stdout.read(nbytes)
                if len(data) < nbytes:
                    break
                self.on_chunk(np.frombuffer(data, dtype=np.float32))
            self._proc.kill()
            self._proc.wait()
            self._target = None
            time.sleep(0.5)

    def _watchdog(self):
        # A stream that goes away (call ended, client restarted) can leave parec blocked
        # forever, so poll and kill it if the target is no longer the right one.
        while not self._stop.wait(2.0):
            if self._proc is None or self._proc.poll() is not None or self._target is None:
                continue
            try:
                resolved = self.resolve()
            except Exception:
                continue
            if not resolved or resolved[1] != self._target:
                self._proc.kill()


class DiscordSource(_ParecSource):
    def resolve(self):
        found = find_discord_stream()
        if not found:
            return None
        index, name = found
        return [f"--monitor-stream={index}"], index, name.capitalize()

    def waiting_message(self):
        return "Waiting for Discord audio…"


class DeviceSource(_ParecSource):
    def __init__(self, match, on_chunk, on_status):
        super().__init__(on_chunk, on_status)
        self.match = match

    def resolve(self):
        found = find_sink_monitor(self.match)
        if not found:
            return None
        monitor, description = found
        return [f"--device={monitor}"], monitor, f"everything on {description}"

    def waiting_message(self):
        return f"Waiting for output device “{self.match}”…" if self.match else "Waiting for an output device…"


class FileSource:
    def __init__(self, path, on_chunk, on_status, on_end=None):
        self.path = path
        self.on_chunk = on_chunk
        self.on_status = on_status
        self.on_end = on_end
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stop.set()

    def _run(self):
        raw = subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", self.path,
             "-f", "f32le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"],
            capture_output=True, check=True).stdout
        audio = np.frombuffer(raw, dtype=np.float32)
        audio = np.concatenate([audio, np.zeros(SAMPLE_RATE * 2, dtype=np.float32)])
        self.on_status("Playing test file")
        t0 = time.monotonic()
        for i in range(0, len(audio), CHUNK):
            if self._stop.is_set():
                return
            delay = t0 + i / SAMPLE_RATE - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self.on_chunk(audio[i:i + CHUNK])
        if self.on_end:
            self.on_end()
