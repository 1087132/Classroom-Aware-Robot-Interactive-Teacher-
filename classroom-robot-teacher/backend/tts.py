"""Robot voice through the computer's speaker.

Primary engine: Kokoro TTS (kokoro-onnx) - a natural, warm neural voice that
runs fully OFFLINE on this machine (model files in data/tts/). Falls back to
the macOS `say` command if the model isn't available.

Synthesis is CPU-bound, so call synth_to_wav() inside a thread; playback uses
macOS `afplay`.
"""
import tempfile
from pathlib import Path

TTS_DIR = Path(__file__).resolve().parent.parent / "data" / "tts"
MODEL = TTS_DIR / "kokoro-v1.0.onnx"
VOICES = TTS_DIR / "voices-v1.0.bin"

DEFAULT_VOICE = "af_heart"     # warm, cheerful US female - great for a classroom robot
DEFAULT_SPEED = 1.04

_kokoro = None
_load_failed = False


def available() -> bool:
    return MODEL.exists() and VOICES.exists() and not _load_failed


def engine_name() -> str:
    return f"kokoro ({DEFAULT_VOICE})" if available() else "macOS say"


def _get_kokoro():
    """Lazy-load the model once (takes a couple of seconds)."""
    global _kokoro, _load_failed
    if _kokoro is None and not _load_failed:
        try:
            from kokoro_onnx import Kokoro
            _kokoro = Kokoro(str(MODEL), str(VOICES))
        except Exception as e:
            print("[tts] Kokoro unavailable, falling back to `say`:", e)
            _load_failed = True
    return _kokoro


def synth_to_wav(text: str, voice: str = DEFAULT_VOICE, speed: float = DEFAULT_SPEED):
    """BLOCKING: synthesize text -> temp wav path, or None to use the fallback."""
    kokoro = _get_kokoro()
    if kokoro is None:
        return None
    try:
        import soundfile as sf
        samples, sample_rate = kokoro.create(text, voice=voice, speed=speed, lang="en-us")
        f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        sf.write(f.name, samples, sample_rate)
        return f.name
    except Exception as e:
        print("[tts] synth error:", e)
        return None
