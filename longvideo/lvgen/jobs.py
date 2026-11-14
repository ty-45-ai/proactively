"""ジョブ（=完成動画1本）とセグメント（=約5秒の断片）のファイル管理。

すべての状態をファイルに保存するので、GPU サーバーやブラウザが途中で落ちても、
完成済みのセグメントはそのまま残り、続きから再開できる。

ディレクトリ構成:
    <jobs_root>/<job_id>/
        job.json          … 画面で決めた設定（プロンプト・シード・長さなど）。画面側が書く
        status.json       … 実行状態（queued / running / done / failed / cancelled）。主にワーカーが書く
        input.png         … アップロードされた画像
        CANCEL            … これがあるとワーカーは次のセグメントの前で止まる
        segments/
            000.json      … セグメントの記録（所要時間・シードなど）
            000_full.mp4  … 低解像度(480p/15fps)の生成結果 93 フレーム（先頭13枚は前セグメントとの重なり）
            000_tail.mkv  … 次のセグメントの条件付けに使う末尾13フレーム（可逆圧縮）
            000_view.mp4  … 新しく増えた部分だけ（プレビュー・連結用）
            000_rtail.mkv / 000_rview.mp4 … 720p/30fps に高画質化した版（refine 有効時）
        final.mp4         … 全セグメントを連結したもの
        output.mp4        … 指定解像度・fps に変換した完成品
"""

from __future__ import annotations

import json
import math
import os
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# ---- LongCat-Video の生成単位（公式デモと同じ値） -------------------------------
NUM_FRAMES = 93          # 1回の生成フレーム数 (480p, 15fps → 約6.2秒)
COND_FRAMES = 13         # 続きを作るときに引き継ぐフレーム数
REFINE_COND_FRAMES = 26  # 720p/30fps 化のときに引き継ぐフレーム数（時間方向に2倍）
STAGE1_FPS = 15
REFINE_FPS = 30

FIRST_SEG_SECONDS = NUM_FRAMES / STAGE1_FPS                       # 6.2 秒
NEXT_SEG_SECONDS = (NUM_FRAMES - COND_FRAMES) / STAGE1_FPS        # 約5.33 秒

DEFAULT_NEGATIVE = (
    "Bright tones, overexposed, static, blurred details, subtitles, style, works, paintings, images, static, "
    "overall gray, worst quality, low quality, JPEG compression residue, ugly, incomplete, extra fingers, "
    "poorly drawn hands, poorly drawn faces, deformed, disfigured, misshapen limbs, fused fingers, still picture, "
    "messy background, three legs, many people in the background, walking backwards"
)


def segments_for_seconds(seconds: float) -> int:
    if seconds <= FIRST_SEG_SECONDS:
        return 1
    return 1 + math.ceil((seconds - FIRST_SEG_SECONDS) / NEXT_SEG_SECONDS - 1e-9)


def seconds_for_segments(n: int) -> float:
    return FIRST_SEG_SECONDS + max(0, n - 1) * NEXT_SEG_SECONDS


def _now() -> float:
    return time.time()


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(path)


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


@dataclass
class SegmentSpec:
    prompt: str
    seed: int


@dataclass
class JobSettings:
    mode: str = "fast"          # "fast"(蒸留LoRA・16ステップ) / "quality"(50ステップ)
    refine: bool = False        # True で 720p/30fps に高画質化（LongCat の refinement LoRA）
    export_height: int | None = None  # 最終出力の高さ。None なら生成解像度のまま。1080 で拡大
    export_fps: int | None = None     # 最終出力の fps。None なら生成 fps のまま
    negative_prompt: str = DEFAULT_NEGATIVE


@dataclass
class JobSpec:
    id: str
    title: str
    created_at: float
    settings: JobSettings
    segments: list[SegmentSpec] = field(default_factory=list)

    @staticmethod
    def from_dict(d: dict) -> "JobSpec":
        return JobSpec(
            id=d["id"],
            title=d.get("title", d["id"]),
            created_at=d.get("created_at", 0.0),
            settings=JobSettings(**d.get("settings", {})),
            segments=[SegmentSpec(**s) for s in d.get("segments", [])],
        )


class Job:
    def __init__(self, root: Path):
        self.root = Path(root)

    # ---- パス -----------------------------------------------------------------
    @property
    def id(self) -> str:
        return self.root.name

    @property
    def seg_dir(self) -> Path:
        return self.root / "segments"

    @property
    def input_image(self) -> Path:
        return self.root / "input.png"

    @property
    def final_path(self) -> Path:
        return self.root / "final.mp4"

    @property
    def output_path(self) -> Path:
        return self.root / "output.mp4"

    def seg_path(self, k: int, kind: str) -> Path:
        ext = "mkv" if kind in ("tail", "rtail") else ("json" if kind == "meta" else "mp4")
        name = f"{k:03d}.json" if kind == "meta" else f"{k:03d}_{kind}.{ext}"
        return self.seg_dir / name

    # ---- 設定 -----------------------------------------------------------------
    @property
    def spec(self) -> JobSpec:
        return JobSpec.from_dict(_read_json(self.root / "job.json"))

    def save_spec(self, spec: JobSpec) -> None:
        _write_json(self.root / "job.json", asdict(spec))

    # ---- 状態 -----------------------------------------------------------------
    @property
    def status(self) -> dict:
        return _read_json(self.root / "status.json", {"state": "new"})

    def set_status(self, **kw) -> dict:
        st = self.status
        st.update(kw)
        st["updated_at"] = _now()
        _write_json(self.root / "status.json", st)
        return st

    def seg_meta(self, k: int) -> dict:
        return _read_json(self.seg_path(k, "meta"), {})

    def update_seg_meta(self, k: int, **kw) -> dict:
        m = self.seg_meta(k)
        m.update(kw)
        self.seg_dir.mkdir(parents=True, exist_ok=True)
        _write_json(self.seg_path(k, "meta"), m)
        return m

    def stage1_done(self, k: int) -> bool:
        return all(self.seg_path(k, x).exists() for x in ("full", "tail", "view"))

    def refine_done(self, k: int) -> bool:
        return all(self.seg_path(k, x).exists() for x in ("rtail", "rview"))

    def segment_done(self, k: int) -> bool:
        return self.stage1_done(k) and (not self.spec.settings.refine or self.refine_done(k))

    def done_count(self) -> int:
        n = 0
        for k in range(len(self.spec.segments)):
            if not self.segment_done(k):
                break
            n += 1
        return n

    # ---- 操作 -----------------------------------------------------------------
    def request_cancel(self) -> None:
        (self.root / "CANCEL").touch()

    def cancel_requested(self) -> bool:
        return (self.root / "CANCEL").exists()

    def clear_cancel(self) -> None:
        (self.root / "CANCEL").unlink(missing_ok=True)

    def enqueue(self) -> None:
        self.clear_cancel()
        self.set_status(state="queued", error=None, message="順番待ち", queued_at=_now())

    def invalidate_from(self, k: int, *, refine_only: bool = False) -> None:
        """セグメント k 以降を削除する（後ろのセグメントは k に依存しているため）。"""
        kinds = ("rtail", "rview") if refine_only else ("full", "tail", "view", "rtail", "rview", "meta")
        for p in list(self.seg_dir.glob("*")):
            stem = p.name.split("_")[0].split(".")[0]
            if not stem.isdigit() or int(stem) < k:
                continue
            kind = "meta" if p.suffix == ".json" else p.stem.split("_", 1)[-1]
            if kind in kinds:
                p.unlink(missing_ok=True)
        self.final_path.unlink(missing_ok=True)
        self.output_path.unlink(missing_ok=True)

    def regenerate_segment(self, k: int, *, new_seed: int | None = None, new_prompt: str | None = None) -> None:
        spec = self.spec
        if new_seed is not None:
            spec.segments[k].seed = int(new_seed)
        if new_prompt is not None:
            spec.segments[k].prompt = new_prompt
        self.save_spec(spec)
        self.invalidate_from(k)

    def total_gpu_seconds(self) -> float:
        tot = 0.0
        for p in self.seg_dir.glob("*.json"):
            m = _read_json(p, {})
            tot += float(m.get("stage1_seconds", 0)) + float(m.get("refine_seconds", 0))
        return tot

    def delete(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class JobStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, image_bytes: bytes, prompts: list[str], *, settings: JobSettings, base_seed: int = 42,
               title: str = "") -> Job:
        if not prompts:
            raise ValueError("prompts must not be empty")
        job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        job = Job(self.root / job_id)
        job.root.mkdir(parents=True)
        job.seg_dir.mkdir()
        _save_png(image_bytes, job.input_image)
        spec = JobSpec(
            id=job_id,
            title=title or job_id,
            created_at=_now(),
            settings=settings,
            segments=[SegmentSpec(prompt=p, seed=base_seed + i) for i, p in enumerate(prompts)],
        )
        job.save_spec(spec)
        job.set_status(state="new", message="作成済み")
        return job

    def get(self, job_id: str) -> Job:
        job = Job(self.root / job_id)
        if not (job.root / "job.json").exists():
            raise KeyError(job_id)
        return job

    def list(self) -> list[Job]:
        jobs = [Job(p) for p in self.root.iterdir() if (p / "job.json").exists()]
        return sorted(jobs, key=lambda j: j.id, reverse=True)

    def next_queued(self) -> Job | None:
        queued = [j for j in self.list() if j.status.get("state") == "queued"]
        return min(queued, key=lambda j: j.status.get("queued_at", 0)) if queued else None


def _save_png(image_bytes: bytes, dest: Path) -> None:
    from io import BytesIO

    from PIL import Image, ImageOps

    img = Image.open(BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.save(dest, format="PNG")
