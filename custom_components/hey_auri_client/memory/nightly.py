"""Nightly memory job: turn a day of owner-mode conversation into a digest and facts.

For each finished local day (days end at the configured boundary hour):
  1. build the transcript from the archive;
  2. /memory/digest: draft + review (per-part drafts + merge for long days);
  3. embed the digest and the day's exchanges;
  4. /memory/extract: candidate facts with embeddings, in chunks;
  5. drop repeats and anything the household asked to forget;
  6. compare each candidate with its nearest current facts and, where there
     are close ones, let /memory/reconcile decide to add, update or skip;
  7. write everything in one transaction (digest, 7-day exchanges, facts,
     supersede links and the run row), so a failure leaves nothing partial;
  8. write the Obsidian copy and fire hey_auri_client.memory_run_finished.
Every cloud call happens before the write, so a retry starts clean.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

from ..const import EVENT_MEMORY_RUN_FINISHED
from .api import MemoryApiError, embedding_tag
from .identity import household_persons
from .obsidian import remove_daily_note, validate_vault, write_daily_note, write_memory_note
from .recall import normalize_statement, repeats
from .store import COMMITMENT_GRACE, EXCHANGE_TTL, NewMemory
from .transcript import build_transcript, chunk_lines, current_day, day_window, exchanges_for_memory, split_chunk

if TYPE_CHECKING:
    from .runtime import MemoryRuntime

_LOGGER = logging.getLogger(__name__)

# Request limits (the server enforces the same ones).
DIGEST_CHUNK_CHARS = 100_000  # server default memoryMaxTranscriptChars is 120k
EXTRACT_CHUNK_CHARS = 60_000
MAX_CANDIDATES_PER_CHUNK = 30
MAX_KNOWN_SUBJECTS = 200
MERGE_GROUP = 8
MAX_PARTIAL_CHARS = 6000
MAX_DRAFT_CHARS = 12_000
MAX_FORGOTTEN = 50
RECONCILE_BATCH = 25
# Halve a chunk at most this many times when a stage says it was too big or too slow.
MAX_SPLIT_DEPTH = 3

MAX_ATTEMPTS_PER_DAY = 3
NEIGHBOR_LIMIT = 5
NEIGHBOR_MIN_SIM = 0.55
CANDIDATE_DUPLICATE_SIM = 0.92
FORGOTTEN_MATCH_SIM = 0.9
# Failures that won't clear up by trying the next day tonight.
_STOP_REASONS = frozenset({"over_budget", "memory_disabled"})
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_NOT_A_PERSON = frozenset({"assistant", "a household member", "household member", "someone"})


def _cosine(a: list[float], b: list[float]) -> float:
    # The backend returns unit-length vectors, so the dot product is the cosine.
    return sum(x * y for x, y in zip(a, b))


def _parse_day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or ""))
    except ValueError:
        return None


def _response_tag(response: dict[str, Any]) -> str:
    return embedding_tag(str(response.get("embedding_model") or "unknown"), int(response.get("dimensions") or 0))


def _subjects(candidate: dict[str, Any]) -> list[tuple[str, str]]:
    """(name, kind) of who or what a candidate is about."""
    return [(s["name"], s.get("kind") or "topic") for s in candidate.get("subjects") or [] if s.get("name")]


def parse_speaker(label: Any) -> tuple[str | None, bool]:
    """(name, stated) from a transcript speaker label; (None, False) when nobody knows who spoke."""
    text = " ".join(str(label or "").split())
    stated = text.lower().endswith("(stated)")
    if stated:
        text = text[: -len("(stated)")].strip()
    if not text or text.lower().startswith("unknown speaker") or text.lower() in _NOT_A_PERSON:
        return None, False
    return text[:60], stated


@dataclass(slots=True)
class _Run:
    """One day's run."""

    day: date
    run_id: int
    envelope: dict[str, Any]
    stats: dict[str, Any] = field(default_factory=lambda: {"usage": {"prompt_tokens": 0, "completion_tokens": 0}})
    # Embedding space of this run's vectors, fixed by the first embedding response.
    tag: str | None = None

    def add_usage(self, response: dict[str, Any]) -> None:
        usage = response.get("usage") or {}
        for key in ("prompt_tokens", "completion_tokens"):
            self.stats["usage"][key] += int(usage.get(key) or 0)

    def accept_tag(self, tag: str) -> bool:
        if self.tag is None:
            self.tag = tag
        return tag == self.tag


class NightlyJob:
    """The nightly pipeline for one MemoryRuntime (which serializes calls)."""

    def __init__(self, runtime: "MemoryRuntime") -> None:
        self._runtime = runtime
        self._hass = runtime.hass
        self._vault_problem: str | None = None

    @property
    def _store(self):
        return self._runtime.store

    async def _run(self, func, *args):
        return await self._hass.async_add_executor_job(func, *args)

    # ── entry points ────────────────────────────────────────────────────────

    async def async_catch_up(self, *, days_back: int = 7) -> dict[str, Any]:
        """Process finished days that have turns but no successful run (oldest first), then tidy up."""
        store = self._store
        config = self._runtime.config
        tz = dt_util.get_default_time_zone()
        today = current_day(dt_util.now(), config.day_boundary_hour)
        first = today - timedelta(days=max(days_back, 1))
        await self._run(store.fail_stale_runs)
        days = await self._run(
            lambda: store.days_with_messages(
                since=dt_util.as_utc(day_window(first, tz, config.day_boundary_hour)[0]),
                time_zone=str(self._hass.config.time_zone),
                boundary_hour=config.day_boundary_hour,
            )
        )

        results: dict[str, Any] = {"status": "succeeded", "days": {}}
        stopped: str | None = None
        for day in days:
            if not first <= day < today:
                continue
            history = await self._run(store.run_history, "nightly", day)
            if history.get("succeeded") or history.get("skipped") or history.get("failed", 0) >= MAX_ATTEMPTS_PER_DAY:
                continue
            result = await self._async_run_day(day, force=False)
            results["days"][day.isoformat()] = result
            if result.get("reason") in _STOP_REASONS:
                # Nothing else will get through tonight; the next run picks up the rest.
                stopped = result["reason"]
                break

        if stopped:
            results["stopped"] = stopped
        else:
            results["scrubbed_digests"] = await self._async_scrub_digests()
        results["housekeeping"] = await self._run(
            lambda: store.housekeeping(archive_retention_days=config.archive_retention_days)
        )
        if not stopped:
            results["embedded"] = await self._runtime.async_backfill_embeddings()
        await self._async_write_memory_note()
        return results

    async def async_run_day(self, day: date, *, force: bool = False) -> dict[str, Any]:
        """Process one day (the run_memory_job service with a date)."""
        result = await self._async_run_day(day, force=force)
        if result["status"] == "succeeded":
            await self._runtime.async_backfill_embeddings()
            await self._async_write_memory_note()
        return result

    async def _async_run_day(self, day: date, *, force: bool) -> dict[str, Any]:
        store = self._store
        history = await self._run(store.run_history, "nightly", day)
        if history.get("succeeded") and not force:
            return {"status": "skipped", "reason": "already_processed"}
        run_id = await self._run(lambda: store.start_run("nightly", day, attempt=sum(history.values()) + 1))
        if run_id is None:
            return {"status": "skipped", "reason": "already_running"}

        run = _Run(day=day, run_id=run_id, envelope=self._envelope(day))
        try:
            result = await self._async_process_day(run)
        except MemoryApiError as err:
            reason = "over_budget" if err.over_budget else "memory_disabled" if err.disabled else "cloud_error"
            _LOGGER.warning("Memory processing for %s failed (%s): %s", day, reason, err)
            result = {"status": "failed", "reason": reason}
            await self._async_record_failure(run, f"{reason}: {err}")
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Memory processing for %s failed", day)
            result = {"status": "failed", "reason": "error"}
            await self._async_record_failure(run, f"{type(err).__name__}: {err}")

        self._hass.bus.async_fire(EVENT_MEMORY_RUN_FINISHED, {"day": day.isoformat(), **result})
        return result

    async def _async_record_failure(self, run: _Run, error: str) -> None:
        try:
            await self._run(lambda: self._store.finish_run(run.run_id, status="failed", stats=run.stats, error=error))
        except Exception as err:  # noqa: BLE001
            # The row stays "running" until the next start marks it failed.
            _LOGGER.debug("Couldn't record the failed memory run: %s", err)

    def _envelope(self, day: date) -> dict[str, Any]:
        """Fields every stage of a day's run sends (kept identical so prompt caching works)."""
        return {
            "day": day.isoformat(),
            "weekday": _WEEKDAYS[day.weekday()],
            "timezone": str(self._hass.config.time_zone),
            "household": self._runtime.household_roster(),
        }

    # ── one day ─────────────────────────────────────────────────────────────

    async def _async_process_day(self, run: _Run) -> dict[str, Any]:
        store = self._store
        tz = dt_util.get_default_time_zone()
        day_start, day_end = day_window(run.day, tz, self._runtime.config.day_boundary_hour)
        rows = await self._run(store.messages_between, dt_util.as_utc(day_start), dt_util.as_utc(day_end))
        lines = build_transcript(rows, tz)
        if not lines:
            await self._run(lambda: store.finish_run(run.run_id, status="skipped", stats={"reason": "nothing_to_process"}))
            return {"status": "skipped", "reason": "nothing_to_process"}
        run.stats["transcript_lines"] = len(lines)
        run.stats["transcript_chars"] = sum(len(line) + 1 for line in lines)

        await self._run(store.set_run_stage, run.run_id, "digest")
        digest = await self._async_digest(run, lines)

        await self._run(store.set_run_stage, run.run_id, "embed")
        now = dt_util.utcnow()
        # A day processed late doesn't add exchanges that would already be expired.
        exchanges = [item for item in exchanges_for_memory(rows) if item["occurred_at"] + EXCHANGE_TTL > now]
        texts = ([digest] if digest else []) + [item["content"] for item in exchanges]
        digest_vector: list[float] | None = None
        if texts:
            vectors = await self._async_embed(run, texts)
            if digest:
                digest_vector, vectors = vectors[0], vectors[1:]
            for item, vector in zip(exchanges, vectors):
                item["embedding"] = vector

        await self._run(store.set_run_stage, run.run_id, "extract")
        candidates = await self._async_extract(run, lines)

        await self._run(store.set_run_stage, run.run_id, "reconcile")
        candidates = await self._async_filter_candidates(run, candidates)
        decisions = await self._async_reconcile(run, candidates)

        await self._run(store.set_run_stage, run.run_id, "apply")
        persons = {person["name"].lower(): person for person in household_persons(self._hass)}
        counts = await self._run(self._apply_blocking, run, day_start, digest, digest_vector, exchanges, decisions, persons)
        if run.tag:
            self._runtime.note_embedding_tag(run.tag)

        if digest:
            await self._async_vault(lambda vault: write_daily_note(vault, run.day, digest))
        return {"status": "succeeded", **counts}

    async def _async_embed(self, run: _Run, texts: list[str]) -> list[list[float]]:
        vectors, tag = await self._runtime.api.embed(texts, task_type="RETRIEVAL_DOCUMENT")
        if not run.accept_tag(tag):
            raise MemoryApiError("The embedding model changed during the run")
        return vectors

    # ── digest ──────────────────────────────────────────────────────────────

    async def _async_digest(self, run: _Run, lines: list[str]) -> str | None:
        transcript = "\n".join(lines)
        if len(transcript) <= DIGEST_CHUNK_CHARS:
            return await self._async_digest_part(run, transcript, 1, 1, review=True)
        parts = chunk_lines(lines, DIGEST_CHUNK_CHARS)
        drafts: list[str] = []
        for index, part in enumerate(parts, start=1):
            draft = await self._async_digest_part(run, part, index, len(parts), review=False)
            if draft:
                drafts.append(draft)
        return await self._async_merge(run, drafts)

    async def _async_digest_part(
        self, run: _Run, transcript: str, index: int, count: int, *, review: bool, depth: int = 0
    ) -> str | None:
        part = {"index": index, "count": count}
        try:
            draft = await self._runtime.api.digest(stage="draft", transcript=transcript, part=part, **run.envelope)
        except MemoryApiError as err:
            if not err.retry_smaller or depth >= MAX_SPLIT_DEPTH:
                raise
            halves: list[str] = []
            for half in split_chunk(transcript):
                text = await self._async_digest_part(run, half, index, count, review=False, depth=depth + 1)
                if text:
                    halves.append(text)
            return await self._async_merge(run, halves)

        run.add_usage(draft)
        if draft.get("empty"):
            return None
        if not review:
            return draft["digest"]
        try:
            reviewed = await self._runtime.api.digest(
                stage="review", transcript=transcript, part=part, draft=draft["digest"][:MAX_DRAFT_CHARS], **run.envelope
            )
        except MemoryApiError as err:
            if err.over_budget or err.disabled:
                raise
            # The review only polishes the draft; keep the draft rather than lose the day.
            _LOGGER.debug("Digest review skipped: %s", err)
            return draft["digest"]
        run.add_usage(reviewed)
        return None if reviewed.get("empty") else reviewed["digest"]

    async def _async_merge(self, run: _Run, partials: list[str]) -> str | None:
        """Merge partial digests, at most MERGE_GROUP per request, until one is left."""
        while len(partials) > 1:
            merged: list[str] = []
            for start in range(0, len(partials), MERGE_GROUP):
                group = partials[start:start + MERGE_GROUP]
                if len(group) == 1:
                    merged.append(group[0])
                    continue
                response = await self._runtime.api.digest(
                    stage="merge", partials=[text[:MAX_PARTIAL_CHARS] for text in group], **run.envelope
                )
                run.add_usage(response)
                if not response.get("empty"):
                    merged.append(response["digest"])
            partials = merged
        return partials[0] if partials else None

    # ── facts ───────────────────────────────────────────────────────────────

    async def _async_extract(self, run: _Run, lines: list[str]) -> list[dict[str, Any]]:
        known_subjects = [
            {"name": entity.name, "kind": entity.kind, "aliases": list(entity.aliases)}
            for entity in self._runtime.entities
            if entity.kind != "household"
        ][:MAX_KNOWN_SUBJECTS]
        chunks = chunk_lines(lines, EXTRACT_CHUNK_CHARS)
        candidates: list[dict[str, Any]] = []
        for index, chunk in enumerate(chunks, start=1):
            candidates.extend(await self._async_extract_chunk(run, chunk, index, len(chunks), known_subjects))
        run.stats["candidates"] = len(candidates)
        return candidates

    async def _async_extract_chunk(
        self,
        run: _Run,
        chunk: str,
        index: int,
        count: int,
        known_subjects: list[dict[str, Any]],
        depth: int = 0,
    ) -> list[dict[str, Any]]:
        try:
            response = await self._runtime.api.extract(
                transcript=chunk,
                part={"index": index, "count": count},
                known_subjects=known_subjects,
                max_candidates=MAX_CANDIDATES_PER_CHUNK,
                embed=True,
                **run.envelope,
            )
        except MemoryApiError as err:
            if not err.retry_smaller or depth >= MAX_SPLIT_DEPTH:
                raise
            found: list[dict[str, Any]] = []
            for half in split_chunk(chunk):
                found.extend(await self._async_extract_chunk(run, half, index, count, known_subjects, depth + 1))
            return found

        run.add_usage(response)
        candidates = [item for item in response.get("candidates") or [] if item.get("statement")]
        if any(item.get("embedding") for item in candidates) and not run.accept_tag(_response_tag(response)):
            for item in candidates:
                item.pop("embedding", None)
        missing = [item for item in candidates if not item.get("embedding")]
        if missing:
            vectors = await self._async_embed(run, [item["statement"] for item in missing])
            for item, vector in zip(missing, vectors):
                item["embedding"] = vector
        return candidates

    async def _async_filter_candidates(self, run: _Run, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop repeats within the day and things the household asked to forget."""
        kept: list[dict[str, Any]] = []
        seen: set[str] = set()
        repeats = forgotten = 0
        for candidate in candidates:
            key = normalize_statement(candidate["statement"])
            vector = candidate["embedding"]
            if key in seen or any(_cosine(vector, other["embedding"]) >= CANDIDATE_DUPLICATE_SIM for other in kept):
                repeats += 1
                continue
            if await self._async_was_forgotten(run, vector):
                forgotten += 1
                continue
            seen.add(key)
            kept.append(candidate)
        run.stats["dropped_repeats"] = repeats
        run.stats["dropped_forgotten"] = forgotten
        return kept

    async def _async_was_forgotten(self, run: _Run, vector: list[float]) -> bool:
        """True when this was forgotten on or after the day being processed.

        Saying it again on a later day counts as new information. On the day
        of the forget itself it's dropped: nothing tells whether it was said
        before or after.
        """
        rows = await self._run(
            lambda: self._store.retracted_neighbors(vector, run.tag, min_sim=FORGOTTEN_MATCH_SIM)
        )
        for row in rows:
            changed = row.get("status_changed_at")
            if changed is None or run.day <= dt_util.as_local(changed).date():
                return True
        return False

    async def _async_reconcile(
        self, run: _Run, candidates: list[dict[str, Any]]
    ) -> list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]]:
        """(candidate, decision, neighbors) for each candidate."""
        results: list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]] = []
        pending: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        for candidate in candidates:
            rows = await self._run(
                lambda vector=candidate["embedding"]: self._store.neighbors(vector, run.tag, limit=NEIGHBOR_LIMIT)
            )
            neighbors = [row for row in rows if row["sim"] >= NEIGHBOR_MIN_SIM]
            if neighbors:
                pending.append((candidate, neighbors))
            else:
                # Nothing close is known yet: a new memory, no need to ask.
                results.append((candidate, {"action": "add", "replaces": [], "statement": candidate["statement"]}, []))
        for start in range(0, len(pending), RECONCILE_BATCH):
            results.extend(await self._async_reconcile_batch(run, pending[start:start + RECONCILE_BATCH]))
        run.stats["reconciled"] = len(pending)
        return results

    async def _async_reconcile_batch(
        self, run: _Run, batch: list[tuple[dict[str, Any], list[dict[str, Any]]]], depth: int = 0
    ) -> list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]]:
        items = [
            {
                "idx": position,
                "statement": candidate["statement"],
                "type": candidate.get("type") or "fact",
                "speaker": candidate.get("speaker") or "",
                "occurred_on": candidate.get("occurred_on") or "",
                "existing": [
                    {
                        "id": int(row["id"]),
                        "statement": row["content"][:1000],
                        "kind": row["kind"],
                        "date": dt_util.as_local(row["at"]).date().isoformat() if row.get("at") else "",
                    }
                    for row in neighbors
                ],
            }
            for position, (candidate, neighbors) in enumerate(batch)
        ]
        try:
            response = await self._runtime.api.reconcile(
                day=run.envelope["day"], household=run.envelope["household"], items=items, embed_updates=True
            )
        except MemoryApiError as err:
            if not err.retry_smaller or depth >= MAX_SPLIT_DEPTH or len(batch) < 2:
                raise
            middle = len(batch) // 2
            return [
                *await self._async_reconcile_batch(run, batch[:middle], depth + 1),
                *await self._async_reconcile_batch(run, batch[middle:], depth + 1),
            ]

        run.add_usage(response)
        decisions = {
            decision["idx"]: decision
            for decision in response.get("decisions") or []
            if isinstance(decision, dict) and isinstance(decision.get("idx"), int)
        }
        keep_vectors = not any(d.get("embedding") for d in decisions.values()) or run.accept_tag(_response_tag(response))
        results = []
        for position, (candidate, neighbors) in enumerate(batch):
            decision = decisions.get(position) or {"action": "add", "replaces": [], "statement": candidate["statement"]}
            if not keep_vectors:
                decision.pop("embedding", None)
            results.append((candidate, decision, neighbors))
        return results

    # ── write ───────────────────────────────────────────────────────────────

    def _apply_blocking(
        self,
        run: _Run,
        day_start: datetime,
        digest: str | None,
        digest_vector: list[float] | None,
        exchanges: list[dict[str, Any]],
        decisions: list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]],
        persons: dict[str, dict[str, Any]],
    ) -> dict[str, int]:
        """Write the day in one transaction, including the run row's success."""
        store = self._store
        counts = {"digest": 0, "exchanges": 0, "facts_added": 0, "facts_updated": 0, "facts_confirmed": 0}
        with store.engine.begin() as conn:
            if digest:
                previous = store.retire_current_digest(conn, run.day)
                digest_id, _ = store.insert_memory(
                    NewMemory(
                        kind="digest",
                        content=digest,
                        source="nightly",
                        about_entity_ids=[store.ensure_household(conn)],
                        embedding=digest_vector,
                        embedding_model=run.tag,
                        occurred_at=day_start,
                        source_ref=run.day,
                        run_id=run.run_id,
                    ),
                    conn,
                )
                store.link_superseded(conn, previous, digest_id)
                counts["digest"] = 1

            for item in exchanges:
                _, duplicate = store.insert_memory(
                    NewMemory(
                        kind="exchange",
                        content=item["content"],
                        source="nightly",
                        importance=2,
                        embedding=item.get("embedding"),
                        embedding_model=run.tag,
                        occurred_at=item["occurred_at"],
                        expires_at=item["occurred_at"] + EXCHANGE_TTL,
                        source_ref=run.day,
                        run_id=run.run_id,
                        conversation_id=item["conversation_id"],
                        exchange_id=item["exchange_id"],
                    ),
                    conn,
                )
                counts["exchanges"] += 0 if duplicate else 1

            for candidate, decision, neighbors in decisions:
                action = decision.get("action") or "add"
                if action == "skip":
                    covered = [int(i) for i in decision.get("replaces") or []]
                    if not covered and neighbors:
                        covered = [int(neighbors[0]["id"])]
                    store.confirm(conn, covered)
                    # A known fact can still tell what its subject is (Biscuit is a pet).
                    store.learn_kinds(conn, _subjects(candidate))
                    counts["facts_confirmed"] += 1
                    continue

                memory_id, duplicate = store.insert_memory(
                    self._fact(run, day_start, candidate, decision, persons, conn), conn
                )
                # An update whose merged sentence is already stored word for word
                # still retires what it replaces, in favour of that stored memory.
                replaced = (
                    store.supersede(conn, [int(i) for i in decision.get("replaces") or []], memory_id)
                    if action == "update"
                    else []
                )
                if duplicate:
                    store.confirm(conn, [memory_id])
                if replaced:
                    counts["facts_updated"] += 1
                elif duplicate:
                    counts["facts_confirmed"] += 1
                else:
                    counts["facts_added"] += 1

            run.stats.update(counts)
            store.finish_run(run.run_id, status="succeeded", stats=run.stats, conn=conn)
        return counts

    def _fact(
        self,
        run: _Run,
        day_start: datetime,
        candidate: dict[str, Any],
        decision: dict[str, Any],
        persons: dict[str, dict[str, Any]],
        conn,
    ) -> NewMemory:
        tz = day_start.tzinfo
        statement = decision.get("statement") or candidate["statement"]
        kind = candidate.get("type") or "fact"
        if kind == "commitment" and repeats(statement):
            # A routine ("every Thursday") mustn't expire two weeks after one of its dates.
            kind = "fact"
        subjects = _subjects(candidate)
        speaker_name, stated = parse_speaker(candidate.get("speaker"))
        person = persons.get(speaker_name.lower()) if speaker_name else None
        said_on = _parse_day(candidate.get("occurred_on")) or run.day
        due_on = _parse_day(candidate.get("due_on")) if kind == "commitment" else None
        due_at = datetime.combine(due_on, time.min, tzinfo=tz) if due_on else None
        if decision.get("action") == "update":
            # The merged statement's own vector; without one, the backfill embeds it later.
            vector = decision.get("embedding")
        else:
            vector = candidate.get("embedding")
        return NewMemory(
            kind=kind,
            content=statement,
            source="nightly",
            importance=int(candidate.get("importance") or 3),
            sensitivity="private" if candidate.get("sensitivity") == "private" else "normal",
            about_entity_ids=self._store.resolve_entities(subjects, conn) if subjects else [],
            embedding=vector,
            embedding_model=run.tag,
            occurred_at=datetime.combine(said_on, time(hour=12), tzinfo=tz),
            due_at=due_at,
            expires_at=((due_at or day_start) + COMMITMENT_GRACE) if kind == "commitment" else None,
            source_ref=run.day,
            run_id=run.run_id,
            speaker_source=("stated" if stated else "ha_user") if speaker_name else "unknown",
            speaker_name=speaker_name,
            speaker_person_entity_id=person["entity_id"] if person else None,
        )

    # ── after a forget ──────────────────────────────────────────────────────

    async def _async_scrub_digests(self) -> int:
        """Rewrite digests a forget took out of service, without what was forgotten."""
        store = self._store
        done = 0
        for digest in await self._run(store.pending_scrub_digests):
            day: date | None = digest.get("source_ref")
            reason = str(digest.get("status_reason") or "")
            ids = [int(part) for part in reason.partition(":")[2].split(",") if part.strip().isdigit()]
            forgotten = [text for text in (await self._run(store.get_contents, ids)).values() if text][:MAX_FORGOTTEN]
            cleaned: str | None = None
            vector: list[float] | None = None
            tag: str | None = None
            if day is not None and forgotten:
                try:
                    response = await self._runtime.api.digest(
                        stage="scrub", draft=digest["content"][:MAX_DRAFT_CHARS], forgotten=forgotten, **self._envelope(day)
                    )
                    cleaned = None if response.get("empty") else (response.get("digest") or None)
                    if cleaned:
                        vectors, tag = await self._runtime.api.embed([cleaned], task_type="RETRIEVAL_DOCUMENT")
                        vector = vectors[0]
                except MemoryApiError as err:
                    _LOGGER.debug("Digest clean-up postponed: %s", err)
                    break
            # Without a day or the forgotten text the digest just stays out of service.
            await self._run(self._replace_digest_blocking, digest["id"], day, cleaned, vector, tag)
            if day is not None:
                await self._async_vault(
                    lambda vault, d=day, text=cleaned: write_daily_note(vault, d, text) if text else remove_daily_note(vault, d)
                )
            done += 1
        return done

    def _replace_digest_blocking(
        self, old_id: int, day: date | None, cleaned: str | None, vector: list[float] | None, tag: str | None
    ) -> None:
        store = self._store
        with store.engine.begin() as conn:
            # Not linked to the old digest: its text holds what was forgotten.
            if cleaned and day is not None and store.current_digest_id(conn, day) is None:
                day_start = day_window(day, dt_util.get_default_time_zone(), self._runtime.config.day_boundary_hour)[0]
                store.insert_memory(
                    NewMemory(
                        kind="digest",
                        content=cleaned,
                        source="nightly",
                        about_entity_ids=[store.ensure_household(conn)],
                        embedding=vector,
                        embedding_model=tag,
                        occurred_at=day_start,
                        source_ref=day,
                    ),
                    conn,
                )
            store.mark_digest_scrubbed(conn, old_id)

    # ── Obsidian copy ───────────────────────────────────────────────────────

    async def _async_write_memory_note(self) -> None:
        store = self._store
        include_private = self._runtime.config.vault_include_private
        tz = dt_util.get_default_time_zone()
        today = dt_util.now().date()

        def _write(vault: str) -> None:
            rows = store.current_facts_for_export(include_private=include_private)
            write_memory_note(vault, rows, tz=tz, generated_on=today)

        await self._async_vault(_write)

    async def _async_vault(self, write: Callable[[str], Any]) -> None:
        """Run a vault write in the executor when a usable vault is set; never fails the job."""
        vault = self._runtime.config.vault_path
        if not vault:
            return
        hass = self._hass

        def _job() -> str | None:
            problem = validate_vault(vault, config_dir=hass.config.config_dir, is_allowed_path=hass.config.is_allowed_path)
            if problem is None:
                write(vault)
            return problem

        try:
            problem = await self._run(_job)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Couldn't write the Obsidian copy of Auri's memory: %s", err)
            return
        if problem and problem != self._vault_problem:
            _LOGGER.warning("Not writing the Obsidian copy of Auri's memory to %s (%s)", vault, problem)
        self._vault_problem = problem
