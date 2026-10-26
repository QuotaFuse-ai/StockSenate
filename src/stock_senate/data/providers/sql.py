"""Read-only SQL provider.

The connection string is read from an environment variable named by
``data_source.toml`` (``STOCKSENATE_SQL_DSN`` by default); it is never stored in
the repository or echoed in logs. Only SQLite is implemented with the standard
library. MySQL/PostgreSQL drivers are optional and, when absent, the provider
reports itself unavailable instead of failing the run.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from stock_senate.data.envelope import date_only
from stock_senate.data.providers.base import (
    Provider,
    ProviderError,
    ProviderNotAvailable,
    envelope_from_payload,
)

_QUERY = (
    "SELECT data_as_of, payload, source_detail FROM data_points "
    "WHERE domain = ? AND code = ? AND data_as_of <= ? "
    "ORDER BY data_as_of DESC LIMIT 1"
)


class SqlProvider(Provider):
    kind = "sql"

    def fetch(self, domain: str, code: str, as_of: str,
              params: dict[str, Any] | None = None) -> Any:
        driver = str(self.config.get("driver") or "sqlite").lower()
        dsn = self._dsn()
        if not dsn:
            raise ProviderNotAvailable(f"sqlite: {self._dsn_env()} is not set")
        if driver != "sqlite":
            raise ProviderNotAvailable(f"{driver}: driver is not installed")
        if self.config.get("read_only") is not True:
            raise ProviderError("sql provider must declare read_only=true")

        path = _sqlite_path(dsn)
        if not path.is_file():
            raise ProviderNotAvailable("sqlite: database file is absent")
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            raise ProviderNotAvailable(f"sqlite: {exc}") from exc
        try:
            row = connection.execute(_QUERY, (domain, code, as_of)).fetchone()
        except sqlite3.Error as exc:
            raise ProviderNotAvailable(f"sqlite: {exc}") from exc
        finally:
            connection.close()

        if row is None:
            raise ProviderNotAvailable(f"sqlite: no {domain} row for {code} on or before {as_of}")
        data_as_of, payload_raw, detail = row
        try:
            payload = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
        except json.JSONDecodeError as exc:
            raise ProviderError(f"sqlite: malformed payload: {exc}") from exc
        return envelope_from_payload(
            domain=domain,
            code=code,
            payload=payload,
            source=self.name,
            source_detail=str(detail or f"sqlite:{domain}"),
            data_as_of=date_only(data_as_of),
            default_confidence="high",
        )

    def _dsn_env(self) -> str:
        return str(self.config.get("dsn_env") or "STOCKSENATE_SQL_DSN")

    def _dsn(self) -> str:
        return os.environ.get(self._dsn_env(), "").strip()


def _sqlite_path(dsn: str) -> Path:
    if dsn.startswith("sqlite:///"):
        dsn = dsn[len("sqlite:///"):]
    elif dsn.startswith("sqlite://"):
        dsn = dsn[len("sqlite://"):]
    return Path(dsn).expanduser()


__all__ = ["SqlProvider"]
