"""A stand-in engine for trying the app without a model: it writes a short tone pattern
whose length follows the sentence, so the whole record -> generate -> play flow can be tested."""

from __future__ import annotations

import math
import struct
import time
import wave
from pathlib import Path

RATE = 24000


class DummyEngine:
    name = "dummy"

    def __init__(self, delay: float = 0.0):
        self.delay = delay

    def synthesize(self, text: str, lang: str, reference_wav: Path, out_wav: Path) -> None:
        if not Path(reference_wav).is_file():
            raise FileNotFoundError(reference_wav)
        if self.delay:
            time.sleep(self.delay)
        base = 330.0 if lang == "ja" else 440.0
        frames = bytearray()
        for i, ch in enumerate(text.strip()[:200]):
            if ch.isspace():
                frames += b"\x00\x00" * int(RATE * 0.08)
                continue
            freq = base * (1 + (ord(ch) % 7) / 14)
            n = int(RATE * 0.12)
            for k in range(n):
                env = math.sin(math.pi * k / n)
                frames += struct.pack("<h", int(9000 * env * math.sin(2 * math.pi * freq * k / RATE)))
        with wave.open(str(out_wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(bytes(frames))
