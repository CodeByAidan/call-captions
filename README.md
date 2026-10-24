# Call Captions

<img width="1600" height="720" alt="image" src="https://github.com/user-attachments/assets/b791dd52-d10c-4d11-997b-a8753f0aad8a" />

Live English captions for voice calls. It listens to Discord, translates whatever language
your friends are speaking into English with Whisper, and shows it in a clean, translucent
overlay that fades away on its own. It uses your NVIDIA graphics card when you have one
(GTX 900 series or newer) and falls back to the CPU when you don't.

## Windows

### Install

1. Download `CallCaptions-Setup-<version>.exe` from the
   [Releases page](../../releases) (or from a build's artifacts on the Actions tab).
2. Run it. The installer isn't code-signed, so Windows may say *"Windows protected your PC"*:
   click **More info → Run anyway**. No admin rights are needed.
3. Call Captions starts and puts a speech-bubble icon in the system tray.

The first launch downloads a few things once, with progress shown on screen:

| What | Size | When |
| --- | --- | --- |
| GPU support (NVIDIA cuBLAS) | 553 MB | only on PCs with an NVIDIA card |
| Speech model | 3.1 GB on 6 GB+ cards, 1.5 GB on 4 GB cards, 0.5 GB otherwise | always |

Needs 64-bit Windows 10 version 2004 or newer, and for GPU speed an NVIDIA driver from
2023 or later (version 528+).

### Using it

Join a voice call; when someone talks, captions appear near the bottom of the screen.
Right-click the tray icon for settings, which are saved and apply immediately:

- **Listen to**: *Discord only* (default; ignores game sound and music) or *Everything I hear*
- **Spoken language**: detected automatically, or pick it for faster, more accurate captions
- **Quality**: *Automatic* picks the best model your graphics card can fit next to a game;
  pick *Balanced* or *Fastest* if a game needs the video memory
- **Position**, **Text size**, **Background**, **Pause captions**, **Start with Windows**

Captions show over windowed and *borderless* fullscreen games. Exclusive fullscreen draws
over everything, so switch the game to borderless if captions don't show.

**Open log** in the tray menu shows a running transcript plus any errors.

To uninstall, use *Settings → Apps → Call Captions*. That also removes the downloaded
models and GPU libraries.

## Linux

Needs PipeWire (or PulseAudio), Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
./captions
```

Flags override the tray settings for one run: `--source all`, `--lang ja`,
`--model medium`, `--position top`, `--file clip.mp3` (needs ffmpeg). See `./captions --help`.

## Building the Windows installer

**On GitHub:** the workflow in `.github/workflows/windows.yml` builds the installer on every
push to `main`. Download it from the run's artifacts. Push a tag like `v1.0.0` to publish a
release with the installer attached.

**On a Windows PC** with Python 3.12 and [Inno Setup 6](https://jrsoftware.org/isdl.php):

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1 -Version 1.0.0
```

Both bundle the app with PyInstaller, run its self-test (`CallCaptions.exe --selftest`), then
build `dist\CallCaptions-Setup-<version>.exe` with Inno Setup.

## How it works

| File | Job |
| --- | --- |
| `audio.py` | Captures audio as 16 kHz mono. On Linux it records just Discord's stream (`parec --monitor-stream`) or an output device's monitor. |
| `audio_windows.py` | Windows capture through WASAPI. *Discord only* uses per-app loopback (the API behind OBS's Application Audio Capture) on Discord's process tree. |
| `translator.py` | Detects speech, then translates it with faster-whisper. A quick pass shows a live preview while someone talks; a careful pass gives the final line when they pause. Also removes Whisper's filler and repeat glitches. |
| `gpu.py` | Detects the NVIDIA card, picks the model and number format, and fetches cuBLAS on Windows. |
| `overlay.py` | The click-through, always-on-top caption box and its animations. |
| `captions.py` | Ties it together: tray menu, settings, logging, self-test. |
