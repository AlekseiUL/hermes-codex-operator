from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import multiprocessing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from hermes_codex_operator.adapters.base import (
    ProtocolIntegrityError,
    ThreadPage,
    ThreadSummary,
)
from hermes_codex_operator.adapters.fake import FakeBackend, FakeCodexAdapter
from hermes_codex_operator.catalog import CatalogOperator
from hermes_codex_operator.journal import DATABASE_VERSION, SQLiteJournal
from hermes_codex_operator.models import sha256_text


def _process_adopt(database: str, start, results) -> None:
    with SQLiteJournal(database) as journal:
        start.wait(timeout=5)
        results.put(journal.adopt_watch("thread-process", "turn-process").watch_id)


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_catalog_uses_bounded_pagination_and_read_only_methods(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            first = ThreadSummary("thread-1", workspace, "one", "idle", 1, 2, ("turn-1",))
            second = ThreadSummary("thread-2", workspace, "two", "idle", 3, 4, ())
            backend = FakeBackend(
                list_pages={
                    None: ThreadPage((first,), "cursor-2"),
                    "cursor-2": ThreadPage((second,), None),
                }
            )
            with SQLiteJournal(workspace / "state.sqlite3") as journal:
                result = await CatalogOperator(
                    journal, lambda: FakeCodexAdapter(backend)
                ).catalog(page_size=1, max_pages=2)
            self.assertEqual(tuple(item.thread_id for item in result.threads), ("thread-1", "thread-2"))
            self.assertEqual(result.pages_read, 2)
            self.assertFalse(result.truncated)
            self.assertEqual(backend.list_thread_calls, 2)
            self.assertEqual(backend.start_thread_calls, 0)
            self.assertEqual(backend.resume_thread_calls, 0)
            self.assertEqual(backend.start_turn_calls, 0)

    async def test_read_returns_exact_turns_and_result_without_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            backend = FakeBackend()
            backend.threads[backend.thread_id] = workspace
            backend.turns[(backend.thread_id, backend.turn_id)] = "completed"
            with SQLiteJournal(workspace / "state.sqlite3") as journal:
                result = await CatalogOperator(
                    journal, lambda: FakeCodexAdapter(backend)
                ).read(backend.thread_id)
            self.assertEqual(result.thread.thread_id, backend.thread_id)
            self.assertEqual(result.turns[0].turn_id, backend.turn_id)
            self.assertEqual(result.turns[0].final_text, backend.final_text)
            self.assertEqual(backend.read_thread_calls, 1)
            self.assertEqual(backend.start_thread_calls, 0)
            self.assertEqual(backend.resume_thread_calls, 0)
            self.assertEqual(backend.start_turn_calls, 0)

    async def test_catalog_rejects_cursor_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            backend = FakeBackend(list_pages={None: ThreadPage((), "cycle"), "cycle": ThreadPage((), "cycle")})
            with SQLiteJournal(Path(root) / "state.sqlite3") as journal:
                with self.assertRaises(ProtocolIntegrityError):
                    await CatalogOperator(journal, lambda: FakeCodexAdapter(backend)).catalog()


class WatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_preserves_watch_id_and_repeated_poll_emits_once(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            database = root_path / "state.sqlite3"
            backend = FakeBackend()
            backend.threads[backend.thread_id] = root_path
            backend.turns[(backend.thread_id, backend.turn_id)] = "inProgress"  # type: ignore[assignment]
            with SQLiteJournal(database) as journal:
                first = await CatalogOperator(journal, lambda: FakeCodexAdapter(backend)).adopt(
                    backend.thread_id, backend.turn_id
                )
                watch_id = first.watches[0].watch_id
                self.assertEqual(first.watches[0].state, "ACTIVE")
                self.assertEqual(first.events, ())

            backend.turns[(backend.thread_id, backend.turn_id)] = "completed"
            with SQLiteJournal(database) as reopened:
                self.assertEqual(reopened.get_watch(watch_id).watch_id, watch_id)
                operator = CatalogOperator(reopened, lambda: FakeCodexAdapter(backend))
                completed = await operator.poll(watch_id)
                repeated = await operator.poll(watch_id)
                self.assertEqual(completed.watches[0].state, "COMPLETED")
                self.assertEqual(completed.watches[0].result_sha256, sha256_text(backend.final_text))
                self.assertEqual(len(completed.events), 1)
                self.assertEqual(repeated.events, ())
                self.assertEqual(len(reopened.list_events(include_acked=True)), 1)
            self.assertEqual(backend.start_thread_calls, 0)
            self.assertEqual(backend.resume_thread_calls, 0)
            self.assertEqual(backend.start_turn_calls, 0)

    async def test_missing_or_conflicting_identity_becomes_unknown_without_dispatch(self) -> None:
        class ConflictingAdapter(FakeCodexAdapter):
            async def read_turn(self, thread_id: str, turn_id: str):
                self.backend.read_turn_calls += 1
                raise ProtocolIntegrityError("synthetic identity conflict")

        with tempfile.TemporaryDirectory() as root:
            backend = FakeBackend()
            with SQLiteJournal(Path(root) / "state.sqlite3") as journal:
                result = await CatalogOperator(
                    journal, lambda: ConflictingAdapter(backend)
                ).adopt("thread-exact", "turn-exact")
                self.assertEqual(result.watches[0].state, "UNKNOWN")
                self.assertEqual(result.watches[0].failure_code, "exact_identity_unconfirmed")
                self.assertEqual(len(result.events), 1)
            self.assertEqual(backend.start_thread_calls, 0)
            self.assertEqual(backend.resume_thread_calls, 0)
            self.assertEqual(backend.start_turn_calls, 0)


class WatchJournalTests(unittest.TestCase):
    def test_two_journals_concurrently_adopt_one_exact_watch(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            database = Path(root) / "state.sqlite3"
            first = SQLiteJournal(database)
            second = SQLiteJournal(database)
            barrier = threading.Barrier(2)

            def adopt(journal: SQLiteJournal) -> str:
                barrier.wait(timeout=2)
                return journal.adopt_watch("thread-exact", "turn-exact").watch_id

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    ids = tuple(executor.map(adopt, (first, second)))
                self.assertEqual(ids[0], ids[1])
                self.assertEqual(len(first.list_watches()), 1)
            finally:
                first.close()
                second.close()

    def test_two_processes_concurrently_adopt_one_exact_watch(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            database = str(Path(root) / "state.sqlite3")
            with SQLiteJournal(database):
                pass
            context = multiprocessing.get_context("spawn")
            start = context.Event()
            results = context.Queue()
            processes = [
                context.Process(target=_process_adopt, args=(database, start, results))
                for _ in range(2)
            ]
            try:
                for process in processes:
                    process.start()
                start.set()
                ids = tuple(results.get(timeout=10) for _ in processes)
                for process in processes:
                    process.join(timeout=10)
                    self.assertEqual(process.exitcode, 0)
                self.assertEqual(ids[0], ids[1])
                with SQLiteJournal(database) as journal:
                    self.assertEqual(len(journal.list_watches()), 1)
            finally:
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                    process.join(timeout=2)
                results.close()
                results.join_thread()

    def test_v1_database_migrates_atomically_and_reopens(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            database = Path(root) / "state.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("PRAGMA user_version = 1")
            with SQLiteJournal(database) as migrated:
                watch = migrated.adopt_watch("thread-exact", "turn-exact")
                self.assertEqual(watch.state, "ACTIVE")
            with SQLiteJournal(database) as reopened:
                self.assertEqual(reopened.get_watch(watch.watch_id).watch_id, watch.watch_id)
                with closing(sqlite3.connect(database)) as connection:
                    version = connection.execute("PRAGMA user_version").fetchone()[0]
                self.assertEqual(version, DATABASE_VERSION)

    def test_event_ack_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            with SQLiteJournal(Path(root) / "state.sqlite3") as journal:
                watch = journal.adopt_watch("thread-exact", "turn-exact")
                _, event = journal.finish_watch(
                    watch.watch_id, "COMPLETED", result_sha256=sha256_text("result")
                )
                first = journal.ack_event(event.event_id)
                second = journal.ack_event(event.event_id)
                self.assertIsNotNone(first.acked_at)
                self.assertEqual(first, second)
                self.assertEqual(journal.list_events(), ())
                self.assertEqual(len(journal.list_events(include_acked=True)), 1)


if __name__ == "__main__":
    unittest.main()
