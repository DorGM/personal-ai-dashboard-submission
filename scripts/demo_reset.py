"""Rebuild the Demo Mode working copy from the fixture: python scripts/demo_reset.py

Runs the same build as the app's start and its Reset button (app.demo): verifies the fixture
checksum, removes old demo working copies under data/demo/ (only files carrying the demo
marker) and builds a fresh data/demo/working-<generation>.db with dates shifted to today
(Asia/Jerusalem). Run it while the server is stopped. The app builds a fresh copy on every
start anyway, so this is mainly a quick offline check that the fixture imports cleanly.
Never touches DATABASE_PATH or any personal database.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.demo import DEMO_DIR, DemoError, DemoRuntime  # noqa: E402
from app.records.dates import today_local  # noqa: E402


def main(demo_dir: Path = DEMO_DIR) -> int:
    try:
        copy = DemoRuntime(demo_dir, today_local).start()
    except DemoError as exc:
        print(f"Demo reset failed: {exc}", file=sys.stderr)
        return 1
    print(f"Demo working copy ready: {copy.path} (anchor {copy.anchor.isoformat()})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
