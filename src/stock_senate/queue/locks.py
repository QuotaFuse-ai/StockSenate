"""File locks that work the same on Windows and POSIX.

Two kinds are used by the queue:

* a per-code lock under ``<home>/locks`` so the same instrument is never
  analysed twice at once, even across processes;
* a state lock under ``<home>/queue`` so read-modify-write cycles on the job
  file cannot lose an update made by another CLI invocation.

``O_CREAT|O_EXCL`` is atomic on both platforms. A lock older than the caller's
stale threshold is presumed abandoned and reclaimed by renaming it aside, which
only one contender can win.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

from stock_senate.config.loader import ensure_home

_DEFAULT_STALE_SECONDS = 6 * 60 * 60


class LockBusy(RuntimeError):
    """The lock is held by someone else."""


def lock_name(code: str) -> str:
    return f"analysis-{re.sub(r'[^A-Za-z0-9_.-]', '_', str(code)) or 'unknown'}.lock"


@contextlib.contextmanager
def code_lock(
    code: str,
    *,
    home: Path | str | None = None,
    stale_seconds: float = _DEFAULT_STALE_SECONDS,
) -> Iterator[Path]:
    """Hold the per-code analysis lock; raise :class:`LockBusy` if taken."""
    root = ensure_home(Path(home).expanduser() if home else None) / "locks"
    path = root / lock_name(code)
    token = _acquire(
        path,
        {"code": str(code), "pid": os.getpid(), "at": _stamp()},
        stale_seconds=stale_seconds,
    )
    if token is None:
        raise LockBusy(f"{code} 已有分析在运行（{path.name}）")
    try:
        yield path
    finally:
        _release(path, token)


@contextlib.contextmanager
def file_lock(
    path: Path | str,
    *,
    timeout: float = 30.0,
    stale_seconds: float = 120.0,
) -> Iterator[Path]:
    """Blocking exclusive lock on ``path`` used for state read-modify-write."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0.0, float(timeout))
    token: str | None = None
    while token is None:
        token = _acquire(
            target,
            {"pid": os.getpid(), "at": _stamp()},
            stale_seconds=stale_seconds,
        )
        if token is not None:
            break
        if time.monotonic() >= deadline:
            raise LockBusy(f"{target} 被占用超过 {timeout:g}s")
        time.sleep(0.05)
    try:
        yield target
    finally:
        _release(target, token)


def _acquire(path: Path, payload: dict, *, stale_seconds: float) -> str | None:
    token = uuid.uuid4().hex
    body = {**payload, "token": token}
    for attempt in range(2):
        if _create(path, body):
            return token
        if attempt == 0 and _reclaim(path, stale_seconds):
            continue
        return None
    return None


def _create(path: Path, body: dict) -> bool:
    try:
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(body, stream, ensure_ascii=False)
    except OSError:
        # The file exists, so the lock is held even if writing the owner failed.
        return True
    return True


def _reclaim(path: Path, stale_seconds: float) -> bool:
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return True
    if age <= max(0.0, stale_seconds):
        return False
    aside = path.with_name(f"{path.name}.stale-{uuid.uuid4().hex[:8]}")
    try:
        os.replace(path, aside)
    except OSError:
        return False
    with contextlib.suppress(OSError):
        aside.unlink()
    return True


def _release(path: Path, token: str) -> None:
    try:
        owner = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(owner, dict) and owner.get("token") == token:
        with contextlib.suppress(OSError):
            path.unlink()


def _stamp() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


__all__ = ["LockBusy", "code_lock", "file_lock", "lock_name"]
