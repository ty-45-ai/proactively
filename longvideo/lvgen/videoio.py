"""FFmpeg を使った動画の読み書き。

フレームはすべて numpy の uint8 配列 (高さ, 幅, 3) / RGB で扱う。
- 表示・連結用: H.264 (yuv420p) の mp4
- 次のセグメントの条件付けに使うフレーム: 劣化の積み重ねを防ぐため、可逆圧縮 (libx264rgb, crf 0) の mkv
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"


def _run(cmd: list[str], input_bytes: bytes | None = None) -> bytes:
    proc = subprocess.run(cmd, input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed ({' '.join(cmd[:6])} ...):\n{proc.stderr.decode(errors='replace')[-2000:]}")
    return proc.stdout


def _as_frames(frames: Iterable) -> list[np.ndarray]:
    out = []
    for f in frames:
        a = np.asarray(f)
        if a.dtype != np.uint8:
            a = (np.clip(a, 0.0, 1.0) * 255).round().astype(np.uint8)
        if a.ndim != 3 or a.shape[2] != 3:
            raise ValueError(f"frame must be HxWx3, got {a.shape}")
        out.append(np.ascontiguousarray(a))
    if not out:
        raise ValueError("no frames")
    return out


def write_video(path: str | Path, frames: Sequence, fps: float, *, crf: int = 14, lossless: bool = False) -> Path:
    """フレーム列を動画ファイルに書き出す（一時ファイル経由で原子的に置き換え）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = _as_frames(frames)
    h, w, _ = frames[0].shape
    tmp = path.with_name(path.stem + ".tmp" + path.suffix)
    cmd = [FFMPEG, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{w}x{h}", "-r", f"{fps}", "-i", "-"]
    if lossless:
        cmd += ["-c:v", "libx264rgb", "-crf", "0", "-preset", "veryfast", "-pix_fmt", "rgb24"]
    else:
        if w % 2 or h % 2:
            raise ValueError("width/height must be even for yuv420p")
        cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", str(crf), "-pix_fmt", "yuv420p",
                "-movflags", "+faststart"]
    cmd.append(str(tmp))
    _run(cmd, input_bytes=b"".join(f.tobytes() for f in frames))
    tmp.replace(path)
    return path


def probe(path: str | Path) -> dict:
    out = _run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-count_frames",
                "-show_entries", "stream=width,height,r_frame_rate,nb_read_frames",
                "-show_entries", "format=duration", "-of", "json", str(path)])
    info = json.loads(out)
    s = info["streams"][0]
    num, den = s["r_frame_rate"].split("/")
    return {
        "width": int(s["width"]),
        "height": int(s["height"]),
        "fps": float(num) / float(den),
        "frames": int(s.get("nb_read_frames", 0)),
        "duration": float(info.get("format", {}).get("duration", 0.0)),
    }


def read_video(path: str | Path) -> list[np.ndarray]:
    meta = probe(path)
    w, h = meta["width"], meta["height"]
    raw = _run([FFMPEG, "-loglevel", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
    arr = np.frombuffer(raw, dtype=np.uint8)
    n = arr.size // (w * h * 3)
    return [arr[i * w * h * 3:(i + 1) * w * h * 3].reshape(h, w, 3).copy() for i in range(n)]


def concat_videos(paths: Sequence[str | Path], out_path: str | Path) -> Path:
    """同じ解像度・fps・コーデックの mp4 を再エンコードなしで連結する。"""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    list_file = out_path.with_suffix(".concat.txt")
    list_file.write_text("".join(f"file '{Path(p).resolve().as_posix()}'\n" for p in paths))
    tmp = out_path.with_name(out_path.stem + ".tmp" + out_path.suffix)
    try:
        _run([FFMPEG, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(list_file),
              "-c", "copy", "-movflags", "+faststart", str(tmp)])
    finally:
        list_file.unlink(missing_ok=True)
    tmp.replace(out_path)
    return out_path


def export_video(src: str | Path, out_path: str | Path, *, height: int | None, fps: int | None, crf: int = 16) -> Path:
    """最終出力用: 解像度（Lanczos 拡大）と fps をそろえて mp4 にする。

    ※ ここでの拡大は従来型の補間。AI アップスケーラーは第5段階で差し替える予定。
    """
    out_path = Path(out_path)
    filters = []
    if fps:
        filters.append(f"fps={fps}")
    if height:
        filters.append(f"scale=-2:{height}:flags=lanczos")
    tmp = out_path.with_name(out_path.stem + ".tmp" + out_path.suffix)
    cmd = [FFMPEG, "-y", "-loglevel", "error", "-i", str(src)]
    if filters:
        cmd += ["-vf", ",".join(filters)]
    cmd += ["-c:v", "libx264", "-preset", "slow", "-crf", str(crf), "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(tmp)]
    _run(cmd)
    tmp.replace(out_path)
    return out_path
