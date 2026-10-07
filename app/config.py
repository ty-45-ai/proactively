"""Settings loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader: KEY=VALUE lines, existing env vars win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    engine: str = "xtts"
    admin_password: str | None = None
    require_approval: bool = False
    secure_cookie: bool = False
    device: str | None = None
    host: str = "127.0.0.1"
    port: int = 8000
    ssl_certfile: str | None = None
    ssl_keyfile: str | None = None


def load_settings() -> Settings:
    load_dotenv()
    return Settings(
        data_dir=Path(os.environ.get("ONDOKU_DATA_DIR", "data")).resolve(),
        engine=os.environ.get("ONDOKU_ENGINE", "xtts").strip().lower(),
        admin_password=os.environ.get("ONDOKU_ADMIN_PASSWORD") or None,
        require_approval=_flag("ONDOKU_REQUIRE_APPROVAL"),
        secure_cookie=_flag("ONDOKU_SECURE_COOKIE"),
        device=os.environ.get("ONDOKU_DEVICE") or None,
        host=os.environ.get("ONDOKU_HOST", "127.0.0.1"),
        port=int(os.environ.get("ONDOKU_PORT", "8000")),
        ssl_certfile=os.environ.get("ONDOKU_SSL_CERTFILE") or None,
        ssl_keyfile=os.environ.get("ONDOKU_SSL_KEYFILE") or None,
    )
