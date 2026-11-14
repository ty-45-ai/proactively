#!/usr/bin/env bash
# LongCat-Video の重みを /workspace/weights/LongCat-Video にダウンロードする（途中で切れても再実行で続きから）。
set -euo pipefail
WS=${WORKSPACE:-/workspace}
DEST=${LONGCAT_WEIGHTS:-$WS/weights/LongCat-Video}
# shellcheck disable=SC1091
[ -f "$WS/venv/bin/activate" ] && source "$WS/venv/bin/activate"
mkdir -p "$DEST"
df -h "$DEST" | tail -1
export HF_HUB_ENABLE_HF_TRANSFER=1
python - "$DEST" <<'PY'
import sys
from huggingface_hub import snapshot_download
dest = sys.argv[1]
snapshot_download("meituan-longcat/LongCat-Video", local_dir=dest)
print("downloaded to", dest)
PY
du -sh "$DEST"
for f in dit text_encoder vae tokenizer scheduler lora/cfg_step_lora.safetensors lora/refinement_lora.safetensors; do
  [ -e "$DEST/$f" ] || { echo "!! 見つかりません: $DEST/$f"; exit 1; }
done
echo "重みの確認 OK"
