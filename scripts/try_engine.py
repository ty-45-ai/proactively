"""Check that a speech engine is installed and works, without starting the web app.

    python scripts/try_engine.py my_voice.wav "こんにちは。きょうは いい てんき です。"
    python scripts/try_engine.py my_voice.wav "Hello, my name is Ken." --lang en
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings  # noqa: E402
from app.engines import ENGINE_NAMES, load_engine  # noqa: E402
from app.store import guess_lang  # noqa: E402


def main() -> None:
    settings = load_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("reference", type=Path, help="声のサンプル (WAV, 6秒以上推奨)")
    ap.add_argument("text", help="読み上げる文章")
    ap.add_argument("--lang", choices=["ja", "en"], help="省略時は文章から自動判定")
    ap.add_argument("--engine", choices=ENGINE_NAMES, default=settings.engine)
    ap.add_argument("--out", type=Path, default=Path("try_engine_out.wav"))
    args = ap.parse_args()

    t0 = time.time()
    engine = load_engine(args.engine, settings.device)
    print(f"engine '{engine.name}' loaded in {time.time() - t0:.1f}s")
    t1 = time.time()
    engine.synthesize(args.text, args.lang or guess_lang(args.text), args.reference, args.out)
    print(f"wrote {args.out} in {time.time() - t1:.1f}s")


if __name__ == "__main__":
    main()
