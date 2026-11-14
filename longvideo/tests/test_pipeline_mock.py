"""GPU なしで、ジョブの生成・再開・作り直し・連結を確認するテスト（MockBackend を使用）。

実行: cd longvideo && python -m pytest -q tests
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lvgen import videoio  # noqa: E402
from lvgen.backends import MockBackend  # noqa: E402
from lvgen.jobs import (COND_FRAMES, NUM_FRAMES, JobSettings, JobStore, seconds_for_segments,  # noqa: E402
                        segments_for_seconds)
from lvgen.runner import run_job  # noqa: E402


def _png() -> bytes:
    rng = np.random.default_rng(0)
    arr = (rng.random((360, 640, 3)) * 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return buf.getvalue()


def test_segment_math():
    assert segments_for_seconds(6) == 1
    assert segments_for_seconds(6.2) == 1
    assert segments_for_seconds(10) == 2
    assert segments_for_seconds(600) == 113
    assert seconds_for_segments(113) >= 600


def test_lossless_roundtrip(tmp_path):
    frames = [(np.random.default_rng(i).random((48, 64, 3)) * 255).astype(np.uint8) for i in range(5)]
    p = videoio.write_video(tmp_path / "x.mkv", frames, 15, lossless=True)
    back = videoio.read_video(p)
    assert len(back) == 5
    assert all(np.array_equal(a, b) for a, b in zip(frames, back))


def test_job_runs_and_resumes(tmp_path):
    store = JobStore(tmp_path)
    job = store.create(_png(), ["a", "b", "c"], settings=JobSettings(mode="fast"), base_seed=1)
    backend = MockBackend()

    # 2セグメント目の途中で止める → 再開で続きから
    class Boom(MockBackend):
        def vc(self, *a, **k):
            raise RuntimeError("GPU died")

    with pytest.raises(RuntimeError):
        run_job(job, Boom())
    assert job.status["state"] == "failed"
    assert job.stage1_done(0) and not job.stage1_done(1)
    first_mtime = job.seg_path(0, "full").stat().st_mtime_ns

    run_job(job, backend)
    assert job.status["state"] == "done"
    assert job.seg_path(0, "full").stat().st_mtime_ns == first_mtime  # 完成済みは作り直さない
    meta = videoio.probe(job.output_path)
    assert meta["frames"] == NUM_FRAMES + 2 * (NUM_FRAMES - COND_FRAMES)
    assert meta["fps"] == pytest.approx(15)


def test_regenerate_from_middle(tmp_path):
    store = JobStore(tmp_path)
    job = store.create(_png(), ["a"] * 3, settings=JobSettings(), base_seed=5)
    run_job(job, MockBackend())
    keep = job.seg_path(0, "full").stat().st_mtime_ns
    job.regenerate_segment(1, new_seed=999, new_prompt="new")
    assert job.stage1_done(0) and not job.stage1_done(1) and not job.stage1_done(2)
    assert not job.output_path.exists()
    assert job.spec.segments[1].seed == 999 and job.spec.segments[1].prompt == "new"
    run_job(job, MockBackend())
    assert job.seg_path(0, "full").stat().st_mtime_ns == keep
    assert job.output_path.exists()


def test_refine_and_export(tmp_path):
    store = JobStore(tmp_path)
    job = store.create(_png(), ["a"] * 2, settings=JobSettings(refine=True, export_height=1080, export_fps=24))
    run_job(job, MockBackend())
    final = videoio.probe(job.final_path)
    assert final["fps"] == pytest.approx(30)
    assert final["frames"] == 2 * NUM_FRAMES + (2 * NUM_FRAMES - 26)
    out = videoio.probe(job.output_path)
    assert out["height"] == 1080 and out["fps"] == pytest.approx(24)


def test_cancel(tmp_path):
    store = JobStore(tmp_path)
    job = store.create(_png(), ["a"] * 3, settings=JobSettings())

    class CancelAfterFirst(MockBackend):
        def i2v(self, *a, **k):
            out = super().i2v(*a, **k)
            job.request_cancel()
            return out

    run_job(job, CancelAfterFirst())
    assert job.status["state"] == "cancelled"
    assert job.done_count() == 1
    job.enqueue()
    assert store.next_queued().id == job.id
