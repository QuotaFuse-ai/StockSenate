"""Console channel: one JSON line per alert on stderr.

stdout stays reserved for machine-readable command output, so a serving alert
can never corrupt a piped CSV or JSON document.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def send(
    event: Mapping[str, Any], config: Mapping[str, Any], *, home: Path | str | None = None
) -> dict[str, Any]:
    line = json.dumps(dict(event), ensure_ascii=False, sort_keys=True)
    print(f"alert[{event.get('level', 'mid')}] {line}", file=sys.stderr)
    return {"channel": "console", "ok": True}


__all__ = ["send"]
