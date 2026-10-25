"""Whisper on Apple Silicon GPUs through MLX, shaped like faster-whisper's WhisperModel so
the translator can use either.

Only mlx-whisper's decoder is used. Its package __init__ also imports the long-form
transcriber, which pulls in numba and scipy (and, at install time, PyTorch) for word
timestamps this app doesn't need, so the package is registered without running __init__.
"""

import importlib
import importlib.util
import sys
import types
from types import SimpleNamespace

import numpy as np

MODEL_REPOS = {name: f"mlx-community/whisper-{name}-mlx" for name in ("tiny", "small", "medium", "large-v3")}
SUBMODULES = ("audio", "decoding", "load_models", "tokenizer", "whisper")


def _submodule(name):
    if "mlx_whisper" not in sys.modules:
        spec = importlib.util.find_spec("mlx_whisper")
        if spec is None:
            raise ImportError("mlx-whisper is not installed")
        package = types.ModuleType("mlx_whisper")
        package.__path__ = list(spec.submodule_search_locations)
        package.__package__ = "mlx_whisper"
        sys.modules["mlx_whisper"] = package
    return importlib.import_module(f"mlx_whisper.{name}")


def available():
    """True on Macs whose GPU MLX can use."""
    try:
        import mlx.core as mx

        return mx.metal.is_available()
    except Exception:
        return False


class MlxWhisperModel:
    def __init__(self, path, dtype="float16"):
        import mlx.core as mx

        self.dtype = getattr(mx, dtype)
        self.fp16 = self.dtype == mx.float16
        self.audio = _submodule("audio")
        self.decoding = _submodule("decoding")
        self.model = _submodule("load_models").load_model(path, dtype=self.dtype)

    def transcribe(self, audio, language=None, task="transcribe", temperature=0.0, **_unsupported):
        """Decode one clip of up to 30 s. Beam search and VAD filtering aren't available
        in MLX Whisper, so those options are ignored."""
        mel = self.audio.log_mel_spectrogram(np.asarray(audio, np.float32), n_mels=self.model.dims.n_mels)
        mel = self.audio.pad_or_trim(mel, self.audio.N_FRAMES, axis=-2).astype(self.dtype)
        temperatures = temperature if isinstance(temperature, (list, tuple)) else (temperature,)
        for t in temperatures:
            options = self.decoding.DecodingOptions(task=task, language=language, temperature=t,
                                                    without_timestamps=True, fp16=self.fp16)
            result = self.decoding.decode(self.model, mel, options)
            # faster-whisper's fallback rule: retry hotter if the text looks like a loop or a guess.
            if result.compression_ratio <= 2.4 and result.avg_logprob >= -1.0:
                break
        probabilities = result.language_probs or {}
        info = SimpleNamespace(language=result.language,
                               language_probability=probabilities.get(result.language, 1.0))
        # faster-whisper's silence rule.
        if not result.text.strip() or (result.no_speech_prob > 0.6 and result.avg_logprob < -1.0):
            return [], info
        return [SimpleNamespace(text=result.text, avg_logprob=result.avg_logprob,
                                no_speech_prob=result.no_speech_prob)], info
