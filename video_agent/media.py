import json
import re
import subprocess
from pathlib import Path
import imageio_ffmpeg

from .config import settings


def run(*args: str):
    executable = imageio_ffmpeg.get_ffmpeg_exe() if args[0] == "ffmpeg" else args[0]
    return subprocess.run((executable, *args[1:]), check=True, capture_output=True, text=True, timeout=3600).stdout


def duration_ms(path: str) -> int:
    try:
        output = run("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path)
        value = float(json.loads(output)["format"]["duration"])
    except FileNotFoundError:
        result = subprocess.run((imageio_ffmpeg.get_ffmpeg_exe(), "-i", path),
                                capture_output=True, text=True, timeout=30)
        match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
        if not match:
            raise ValueError("cannot determine video duration")
        hours, minutes, seconds = match.groups()
        value = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    if value <= 0 or value > settings.max_video_seconds:
        raise ValueError("video duration outside allowed range")
    return round(value * 1000)


def audio_segments(path: str) -> list[dict]:
    from faster_whisper import WhisperModel

    wav = str(Path(path).with_suffix(".wav"))
    run("ffmpeg", "-nostdin", "-y", "-i", path, "-vn", "-ac", "1", "-ar", "16000", wav)
    model = WhisperModel(settings.asr_model, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(wav, language="zh", vad_filter=True)
    result = []
    for segment in segments:
        value = segment.text.strip()
        if value:
            result.append({"modality": "speech", "start_ms": round(segment.start * 1000),
                           "end_ms": round(segment.end * 1000), "text": value,
                           "confidence": None, "raw": {"model": f"whisper-{settings.asr_model}"}})
    return result


def visual_segments(path: str, duration: int) -> list[dict]:
    """Sample every 10 s; de-duplicate near-identical slides and OCR their text."""
    from PIL import Image
    import numpy as np
    from rapidocr_onnxruntime import RapidOCR

    ocr = RapidOCR()
    result = []
    previous = None
    for start in range(0, duration, 10000):
        frame = str(Path(path).with_name(f"frame-{start}.jpg"))
        run("ffmpeg", "-nostdin", "-y", "-ss", str(start / 1000), "-i", path,
            "-frames:v", "1", "-vf", "scale=960:-1", frame)
        image = Image.open(frame).convert("L").resize((32, 32))
        pixels = np.asarray(image, dtype=np.float32)
        delta = float(np.mean(np.abs(pixels - previous))) if previous is not None else 255.0
        if delta < 10:
            continue
        previous = pixels
        found, _ = ocr(frame)
        words = [item[1] for item in (found or [])]
        text = " ".join(words).strip()
        result.append({"modality": "slide", "start_ms": start,
                       "end_ms": min(start + 10000, duration), "text": text,
                       "confidence": None, "raw": {"frame_delta": delta, "ocr": found or []},
                       "frame": frame})
    return result
