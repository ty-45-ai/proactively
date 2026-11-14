"""コマンドラインで1本生成する（第1段階の動作確認用）。

例: 画像1枚から約6秒の動画（1セグメント）
    python -m lvgen.cli --image my.png --prompt "She slowly turns her head and smiles." --seconds 6

例: 約30秒・720p/30fps に高画質化・最後に1080pへ拡大
    python -m lvgen.cli --image my.png --prompt-file prompts.txt --seconds 30 --refine --export-height 1080

途中で止まっても、同じ --resume <ジョブID> で続きから再開できる。
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .backends import make_backend
from .jobs import JobSettings, JobStore, seconds_for_segments, segments_for_seconds
from .runner import run_job


def build_prompts(prompt: str | None, prompt_file: str | None, n: int) -> list[str]:
    if prompt_file:
        lines = [ln.strip() for ln in Path(prompt_file).read_text(encoding="utf-8").splitlines()]
        lines = [ln for ln in lines if ln and not ln.startswith("#")]
    elif prompt:
        lines = [prompt]
    else:
        raise SystemExit("--prompt か --prompt-file を指定してください")
    # 行数がセグメント数より少なければ、最後の行を繰り返す
    return [lines[min(i, len(lines) - 1)] for i in range(n)]


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="画像1枚から動画を生成（LongCat-Video）")
    ap.add_argument("--jobs", default=os.environ.get("LVGEN_JOBS", "/workspace/jobs"))
    ap.add_argument("--image")
    ap.add_argument("--prompt")
    ap.add_argument("--prompt-file", help="1行=1セグメント(約5秒)のプロンプト")
    ap.add_argument("--seconds", type=float, default=6.0, help="目標の長さ（秒）")
    ap.add_argument("--segments", type=int, help="セグメント数を直接指定（--seconds より優先）")
    ap.add_argument("--mode", choices=["fast", "quality"], default="fast")
    ap.add_argument("--refine", action="store_true", help="720p/30fps に高画質化")
    ap.add_argument("--export-height", type=int, help="最終出力の高さ（例: 1080）")
    ap.add_argument("--export-fps", type=int, help="最終出力の fps（例: 24 / 30）")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--backend", help="longcat（既定）/ mock")
    ap.add_argument("--resume", help="途中で止まったジョブIDを再開")
    args = ap.parse_args(argv)

    store = JobStore(args.jobs)
    if args.resume:
        job = store.get(args.resume)
    else:
        if not args.image:
            raise SystemExit("--image を指定してください")
        n = args.segments or segments_for_seconds(args.seconds)
        prompts = build_prompts(args.prompt, args.prompt_file, n)
        settings = JobSettings(mode=args.mode, refine=args.refine, export_height=args.export_height,
                               export_fps=args.export_fps)
        job = store.create(Path(args.image).read_bytes(), prompts, settings=settings, base_seed=args.seed,
                           title=Path(args.image).stem)
        print(f"ジョブ {job.id}: {n} セグメント（約 {seconds_for_segments(n):.1f} 秒）")

    backend = make_backend(args.backend)
    run_job(job, backend)
    print(f"\n完成: {job.output_path}")
    print(f"生成にかかったGPU時間: {job.total_gpu_seconds() / 60:.1f} 分（モデル読み込みを除く）")


if __name__ == "__main__":
    main()
