import io
import math
import struct
import wave

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.engines.dummy import DummyEngine
from app.main import create_app


def make_wav(seconds=6.0, rate=24000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(
            struct.pack("<h", int(8000 * math.sin(2 * math.pi * 220 * i / rate))) for i in range(int(seconds * rate))
        ))
    return buf.getvalue()


@pytest.fixture
def env(tmp_path):
    settings = Settings(data_dir=tmp_path, engine="dummy", admin_password="secret-admin")
    app = create_app(settings, start_worker=False, engine_factory=DummyEngine)
    with TestClient(app) as admin, TestClient(app) as kid:
        assert admin.post("/api/admin/login", json={"password": "secret-admin"}).status_code == 200
        yield app, admin, kid


def drain(app):
    while app.state.worker.process_once():
        pass


def test_full_flow(env):
    app, admin, kid = env
    created = admin.post("/api/admin/participants", json={"count": 30, "prefix": "P"}).json()
    assert len(created) == 30 and created[0]["id"] == "P001" and created[-1]["id"] == "P030"
    admin.post("/api/admin/sentences", json={"text": "きょうは いい てんき です。"})
    admin.post("/api/admin/sentences", json={"text": "I like apples."})

    assert kid.get("/api/me").status_code == 401
    assert kid.post("/api/login", json={"id": "p001", "passcode": "wrong"}).status_code == 401
    assert kid.post("/api/login", json={"id": "p001", "passcode": created[0]["passcode"]}).status_code == 200

    me = kid.get("/api/me").json()
    assert me["has_reference"] is False
    assert [i["status"] for i in me["items"]] == ["no_reference", "no_reference"]
    assert [i["lang"] for i in me["items"]] == ["ja", "en"]

    r = kid.post("/api/reference", files={"file": ("v.wav", make_wav(), "audio/wav")})
    assert r.status_code == 200, r.text
    assert [i["status"] for i in kid.get("/api/me").json()["items"]] == ["queued", "queued"]

    drain(app)
    items = kid.get("/api/me").json()["items"]
    assert [i["status"] for i in items] == ["done", "done"]
    audio = kid.get(items[0]["audio_url"])
    assert audio.status_code == 200 and audio.content[:4] == b"RIFF"
    assert audio.headers["cache-control"] == "no-store"

    # A new sentence is generated for participants who already have a voice sample.
    admin.post("/api/admin/sentences", json={"text": "Good morning.", "lang": "en"})
    drain(app)
    assert [i["status"] for i in kid.get("/api/me").json()["items"]] == ["done"] * 3

    kid.post("/api/events", json={"type": "play", "sentence_id": items[0]["sentence_id"], "detail": "rate=1"})
    csv_text = admin.get("/api/admin/events.csv").text
    assert "P001" in csv_text and "play" in csv_text


def test_other_participants_audio_is_private(env):
    app, admin, kid = env
    a, b = admin.post("/api/admin/participants", json={"count": 2}).json()
    admin.post("/api/admin/sentences", json={"text": "Hello."})
    kid.post("/api/login", json={"id": a["id"], "passcode": a["passcode"]})
    kid.post("/api/reference", files={"file": ("v.wav", make_wav(), "audio/wav")})
    drain(app)
    url = kid.get("/api/me").json()["items"][0]["audio_url"]

    kid.post("/api/logout")
    kid.post("/api/login", json={"id": b["id"], "passcode": b["passcode"]})
    assert kid.get(url).status_code == 404
    assert kid.get("/api/admin/participants").status_code == 401


def test_approval_hides_unapproved_audio(env):
    app, admin, kid = env
    (p,) = admin.post("/api/admin/participants", json={"count": 1}).json()
    admin.put("/api/admin/settings", json={"require_approval": True})
    admin.post("/api/admin/sentences", json={"text": "Hello."})
    kid.post("/api/login", json={"id": p["id"], "passcode": p["passcode"]})
    kid.post("/api/reference", files={"file": ("v.wav", make_wav(), "audio/wav")})
    drain(app)
    item = kid.get("/api/me").json()["items"][0]
    assert item["status"] == "reviewing" and item["audio_url"] is None
    clip = admin.get("/api/admin/clips").json()[0]
    admin.post(f"/api/admin/clips/{clip['id']}/approve", json={"approved": True})
    assert kid.get("/api/me").json()["items"][0]["status"] == "done"


def test_reference_validation(env):
    app, admin, kid = env
    (p,) = admin.post("/api/admin/participants", json={"count": 1}).json()
    kid.post("/api/login", json={"id": p["id"], "passcode": p["passcode"]})
    assert kid.post("/api/reference", files={"file": ("v.wav", make_wav(1.0), "audio/wav")}).status_code == 400
    assert kid.post("/api/reference", files={"file": ("v.wav", b"not audio", "audio/wav")}).status_code == 400


def test_edit_sentence_regenerates_and_delete_participant_removes_files(env):
    app, admin, kid = env
    (p,) = admin.post("/api/admin/participants", json={"count": 1}).json()
    sid = admin.post("/api/admin/sentences", json={"text": "Hello."}).json()["id"]
    kid.post("/api/login", json={"id": p["id"], "passcode": p["passcode"]})
    kid.post("/api/reference", files={"file": ("v.wav", make_wav(), "audio/wav")})
    drain(app)

    admin.patch(f"/api/admin/sentences/{sid}", json={"text": "Hello there."})
    assert kid.get("/api/me").json()["items"][0]["status"] == "queued"
    drain(app)
    assert kid.get("/api/me").json()["items"][0]["status"] == "done"

    store = app.state.store
    assert (store.refs_dir / f"{p['id']}.wav").exists()
    assert any((store.clips_dir / p["id"]).iterdir())
    assert admin.delete(f"/api/admin/participants/{p['id']}").status_code == 200
    assert not (store.refs_dir / f"{p['id']}.wav").exists()
    assert not (store.clips_dir / p["id"]).exists()
    assert kid.get("/api/me").status_code == 401


def test_failed_engine_marks_clip_failed(tmp_path):
    class Broken:
        name = "broken"

        def synthesize(self, *a):
            raise RuntimeError("boom")

    app = create_app(Settings(data_dir=tmp_path, admin_password="x"), start_worker=False, engine_factory=Broken)
    with TestClient(app) as admin, TestClient(app) as kid:
        admin.post("/api/admin/login", json={"password": "x"})
        (p,) = admin.post("/api/admin/participants", json={"count": 1}).json()
        admin.post("/api/admin/sentences", json={"text": "Hello."})
        kid.post("/api/login", json={"id": p["id"], "passcode": p["passcode"]})
        kid.post("/api/reference", files={"file": ("v.wav", make_wav(), "audio/wav")})
        drain(app)
        clips = admin.get("/api/admin/clips").json()
        assert clips[0]["status"] == "failed" and "boom" in clips[0]["error"]


def test_login_throttle(env):
    app, admin, kid = env
    admin.post("/api/admin/participants", json={"count": 1})
    for _ in range(8):
        assert kid.post("/api/login", json={"id": "P001", "passcode": "nope"}).status_code == 401
    assert kid.post("/api/login", json={"id": "P001", "passcode": "nope"}).status_code == 429
