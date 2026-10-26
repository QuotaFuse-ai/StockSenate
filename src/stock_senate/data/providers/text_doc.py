"""Qualitative document provider.

Text documents are read as prose only. ``numeric_extraction`` is forced off by
``config/schema.py``; this module additionally strips injected instructions
before the text can reach a prompt. PDF/DOCX files are listed but not parsed:
without an optional parser installed they are reported as degraded rather
than silently skipped.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from stock_senate.data.envelope import Confidence, Freshness, SourceStatus, now_iso
from stock_senate.data.providers.base import Provider, ProviderNotAvailable
from stock_senate.data.sanitize import sanitize_external_text

_PARSEABLE = {".txt", ".md", ".markdown", ".rst"}
_KNOWN_UNPARSED = {".pdf", ".docx", ".doc"}
_DATE_IN_NAME = re.compile(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})")


class TextDocProvider(Provider):
    kind = "text_doc"

    def __init__(self, name: str, config: dict[str, Any] | None = None, **kwargs: Any) -> None:
        super().__init__(name, config, **kwargs)
        if self.config.get("numeric_extraction") is not False:
            # Fail closed: this provider must never be trusted to yield numbers.
            raise ValueError("text_doc.numeric_extraction 必须为 false（文本资料只做定性阅读）")

    def fetch(
        self, domain: str, code: str, as_of: str, params: dict[str, Any] | None = None
    ) -> Any:
        from stock_senate.data.envelope import DataEnvelope

        root, code = self._safe_subdir(domain, code)
        if not root.is_dir():
            raise ProviderNotAvailable(f"{domain}/{code}: no document directory")

        documents: list[dict[str, Any]] = []
        skipped: list[str] = []
        latest: str = ""
        for path in sorted(root.iterdir()):
            if not path.is_file() or path.stem > as_of:
                continue
            if path.suffix.lower() in _KNOWN_UNPARSED:
                skipped.append(path.name)
                continue
            if path.suffix.lower() not in _PARSEABLE:
                continue
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                skipped.append(path.name)
                continue
            result = sanitize_external_text(raw)
            doc_date = _document_date(path, result.text)
            if doc_date > as_of:
                continue
            latest = max(latest, doc_date)
            documents.append(
                {
                    "title": path.stem,
                    "file": str(path.relative_to(self._root().resolve())),
                    "as_of": doc_date,
                    "text": result.text,
                    "injection_flags": result.removed_injections,
                }
            )

        if not documents:
            detail = "no readable document"
            if skipped:
                detail += f"; unparsed={','.join(sorted(set(skipped)))}"
            raise ProviderNotAvailable(f"{domain}/{code}: {detail}")

        detail = f"{domain}/{code}: {len(documents)} document(s)"
        if skipped:
            detail += f"; unparsed={','.join(sorted(set(skipped)))}"
        status = SourceStatus.DEGRADED if skipped else SourceStatus.OK
        return DataEnvelope(
            domain=domain,
            code=code,
            data_as_of=latest or as_of,
            payload={"documents": documents, "numeric_extraction": False},
            source=self.name,
            source_detail=detail,
            fetched_at=now_iso(),
            freshness=Freshness.ARCHIVE,
            confidence=Confidence.LOW,
            data_source_status=status,
        )


def _document_date(path: Path, text: str) -> str:
    match = _DATE_IN_NAME.search(path.stem)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    try:
        stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        return stamp.date().isoformat()
    except OSError:
        return "1970-01-01"


__all__ = ["TextDocProvider"]
