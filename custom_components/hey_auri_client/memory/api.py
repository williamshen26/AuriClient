"""Signed calls to AuriService's /memory/* endpoints (embeddings and nightly processing)."""
from __future__ import annotations

import json
from typing import Any, Sequence
import uuid

from ..const import LATENCY_MEASUREMENT_KEY_MEMORY
from ..exceptions import SaaSRequestError
from ..saas_client import SaaSClient

# Each server stage finishes within ~29 s; allow for network time on top.
MEMORY_REQUEST_TIMEOUT_SECONDS = 35
EMBED_BATCH_SIZE = 100
EMBEDDING_DIMENSIONS = 768


class MemoryApiError(Exception):
    """A /memory/* call failed; retry_smaller means split the input and try again."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        max_chars: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.max_chars = max_chars

    @property
    def retry_smaller(self) -> bool:
        return self.status_code in {413, 504}

    @property
    def disabled(self) -> bool:
        return self.status_code == 403

    @property
    def over_budget(self) -> bool:
        return self.status_code == 429


def embedding_tag(model: str, dimensions: int) -> str:
    """How a row records which embedding space its vector is in."""
    return f"{model}@{dimensions}"


class MemoryApi:
    """Thin client for the stateless memory processing endpoints."""

    def __init__(self, client: SaaSClient) -> None:
        self._client = client

    async def _post(self, route: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = {"v": 1, "request_id": str(uuid.uuid4()), "scope": "owner", **payload}
        try:
            return await self._client.post_json(
                f"/memory/{route}",
                body,
                timeout_seconds=MEMORY_REQUEST_TIMEOUT_SECONDS,
                measurement_key=LATENCY_MEASUREMENT_KEY_MEMORY,
            )
        except SaaSRequestError as err:
            details: dict[str, Any] = {}
            try:
                details = json.loads(err.response_body or "{}")
            except ValueError:
                pass
            raise MemoryApiError(
                str(details.get("message") or err),
                status_code=err.status_code,
                error_code=details.get("error"),
                max_chars=details.get("max_chars"),
            ) from err

    async def embed(self, texts: Sequence[str], *, task_type: str) -> tuple[list[list[float]], str]:
        """Embed texts in batches of 100; returns (vectors in order, embedding tag)."""
        vectors: list[list[float]] = []
        tag = embedding_tag("unknown", EMBEDDING_DIMENSIONS)
        for start in range(0, len(texts), EMBED_BATCH_SIZE):
            response = await self._post(
                "embed",
                {
                    "task_type": task_type,
                    "dimensions": EMBEDDING_DIMENSIONS,
                    "texts": list(texts[start:start + EMBED_BATCH_SIZE]),
                },
            )
            vectors.extend(response.get("embeddings") or [])
            tag = embedding_tag(str(response.get("model") or "unknown"), int(response.get("dimensions") or 0))
        if len(vectors) != len(texts):
            raise MemoryApiError("The embedding response didn't match the request")
        return vectors, tag

    async def digest(self, **fields: Any) -> dict[str, Any]:
        return await self._post("digest", fields)

    async def extract(self, **fields: Any) -> dict[str, Any]:
        return await self._post("extract", fields)

    async def reconcile(self, **fields: Any) -> dict[str, Any]:
        return await self._post("reconcile", fields)

    async def grade(self, items: list[dict[str, str]]) -> dict[str, Any]:
        return await self._post("grade", {"items": items})
