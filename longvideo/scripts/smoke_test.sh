#!/usr/bin/env bash
# 第1段階の動作確認: 画像1枚 → 約6秒(93フレーム, 480p/15fps)の動画をコマンドで1本作る。
#   bash smoke_test.sh [画像パス] ["プロンプト"]
# 画像を省略すると LongCat-Video 付属のサンプル画像を使う。
set -euo pipefail
WS=${WORKSPACE:-/workspace}
HERE=$(cd "$(dirname "$0")/.." && pwd)
# shellcheck disable=SC1091
source "$WS/venv/bin/activate"
IMG=${1:-$WS/LongCat-Video/assets/girl.png}
PROMPT=${2:-"A woman sits at a wooden table by the window in a cozy cafe. She picks up the white coffee cup, takes a sip, puts it back and looks out the window."}
export LONGCAT_REPO=$WS/LongCat-Video LONGCAT_WEIGHTS=$WS/weights/LongCat-Video LVGEN_JOBS=$WS/jobs
cd "$HERE"
nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader
time python -m lvgen.cli --image "$IMG" --prompt "$PROMPT" --seconds 6 --mode fast "${@:3}"
nvidia-smi --query-gpu=memory.used --format=csv,noheader
echo "↑ の output.mp4 を確認してください（ブラウザ画面の『ジョブ』からも見られます）"
