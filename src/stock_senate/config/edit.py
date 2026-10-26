"""Audited, atomic edits to ``agent_roles.toml``.

``ssenate role enable/disable`` is the only command that mutates a role file.
Three rules keep that safe:

* the file is rewritten atomically, so a crash never leaves a half-written TOML;
* the resulting role table is re-validated before it replaces the old file, so
  the seat cap (DEC-005) and the write-tool ban cannot be broken by an edit;
* every successful write is audited with the old and new state. The audit line
  never contains an API key or a model credential.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from stock_senate.config.loader import config_dir as resolve_config_dir
from stock_senate.config.schema import ROLE_ALIASES
from stock_senate.observability.logging import audit

ROLE_FILE = "agent_roles.toml"


class ConfigEditError(ValueError):
    """The requested edit is invalid or would break a configuration invariant."""


def canonical_name(name: str) -> str:
    """Normalise a built-in role alias to its canonical key.

    Shared entry point for every role command, so ``role show technical`` and
    ``role enable technical`` resolve the same table.
    """
    return ROLE_ALIASES.get(str(name).strip(), str(name).strip())


def _parse(text: str) -> Any:
    try:
        import tomlkit

        return tomlkit.parse(text)
    except ImportError:  # pragma: no cover - tomlkit is the declared writer
        import toml

        return toml.loads(text)


def _dump(document: Any) -> str:
    try:
        import tomlkit

        return tomlkit.dumps(document)
    except ImportError:  # pragma: no cover
        import toml

        return toml.dumps(document)


def _atomic_write(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _validate_edit(document: Any) -> None:
    from stock_senate.roles.registry import RoleError, RoleRegistry

    data = document if isinstance(document, dict) else document.unwrap()
    try:
        RoleRegistry.from_config({"agent_roles": data})
    except RoleError as exc:
        raise ConfigEditError(f"编辑后角色配置不合法：{exc}") from exc


def set_enabled(
    name: str,
    enabled: bool,
    *,
    config_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Enable or disable one role and persist the change.

    Returns ``{"role", "enabled", "previous", "file", "changed"}`` so the CLI can
    print a precise result. Repeating the same state is a no-op.
    """
    directory = resolve_config_dir(Path(config_dir) if config_dir else None)
    path = directory / ROLE_FILE
    if not path.is_file():
        raise ConfigEditError(f"找不到 {path}；先执行 `ssenate config init`")

    key = canonical_name(name)
    text = path.read_text(encoding="utf-8")
    document = _parse(text)
    roles = document.get("roles") if hasattr(document, "get") else None
    if roles is None or not hasattr(roles, "get") or key not in roles:
        available = ", ".join(sorted(roles or {}))
        raise ConfigEditError(f"未知角色: {name}；可用角色 {available}")

    entry = roles.get(key)
    if entry is None or not hasattr(entry, "get"):
        raise ConfigEditError(f"roles.{key} 必须是表")
    previous = bool(entry.get("enabled", True))
    if previous is bool(enabled):
        return {
            "role": key,
            "enabled": previous,
            "previous": previous,
            "file": str(path),
            "changed": False,
        }

    entry["enabled"] = bool(enabled)
    roles[key] = entry
    _validate_edit(document)
    _atomic_write(path, _dump(document))

    payload = {
        "role": key,
        "enabled": bool(enabled),
        "previous": previous,
        "file": str(path),
        "changed": True,
    }
    audit("config.write", **payload)
    return payload


__all__ = ["ConfigEditError", "ROLE_FILE", "canonical_name", "set_enabled"]
