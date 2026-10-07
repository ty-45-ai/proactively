"""Speech engines. Each engine clones the voice in a reference WAV and reads a sentence aloud."""

from __future__ import annotations

from typing import Protocol
from pathlib import Path


class Engine(Protocol):
    name: str

    def synthesize(self, text: str, lang: str, reference_wav: Path, out_wav: Path) -> None:
        """Write speech for `text` (lang 'ja' or 'en') in the voice of `reference_wav` to `out_wav`."""


ENGINE_NAMES = ("xtts", "chatterbox", "dummy")


def load_engine(name: str, device: str | None = None) -> Engine:
    if name == "dummy":
        from .dummy import DummyEngine

        return DummyEngine()
    if name == "xtts":
        from .xtts import XTTSEngine

        return XTTSEngine(device=device)
    if name == "chatterbox":
        from .chatterbox import ChatterboxEngine

        return ChatterboxEngine(device=device)
    raise ValueError(f"unknown engine {name!r}; choose one of {', '.join(ENGINE_NAMES)}")
