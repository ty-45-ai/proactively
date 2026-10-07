"""SQLite storage and the data rules of the app.

Files live under the data directory:
  references/<participant>.wav   the participant's own voice sample
  clips/<participant>/<file>.wav generated speech, one per sentence
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS participants (
    id TEXT PRIMARY KEY,
    passcode_hash TEXT NOT NULL,
    group_name TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    ref_path TEXT,
    ref_duration REAL,
    ref_uploaded_at REAL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sentences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    lang TEXT NOT NULL,
    group_name TEXT NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    participant_id TEXT NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
    sentence_id INTEGER NOT NULL REFERENCES sentences(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'queued',
    approved INTEGER NOT NULL DEFAULT 0,
    path TEXT,
    error TEXT,
    seconds REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (participant_id, sentence_id)
);
CREATE INDEX IF NOT EXISTS clips_status ON clips(status, id);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    subject TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    participant_id TEXT NOT NULL,
    sentence_id INTEGER,
    type TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULT_REFERENCE_SCRIPT = (
    "きょうは いい てんき です。\n"
    "わたしは まいにち がっこうで べんきょうを しています。\n"
    "すきな たべものは カレーライスと りんごです。\n"
    "やすみの ひには ともだちと こうえんで あそびます。"
)

PARTICIPANT_ID_RE = re.compile(r"^[A-Z0-9_-]{1,32}$")
JAPANESE_RE = re.compile(r"[぀-ヿ㐀-鿿]")


def guess_lang(text: str) -> str:
    return "ja" if JAPANESE_RE.search(text) else "en"


class Store:
    def __init__(self, data_dir: Path, require_approval_default: bool = False):
        self.data_dir = data_dir
        self.refs_dir = data_dir / "references"
        self.clips_dir = data_dir / "clips"
        for d in (self.data_dir, self.refs_dir, self.clips_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.db_path = data_dir / "ondoku.sqlite3"
        self._write_lock = threading.Lock()
        with self.tx() as conn:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES ('reference_script', ?)",
                (DEFAULT_REFERENCE_SCRIPT,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES ('require_approval', ?)",
                ("1" if require_approval_default else "0",),
            )
            # A crash mid-generation leaves rows in 'processing'; run them again.
            conn.execute("UPDATE clips SET status='queued' WHERE status='processing'")

    # ---- connection helpers -------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            conn = self._connect()
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    # ---- settings -----------------------------------------------------------

    def get_setting(self, key: str) -> str | None:
        with self.read() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def require_approval(self) -> bool:
        return self.get_setting("require_approval") == "1"

    # ---- sessions -----------------------------------------------------------

    def create_session(self, token: str, role: str, subject: str, ttl_seconds: float) -> None:
        now = time.time()
        with self.tx() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
            conn.execute(
                "INSERT INTO sessions(token, role, subject, expires_at) VALUES (?, ?, ?, ?)",
                (token, role, subject, now + ttl_seconds),
            )

    def get_session(self, token: str) -> sqlite3.Row | None:
        with self.read() as conn:
            return conn.execute(
                "SELECT * FROM sessions WHERE token=? AND expires_at>=?", (token, time.time())
            ).fetchone()

    def delete_session(self, token: str) -> None:
        with self.tx() as conn:
            conn.execute("DELETE FROM sessions WHERE token=?", (token,))

    # ---- participants -------------------------------------------------------

    def get_participant(self, pid: str) -> sqlite3.Row | None:
        with self.read() as conn:
            return conn.execute("SELECT * FROM participants WHERE id=?", (pid,)).fetchone()

    def next_participant_ids(self, prefix: str, count: int) -> list[str]:
        with self.read() as conn:
            rows = conn.execute("SELECT id FROM participants WHERE id LIKE ?", (prefix + "%",)).fetchall()
        used = set()
        for r in rows:
            tail = r["id"][len(prefix):]
            if tail.isdigit():
                used.add(int(tail))
        ids, n = [], 1
        while len(ids) < count:
            if n not in used:
                ids.append(f"{prefix}{n:03d}")
            n += 1
        return ids

    def add_participant(self, pid: str, passcode_hash: str, group_name: str) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO participants(id, passcode_hash, group_name, created_at) VALUES (?, ?, ?, ?)",
                (pid, passcode_hash, group_name, time.time()),
            )

    def update_participant(self, pid: str, *, active: bool | None = None, group_name: str | None = None,
                           passcode_hash: str | None = None) -> None:
        with self.tx() as conn:
            if active is not None:
                conn.execute("UPDATE participants SET active=? WHERE id=?", (int(active), pid))
                if not active:
                    conn.execute("DELETE FROM sessions WHERE role='participant' AND subject=?", (pid,))
            if passcode_hash is not None:
                conn.execute("UPDATE participants SET passcode_hash=? WHERE id=?", (passcode_hash, pid))
                conn.execute("DELETE FROM sessions WHERE role='participant' AND subject=?", (pid,))
        if group_name is not None:
            with self.tx() as conn:
                conn.execute("UPDATE participants SET group_name=? WHERE id=?", (group_name, pid))
            self.sync_clips(participant_id=pid)

    def list_participants(self) -> list[dict]:
        with self.read() as conn:
            rows = conn.execute(
                """
                SELECT p.*,
                  (SELECT COUNT(*) FROM clips c WHERE c.participant_id=p.id) AS clip_total,
                  (SELECT COUNT(*) FROM clips c WHERE c.participant_id=p.id AND c.status='done') AS clip_done,
                  (SELECT COUNT(*) FROM clips c WHERE c.participant_id=p.id AND c.status='failed') AS clip_failed,
                  (SELECT MAX(created_at) FROM events e WHERE e.participant_id=p.id) AS last_activity
                FROM participants p ORDER BY p.id
                """
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d.pop("passcode_hash", None)
            d["has_reference"] = bool(d.pop("ref_path"))
            out.append(d)
        return out

    def delete_participant(self, pid: str) -> None:
        """Remove every trace of a participant: rows, voice sample, generated audio."""
        with self.tx() as conn:
            conn.execute("DELETE FROM sessions WHERE role='participant' AND subject=?", (pid,))
            conn.execute("DELETE FROM events WHERE participant_id=?", (pid,))
            conn.execute("DELETE FROM clips WHERE participant_id=?", (pid,))
            conn.execute("DELETE FROM participants WHERE id=?", (pid,))
        (self.refs_dir / f"{pid}.wav").unlink(missing_ok=True)
        shutil.rmtree(self.clips_dir / pid, ignore_errors=True)

    def save_reference(self, pid: str, wav_bytes: bytes, duration: float) -> None:
        """Store a new voice sample and regenerate every clip from it."""
        path = self.refs_dir / f"{pid}.wav"
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(wav_bytes)
        tmp.replace(path)
        with self.tx() as conn:
            old = conn.execute("SELECT path FROM clips WHERE participant_id=?", (pid,)).fetchall()
            conn.execute("DELETE FROM clips WHERE participant_id=?", (pid,))
            conn.execute(
                "UPDATE participants SET ref_path=?, ref_duration=?, ref_uploaded_at=? WHERE id=?",
                (str(path), duration, time.time(), pid),
            )
        for r in old:
            if r["path"]:
                Path(r["path"]).unlink(missing_ok=True)
        self.sync_clips(participant_id=pid)

    # ---- sentences ----------------------------------------------------------

    def list_sentences(self) -> list[dict]:
        with self.read() as conn:
            rows = conn.execute("SELECT * FROM sentences ORDER BY sort_order, id").fetchall()
        return [dict(r) for r in rows]

    def add_sentence(self, text: str, lang: str, group_name: str, sort_order: int | None) -> int:
        with self.tx() as conn:
            if sort_order is None:
                row = conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 10 AS n FROM sentences").fetchone()
                sort_order = row["n"]
            cur = conn.execute(
                "INSERT INTO sentences(text, lang, group_name, sort_order, created_at) VALUES (?, ?, ?, ?, ?)",
                (text, lang, group_name, sort_order, time.time()),
            )
            sid = cur.lastrowid
        self.sync_clips()
        return sid

    def update_sentence(self, sid: int, *, text: str | None, lang: str | None, group_name: str | None,
                        sort_order: int | None, active: bool | None) -> None:
        with self.tx() as conn:
            cur = conn.execute("SELECT * FROM sentences WHERE id=?", (sid,)).fetchone()
            if cur is None:
                raise KeyError(sid)
            text_changed = (text is not None and text != cur["text"]) or (lang is not None and lang != cur["lang"])
            conn.execute(
                "UPDATE sentences SET text=?, lang=?, group_name=?, sort_order=?, active=? WHERE id=?",
                (
                    text if text is not None else cur["text"],
                    lang if lang is not None else cur["lang"],
                    group_name if group_name is not None else cur["group_name"],
                    sort_order if sort_order is not None else cur["sort_order"],
                    int(active) if active is not None else cur["active"],
                    sid,
                ),
            )
        if text_changed:
            with self.read() as conn:
                ids = [r["id"] for r in conn.execute("SELECT id FROM clips WHERE sentence_id=?", (sid,))]
            for cid in ids:
                self.requeue_clip(cid)
        self.sync_clips()

    def delete_sentence(self, sid: int) -> None:
        with self.tx() as conn:
            old = conn.execute("SELECT path FROM clips WHERE sentence_id=?", (sid,)).fetchall()
            conn.execute("DELETE FROM clips WHERE sentence_id=?", (sid,))
            conn.execute("DELETE FROM sentences WHERE id=?", (sid,))
        for r in old:
            if r["path"]:
                Path(r["path"]).unlink(missing_ok=True)

    # ---- clips --------------------------------------------------------------

    def sync_clips(self, participant_id: str | None = None) -> int:
        """Queue a clip for every (participant with a voice sample, sentence assigned to them) pair
        that has none yet. Group '' on a sentence means 'everyone'."""
        now = time.time()
        sql = """
            INSERT OR IGNORE INTO clips(participant_id, sentence_id, status, created_at, updated_at)
            SELECT p.id, s.id, 'queued', ?, ?
            FROM participants p JOIN sentences s
              ON s.group_name = '' OR s.group_name = p.group_name
            WHERE p.ref_path IS NOT NULL AND p.active = 1 AND s.active = 1
        """
        params: list = [now, now]
        if participant_id is not None:
            sql += " AND p.id = ?"
            params.append(participant_id)
        with self.tx() as conn:
            return conn.execute(sql, params).rowcount

    def clips_for_participant(self, pid: str) -> list[dict]:
        with self.read() as conn:
            rows = conn.execute(
                """
                SELECT s.id AS sentence_id, s.text, s.lang, c.id AS clip_id, c.status, c.approved
                FROM participants p
                JOIN sentences s ON s.active = 1 AND (s.group_name = '' OR s.group_name = p.group_name)
                LEFT JOIN clips c ON c.sentence_id = s.id AND c.participant_id = p.id
                WHERE p.id = ?
                ORDER BY s.sort_order, s.id
                """,
                (pid,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_clip(self, cid: int) -> sqlite3.Row | None:
        with self.read() as conn:
            return conn.execute(
                """SELECT c.*, s.text, s.lang, p.ref_path FROM clips c
                   JOIN sentences s ON s.id=c.sentence_id
                   JOIN participants p ON p.id=c.participant_id WHERE c.id=?""",
                (cid,),
            ).fetchone()

    def list_clips(self, participant_id: str | None = None, status: str | None = None) -> list[dict]:
        sql = """SELECT c.id, c.participant_id, c.sentence_id, c.status, c.approved, c.error, c.seconds,
                        c.updated_at, s.text, s.lang
                 FROM clips c JOIN sentences s ON s.id=c.sentence_id WHERE 1=1"""
        params: list = []
        if participant_id:
            sql += " AND c.participant_id=?"
            params.append(participant_id)
        if status:
            sql += " AND c.status=?"
            params.append(status)
        sql += " ORDER BY c.participant_id, s.sort_order, s.id"
        with self.read() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def queue_stats(self) -> dict:
        with self.read() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM clips GROUP BY status").fetchall()
        stats = {"queued": 0, "processing": 0, "done": 0, "failed": 0}
        stats.update({r["status"]: r["n"] for r in rows})
        return stats

    def claim_next_clip(self) -> sqlite3.Row | None:
        with self.tx() as conn:
            row = conn.execute(
                """SELECT c.id FROM clips c JOIN participants p ON p.id=c.participant_id
                   WHERE c.status='queued' AND p.active=1 ORDER BY c.id LIMIT 1"""
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE clips SET status='processing', error=NULL, updated_at=? WHERE id=?",
                (time.time(), row["id"]),
            )
        return self.get_clip(row["id"])

    def new_clip_path(self, pid: str) -> Path:
        d = self.clips_dir / pid
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{uuid.uuid4().hex}.wav"

    def finish_clip(self, cid: int, path: Path, seconds: float) -> bool:
        """Record a finished clip. Returns False if the clip was deleted or re-queued meanwhile."""
        with self.tx() as conn:
            row = conn.execute("SELECT path, status FROM clips WHERE id=?", (cid,)).fetchone()
            if row is None or row["status"] != "processing":
                return False
            conn.execute(
                "UPDATE clips SET status='done', path=?, seconds=?, approved=0, error=NULL, updated_at=? WHERE id=?",
                (str(path), seconds, time.time(), cid),
            )
        if row["path"] and row["path"] != str(path):
            Path(row["path"]).unlink(missing_ok=True)
        return True

    def fail_clip(self, cid: int, error: str) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE clips SET status='failed', error=?, updated_at=? WHERE id=? AND status='processing'",
                (error[:1000], time.time(), cid),
            )

    def requeue_clip(self, cid: int) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE clips SET status='queued', approved=0, error=NULL, updated_at=? WHERE id=?",
                (time.time(), cid),
            )

    def set_clip_approved(self, cid: int, approved: bool) -> None:
        with self.tx() as conn:
            conn.execute("UPDATE clips SET approved=? WHERE id=?", (int(approved), cid))

    # ---- events -------------------------------------------------------------

    def log_event(self, pid: str, type_: str, sentence_id: int | None = None, detail: str = "") -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO events(participant_id, sentence_id, type, detail, created_at) VALUES (?, ?, ?, ?, ?)",
                (pid, sentence_id, type_, detail[:200], time.time()),
            )

    def list_events(self) -> list[sqlite3.Row]:
        with self.read() as conn:
            return conn.execute(
                """SELECT e.*, s.text FROM events e LEFT JOIN sentences s ON s.id=e.sentence_id
                   ORDER BY e.id"""
            ).fetchall()
