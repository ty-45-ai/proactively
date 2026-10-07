"""Background generation: one thread takes queued clips in order and runs the speech engine."""

from __future__ import annotations

import logging
import threading
import time
import traceback
import wave
from pathlib import Path
from typing import Callable

from .engines import Engine
from .store import Store

log = logging.getLogger("ondoku.worker")


def wav_seconds(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except (wave.Error, OSError, EOFError):
        return 0.0


class Worker:
    def __init__(self, store: Store, engine_factory: Callable[[], Engine]):
        self.store = store
        self._engine_factory = engine_factory
        self._engine: Engine | None = None
        self.engine_error: str | None = None
        self.engine_loading = False
        self.current_clip: int | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # The model loads lazily on the worker thread so the web server starts at once.
    def engine(self) -> Engine:
        if self._engine is None:
            self.engine_loading = True
            try:
                log.info("loading speech engine ...")
                self._engine = self._engine_factory()
                self.engine_error = None
                log.info("speech engine ready: %s", self._engine.name)
            except Exception as exc:  # noqa: BLE001 - surfaced to the admin page
                self.engine_error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                self.engine_loading = False
        return self._engine

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="ondoku-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.process_once()
            except Exception:  # noqa: BLE001 - keep the loop alive
                log.exception("worker loop error")
                worked = False
            if not worked:
                # Engine failed to load: retry every minute instead of spinning.
                self._wake.wait(60 if self.engine_error else 3)
                self._wake.clear()

    def process_once(self) -> bool:
        try:
            engine = self.engine()
        except Exception:  # noqa: BLE001
            log.error("speech engine failed to load: %s", self.engine_error)
            return False
        clip = self.store.claim_next_clip()
        if clip is None:
            return False
        self.current_clip = clip["id"]
        out = self.store.new_clip_path(clip["participant_id"])
        started = time.time()
        try:
            engine.synthesize(clip["text"], clip["lang"], Path(clip["ref_path"]), out)
            if not out.is_file() or out.stat().st_size == 0:
                raise RuntimeError("engine produced no audio")
            if not self.store.finish_clip(clip["id"], out, wav_seconds(out)):
                out.unlink(missing_ok=True)
            log.info("clip %s done in %.1fs", clip["id"], time.time() - started)
        except Exception as exc:  # noqa: BLE001
            out.unlink(missing_ok=True)
            log.warning("clip %s failed: %s", clip["id"], exc)
            self.store.fail_clip(clip["id"], f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}")
        finally:
            self.current_clip = None
        return True
