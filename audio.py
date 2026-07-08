"""Local speech-to-text (faster-whisper) and text-to-speech (piper)."""
import asyncio
import logging
import re
import subprocess
import tempfile
import wave
from pathlib import Path

import config

log = logging.getLogger(__name__)

_whisper_model = None
_piper_voice = None
_stt_lock = asyncio.Lock()
_tts_lock = asyncio.Lock()


def _load_whisper():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        log.info("Loading whisper model %s ...", config.WHISPER_MODEL)
        _whisper_model = WhisperModel(
            config.WHISPER_MODEL, device="cpu", compute_type="int8"
        )
    return _whisper_model


def _transcribe_sync(path: str) -> str:
    model = _load_whisper()
    segments, _info = model.transcribe(path, vad_filter=True)
    return "".join(seg.text for seg in segments).strip()


async def transcribe(path: str) -> str:
    """Transcribe an audio file (any format ffmpeg/PyAV can decode)."""
    async with _stt_lock:
        return await asyncio.to_thread(_transcribe_sync, path)


def _find_voice_model() -> Path:
    matches = sorted(config.VOICES_DIR.glob(f"{config.PIPER_VOICE}*.onnx"))
    if not matches:
        raise FileNotFoundError(
            f"No piper voice found in {config.VOICES_DIR}. Run:\n"
            f"  python -m piper.download_voices {config.PIPER_VOICE} "
            f"--data-dir {config.VOICES_DIR}"
        )
    return matches[0]


def _load_piper():
    global _piper_voice
    if _piper_voice is None:
        from piper import PiperVoice
        model_path = _find_voice_model()
        log.info("Loading piper voice %s ...", model_path)
        _piper_voice = PiperVoice.load(str(model_path))
    return _piper_voice


_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_MD_NOISE_RE = re.compile(r"[*_#`>|]+")


def speakable(text: str, limit: int = 2500) -> str:
    """Strip markdown/code so the TTS output sounds natural."""
    text = _CODE_BLOCK_RE.sub(" (code omitted) ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)  # links -> label
    text = _MD_NOISE_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _synthesize_sync(text: str, out_ogg: str):
    voice = _load_piper()
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = tmp.name
    try:
        with wave.open(wav_path, "wb") as wav_file:
            voice.synthesize_wav(text, wav_file)
        # Telegram voice notes must be OGG/Opus.
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", wav_path,
             "-c:a", "libopus", "-b:a", "32k", "-application", "voip", out_ogg],
            check=True,
        )
    finally:
        Path(wav_path).unlink(missing_ok=True)


async def synthesize_voice(text: str, out_ogg: str):
    """Render `text` to an OGG/Opus voice note at `out_ogg`."""
    async with _tts_lock:
        await asyncio.to_thread(_synthesize_sync, text, out_ogg)
