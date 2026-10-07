"""Coqui XTTS-v2 via the `coqui-tts` package (pip only, no separate WebUI).

- Japanese and English from one model; clones a voice from a ~6 s or longer sample.
- Model weights (~1.8 GB) download on first use into the coqui cache.
- Weights are under the Coqui Public Model License (non-commercial use). Read it, then set
  COQUI_TOS_AGREED=1 to confirm; the engine will not start otherwise.
"""

from __future__ import annotations

import os
from pathlib import Path

MODEL_NAME = "tts_models/multilingual/multi-dataset/xtts_v2"
LANGS = {"ja": "ja", "en": "en"}


class XTTSEngine:
    name = "xtts"

    def __init__(self, device: str | None = None):
        if os.environ.get("COQUI_TOS_AGREED") != "1":
            raise RuntimeError(
                "XTTS-v2 の利用規約 (Coqui Public Model License) を確認のうえ、"
                ".env に COQUI_TOS_AGREED=1 を設定してください。"
            )
        import torch
        from TTS.api import TTS

        if device is None:
            # XTTS on Apple's MPS backend is unreliable; Macs use the CPU.
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self.device = device
        self.tts = TTS(MODEL_NAME).to(device)

    def synthesize(self, text: str, lang: str, reference_wav: Path, out_wav: Path) -> None:
        self.tts.tts_to_file(
            text=text,
            language=LANGS.get(lang, "en"),
            speaker_wav=str(reference_wav),
            file_path=str(out_wav),
        )
