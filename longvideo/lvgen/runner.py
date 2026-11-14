"""ジョブの実行本体: セグメントを順番に生成し、連結して mp4 にする。

- 完成済みのセグメントはスキップするので、途中で止まっても続きから再開できる。
- セグメントごとに「生成(480p/15fps) → 必要なら高画質化(720p/30fps)」の順で進める。
  こうすると1本目の完成形を早く確認でき、問題があればそこで止めて作り直せる。
"""

from __future__ import annotations

import shutil
import time
import traceback
from typing import Callable

from PIL import Image

from . import videoio
from .jobs import COND_FRAMES, REFINE_COND_FRAMES, REFINE_FPS, STAGE1_FPS, Job


class Cancelled(Exception):
    pass


def _log(msg: str) -> None:
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


def run_job(job: Job, backend, *, on_progress: Callable[[Job], None] | None = None) -> Job:
    spec = job.spec
    s = spec.settings
    n = len(spec.segments)
    image = Image.open(job.input_image).convert("RGB")
    job.set_status(state="running", started_at=time.time(), error=None, total=n, backend=backend.name)

    def progress(msg: str, k: int):
        job.set_status(message=msg, current=k, done=job.done_count())
        _log(f"[{job.id}] {msg}")
        if on_progress:
            on_progress(job)

    try:
        for k, seg in enumerate(spec.segments):
            if job.cancel_requested():
                raise Cancelled()

            # ---- 1) 低解像度で生成 ----------------------------------------------
            if not job.stage1_done(k):
                progress(f"セグメント {k + 1}/{n} を生成中 (480p)", k)
                t0 = time.time()
                if k == 0:
                    frames = backend.i2v(image, seg.prompt, seg.seed, mode=s.mode, negative_prompt=s.negative_prompt)
                    new_frames = frames
                else:
                    if not job.stage1_done(k - 1):
                        raise RuntimeError(f"セグメント {k} の前のセグメントがありません")
                    cond = videoio.read_video(job.seg_path(k - 1, "tail"))
                    frames = backend.vc(cond, seg.prompt, seg.seed, mode=s.mode, negative_prompt=s.negative_prompt)
                    new_frames = frames[COND_FRAMES:]
                videoio.write_video(job.seg_path(k, "full"), frames, STAGE1_FPS, crf=12)
                videoio.write_video(job.seg_path(k, "view"), new_frames, STAGE1_FPS, crf=16)
                # tail は最後に書く（= stage1 完了の目印）
                videoio.write_video(job.seg_path(k, "tail"), frames[-COND_FRAMES:], STAGE1_FPS, lossless=True)
                job.update_seg_meta(k, seed=seg.seed, prompt=seg.prompt, mode=s.mode,
                                    stage1_seconds=round(time.time() - t0, 2),
                                    stage1_peak_vram_gb=getattr(backend, "last_peak_gb", None),
                                    width=int(frames[0].shape[1]), height=int(frames[0].shape[0]))

            # ---- 2) 720p / 30fps に高画質化 ---------------------------------------
            if s.refine and not job.refine_done(k):
                if job.cancel_requested():
                    raise Cancelled()
                progress(f"セグメント {k + 1}/{n} を高画質化中 (720p/30fps)", k)
                t0 = time.time()
                stage1 = videoio.read_video(job.seg_path(k, "full"))
                if k == 0:
                    out = backend.refine(stage1, seg.prompt, seg.seed, image=image)
                    new_frames = out
                else:
                    if not job.refine_done(k - 1):
                        raise RuntimeError(f"セグメント {k} の前の高画質版がありません")
                    cond = videoio.read_video(job.seg_path(k - 1, "rtail"))
                    out = backend.refine(stage1, seg.prompt, seg.seed, cond_frames=cond)
                    new_frames = out[REFINE_COND_FRAMES:]
                videoio.write_video(job.seg_path(k, "rview"), new_frames, REFINE_FPS, crf=14)
                videoio.write_video(job.seg_path(k, "rtail"), out[-REFINE_COND_FRAMES:], REFINE_FPS, lossless=True)
                job.update_seg_meta(k, refine_seconds=round(time.time() - t0, 2),
                                    refine_peak_vram_gb=getattr(backend, "last_peak_gb", None),
                                    refined_width=int(out[0].shape[1]), refined_height=int(out[0].shape[0]))

        # ---- 3) 連結と書き出し --------------------------------------------------
        progress("連結して mp4 に書き出し中", n - 1)
        finalize(job)
        job.set_status(state="done", message="完成", finished_at=time.time(), done=n)
        _log(f"[{job.id}] 完成: {job.output_path}")
    except Cancelled:
        job.clear_cancel()
        job.set_status(state="cancelled", message="停止しました（完成済みのセグメントは残っています）")
    except Exception as e:  # noqa: BLE001 — 失敗内容を画面に出すため
        job.set_status(state="failed", error=f"{type(e).__name__}: {e}", traceback=traceback.format_exc()[-4000:],
                       message="失敗しました（「再開」で続きから実行できます）")
        _log(f"[{job.id}] 失敗: {e}")
        raise
    return job


def finalize(job: Job) -> None:
    s = job.spec.settings
    n = len(job.spec.segments)
    kind = "rview" if s.refine else "view"
    paths = [job.seg_path(k, kind) for k in range(n)]
    missing = [p.name for p in paths if not p.exists()]
    if missing:
        raise RuntimeError(f"未完成のセグメントがあります: {missing[:5]}")
    videoio.concat_videos(paths, job.final_path)
    if s.export_height or s.export_fps:
        videoio.export_video(job.final_path, job.output_path, height=s.export_height, fps=s.export_fps)
    else:
        job.output_path.unlink(missing_ok=True)
        try:
            job.output_path.hardlink_to(job.final_path)
        except OSError:
            shutil.copyfile(job.final_path, job.output_path)
