"""動画生成エンジン。

- LongCatBackend : 本番。LongCat-Video を1枚のGPUで直接呼び出す（torchrun 不要）。
- MockBackend    : GPU なしで画面や再開処理を試すためのダミー。画像をゆっくりズームするだけ。

どちらも同じ3つのメソッドを持つ:
    i2v(image, prompt, seed)        → 93 フレーム（480p / 15fps）
    vc(cond_frames, prompt, seed)   → 93 フレーム（先頭13枚は cond_frames の再生成）
    refine(stage1, prompt, seed, image=None, cond_frames=None)
                                     → 720p / 30fps に高画質化したフレーム
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Sequence

import numpy as np
from PIL import Image

from .jobs import COND_FRAMES, NUM_FRAMES, REFINE_COND_FRAMES


def _to_uint8_list(arr) -> list[np.ndarray]:
    arr = np.asarray(arr)
    if arr.dtype != np.uint8:
        arr = (np.clip(arr, 0.0, 1.0) * 255).round().astype(np.uint8)
    return [np.ascontiguousarray(f) for f in arr]


class LongCatBackend:
    name = "longcat"

    def __init__(
        self,
        repo_dir: str | Path,
        checkpoint_dir: str | Path,
        *,
        offload_text_encoder: bool = False,
        offload_kv_cache: bool = False,
        enable_compile: bool = False,
        device: str = "cuda",
    ):
        self.repo_dir = Path(repo_dir).resolve()
        self.checkpoint_dir = Path(checkpoint_dir).resolve()
        self.offload_text_encoder = offload_text_encoder
        self.offload_kv_cache = offload_kv_cache
        self.device = device
        if not (self.checkpoint_dir / "dit").exists():
            raise FileNotFoundError(f"モデルの重みが見つかりません: {self.checkpoint_dir}（scripts/download_weights.sh を実行してください）")
        if str(self.repo_dir) not in sys.path:
            sys.path.insert(0, str(self.repo_dir))

        import torch
        from transformers import AutoTokenizer, UMT5EncoderModel

        from longcat_video.context_parallel import context_parallel_util
        from longcat_video.modules.autoencoder_kl_wan import AutoencoderKLWan
        from longcat_video.modules.longcat_video_dit import LongCatVideoTransformer3DModel
        from longcat_video.modules.scheduling_flow_match_euler_discrete import FlowMatchEulerDiscreteScheduler
        from longcat_video.pipeline_longcat_video import LongCatVideoPipeline

        self.torch = torch
        ck = str(self.checkpoint_dir)
        dtype = torch.bfloat16
        cp_split_hw = context_parallel_util.get_optimal_split(1)

        t0 = time.time()
        tokenizer = AutoTokenizer.from_pretrained(ck, subfolder="tokenizer", torch_dtype=dtype)
        text_encoder = UMT5EncoderModel.from_pretrained(ck, subfolder="text_encoder", torch_dtype=dtype)
        vae = AutoencoderKLWan.from_pretrained(ck, subfolder="vae", torch_dtype=dtype)
        scheduler = FlowMatchEulerDiscreteScheduler.from_pretrained(ck, subfolder="scheduler", torch_dtype=dtype)
        dit = LongCatVideoTransformer3DModel.from_pretrained(ck, subfolder="dit", cp_split_hw=cp_split_hw, torch_dtype=dtype)
        if enable_compile:
            dit = torch.compile(dit)

        pipe = LongCatVideoPipeline(tokenizer=tokenizer, text_encoder=text_encoder, vae=vae,
                                    scheduler=scheduler, dit=dit)
        pipe.to(device)
        if offload_text_encoder:
            # テキストエンコーダ(UMT5-XXL, 約11GB)は普段CPUに置き、文章を読むときだけGPUへ移す
            pipe.text_encoder = pipe.text_encoder.to("cpu")
            original_encode = pipe.encode_prompt

            def encode_with_offload(*args, **kwargs):
                pipe.text_encoder = pipe.text_encoder.to(device)
                try:
                    return original_encode(*args, **kwargs)
                finally:
                    pipe.text_encoder = pipe.text_encoder.to("cpu")
                    torch.cuda.empty_cache()

            pipe.encode_prompt = encode_with_offload

        pipe.dit.load_lora(str(self.checkpoint_dir / "lora" / "cfg_step_lora.safetensors"), "cfg_step_lora")
        pipe.dit.load_lora(str(self.checkpoint_dir / "lora" / "refinement_lora.safetensors"), "refinement_lora")
        self.pipe = pipe
        self.last_peak_gb = None
        self._lora_state: str | None = None
        self.load_seconds = time.time() - t0

    # ---- 内部ユーティリティ -----------------------------------------------------
    def _gc(self):
        # 直前の処理で使った VRAM のピーク（GB）を記録 → 安いGPUで動くかの判断材料にする
        self.last_peak_gb = round(self.torch.cuda.max_memory_allocated() / 1024**3, 2)
        self.torch.cuda.reset_peak_memory_stats()
        self.torch.cuda.empty_cache()
        self.torch.cuda.ipc_collect()

    def _set_mode(self, state: str | None):
        """LoRA と Block Sparse Attention の切り替え: None / 'distill' / 'refine'"""
        if state == self._lora_state:
            return
        dit = self.pipe.dit
        dit.disable_all_loras()
        dit.disable_bsa()
        if state == "distill":
            dit.enable_loras(["cfg_step_lora"])
        elif state == "refine":
            dit.enable_loras(["refinement_lora"])
            dit.enable_bsa()
        self._lora_state = state

    def _gen(self, seed: int):
        g = self.torch.Generator(device=self.device)
        g.manual_seed(int(seed))
        return g

    @staticmethod
    def _sampling(mode: str, negative_prompt: str | None) -> dict:
        if mode == "fast":
            return {"num_inference_steps": 16, "use_distill": True, "guidance_scale": 1.0, "negative_prompt": None}
        return {"num_inference_steps": 50, "use_distill": False, "guidance_scale": 4.0,
                "negative_prompt": negative_prompt}

    # ---- 公開API ---------------------------------------------------------------
    def i2v(self, image: Image.Image, prompt: str, seed: int, *, mode: str = "fast",
            negative_prompt: str | None = None) -> list[np.ndarray]:
        self._set_mode("distill" if mode == "fast" else None)
        out = self.pipe.generate_i2v(image=image, prompt=prompt, resolution="480p", num_frames=NUM_FRAMES,
                                     generator=self._gen(seed), **self._sampling(mode, negative_prompt))[0]
        frames = _to_uint8_list(out)
        del out
        self._gc()
        return frames

    def vc(self, cond_frames: Sequence[np.ndarray], prompt: str, seed: int, *, mode: str = "fast",
           negative_prompt: str | None = None) -> list[np.ndarray]:
        self._set_mode("distill" if mode == "fast" else None)
        video = [Image.fromarray(f) for f in cond_frames[-COND_FRAMES:]]
        out = self.pipe.generate_vc(video=video, prompt=prompt, resolution="480p", num_frames=NUM_FRAMES,
                                    num_cond_frames=COND_FRAMES, generator=self._gen(seed), use_kv_cache=True,
                                    offload_kv_cache=self.offload_kv_cache, enhance_hf=True,
                                    **self._sampling(mode, negative_prompt))[0]
        frames = _to_uint8_list(out)
        del out
        self._gc()
        return frames

    def refine(self, stage1: Sequence[np.ndarray], prompt: str, seed: int, *, image: Image.Image | None = None,
               cond_frames: Sequence[np.ndarray] | None = None) -> list[np.ndarray]:
        self._set_mode("refine")
        kwargs = {}
        if cond_frames is not None:
            kwargs.update(video=[Image.fromarray(f) for f in cond_frames[-REFINE_COND_FRAMES:]],
                          num_cond_frames=REFINE_COND_FRAMES)
        elif image is not None:
            kwargs.update(image=image, num_cond_frames=1)
        out = self.pipe.generate_refine(prompt=prompt, stage1_video=[np.asarray(f) for f in stage1],
                                        num_inference_steps=50, generator=self._gen(seed),
                                        spatial_refine_only=False, **kwargs)[0]
        frames = _to_uint8_list(out)
        del out
        self._gc()
        return frames


class MockBackend:
    """GPU なしで動作確認するためのダミー。入力画像をゆっくりズーム＆パンするだけ。"""

    name = "mock"

    def __init__(self, delay: float = 0.0, size: tuple[int, int] = (416, 240)):
        self.delay = delay
        self.size = size  # (幅, 高さ) — 本物の 480p の代わりに小さめ
        self._t = 0

    def _frame(self, src: Image.Image, t: float, size) -> np.ndarray:
        w, h = src.size
        z = 1.0 + 0.15 * (0.5 + 0.5 * np.sin(t / 40.0))
        cw, ch = w / z, h / z
        cx = w / 2 + (w - cw) / 2 * np.sin(t / 55.0)
        cy = h / 2 + (h - ch) / 2 * np.cos(t / 70.0)
        box = (cx - cw / 2, cy - ch / 2, cx + cw / 2, cy + ch / 2)
        return np.asarray(src.resize(size, Image.BILINEAR, box=box))

    def _sleep(self):
        if self.delay:
            time.sleep(self.delay)

    def i2v(self, image, prompt, seed, *, mode="fast", negative_prompt=None):
        self._sleep()
        self._src = image.convert("RGB")
        self._t = 0
        frames = [self._frame(self._src, t, self.size) for t in range(NUM_FRAMES)]
        self._t = NUM_FRAMES
        return frames

    def vc(self, cond_frames, prompt, seed, *, mode="fast", negative_prompt=None):
        self._sleep()
        src = getattr(self, "_src", None)
        if src is None:
            src = Image.fromarray(cond_frames[-1])
        start = self._t
        new = [self._frame(src, start + t, self.size) for t in range(NUM_FRAMES - COND_FRAMES)]
        self._t = start + len(new)
        return list(cond_frames[-COND_FRAMES:]) + new

    def refine(self, stage1, prompt, seed, *, image=None, cond_frames=None):
        self._sleep()
        w, h = self.size
        up = [np.asarray(Image.fromarray(f).resize((w * 2, h * 2), Image.BICUBIC)) for f in stage1]
        doubled = [f for f in up for _ in range(2)]
        if cond_frames is not None:
            return list(cond_frames[-REFINE_COND_FRAMES:]) + doubled[REFINE_COND_FRAMES:]
        return doubled


def make_backend(kind: str | None = None, **kw):
    kind = kind or os.environ.get("LVGEN_BACKEND", "longcat")
    if kind == "mock":
        return MockBackend(delay=float(os.environ.get("LVGEN_MOCK_DELAY", "0")))
    repo = kw.pop("repo_dir", None) or os.environ.get("LONGCAT_REPO", "/workspace/LongCat-Video")
    ckpt = kw.pop("checkpoint_dir", None) or os.environ.get("LONGCAT_WEIGHTS", "/workspace/weights/LongCat-Video")
    kw.setdefault("offload_text_encoder", os.environ.get("LVGEN_OFFLOAD_TEXT_ENCODER", "0") == "1")
    kw.setdefault("offload_kv_cache", os.environ.get("LVGEN_OFFLOAD_KV_CACHE", "0") == "1")
    kw.setdefault("enable_compile", os.environ.get("LVGEN_COMPILE", "0") == "1")
    return LongCatBackend(repo, ckpt, **kw)
