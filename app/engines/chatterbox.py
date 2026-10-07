"""Resemble AI Chatterbox Multilingual (MIT licence), an alternative to XTTS-v2.

Experimental here: install with `pip install chatterbox-tts` in a separate virtual
environment, since it pins its own torch version. Supports 'ja' and 'en'.
"""

from __future__ import annotations

from pathlib import Path


class ChatterboxEngine:
    name = "chatterbox"

    def __init__(self, device: str | None = None):
        import torch
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS

        if device is None:
            if torch.cuda.is_available():
                device = "cuda"
            elif torch.backends.mps.is_available():
                device = "mps"
            else:
                device = "cpu"
        self.device = device
        self.model = ChatterboxMultilingualTTS.from_pretrained(device=device)

    def synthesize(self, text: str, lang: str, reference_wav: Path, out_wav: Path) -> None:
        import soundfile

        wav = self.model.generate(text, language_id=lang, audio_prompt_path=str(reference_wav))
        soundfile.write(str(out_wav), wav.squeeze().cpu().numpy(), self.model.sr)
