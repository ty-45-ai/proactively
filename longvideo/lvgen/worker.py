"""常駐ワーカー: モデルを1回だけGPUに読み込み、キューに入ったジョブを順番に処理する。

使い方:
    python -m lvgen.worker --jobs /workspace/jobs

ブラウザ画面（app.py）は job.json と status.json を書くだけで、重い処理はすべてこのプロセスが行う。
そのためブラウザを閉じても生成は止まらない。ワーカーを再起動しても、"running" のまま
残ったジョブは自動的にキューへ戻され、完成済みセグメントの続きから再開する。
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from .backends import make_backend
from .jobs import JobStore
from .runner import _log, run_job


def write_heartbeat(path: Path, **kw) -> None:
    data = {"pid": os.getpid(), "time": time.time(), **kw}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.replace(path)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="LongCat-Video 長尺動画ワーカー")
    ap.add_argument("--jobs", default=os.environ.get("LVGEN_JOBS", "/workspace/jobs"))
    ap.add_argument("--backend", default=None, help="longcat（既定）/ mock")
    ap.add_argument("--once", action="store_true", help="キューが空になったら終了する（テスト用）")
    ap.add_argument("--poll", type=float, default=2.0)
    args = ap.parse_args(argv)

    store = JobStore(args.jobs)
    hb = store.root / "worker.json"

    # 前回ワーカーが落ちたときに "running" のまま残ったジョブをキューに戻す
    for job in store.list():
        if job.status.get("state") == "running":
            job.enqueue()
            _log(f"[{job.id}] 前回の途中から再開するためキューに戻しました")

    write_heartbeat(hb, state="loading", message="モデル読み込み中")
    _log("モデルを読み込んでいます（初回は数分かかります）...")
    backend = make_backend(args.backend)
    load_s = getattr(backend, "load_seconds", 0.0)
    _log(f"読み込み完了 backend={backend.name} ({load_s:.0f}秒)")
    started = time.time()

    while True:
        job = store.next_queued()
        if job is None:
            write_heartbeat(hb, state="idle", message="待機中", backend=backend.name, started=started)
            if args.once:
                return
            time.sleep(args.poll)
            continue
        write_heartbeat(hb, state="busy", job=job.id, backend=backend.name, started=started)
        try:
            run_job(job, backend, on_progress=lambda j: write_heartbeat(
                hb, state="busy", job=j.id, backend=backend.name, started=started))
        except Exception:  # noqa: BLE001 — 失敗はジョブの status.json に記録済み。ワーカーは止めない
            pass


if __name__ == "__main__":
    main()
