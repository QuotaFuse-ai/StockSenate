"""Webhook channel: HMAC-signed, idempotent POST with bounded retries.

The receiver can prove the body came from us (``X-StockSenate-Signature``) and
deduplicate re-deliveries (``X-StockSenate-Idempotency-Key``). The URL and
secret are read from environment variables named in the config — neither is
ever written to logs, the delivery log, or the event body.

Uses ``urllib`` on purpose: DEC-007 forbids a direct ``httpx`` dependency.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_DEFAULT_BACKOFF = (2, 10, 30)
_TIMEOUT = 10.0


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def idempotency_key(event: Mapping[str, Any]) -> str:
    seed = (
        f"{event.get('id') or ''}|{event.get('rule', {}).get('id') or ''}|{event.get('code') or ''}"
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _post(url: str, body: bytes, headers: Mapping[str, str]) -> int:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
        return int(getattr(response, "status", 200))


def send(
    event: Mapping[str, Any], config: Mapping[str, Any], *, home: Path | str | None = None
) -> dict[str, Any]:
    import os

    url_env = str(config.get("url_env") or "")
    secret_env = str(config.get("secret_env") or "")
    url = os.environ.get(url_env, "") if url_env else ""
    secret = os.environ.get(secret_env, "") if secret_env else ""
    if not url or not secret:
        return {
            "channel": "webhook",
            "ok": False,
            "reason": "missing_env",
            "detail": f"{url_env or 'url_env'}/{secret_env or 'secret_env'} 未设置",
        }
    if not url.lower().startswith(("http://", "https://")):
        return {"channel": "webhook", "ok": False, "reason": "invalid_url"}

    body = json.dumps(dict(event), ensure_ascii=False, sort_keys=True).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        str(config.get("signature_header") or "X-StockSenate-Signature"): sign(secret, body),
        str(config.get("idempotency_header") or "X-StockSenate-Idempotency-Key"): idempotency_key(
            event
        ),
    }
    backoff = list(config.get("backoff_seconds") or _DEFAULT_BACKOFF)
    attempts = 1 + max(0, int(config.get("max_retries", len(backoff))))
    last = "not_sent"
    for index in range(attempts):
        try:
            status = _post(url, body, headers)
        except urllib.error.HTTPError as exc:
            last = f"http_{exc.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = type(exc).__name__
        else:
            if 200 <= status < 300:
                return {"channel": "webhook", "ok": True, "attempts": index + 1, "status": status}
            last = f"http_{status}"
        if index + 1 < attempts:
            delay = backoff[min(index, len(backoff) - 1)] if backoff else 0
            time.sleep(max(0.0, float(delay)))
    return {"channel": "webhook", "ok": False, "attempts": attempts, "reason": last}


__all__ = ["idempotency_key", "send", "sign"]
