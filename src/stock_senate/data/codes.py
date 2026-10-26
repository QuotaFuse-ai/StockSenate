"""A-share code normalisation shared by screening, queue and data providers."""

from __future__ import annotations

import re

_CODE_RE = re.compile(r"^(?:(?:sh|sz|bj))?(\d{6})(?:\.(?:sh|sz|bj|ss))?$", re.IGNORECASE)


def normalize_code(code: str) -> str:
    """``sh600519`` / ``600519.SH`` -> ``600519``; raise on invalid input."""
    if code is None:
        raise ValueError("code is required")
    text = str(code).strip()
    match = _CODE_RE.match(text)
    if not match:
        raise ValueError(f"非法 A 股代码：{code!r}（应为 6 位数字）")
    return match.group(1)


def exchange_of(code: str) -> str:
    value = normalize_code(code)
    if value.startswith(("60", "68", "11", "51")):
        return "sh"
    if value.startswith(("00", "30", "12", "15")):
        return "sz"
    if value.startswith(("4", "8", "92")):
        return "bj"
    return "sz"


__all__ = ["exchange_of", "normalize_code"]
