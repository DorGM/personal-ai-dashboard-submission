"""Create a small synthetic *test seed* database: python scripts/seed_synthetic.py [path]  (default data/demo.db).

This is the B-03 test seed (app/demo_seed.py: raw INSERTs, about two weeks of data), used for
schema and data-preservation tests. It is NOT Demo Mode: Demo Mode (APP_MODE=demo) builds its
own working copy from demo/synthetic_v1.json through the record repositories (app/demo.py).
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import connect, migrate  # noqa: E402
from app.demo_seed import seed  # noqa: E402


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "data/demo.db")
    if path.exists():
        sys.exit(f"{path} already exists; delete it first to reseed.")
    today = datetime.now(ZoneInfo("Asia/Jerusalem")).date()  # before touching the disk
    conn = connect(path)
    migrate(conn)
    uid = seed(conn, today)
    print(f"Seeded synthetic user {uid} into {path}")


if __name__ == "__main__":
    main()
