"""ブラウザ操作画面（Streamlit）。

起動: streamlit run app.py --server.port 8501 --server.address 0.0.0.0
重い生成処理は別プロセスのワーカー（python -m lvgen.worker）が行う。
この画面はジョブの作成・状態表示・プレビュー・作り直し指示だけを担当する。
"""

from __future__ import annotations

import hmac
import json
import os
import time
from pathlib import Path

import streamlit as st

from lvgen.jobs import (DEFAULT_NEGATIVE, JobSettings, JobStore, seconds_for_segments,
                        segments_for_seconds)

JOBS_DIR = os.environ.get("LVGEN_JOBS", "/workspace/jobs")
PASSWORD = os.environ.get("LVGEN_PASSWORD", "")
DEFAULT_PRICE = float(os.environ.get("LVGEN_GPU_PRICE", "2.0"))

LENGTHS = {
    "約6秒（お試し・1セグメント）": 6.2,
    "約30秒": 30,
    "1分": 60,
    "3分": 180,
    "5分": 300,
    "10分": 600,
    "15分": 900,
    "20分": 1200,
}
STATE_LABEL = {
    "new": "未開始", "queued": "順番待ち", "running": "生成中", "done": "完成",
    "failed": "失敗", "cancelled": "停止",
}
PAGE_SIZE = 12

st.set_page_config(page_title="長尺動画スタジオ", page_icon="🎬", layout="wide")


# ---- ログイン -------------------------------------------------------------------
def require_password() -> None:
    if not PASSWORD:
        st.warning("LVGEN_PASSWORD が未設定です。URL を知っている人なら誰でも操作できます。start.sh で設定してください。")
        return
    if st.session_state.get("authed"):
        return
    st.title("🎬 長尺動画スタジオ")
    pw = st.text_input("パスワード", type="password")
    if st.button("ログイン"):
        if hmac.compare_digest(pw, PASSWORD):
            st.session_state.authed = True
            st.rerun()
        st.error("パスワードが違います")
    st.stop()


require_password()
store = JobStore(JOBS_DIR)


# ---- サイドバー --------------------------------------------------------------------
def worker_info() -> dict:
    try:
        return json.loads((store.root / "worker.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


with st.sidebar:
    st.header("⚙️ サーバー")
    price = st.number_input("GPU料金（$/時間）", min_value=0.0, value=DEFAULT_PRICE, step=0.1,
                            help="RunPod で借りているGPUの時間単価。概算料金の計算に使います。")

    @st.fragment(run_every=5)
    def worker_panel():
        w = worker_info()
        age = time.time() - w.get("time", 0)
        if not w:
            st.error("ワーカー未起動")
        elif w.get("state") == "loading":
            st.info("ワーカー: モデル読み込み中…")
        elif age > 120 and w.get("state") != "busy":
            st.error(f"ワーカー応答なし（{age:.0f}秒前）")
        else:
            label = {"idle": "待機中", "busy": "生成中"}.get(w.get("state"), w.get("state"))
            st.success(f"ワーカー: {label}（{w.get('backend', '?')}）")
        if w.get("started"):
            up_h = (time.time() - w["started"]) / 3600
            st.caption(f"ワーカー稼働 {up_h:.2f} 時間 ≒ ${up_h * price:.2f}（Pod 起動中は待機中も課金されます）")

    worker_panel()

    jobs = store.list()
    st.header("📁 ジョブ")
    options = ["＋ 新規作成"] + [j.id for j in jobs]

    def label_of(x: str) -> str:
        if x == "＋ 新規作成":
            return x
        j = store.get(x)
        return f"{j.spec.title}｜{STATE_LABEL.get(j.status.get('state'), '?')}"

    default_idx = options.index(st.session_state["selected"]) if st.session_state.get("selected") in options else 0
    selected = st.radio("選択", options, index=default_idx, format_func=label_of, label_visibility="collapsed")
    st.session_state["selected"] = selected


# ---- 新規作成 --------------------------------------------------------------------
def avg_seconds_per_segment(refine: bool) -> float | None:
    vals = []
    for j in store.list()[:20]:
        for p in j.seg_dir.glob("*.json"):
            try:
                m = json.loads(p.read_text())
            except json.JSONDecodeError:
                continue
            if "stage1_seconds" in m and (not refine or "refine_seconds" in m):
                vals.append(m["stage1_seconds"] + (m.get("refine_seconds", 0) if refine else 0))
    return sum(vals) / len(vals) if vals else None


def page_new() -> None:
    st.title("🎬 新しい動画を作る")
    c1, c2 = st.columns([1, 1])
    with c1:
        up = st.file_uploader("画像（1枚）", type=["png", "jpg", "jpeg", "webp"])
        if up:
            st.image(up, use_container_width=True)
        title = st.text_input("タイトル（任意）")
    with c2:
        length = st.selectbox("動画の長さ", list(LENGTHS), index=0)
        mode = st.radio("生成モード", ["fast", "quality"], horizontal=True,
                        format_func=lambda m: {"fast": "高速（16ステップ）", "quality": "高品質（50ステップ・約3倍遅い）"}[m])
        refine = st.checkbox("720p / 30fps に高画質化（LongCat 内蔵の refine。時間は約2倍以上）", value=False)
        cc1, cc2 = st.columns(2)
        with cc1:
            res = st.selectbox("出力解像度", ["生成したまま", "720p", "1080p"], index=0,
                               help="1080p は現在 FFmpeg の Lanczos 拡大です（AI 超解像は第5段階で追加予定）")
        with cc2:
            fps = st.selectbox("出力FPS", ["生成したまま", "24", "30"], index=0,
                               help="生成は 15fps（高画質化ありなら 30fps）。24/30 への変換はフレーム複製・間引きです")
        seed = st.number_input("シード", min_value=0, max_value=2**31 - 1, value=42)

    n = segments_for_seconds(LENGTHS[length])
    st.markdown(f"**{n} セグメント**（約 {seconds_for_segments(n):.0f} 秒）。1セグメント ≒ 5.3 秒ずつ続きを生成します。")
    avg = avg_seconds_per_segment(refine)
    if avg:
        est_h = avg * n / 3600
        st.caption(f"これまでの実績からの見積り: 約 {est_h * 60:.0f} 分 ≒ ${est_h * price:.2f}")
    else:
        st.caption("所要時間の見積りは、まず短い動画を1本作ると表示されます。")

    st.subheader("動きの指示（英語推奨）")
    st.caption("1行だけなら全セグメント共通。複数行なら 1行目→セグメント1、2行目→セグメント2…（足りない分は最後の行を使う）")
    prompt_text = st.text_area(
        "プロンプト", height=140, label_visibility="collapsed",
        placeholder="A woman sits by the window in a cozy cafe, slowly turns her head toward the camera and smiles. "
                    "Natural light, realistic, stable camera.")
    with st.expander("ネガティブプロンプト（高品質モードのみ有効）"):
        negative = st.text_area("negative", value=DEFAULT_NEGATIVE, height=100, label_visibility="collapsed")

    if st.button("▶ 生成開始", type="primary"):
        if not up or not prompt_text.strip():
            st.error("画像とプロンプトを入力してください")
            return
        lines = [ln.strip() for ln in prompt_text.splitlines() if ln.strip()]
        prompts = [lines[min(i, len(lines) - 1)] for i in range(n)]
        settings = JobSettings(
            mode=mode, refine=refine,
            export_height={"生成したまま": None, "720p": 720, "1080p": 1080}[res],
            export_fps=None if fps == "生成したまま" else int(fps),
            negative_prompt=negative,
        )
        job = store.create(up.getvalue(), prompts, settings=settings, base_seed=int(seed),
                           title=title or Path(up.name).stem)
        job.enqueue()
        st.session_state["selected"] = job.id
        st.rerun()


# ---- ジョブ詳細 --------------------------------------------------------------------
def page_job(job_id: str) -> None:
    try:
        job = store.get(job_id)
    except KeyError:
        st.error("ジョブが見つかりません")
        return
    spec = job.spec
    s = spec.settings
    n = len(spec.segments)
    st.title(f"🎞 {spec.title}")
    st.caption(f"ID: {job.id}｜{n} セグメント（約 {seconds_for_segments(n):.0f} 秒）｜"
               f"{'高速' if s.mode == 'fast' else '高品質'}｜{'720p/30fps 高画質化あり' if s.refine else '480p/15fps'}")

    @st.fragment(run_every=3)
    def status_panel():
        stt = job.status
        state = stt.get("state", "new")
        done = job.done_count()
        # 状態や完了数が変わったら、セグメント一覧やサイドバーも描き直す
        key = f"_last_{job.id}"
        snapshot = (state, done)
        if st.session_state.get(key) not in (None, snapshot):
            st.session_state[key] = snapshot
            st.rerun()
        st.session_state[key] = snapshot
        st.progress(done / n, text=f"{STATE_LABEL.get(state, state)}：{done}/{n} セグメント完了｜{stt.get('message', '')}")
        gpu_s = job.total_gpu_seconds()
        st.caption(f"生成に使ったGPU時間 {gpu_s / 60:.1f} 分 ≒ ${gpu_s / 3600 * price:.2f}（モデル読み込み・待機時間を除く）")
        if state == "failed":
            st.error(stt.get("error", ""))
            with st.expander("詳細"):
                st.code(stt.get("traceback", ""))
        if state in ("queued", "running"):
            if st.button("⏹ 停止（今のセグメントが終わったら止まる）"):
                job.request_cancel()
                if state == "queued":
                    job.set_status(state="cancelled", message="停止しました")
                st.rerun()
        elif done < n:
            if st.button("▶ 続きから再開", type="primary"):
                job.enqueue()
                st.rerun()
        if job.output_path.exists() and state == "done":
            st.video(str(job.output_path))
            with open(job.output_path, "rb") as f:
                st.download_button("⬇ 完成動画をダウンロード (MP4)", f, file_name=f"{spec.title}.mp4",
                                   mime="video/mp4", type="primary")

    status_panel()

    busy = job.status.get("state") in ("queued", "running")
    st.subheader("セグメント")
    if busy:
        st.caption("生成中は作り直しできません。停止してから操作してください。")
    pages = max(1, (n + PAGE_SIZE - 1) // PAGE_SIZE)
    page = st.number_input("ページ", 1, pages, 1) if pages > 1 else 1
    start = (page - 1) * PAGE_SIZE
    cols = st.columns(3)
    for i, k in enumerate(range(start, min(n, start + PAGE_SIZE))):
        seg = spec.segments[k]
        meta = job.seg_meta(k)
        with cols[i % 3].container(border=True):
            st.markdown(f"**#{k + 1}**　seed {seg.seed}")
            view = job.seg_path(k, "rview" if s.refine and job.refine_done(k) else "view")
            if view.exists():
                st.video(str(view))
                secs = meta.get("stage1_seconds", 0) + meta.get("refine_seconds", 0)
                peak = max(meta.get("stage1_peak_vram_gb") or 0, meta.get("refine_peak_vram_gb") or 0)
                st.caption(f"生成 {secs:.0f} 秒" + (f"｜VRAM ピーク {peak:.1f} GB" if peak else ""))
            else:
                st.caption("未生成")
            with st.expander("作り直す"):
                new_prompt = st.text_area("プロンプト", seg.prompt, key=f"p{k}", height=100)
                if st.button("🔁 このセグメントから作り直す", key=f"r{k}", disabled=busy,
                             help="このセグメントと、それ以降をすべて作り直します（後ろは前の続きとして作られるため）"):
                    job.regenerate_segment(k, new_seed=seg.seed + 1000, new_prompt=new_prompt)
                    job.enqueue()
                    st.rerun()

    with st.expander("ジョブを削除"):
        if st.button("🗑 このジョブを削除する", disabled=busy):
            job.delete()
            st.session_state["selected"] = "＋ 新規作成"
            st.rerun()


if selected == "＋ 新規作成":
    page_new()
else:
    page_job(selected)
