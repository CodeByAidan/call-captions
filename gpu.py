"""Decides where Whisper runs, and makes NVIDIA's libraries loadable.

Any NVIDIA card that CUDA 12 supports (GTX 900 series and newer) is used automatically:
the model size follows its memory, and the number format follows what the card can do.
On Apple Silicon Macs, Whisper runs on the GPU through MLX (whisper_mlx.py), sized by the
Mac's memory. Without either, it runs on the CPU with a smaller model.

CTranslate2 needs only cuBLAS for Whisper (it runs convolutions without cuDNN), which comes
from NVIDIA's PyPI wheel. On Linux it's installed into the venv; on Windows it's downloaded
once, on first run, into the app's data folder, so the installer stays small and PCs
without an NVIDIA card never fetch it.
"""

import glob
import hashlib
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from dataclasses import dataclass

from settings import data_dir

MODEL_SIZES_GB = {"tiny": 0.08, "small": 0.5, "medium": 1.5, "large-v2": 3.1, "large-v3": 3.1}

WINDOWS_CUDA_WHEELS = (
    ("https://files.pythonhosted.org/packages/20/e2/fc9a0e985249d873150276d5afb02e39a66817fedbf1a385724393e505ed/"
     "nvidia_cublas_cu12-12.9.2.10-py3-none-win_amd64.whl",
     553162896, "623f43027d40d44ceadf0043f002bd25cf353e8f13ce90b9a87057019f560661"),
)
WINDOWS_CUDA_DLLS = ("cublas64_12.dll", "cublasLt64_12.dll")
WINDOWS_CUDA_MARKER = "cublas-12.9.2.10.ok"

_dll_dirs = []  # handles from os.add_dll_directory must stay alive


@dataclass
class Plan:
    device: str          # "cuda", "mlx" (Apple GPU) or "cpu"
    compute_type: str
    model: str
    gpu_name: str = None

    def describe(self):
        return f"{self.gpu_name or 'CPU'} · {self.model}"


def nvidia_gpu():
    """(name, memory in GB) of the first NVIDIA GPU, or None."""
    try:
        import pynvml

        pynvml.nvmlInit()
        try:
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            name = pynvml.nvmlDeviceGetName(handle)
            memory = pynvml.nvmlDeviceGetMemoryInfo(handle).total
        finally:
            pynvml.nvmlShutdown()
        return (name.decode() if isinstance(name, bytes) else name), memory / 1024**3
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        name, mib = out.splitlines()[0].rsplit(",", 1)
        return name.strip(), int(mib) / 1024
    except Exception:
        return None


def model_for_memory(gb):
    # large-v3 needs ~3 GB at int8, leaving room for a game on a 6 GB card.
    if gb is None:
        return "medium"
    if gb >= 5.5:
        return "large-v3"
    if gb >= 3.5:
        return "medium"
    return "small"


def make_plan(model_choice="auto", device_choice="auto"):
    if sys.platform == "darwin":
        return _mac_plan(model_choice, device_choice)
    import ctranslate2

    if device_choice != "cpu" and ctranslate2.get_cuda_device_count() > 0:
        name, memory = nvidia_gpu() or ("NVIDIA GPU", None)
        model = model_for_memory(memory) if model_choice == "auto" else model_choice
        supported = ctranslate2.get_supported_compute_types("cuda")
        compute = next((t for t in ("int8_float16", "int8", "float16", "float32") if t in supported), "default")
        return Plan("cuda", compute, model, name)
    return cpu_plan(model_choice)


def cpu_plan(model_choice="auto"):
    return Plan("cpu", "int8", "small" if model_choice == "auto" else model_choice)


def models_dir():
    # On Linux, share the usual Hugging Face cache; on Windows and macOS, keep models with
    # the app's data so uninstalling cleans them up.
    return os.path.join(data_dir(), "models") if sys.platform in ("win32", "darwin") else None


# -- macOS -------------------------------------------------------------------------

def _mac_plan(model_choice, device_choice):
    import whisper_mlx

    if device_choice != "cpu" and whisper_mlx.available():
        # Apple GPUs share the Mac's memory. large-v3 takes ~3 GB, so it needs room to spare.
        memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024**3
        auto = "large-v3" if memory >= 15 else "medium" if memory >= 7 else "small"
        return Plan("mlx", "float16", auto if model_choice == "auto" else model_choice, f"{_mac_chip()} GPU")
    return cpu_plan(model_choice)


def _mac_chip():
    try:
        return subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                              capture_output=True, text=True, timeout=5).stdout.strip() or "Apple"
    except Exception:
        return "Apple"


# -- Linux -------------------------------------------------------------------------

def reexec_with_linux_cuda_libs():
    """Put the venv's NVIDIA wheels on the loader path. LD_LIBRARY_PATH is only read at
    startup, so this re-runs the program once."""
    dirs = sorted(glob.glob(os.path.join(sys.prefix, "lib", "python3*", "site-packages", "nvidia", "*", "lib")))
    current = [d for d in os.environ.get("LD_LIBRARY_PATH", "").split(":") if d]
    missing = [d for d in dirs if d not in current]
    if missing:
        os.environ["LD_LIBRARY_PATH"] = ":".join(missing + current)
        os.execv(sys.executable, [sys.executable, *sys.argv])


# -- Windows -----------------------------------------------------------------------

def prepare_windows_cuda(on_progress):
    """Make cuBLAS loadable, downloading it the first time. Raises on failure."""
    import ctypes

    cuda_dir = os.path.join(data_dir(), "cuda")
    if not os.path.exists(os.path.join(cuda_dir, WINDOWS_CUDA_MARKER)):
        _download_windows_cuda(cuda_dir, on_progress)
    os.environ["PATH"] = cuda_dir + os.pathsep + os.environ.get("PATH", "")
    _dll_dirs.append(os.add_dll_directory(cuda_dir))
    # CTranslate2 loads cuBLAS by bare name; once loaded from here, Windows reuses it.
    for name in reversed(WINDOWS_CUDA_DLLS):
        ctypes.WinDLL(os.path.join(cuda_dir, name))


def _download_windows_cuda(cuda_dir, on_progress):
    os.makedirs(cuda_dir, exist_ok=True)
    total = sum(size for _, size, _ in WINDOWS_CUDA_WHEELS)
    done, last_report = 0, 0.0
    part = os.path.join(cuda_dir, "download.part")
    for url, _, sha256 in WINDOWS_CUDA_WHEELS:
        digest = hashlib.sha256()
        with urllib.request.urlopen(url, timeout=60) as response, open(part, "wb") as f:
            while chunk := response.read(1 << 20):
                f.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if time.monotonic() - last_report > 0.5:
                    on_progress(done, total)
                    last_report = time.monotonic()
        if digest.hexdigest() != sha256:
            os.remove(part)
            raise RuntimeError("the GPU library download was corrupted")
        with zipfile.ZipFile(part) as wheel:
            for member in wheel.namelist():
                if os.path.basename(member) in WINDOWS_CUDA_DLLS:
                    with wheel.open(member) as src, open(os.path.join(cuda_dir, os.path.basename(member)), "wb") as dst:
                        shutil.copyfileobj(src, dst, 1 << 20)
        os.remove(part)
    open(os.path.join(cuda_dir, WINDOWS_CUDA_MARKER), "w").close()
