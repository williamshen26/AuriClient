"""Signed calls to AuriService's /memory/* endpoints (embeddings and nightly processing)."""
from __future__ import annotations

import asyncio
import json
from time import monotonic
from typing import Any, Sequence
import uuid

from homeassistant.core import HomeAssistant

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


# Query embeddings started before their turn (prefetch_query): each is kept this long, at most this many.
_PREFETCH_TTL_SECONDS = 30.0
_PREFETCH_MAX = 4


def query_key(text: str) -> str:
    """A message's words, to match the words heard as speech ended with the final transcript.

    Case, spaces and punctuation are left out: the final transcript often only adds those.
    """
    return "".join(char for char in text.casefold() if char.isalnum())


def _mark_retrieved(task: asyncio.Task) -> None:
    """A prefetch no turn used needs no error report: its turn made its own call."""
    if not task.cancelled():
        task.exception()


class MemoryApi:
    """Thin client for the stateless memory processing endpoints."""

    def __init__(self, client: SaaSClient, hass: HomeAssistant) -> None:
        self._client = client
        self._hass = hass
        # (scope, query_key) -> (when it started, the embedding call): see prefetch_query.
        self._prefetched: dict[tuple[str, str], tuple[float, asyncio.Task]] = {}

    def prefetch_query(self, text: str, *, scope: str) -> None:
        """Start embedding a voice turn's words before its final transcript arrives.

        The turn's recall by meaning waits on this call, so starting it while the transcript
        finishes takes it off the time to the reply. The turn uses the result when its text has
        the same words (embed); otherwise it makes its own call and this one goes unused.
        """
        key = (scope, query_key(text))
        if not key[1]:
            return
        now = monotonic()
        for old_key, (started, task) in list(self._prefetched.items()):
            if now - started > _PREFETCH_TTL_SECONDS:
                task.cancel()
                del self._prefetched[old_key]
        if key in self._prefetched:
            return
        while len(self._prefetched) >= _PREFETCH_MAX:
            self._prefetched.pop(next(iter(self._prefetched)))[1].cancel()
        task = self._hass.async_create_background_task(
            self._embed_now([text], task_type="RETRIEVAL_QUERY", scope=scope),
            "hey_auri_client prefetched query embedding",
        )
        task.add_done_callback(_mark_retrieved)
        self._prefetched[key] = (now, task)

    def _take_prefetched(self, text: str, scope: str) -> asyncio.Task | None:
        entry = self._prefetched.pop((scope, query_key(text)), None)
        if entry is None:
            return None
        started, task = entry
        if monotonic() - started > _PREFETCH_TTL_SECONDS or task.cancelled():
            task.cancel()
            return None
        return task

    async def _post(self, route: str, payload: dict[str, Any]) -> dict[str, Any]:
        # scope: "owner", or "guest" for a home-share stay (the payload says which).
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

    async def embed(
        self, texts: Sequence[str], *, task_type: str, scope: str = "owner"
    ) -> tuple[list[list[float]], str]:
        """Embed texts in batches of 100; returns (vectors in order, embedding tag).

        A query whose words were prefetched (prefetch_query) waits for that call instead.
        """
        if len(texts) == 1 and task_type == "RETRIEVAL_QUERY":
            prefetched = self._take_prefetched(texts[0], scope)
            if prefetched is not None:
                return await prefetched
        return await self._embed_now(texts, task_type=task_type, scope=scope)

    async def _embed_now(
        self, texts: Sequence[str], *, task_type: str, scope: str
    ) -> tuple[list[list[float]], str]:
        vectors: list[list[float]] = []
        tag = embedding_tag("unknown", EMBEDDING_DIMENSIONS)
        for start in range(0, len(texts), EMBED_BATCH_SIZE):
            response = await self._post(
                "embed",
                {
                    "scope": scope,
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

    async def rate(self, **fields: Any) -> dict[str, Any]:
        return await self._post("rate", fields)

    async def mishearings(self, **fields: Any) -> dict[str, Any]:
        return await self._post("mishearings", fields)

    async def grade(self, items: list[dict[str, str]]) -> dict[str, Any]:
        return await self._post("grade", {"items": items})

    async def exam_allowed(self) -> bool:
        """Whether this account may run the memory exam (memory_exam on its account). No model call.

        A refusal (403) or a backend without the check (404) is a no; any other
        failure raises MemoryApiError.
        """
        try:
            response = await self._post("exam", {})
        except MemoryApiError as err:
            if err.status_code in {403, 404}:
                return False
            raise
        return response.get("allowed") is True
