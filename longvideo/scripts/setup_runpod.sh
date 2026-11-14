#!/usr/bin/env bash
# RunPod の Pod 上で1回だけ実行する初期セットアップ。
#   bash /workspace/proactively/longvideo/scripts/setup_runpod.sh
# /workspace（ネットワークボリューム / ボリュームディスク）に全部入れるので、
# Pod を止めて再開しても入れ直しは不要（ffmpeg だけは start.sh が毎回確認する）。
set -euo pipefail

WS=${WORKSPACE:-/workspace}
VENV=$WS/venv
LONGCAT_DIR=$WS/LongCat-Video
# 動作確認した LongCat-Video のコミット（2026-05-27）。更新するときはここを変える
LONGCAT_COMMIT=${LONGCAT_COMMIT:-6b3f4b8582a8bc3f20f795735f5383716c4ba794}

echo "== 1/5 GPU の確認 =="
nvidia-smi --query-gpu=name,memory.total,compute_cap --format=csv,noheader
CAP=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -1 | tr -d ' .')
case "$CAP" in
  80|86|89|90) echo "OK: 対応GPU (compute capability $CAP)";;
  *) echo "!! この GPU (compute capability $CAP) は第1段階では未対応です。"
     echo "   RTX 5090 / B200 などの Blackwell 世代は PyTorch 2.6 + flash-attn 2.7.4 では動きません。"
     echo "   H100 / A100 / L40S / RTX 6000 Ada で Pod を作り直してください。"; exit 1;;
esac

echo "== 2/5 システムパッケージ =="
if ! command -v ffmpeg >/dev/null; then
  apt-get update -qq && apt-get install -y -qq ffmpeg git >/dev/null
fi
ffmpeg -version | head -1

echo "== 3/5 LongCat-Video の取得 =="
if [ ! -d "$LONGCAT_DIR/.git" ]; then
  git clone https://github.com/meituan-longcat/LongCat-Video "$LONGCAT_DIR"
fi
git -C "$LONGCAT_DIR" fetch -q origin
git -C "$LONGCAT_DIR" checkout -q "$LONGCAT_COMMIT"
echo "LongCat-Video @ $(git -C "$LONGCAT_DIR" rev-parse --short HEAD)"

echo "== 4/5 Python 環境（$VENV）=="
PY=$(command -v python3.10 || command -v python3)
if [ ! -x "$VENV/bin/python" ]; then
  "$PY" -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install -q --upgrade pip wheel setuptools ninja packaging psutil
# 公式 README と同じ PyTorch 2.6 + CUDA 12.4
pip install -q torch==2.6.0 torchvision==0.21.0 torchaudio==2.6.0 --index-url https://download.pytorch.org/whl/cu124
# flash-attn はビルド済みファイルが自動で選ばれる（見つからないとソースからビルドになり 30 分以上かかる）
MAX_JOBS=${MAX_JOBS:-8} pip install -q flash_attn==2.7.4.post1 --no-build-isolation
pip install -q -r "$LONGCAT_DIR/requirements.txt"
pip install -q "huggingface_hub[cli]>=0.25" hf_transfer
python - <<'PY'
import torch, flash_attn
print("torch", torch.__version__, "cuda", torch.version.cuda, "gpu", torch.cuda.get_device_name(0))
print("flash_attn", flash_attn.__version__)
PY

echo "== 5/5 モデルの重み =="
bash "$(dirname "$0")/download_weights.sh"

echo
echo "セットアップ完了。次は:"
echo "  bash $(dirname "$0")/smoke_test.sh      # 画像1枚 → 約6秒の動画（コマンドで確認）"
echo "  bash $(dirname "$0")/start.sh           # ブラウザ画面とワーカーを起動"
