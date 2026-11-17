"""Qdrant provider.

知识库按股票可细分：优先读 ``{collection_prefix}{domain}_{code}`` 这种单票
collection；不存在时退化到共享 collection ``{collection_prefix}{domain}`` 并按
``code`` 过滤。两种都找不到就是 ``ProviderNotAvailable``，交给路由回退到本地
文档，不报硬错误、也不编造内容。

检索走 ``scroll`` 而不是向量查询：本组件没有 embedding 模型，按代码与时间
游标取最近的片段即可，避免引入第二套模型依赖。
"""

from __future__ import annotations

import re
from typing import Any

from stock_senate.data.envelope import date_only
from stock_senate.data.providers.base import (
    Provider,
    ProviderError,
    ProviderNotAvailable,
    envelope_from_payload,
)

DEFAULT_URL = "http://127.0.0.1:6333"
DEFAULT_PREFIX = "senate_"
TEXT_KEYS = ("text", "content", "chunk", "summary", "body", "document")


class QdrantProvider(Provider):
    kind = "qdrant"

    def fetch(
        self, domain: str, code: str, as_of: str, params: dict[str, Any] | None = None
    ) -> Any:
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.models import FieldCondition, Filter, MatchValue
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ProviderNotAvailable(
                "qdrant-client 未安装；pip install stocksenate[qdrant] 后可用"
            ) from exc

        url = str(self.config.get("url") or DEFAULT_URL)
        api_key = self.config.get("api_key")
        if not api_key:
            env_name = self.config.get("api_key_env")
            if env_name:
                import os

                api_key = os.environ.get(str(env_name))
        try:
            client = QdrantClient(
                url=url,
                api_key=api_key,
                timeout=float(self.config.get("timeout", 10)),
            )
        except Exception as exc:
            raise ProviderNotAvailable(f"qdrant 不可达（{url}）：{exc}") from exc

        prefix = str(self.config.get("collection_prefix") or DEFAULT_PREFIX)
        limit = max(1, int(self.config.get("top_k", 8)))
        candidates = [
            f"{prefix}{domain}_{code}",
            str(self.config.get("collection") or f"{prefix}{domain}"),
        ]
        chosen = ""
        for name in candidates:
            if self._collection_exists(client, name):
                chosen = name
                break
        if not chosen:
            raise ProviderNotAvailable(
                f"{domain}/{code}: 没有可用的 collection（尝试过 {', '.join(candidates)}）"
            )

        scroll_filter = None
        if chosen != candidates[0]:
            scroll_filter = Filter(must=[FieldCondition(key="code", match=MatchValue(value=code))])
        try:
            points, _ = client.scroll(
                collection_name=chosen,
                scroll_filter=scroll_filter,
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:
            raise ProviderError(f"qdrant scroll 失败（{chosen}）：{exc}") from exc

        records = [self._record(point) for point in points or []]
        records = [item for item in records if item.get("text")]
        if not records and scroll_filter is not None:
            # 共享 collection 的 code 常放在 metadata 里（或 metadata 是字符串化 dict），
            # 顶层 Filter 匹配不到时退一步在进程内过滤，避免把「有数据」误判成缺口。
            try:
                points, _ = client.scroll(
                    collection_name=chosen,
                    limit=max(limit * 4, 32),
                    with_payload=True,
                    with_vectors=False,
                )
            except Exception as exc:
                raise ProviderError(f"qdrant scroll 失败（{chosen}）：{exc}") from exc
            loose = [self._record(point) for point in points or []]
            records = [item for item in loose if item.get("text") and item.get("code") == code]
        if not records:
            raise ProviderNotAvailable(f"{domain}/{code}: collection 内无匹配片段（{chosen}）")

        return envelope_from_payload(
            domain=domain,
            code=code,
            payload={"records": records, "count": len(records), "collection": chosen},
            source=self.name,
            source_detail=f"qdrant {url} / {chosen} [scroll]",
            data_as_of=date_only(as_of),
            meta={"confidence": "medium"},
            default_confidence="medium",
        )

    @staticmethod
    def _collection_exists(client: Any, name: str) -> bool:
        try:
            collections = client.get_collections().collections
        except Exception:
            return False
        return any(getattr(item, "name", "") == name for item in collections)

    @staticmethod
    def _code_of(payload: dict[str, Any]) -> str:
        """取记录所属股票代码：优先顶层 code，其次 metadata（dict 或字符串化 dict）。"""
        code = str(payload.get("code") or "").strip()
        if code:
            return code
        meta = payload.get("metadata")
        if isinstance(meta, dict):
            return str(meta.get("code") or meta.get("symbol") or "").strip()
        if isinstance(meta, str):
            found = re.search(r"(?:code|symbol)['\"]?\s*[:=]\s*['\"]([^'\"]+)['\"]", meta)
            if found:
                return found.group(1).strip()
        return ""

    @staticmethod
    def _record(point: Any) -> dict[str, Any]:
        payload = dict(getattr(point, "payload", None) or {})
        text = ""
        for key in TEXT_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                text = value.strip()
                break
        return {
            "text": text,
            "code": QdrantProvider._code_of(payload),
            "title": payload.get("title", ""),
            "data_as_of": payload.get("data_as_of", ""),
            "source": payload.get("source", ""),
            "score": getattr(point, "score", None),
        }


__all__ = ["QdrantProvider"]
