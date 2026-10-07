"""Start the server: `python -m app`."""

import logging

import uvicorn

from .config import load_settings
from .main import create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    app = create_app(settings)
    print(f"\n  参加者用:  http{'s' if settings.ssl_certfile else ''}://{settings.host}:{settings.port}/")
    print(f"  管理者用:  http{'s' if settings.ssl_certfile else ''}://{settings.host}:{settings.port}/admin")
    print(f"  音声エンジン: {settings.engine}\n")
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        ssl_certfile=settings.ssl_certfile,
        ssl_keyfile=settings.ssl_keyfile,
        proxy_headers=True,
        log_level="info",
    )


if __name__ == "__main__":
    main()
