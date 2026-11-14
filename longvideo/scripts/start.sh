#!/usr/bin/env bash
# ワーカー（GPUで生成）とブラウザ画面（Streamlit, ポート 8501）を起動する。Pod を起動するたびに実行。
#   LVGEN_PASSWORD=好きなパスワード bash start.sh
set -euo pipefail
WS=${WORKSPACE:-/workspace}
HERE=$(cd "$(dirname "$0")/.." && pwd)
LOG=$WS/logs; mkdir -p "$LOG"
command -v ffmpeg >/dev/null || { apt-get update -qq && apt-get install -y -qq ffmpeg >/dev/null; }
# shellcheck disable=SC1091
source "$WS/venv/bin/activate"

if [ -z "${LVGEN_PASSWORD:-}" ]; then
  echo "!! LVGEN_PASSWORD を指定してください（例: LVGEN_PASSWORD=xxxx bash start.sh）"
  echo "   RunPod の公開URLは推測されにくいものの、誰でもアクセスできる状態になるためです。"
  exit 1
fi
export LONGCAT_REPO=$WS/LongCat-Video LONGCAT_WEIGHTS=$WS/weights/LongCat-Video LVGEN_JOBS=$WS/jobs
export LVGEN_PASSWORD LVGEN_GPU_PRICE=${LVGEN_GPU_PRICE:-2.0}
# VRAM 48GB 級（L40S 等）ではテキストエンコーダをCPUに逃がす
VRAM_MB=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1)
if [ "${VRAM_MB:-0}" -lt 70000 ]; then export LVGEN_OFFLOAD_TEXT_ENCODER=1 LVGEN_OFFLOAD_KV_CACHE=1; fi

pkill -f "lvgen.worker" 2>/dev/null || true
pkill -f "streamlit run app.py" 2>/dev/null || true
cd "$HERE"
nohup python -m lvgen.worker >> "$LOG/worker.log" 2>&1 &
echo "worker pid $!  (ログ: tail -f $LOG/worker.log)"
nohup streamlit run app.py --server.port 8501 --server.address 0.0.0.0 --server.headless true \
  --server.fileWatcherType none --server.maxUploadSize 50 >> "$LOG/ui.log" 2>&1 &
echo "UI pid $!  (ログ: tail -f $LOG/ui.log)"
echo
echo "ブラウザで RunPod の「Connect」→「HTTP Service [Port 8501]」を開いてください。"
echo "URL の形: https://<POD_ID>-8501.proxy.runpod.net"
