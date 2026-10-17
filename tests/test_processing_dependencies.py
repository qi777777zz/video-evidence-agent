import math
import struct
import wave

import av
import numpy as np

from faster_whisper.audio import decode_audio
from video_agent.workflow import failure_message


def test_pyav_version_is_compatible_with_released_faster_whisper():
    assert int(av.__version__.split(".")[0]) < 19


def test_faster_whisper_can_decode_generated_wav(tmp_path):
    path = tmp_path / "tone.wav"
    rate = 16_000
    samples = [round(4_000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(rate)]
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(struct.pack(f"<{len(samples)}h", *samples))

    decoded = decode_audio(str(path), sampling_rate=rate)
    assert isinstance(decoded, np.ndarray)
    assert decoded.dtype == np.float32
    assert len(decoded) == rate


def test_failure_message_uses_root_cause():
    try:
        try:
            raise TypeError("decoder mismatch")
        except TypeError as cause:
            raise RuntimeError("Activity task failed") from cause
    except RuntimeError as exc:
        assert failure_message(exc) == "TypeError: decoder mismatch"
