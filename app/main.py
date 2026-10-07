"""HTTP API and pages."""

from __future__ import annotations

import csv
import io
import logging
import os
import time
import wave
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth
from .config import Settings, load_settings
from .engines import load_engine
from .store import PARTICIPANT_ID_RE, Store, guess_lang
from .worker import Worker

log = logging.getLogger("ondoku")
STATIC_DIR = Path(__file__).parent / "static"
MAX_REFERENCE_BYTES = 30 * 1024 * 1024
MIN_REFERENCE_SECONDS = 3.0
MAX_REFERENCE_SECONDS = 180.0


# ---- request bodies ----------------------------------------------------------

class LoginBody(BaseModel):
    id: str = Field(max_length=64)
    passcode: str = Field(max_length=64)


class AdminLoginBody(BaseModel):
    password: str = Field(max_length=256)


class EventBody(BaseModel):
    type: Literal["play", "practice_record", "practice_play"]
    sentence_id: int | None = None
    detail: str = Field(default="", max_length=200)


class CreateParticipantsBody(BaseModel):
    count: int = Field(ge=1, le=200)
    prefix: str = Field(default="P", pattern=r"^[A-Za-z]{0,8}$")
    group_name: str = Field(default="", max_length=64)


class UpdateParticipantBody(BaseModel):
    active: bool | None = None
    group_name: str | None = Field(default=None, max_length=64)


class SentenceBody(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    lang: Literal["auto", "ja", "en"] = "auto"
    group_name: str = Field(default="", max_length=64)
    sort_order: int | None = None


class SentenceUpdateBody(BaseModel):
    text: str | None = Field(default=None, min_length=1, max_length=500)
    lang: Literal["auto", "ja", "en"] | None = None
    group_name: str | None = Field(default=None, max_length=64)
    sort_order: int | None = None
    active: bool | None = None


class ApproveBody(BaseModel):
    approved: bool


class SettingsBody(BaseModel):
    reference_script: str | None = Field(default=None, max_length=4000)
    require_approval: bool | None = None


# ---- helpers -----------------------------------------------------------------

def parse_reference_wav(data: bytes) -> float:
    """Validate an uploaded voice sample (the browser always sends 16-bit PCM WAV)."""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            if w.getsampwidth() != 2 or w.getnchannels() not in (1, 2):
                raise HTTPException(400, "WAV は 16bit モノラル/ステレオのみ対応しています")
            if not 8000 <= w.getframerate() <= 96000:
                raise HTTPException(400, "サンプリングレートが不正です")
            seconds = w.getnframes() / float(w.getframerate())
    except (wave.Error, EOFError) as exc:
        raise HTTPException(400, "音声ファイルを読み取れませんでした") from exc
    if seconds < MIN_REFERENCE_SECONDS:
        raise HTTPException(400, f"録音が短すぎます（{MIN_REFERENCE_SECONDS:.0f}秒以上）")
    if seconds > MAX_REFERENCE_SECONDS:
        raise HTTPException(400, f"録音が長すぎます（{MAX_REFERENCE_SECONDS:.0f}秒まで）")
    return seconds


def audio_response(path: str | None) -> FileResponse:
    if not path or not Path(path).is_file():
        raise HTTPException(404, "音声がありません")
    # Played in the page only: no download name, no caching on shared computers.
    return FileResponse(
        path,
        media_type="audio/wav",
        headers={"Cache-Control": "no-store", "Content-Disposition": "inline"},
    )


def fmt_time(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else ""


def ensure_admin_password(store: Store, settings: Settings) -> None:
    if settings.admin_password:
        return
    if store.get_setting("admin_password_hash"):
        return
    password = auth.new_passcode(12)
    store.set_setting("admin_password_hash", auth.hash_secret(password))
    note = store.data_dir / "admin-password.txt"
    note.write_text(password + "\n", encoding="utf-8")
    os.chmod(note, 0o600)
    log.warning("管理者パスワードを生成しました: %s （%s にも保存。ログイン後に削除してください）", password, note)


# ---- app ---------------------------------------------------------------------

def create_app(settings: Settings | None = None, start_worker: bool = True, engine_factory=None) -> FastAPI:
    settings = settings or load_settings()
    store = Store(settings.data_dir, require_approval_default=settings.require_approval)
    ensure_admin_password(store, settings)
    admin_hash = auth.hash_secret(settings.admin_password) if settings.admin_password else None
    worker = Worker(store, engine_factory or (lambda: load_engine(settings.engine, settings.device)))
    throttle = auth.LoginThrottle()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if start_worker:
            worker.start()
        yield
        worker.stop()

    app = FastAPI(title="Ondoku Voice", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.worker = worker
    app.state.settings = settings

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Permissions-Policy", "microphone=(self)")
        return response

    def set_cookie(response: Response, token: str, ttl: int) -> None:
        response.set_cookie(
            auth.COOKIE_NAME, token, max_age=ttl, httponly=True, samesite="strict",
            secure=settings.secure_cookie, path="/",
        )

    def session(request: Request, role: str):
        token = request.cookies.get(auth.COOKIE_NAME)
        row = store.get_session(token) if token else None
        if row is None or row["role"] != role:
            raise HTTPException(401, "ログインしてください")
        return row

    def participant(request: Request):
        row = session(request, "participant")
        p = store.get_participant(row["subject"])
        if p is None or not p["active"]:
            raise HTTPException(401, "ログインしてください")
        return p

    def admin(request: Request):
        return session(request, "admin")

    def client_key(request: Request, name: str) -> str:
        host = request.client.host if request.client else "?"
        return f"{host}|{name}"

    # ---- pages ----

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
    def admin_page():
        return FileResponse(STATIC_DIR / "admin.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # ---- participant API ----

    @app.post("/api/login")
    def login(body: LoginBody, request: Request, response: Response):
        pid = body.id.strip().upper()
        key = client_key(request, pid)
        if throttle.blocked(key):
            raise HTTPException(429, "しばらく待ってから、もう一度ためしてください")
        p = store.get_participant(pid) if PARTICIPANT_ID_RE.match(pid) else None
        if p is None or not p["active"] or not auth.verify_secret(body.passcode.strip().lower(), p["passcode_hash"]):
            throttle.fail(key)
            raise HTTPException(401, "ID か あいことば が ちがいます")
        throttle.reset(key)
        token = auth.new_token()
        store.create_session(token, "participant", pid, auth.PARTICIPANT_TTL)
        store.log_event(pid, "login")
        set_cookie(response, token, auth.PARTICIPANT_TTL)
        return {"ok": True}

    @app.post("/api/logout")
    def logout(request: Request, response: Response):
        token = request.cookies.get(auth.COOKIE_NAME)
        if token:
            store.delete_session(token)
        response.delete_cookie(auth.COOKIE_NAME, path="/")
        return {"ok": True}

    @app.get("/api/me")
    def me(p=Depends(participant)):
        need_approval = store.require_approval()
        items = []
        for c in store.clips_for_participant(p["id"]):
            status = c["status"] or ("waiting" if p["ref_path"] else "no_reference")
            if status == "done" and need_approval and not c["approved"]:
                status = "reviewing"
            items.append({
                "sentence_id": c["sentence_id"],
                "text": c["text"],
                "lang": c["lang"],
                "status": status,
                "audio_url": f"/api/clips/{c['clip_id']}/audio" if status == "done" else None,
            })
        return {
            "id": p["id"],
            "has_reference": bool(p["ref_path"]),
            "reference_script": store.get_setting("reference_script") or "",
            "items": items,
        }

    @app.post("/api/reference")
    async def upload_reference(file: UploadFile = File(...), p=Depends(participant)):
        data = await file.read(MAX_REFERENCE_BYTES + 1)
        if len(data) > MAX_REFERENCE_BYTES:
            raise HTTPException(413, "ファイルが大きすぎます")
        seconds = parse_reference_wav(data)
        store.save_reference(p["id"], data, seconds)
        store.log_event(p["id"], "reference", detail=f"{seconds:.1f}s")
        worker.wake()
        return {"ok": True, "seconds": round(seconds, 1)}

    @app.get("/api/clips/{clip_id}/audio")
    def clip_audio(clip_id: int, p=Depends(participant)):
        clip = store.get_clip(clip_id)
        if clip is None or clip["participant_id"] != p["id"] or clip["status"] != "done":
            raise HTTPException(404, "音声がありません")
        if store.require_approval() and not clip["approved"]:
            raise HTTPException(404, "音声がありません")
        return audio_response(clip["path"])

    @app.post("/api/events")
    def event(body: EventBody, p=Depends(participant)):
        store.log_event(p["id"], body.type, body.sentence_id, body.detail)
        return {"ok": True}

    # ---- admin API ----

    @app.post("/api/admin/login")
    def admin_login(body: AdminLoginBody, request: Request, response: Response):
        key = client_key(request, "admin")
        if throttle.blocked(key):
            raise HTTPException(429, "ログイン失敗が続いたため、5分ほど待ってください")
        stored = admin_hash or store.get_setting("admin_password_hash") or ""
        if not auth.verify_secret(body.password, stored):
            throttle.fail(key)
            raise HTTPException(401, "パスワードが違います")
        throttle.reset(key)
        token = auth.new_token()
        store.create_session(token, "admin", "admin", auth.ADMIN_TTL)
        set_cookie(response, token, auth.ADMIN_TTL)
        return {"ok": True}

    @app.get("/api/admin/status")
    def admin_status(_=Depends(admin)):
        return {
            "engine": settings.engine,
            "engine_loading": worker.engine_loading,
            "engine_error": worker.engine_error,
            "current_clip": worker.current_clip,
            "queue": store.queue_stats(),
            "require_approval": store.require_approval(),
            "reference_script": store.get_setting("reference_script") or "",
        }

    @app.get("/api/admin/participants")
    def list_participants(_=Depends(admin)):
        rows = store.list_participants()
        for r in rows:
            r["ref_uploaded_at"] = fmt_time(r["ref_uploaded_at"])
            r["last_activity"] = fmt_time(r["last_activity"])
        return rows

    @app.post("/api/admin/participants")
    def create_participants(body: CreateParticipantsBody, _=Depends(admin)):
        created = []
        for pid in store.next_participant_ids(body.prefix.upper(), body.count):
            code = auth.new_passcode()
            store.add_participant(pid, auth.hash_secret(code), body.group_name.strip())
            created.append({"id": pid, "passcode": code, "group_name": body.group_name.strip()})
        return created

    def existing_participant(pid: str):
        p = store.get_participant(pid)
        if p is None:
            raise HTTPException(404, "参加者が見つかりません")
        return p

    @app.patch("/api/admin/participants/{pid}")
    def update_participant(pid: str, body: UpdateParticipantBody, _=Depends(admin)):
        existing_participant(pid)
        group = body.group_name.strip() if body.group_name is not None else None
        store.update_participant(pid, active=body.active, group_name=group)
        if body.active:
            store.sync_clips(participant_id=pid)
            worker.wake()
        return {"ok": True}

    @app.post("/api/admin/participants/{pid}/passcode")
    def reset_passcode(pid: str, _=Depends(admin)):
        existing_participant(pid)
        code = auth.new_passcode()
        store.update_participant(pid, passcode_hash=auth.hash_secret(code))
        return {"id": pid, "passcode": code}

    @app.delete("/api/admin/participants/{pid}")
    def delete_participant(pid: str, _=Depends(admin)):
        existing_participant(pid)
        store.delete_participant(pid)
        return {"ok": True}

    @app.get("/api/admin/participants/{pid}/reference")
    def participant_reference(pid: str, _=Depends(admin)):
        return audio_response(existing_participant(pid)["ref_path"])

    @app.get("/api/admin/sentences")
    def list_sentences(_=Depends(admin)):
        return store.list_sentences()

    @app.post("/api/admin/sentences")
    def add_sentence(body: SentenceBody, _=Depends(admin)):
        text = body.text.strip()
        lang = guess_lang(text) if body.lang == "auto" else body.lang
        sid = store.add_sentence(text, lang, body.group_name.strip(), body.sort_order)
        worker.wake()
        return {"id": sid}

    @app.patch("/api/admin/sentences/{sid}")
    def update_sentence(sid: int, body: SentenceUpdateBody, _=Depends(admin)):
        text = body.text.strip() if body.text is not None else None
        lang = body.lang
        if lang == "auto":
            if text is None:
                current = next((s for s in store.list_sentences() if s["id"] == sid), None)
                text_for_guess = current["text"] if current else ""
            else:
                text_for_guess = text
            lang = guess_lang(text_for_guess)
        try:
            store.update_sentence(
                sid, text=text, lang=lang,
                group_name=body.group_name.strip() if body.group_name is not None else None,
                sort_order=body.sort_order, active=body.active,
            )
        except KeyError as exc:
            raise HTTPException(404, "課題文が見つかりません") from exc
        worker.wake()
        return {"ok": True}

    @app.delete("/api/admin/sentences/{sid}")
    def delete_sentence(sid: int, _=Depends(admin)):
        store.delete_sentence(sid)
        return {"ok": True}

    @app.get("/api/admin/clips")
    def list_clips(participant_id: str | None = None, status: str | None = None, _=Depends(admin)):
        rows = store.list_clips(participant_id, status)
        for r in rows:
            r["updated_at"] = fmt_time(r["updated_at"])
        return rows

    @app.get("/api/admin/clips/{clip_id}/audio")
    def admin_clip_audio(clip_id: int, _=Depends(admin)):
        clip = store.get_clip(clip_id)
        if clip is None:
            raise HTTPException(404, "音声がありません")
        return audio_response(clip["path"])

    @app.post("/api/admin/clips/{clip_id}/regenerate")
    def regenerate(clip_id: int, _=Depends(admin)):
        if store.get_clip(clip_id) is None:
            raise HTTPException(404, "音声がありません")
        store.requeue_clip(clip_id)
        worker.wake()
        return {"ok": True}

    @app.post("/api/admin/clips/regenerate-failed")
    def regenerate_failed(_=Depends(admin)):
        rows = store.list_clips(status="failed")
        for r in rows:
            store.requeue_clip(r["id"])
        worker.wake()
        return {"count": len(rows)}

    @app.post("/api/admin/clips/{clip_id}/approve")
    def approve(clip_id: int, body: ApproveBody, _=Depends(admin)):
        if store.get_clip(clip_id) is None:
            raise HTTPException(404, "音声がありません")
        store.set_clip_approved(clip_id, body.approved)
        return {"ok": True}

    @app.put("/api/admin/settings")
    def update_settings(body: SettingsBody, _=Depends(admin)):
        if body.reference_script is not None:
            store.set_setting("reference_script", body.reference_script.strip())
        if body.require_approval is not None:
            store.set_setting("require_approval", "1" if body.require_approval else "0")
        return {"ok": True}

    @app.get("/api/admin/events.csv")
    def events_csv(_=Depends(admin)):
        buf = io.StringIO()
        buf.write("﻿")  # BOM so Excel opens Japanese text correctly
        w = csv.writer(buf)
        w.writerow(["event_id", "datetime", "participant_id", "type", "sentence_id", "sentence_text", "detail"])
        for e in store.list_events():
            w.writerow([e["id"], fmt_time(e["created_at"]), e["participant_id"], e["type"],
                        e["sentence_id"] or "", e["text"] or "", e["detail"]])
        name = f"ondoku-events-{time.strftime('%Y%m%d-%H%M')}.csv"
        return StreamingResponse(
            iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    return app
