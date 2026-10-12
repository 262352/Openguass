#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


pid = int(sys.argv[1])
directory = Path(sys.argv[2])
status_path = directory / "twitter" / "status.json"
output = directory / "twitter" / "hourly_progress.jsonl"


def snapshot() -> None:
    value = {"checked_at": datetime.now(timezone.utc).isoformat()}
    try:
        value["optimizer"] = json.loads(status_path.read_text(encoding="utf-8"))
    except Exception as error:
        value["status_error"] = f"{type(error).__name__}: {error}"
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")


snapshot()
while True:
    for _ in range(60):
        time.sleep(60)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            snapshot()
            raise SystemExit(0)
    snapshot()
