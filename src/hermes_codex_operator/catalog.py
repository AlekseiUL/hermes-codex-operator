"""Read-only thread discovery, exact-ID adoption, and durable watches."""

from __future__ import annotations

from dataclasses import dataclass

from .adapters.base import (
    AdapterError,
    CodexAdapter,
    ProtocolIntegrityError,
    ReadbackMissingResult,
    RemoteTurnFailed,
    ThreadSummary,
    ThreadCatalogReadback,
    TurnInProgress,
)
from .journal import CompletionEvent, SQLiteJournal, WatchRecord
from .models import sha256_text, validate_external_id


MAX_CATALOG_PAGES = 20
MAX_CATALOG_THREADS = 1000


@dataclass(frozen=True, slots=True)
class CatalogResult:
    threads: tuple[ThreadSummary, ...]
    pages_read: int
    truncated: bool
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class WatchPollResult:
    watches: tuple[WatchRecord, ...]
    events: tuple[CompletionEvent, ...]


class CatalogOperator:
    """A separate read-only surface that cannot dispatch Codex work."""

    def __init__(self, journal: SQLiteJournal, adapter_factory) -> None:
        self.journal = journal
        self.adapter_factory = adapter_factory

    async def catalog(
        self,
        *,
        page_size: int = 50,
        max_pages: int = MAX_CATALOG_PAGES,
        cursor: str | None = None,
    ) -> CatalogResult:
        if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= MAX_CATALOG_PAGES:
            raise ValueError(f"max_pages must be an integer from 1 to {MAX_CATALOG_PAGES}")
        if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 100:
            raise ValueError("page_size must be an integer from 1 to 100")
        if cursor is not None:
            validate_external_id(cursor, "cursor")

        adapter: CodexAdapter = self.adapter_factory()
        current = cursor
        seen_cursors: set[str] = set()
        seen_threads: set[str] = set()
        threads: list[ThreadSummary] = []
        pages = 0
        try:
            while pages < max_pages:
                page = await adapter.list_threads(cursor=current, limit=page_size)
                pages += 1
                for thread in page.threads:
                    if thread.thread_id in seen_threads:
                        raise ProtocolIntegrityError(
                            "thread/list repeated a thread identity across pages"
                        )
                    seen_threads.add(thread.thread_id)
                    threads.append(thread)
                    if len(threads) > MAX_CATALOG_THREADS:
                        raise ProtocolIntegrityError("thread catalog exceeds its global bound")
                next_cursor = page.next_cursor
                if next_cursor is None:
                    return CatalogResult(tuple(threads), pages, False, None)
                if next_cursor in seen_cursors or next_cursor == current:
                    raise ProtocolIntegrityError("thread/list cursor cycle detected")
                seen_cursors.add(next_cursor)
                current = next_cursor
            return CatalogResult(tuple(threads), pages, True, current)
        finally:
            await adapter.close()

    async def adopt(self, thread_id: str, turn_id: str) -> WatchPollResult:
        """Validate exact identities with thread/read and persist one watch."""

        validate_external_id(thread_id, "thread_id")
        validate_external_id(turn_id, "turn_id")
        watch = self.journal.adopt_watch(thread_id, turn_id)
        if watch.state != "ACTIVE":
            return WatchPollResult((watch,), self.journal.list_events(include_acked=True))
        return await self._poll_records((watch,))

    async def read(self, thread_id: str) -> ThreadCatalogReadback:
        """Read one exact thread and its turns without resuming it."""

        validate_external_id(thread_id, "thread_id")
        adapter: CodexAdapter = self.adapter_factory()
        try:
            return await adapter.read_thread(thread_id)
        finally:
            await adapter.close()

    async def poll(self, watch_id: str | None = None) -> WatchPollResult:
        if watch_id is None:
            records = self.journal.list_watches(active_only=True)
        else:
            records = (self.journal.get_watch(watch_id),)
        return await self._poll_records(records)

    async def _poll_records(self, records: tuple[WatchRecord, ...]) -> WatchPollResult:
        updated: list[WatchRecord] = []
        emitted: list[CompletionEvent] = []
        for watch in records:
            if watch.state != "ACTIVE":
                updated.append(watch)
                continue
            adapter: CodexAdapter = self.adapter_factory()
            try:
                readback = await adapter.read_turn(watch.thread_id, watch.turn_id)
            except TurnInProgress:
                updated.append(self.journal.get_watch(watch.watch_id))
            except RemoteTurnFailed as exc:
                status = "INTERRUPTED" if exc.status == "interrupted" else "FAILED"
                record, event = self.journal.finish_watch(
                    watch.watch_id,
                    status,
                    failure_code="remote_turn_" + exc.status,
                )
                updated.append(record)
                emitted.append(event)
            except (ReadbackMissingResult, ProtocolIntegrityError):
                record, event = self.journal.finish_watch(
                    watch.watch_id,
                    "UNKNOWN",
                    failure_code="exact_identity_unconfirmed",
                )
                updated.append(record)
                emitted.append(event)
            except AdapterError:
                record, event = self.journal.finish_watch(
                    watch.watch_id,
                    "UNKNOWN",
                    failure_code="authoritative_readback_unavailable",
                )
                updated.append(record)
                emitted.append(event)
            else:
                record, event = self.journal.finish_watch(
                    watch.watch_id,
                    "COMPLETED",
                    result_sha256=sha256_text(readback.final_text),
                )
                updated.append(record)
                emitted.append(event)
            finally:
                await adapter.close()
        return WatchPollResult(tuple(updated), tuple(emitted))


__all__ = [
    "CatalogOperator",
    "CatalogResult",
    "MAX_CATALOG_PAGES",
    "MAX_CATALOG_THREADS",
    "WatchPollResult",
]
