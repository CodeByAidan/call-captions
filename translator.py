"""Splits incoming audio into utterances and translates them to English with Whisper.

While someone is talking, a fast greedy pass runs every ~0.8 s and is shown as a
"live" partial caption; once they pause, a beam-search pass produces the final line.
"""

import os
import queue
import re
import sys
import threading
import time
from collections import deque

import numpy as np

import gpu
from audio import CHUNK, SAMPLE_RATE

CHUNK_SEC = CHUNK / SAMPLE_RATE
PRE_ROLL_SEC = 0.3       # audio kept from just before speech starts
START_SEC = 0.1          # this much voiced audio starts an utterance
END_SILENCE_SEC = 0.6    # this much quiet ends one
MAX_UTTERANCE_SEC = 10   # long monologues are cut into captions of at most this length
MIN_VOICED_SEC = 0.25    # shorter blips (clicks, keyboard) are dropped
PARTIAL_EVERY_SEC = 0.8
MIN_THRESHOLD = 0.004    # RMS; Discord's own voice gate sends near-silence between words

# Things Whisper likes to "hear" in noise, learned from YouTube subtitles.
HALLUCINATIONS = re.compile(
    r"thanks? (you )?for watching|please subscribe|like and subscribe|subtitles? by|"
    r"captions? by|subtitled by|translated by|amara\.org|transcribed by|"
    r"^\W*you\W*$",
    re.IGNORECASE,
)

# Whisper sometimes gets stuck repeating a word or short phrase ("uh, uh, uh, …").
# Four or more in a row collapse to one; "no no no" is left alone.
REPEATS = re.compile(r"\b(\w+(?:\W+\w+){0,3}?)(?:[\s,.…!?-]+\1\b){3,}", re.IGNORECASE)
# Hesitation sounds, but not "uh-huh" or "mm-hmm", which mean yes.
FILLERS = re.compile(r"(?<![-'\w])(?:u+h+m*|u+m+|e+r+m*|a+h+|e+h+|h+m+|m{2,})(?![-'\w])[.…]*",
                     re.IGNORECASE)


def _tidy(text):
    text = REPEATS.sub(r"\1", text)
    text = FILLERS.sub("", text)
    # Clean up the punctuation the removed words leave behind.
    text = re.sub(r"\s+([,.!?…])", r"\1", text)
    text = re.sub(r",(?:\s*,)+", ",", text)
    text = re.sub(r",\s*([.!?…])", r"\1", text)
    text = re.sub(r"^[\s,.…-]+|[\s,-]+$", "", text)
    text = re.sub(r"\s{2,}", " ", text)
    if not re.search(r"\w", text):
        return ""
    return text[0].upper() + text[1:]


def _clean(segments):
    parts = []
    for s in segments:
        if s.no_speech_prob > 0.8 or s.avg_logprob < -2.0:
            continue
        text = s.text.strip()
        if text and not HALLUCINATIONS.search(text) and (not parts or parts[-1] != text):
            parts.append(text)
    return _tidy(" ".join(parts))


class Translator:
    def __init__(self, model_choice, device_choice, language,
                 on_partial, on_final, on_status, on_plan=lambda plan: None):
        self.model_choice = model_choice
        self.device_choice = device_choice
        self.language = language
        self.on_partial = on_partial
        self.on_final = on_final
        self.on_status = on_status      # (text, sticky)
        self.on_plan = on_plan
        self.paused = False
        self.plan = None
        self._queue = queue.Queue()
        self._model = None
        self._pending_model = None
        self._flush = False
        self._sticky_language = None

    def feed(self, chunk):
        if not self.paused and self._model is not None:
            self._queue.put(chunk)

    def start(self, on_ready=None):
        threading.Thread(target=self._run, args=(on_ready,), daemon=True).start()

    def set_language(self, code):
        self.language = code
        self._sticky_language = None

    def set_model(self, choice):
        self._pending_model = choice  # picked up by the worker between utterances

    def flush(self):
        """Forget queued audio and any half-heard utterance (after switching sources)."""
        self._flush = True  # picked up by the worker

    # -- model ---------------------------------------------------------------

    def _setup(self):
        self.on_status("Checking your graphics card…", True)
        plan = gpu.make_plan(self.model_choice, self.device_choice)
        if plan.device == "cuda" and sys.platform == "win32":
            try:
                gpu.prepare_windows_cuda(lambda done, total: self.on_status(
                    f"Downloading GPU support · {done / 1e9:.1f} / {total / 1e9:.1f} GB (first run only)", True))
            except Exception as e:
                print(f"GPU setup failed: {e}", file=sys.stderr)
                self.on_status("Couldn't set up the GPU — using the CPU instead")
                plan = gpu.cpu_plan(self.model_choice)
        try:
            self._model = self._load(plan)
        except Exception as e:
            if plan.device == "cpu":
                raise
            print(f"GPU unavailable ({e}); falling back to CPU", file=sys.stderr)
            self.on_status("GPU didn't work — using the CPU (slower)")
            plan = gpu.cpu_plan(self.model_choice)
            self._model = self._load(plan)
        self.plan = plan
        self.on_plan(plan)
        print(f"· Running on {plan.describe()} ({plan.compute_type})", flush=True)

    def _load(self, plan):
        if plan.device == "mlx":
            from whisper_mlx import MODEL_REPOS, MlxWhisperModel

            path = self._fetch(plan.model, MODEL_REPOS[plan.model])
            self.on_status("Loading translation model…", True)
            model = MlxWhisperModel(path)
        else:
            from faster_whisper import WhisperModel

            path = self._fetch(plan.model)
            self.on_status("Loading translation model…", True)
            model = WhisperModel(path, device=plan.device, compute_type=plan.compute_type)
        model.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), task="translate", language="en", beam_size=1)
        return model

    def _fetch(self, name, mlx_repo=None):
        from faster_whisper.utils import _MODELS, download_model
        from huggingface_hub import snapshot_download

        root = gpu.models_dir()

        def fetch(local_only):
            if mlx_repo:
                return snapshot_download(mlx_repo, cache_dir=root, local_files_only=local_only,
                                         allow_patterns=["config.json", "weights.*"])
            return download_model(name, cache_dir=root, local_files_only=local_only)

        try:
            return fetch(local_only=True)
        except Exception:
            pass
        size = gpu.MODEL_SIZES_GB.get(name)
        self.on_status(f"Downloading the speech model{f' ({size:g} GB, first run only)' if size else ''}…", True)
        done = threading.Event()
        repo = mlx_repo or _MODELS.get(name, name)
        threading.Thread(target=self._report_download, args=(repo, root, size, done), daemon=True).start()
        try:
            return fetch(local_only=False)
        finally:
            done.set()

    def _report_download(self, repo, root, size, done):
        from huggingface_hub.constants import HF_HUB_CACHE

        folder = os.path.join(root or HF_HUB_CACHE, "models--" + repo.replace("/", "--"))
        while not done.wait(1.0):
            got = 0
            for directory, _, files in os.walk(folder):
                for f in files:
                    path = os.path.join(directory, f)
                    try:
                        got += 0 if os.path.islink(path) else os.path.getsize(path)
                    except OSError:
                        pass
            if got and size:
                self.on_status(f"Downloading the speech model · {got / 1e9:.1f} / {size:g} GB (first run only)", True)

    def _switch_model(self):
        choice, self._pending_model = self._pending_model, None
        if self._in_speech:
            self.on_final(self._uid, "", None)
        self._model = None  # free GPU memory before loading the next one
        self.model_choice = choice
        try:
            self._setup()
            self.on_status(f"Now using {self.plan.describe()}")
        except Exception as e:
            print(f"Could not load model: {e}", file=sys.stderr)
            self.on_status("Could not load that model — pick another in the tray menu")
        while not self._queue.empty():
            self._queue.get_nowait()
        self._reset()

    def _translate(self, audio, final):
        language = self.language or (None if final else self._sticky_language)
        options = dict(
            task="translate",
            beam_size=5 if final else 1,
            temperature=(0.0, 0.2, 0.4) if final else 0.0,
            condition_on_previous_text=False,
            without_timestamps=True,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500, speech_pad_ms=200),
        )
        segments, info = self._model.transcribe(audio, language=language, **options)
        segments = list(segments)

        if final and not self.language and info.language:
            if info.language_probability >= 0.75:
                self._sticky_language = info.language
            elif self._sticky_language and info.language != self._sticky_language:
                # Short phrases are often misdetected; trust the language they've been speaking.
                segments, info = self._model.transcribe(audio, language=self._sticky_language, **options)
                segments = list(segments)
        return _clean(segments), info.language

    # -- segmentation loop ---------------------------------------------------

    def _run(self, on_ready):
        try:
            self._setup()
        except Exception as e:
            print(f"Could not load model: {e}", file=sys.stderr)
            self.on_status("Could not load the translation model — see the log", True)
            return
        if on_ready:
            on_ready()
        self._reset()
        while True:
            if self._pending_model is not None:
                self._switch_model()
            if self._flush:
                self._flush = False
                while not self._queue.empty():
                    self._queue.get_nowait()
                if self._in_speech:
                    self.on_final(self._uid, "", None)
                self._reset()
            try:
                chunk = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if self._model is not None:
                self._process(chunk)

    def _reset(self):
        self._pre_roll = deque(maxlen=round(PRE_ROLL_SEC / CHUNK_SEC))
        self._noise = MIN_THRESHOLD / 3
        self._utterance, self._uid = [], getattr(self, "_uid", 0)
        self._in_speech = False
        self._voiced_run = self._silent_run = self._voiced_total = 0
        self._last_partial = 0.0

    def _process(self, chunk):
        rms = float(np.sqrt(np.mean(chunk * chunk)))
        # Noise floor follows quiet quickly and loud slowly, so it adapts to steady
        # background (game audio) without being dragged up by speech.
        self._noise += (rms - self._noise) * (0.1 if rms < self._noise else 0.002)
        voiced = rms > max(MIN_THRESHOLD, self._noise * 3)

        if not self._in_speech:
            self._pre_roll.append(chunk)
            self._voiced_run = self._voiced_run + 1 if voiced else 0
            if self._voiced_run >= round(START_SEC / CHUNK_SEC):
                self._in_speech, self._uid = True, self._uid + 1
                self._utterance = list(self._pre_roll)
                self._pre_roll.clear()
                self._silent_run, self._voiced_total = 0, self._voiced_run
                self._last_partial = time.monotonic()
            return

        self._utterance.append(chunk)
        self._silent_run = 0 if voiced else self._silent_run + 1
        self._voiced_total += voiced

        ended = self._silent_run >= round(END_SILENCE_SEC / CHUNK_SEC)
        if ended or len(self._utterance) >= round(MAX_UTTERANCE_SEC / CHUNK_SEC):
            if self._voiced_total * CHUNK_SEC >= MIN_VOICED_SEC:
                self._emit(self._uid, self._utterance, final=True)
            else:
                self.on_final(self._uid, "", None)
            if ended:
                self._in_speech, self._voiced_run = False, 0
            else:
                self._uid += 1
                self._voiced_total = 0
            self._utterance = []
            self._last_partial = time.monotonic()
        elif (len(self._utterance) * CHUNK_SEC >= 1.0
              and time.monotonic() - self._last_partial >= PARTIAL_EVERY_SEC
              and self._queue.qsize() < 4):  # skip live previews if we're falling behind
            self._emit(self._uid, self._utterance, final=False)
            self._last_partial = time.monotonic()

    def _emit(self, uid, chunks, final):
        try:
            text, language = self._translate(np.concatenate(chunks), final)
        except Exception as e:
            print(f"Translation failed: {e}", file=sys.stderr)
            if final:
                self.on_final(uid, "", None)
            return
        if final:
            self.on_final(uid, text, language)
        elif text:
            self.on_partial(uid, text, language)
