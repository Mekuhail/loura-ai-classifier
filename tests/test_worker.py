
import asyncio
import sqlite3
from contextlib import closing, suppress

import pytest

import main
from classifier import Classification


def test_invalid_classification_is_not_stored(
    tmp_path, monkeypatch
):
    # Create an isolated database for this test.
    db_path = tmp_path / "test.db"

    with closing(sqlite3.connect(db_path)) as db:
        with db:
            db.execute("""
                CREATE TABLE tickets (
                    id TEXT PRIMARY KEY,
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    category TEXT,
                    priority TEXT,
                    summary TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT
                )
            """)

            db.execute(
                """
                INSERT INTO tickets (id, subject, body)
                VALUES (?, ?, ?)
                """,
                ("t-test", "Refund", "Charged twice")
            )

    # Redirect the worker to our temporary database.
    monkeypatch.setattr(main, "DB_PATH", db_path)

    # Simulate an invalid LLM response.
    async def broken_classifier(client, ticket):
        return Classification.model_validate({
            "category": "sales",
            "priority": "urgent",
            "summary": "Invalid classification."
        })

    monkeypatch.setattr(
        main, "classify_ticket", broken_classifier
    )

    async def run_test():
        worker = asyncio.create_task(
            main.ticket_worker(None)
        )

        try:
            # Wait until the worker records the failure.
            for _ in range(100):
                await asyncio.sleep(0.01)

                with closing(sqlite3.connect(db_path)) as db:
                    row = db.execute(
                        """
                        SELECT attempts, last_error
                        FROM tickets
                        WHERE id = 't-test'
                        """
                    ).fetchone()

                if row == (1, "ValidationError"):
                    break
            else:
                pytest.fail("Worker did not record the failure")
        finally:
            worker.cancel()

            with suppress(asyncio.CancelledError):
                await worker

    asyncio.run(run_test())

    # Verify the database contains no invalid results.
    with closing(sqlite3.connect(db_path)) as db:
        row = db.execute(
            """
            SELECT status, attempts, category,
                   priority, summary, last_error
            FROM tickets
            WHERE id = 't-test'
            """
        ).fetchone()

    assert row == (
        "pending",
        1,
        None,
        None,
        None,
        "ValidationError"
    )


def test_third_failure_marks_ticket_failed(tmp_path, monkeypatch):
    db_path = tmp_path / "retry_test.db"

    # Create a ticket that has already failed twice.
    with closing(sqlite3.connect(db_path)) as db:
        with db:
            db.execute("""
                CREATE TABLE tickets (
                    id TEXT PRIMARY KEY,
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    category TEXT,
                    priority TEXT,
                    summary TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT
                )
            """)

            db.execute("""
                INSERT INTO tickets (id, subject, body, attempts)
                VALUES (?, ?, ?, ?)
            """, ("t-retry", "API problem", "Server down", 2))

    monkeypatch.setattr(main, "DB_PATH", db_path)

    calls = 0

    # Simulate another failure without calling OpenAI.
    async def failing_classifier(client, ticket):
        nonlocal calls
        calls += 1
        raise RuntimeError("Simulated provider failure")

    monkeypatch.setattr(
        main, "classify_ticket", failing_classifier
    )

    async def run_test():
        worker = asyncio.create_task(main.ticket_worker(None))

        try:
            for _ in range(100):
                await asyncio.sleep(0.01)

                with closing(sqlite3.connect(db_path)) as db:
                    status = db.execute("""
                        SELECT status FROM tickets
                        WHERE id = 't-retry'
                    """).fetchone()[0]

                if status == "failed":
                    break
            else:
                pytest.fail("Ticket never reached failed status")

        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker

    asyncio.run(run_test())

    with closing(sqlite3.connect(db_path)) as db:
        result = db.execute("""
            SELECT status, attempts, category,
                   priority, summary, last_error
            FROM tickets
            WHERE id = 't-retry'
        """).fetchone()

    assert result == (
        "failed",
        3,
        None,
        None,
        None,
        "RuntimeError"
    )

    assert calls == 1



def test_pending_ticket_is_recovered(tmp_path, monkeypatch):
    db_path = tmp_path / "recovery.db"

    # Simulate a ticket left pending after a crash.
    with closing(sqlite3.connect(db_path)) as db:
        with db:
            db.execute("""
                CREATE TABLE tickets (
                    id TEXT PRIMARY KEY,
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    category TEXT,
                    priority TEXT,
                    summary TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT
                )
            """)

            db.execute("""
                INSERT INTO tickets
                    (id, subject, body, status, attempts)
                VALUES (?, ?, ?, ?, ?)
            """, (
                "t-restart",
                "Upload error",
                "Uploading files fails.",
                "pending",
                1
            ))

    monkeypatch.setattr(main, "DB_PATH", db_path)

    calls = []

    # Replace OpenAI with a predictable test response.
    async def fake_classifier(client, ticket):
        calls.append(ticket["id"])

        return Classification(
            category="technical",
            priority="medium",
            summary="The customer reports failed uploads."
        )

    monkeypatch.setattr(
        main, "classify_ticket", fake_classifier
    )

    async def run_test():
        worker = asyncio.create_task(main.ticket_worker(None))

        try:
            for _ in range(100):
                await asyncio.sleep(0.01)

                with closing(sqlite3.connect(db_path)) as db:
                    row = db.execute("""
                        SELECT status, attempts, category
                        FROM tickets
                        WHERE id = 't-restart'
                    """).fetchone()

                if row[0] == "classified":
                    return

            pytest.fail("Pending ticket was not recovered")

        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker

    asyncio.run(run_test())

    with closing(sqlite3.connect(db_path)) as db:
        result = db.execute("""
            SELECT status, attempts, category
            FROM tickets
            WHERE id = 't-restart'
        """).fetchone()

    assert result == ("classified", 2, "technical")
    assert calls == ["t-restart"]



def test_worker_survives_database_error(tmp_path, monkeypatch):
    # This directory intentionally does not exist.
    unavailable_db = tmp_path / "missing_folder" / "tickets.db"

    monkeypatch.setattr(main, "DB_PATH", unavailable_db)

    async def run_test():
        worker = asyncio.create_task(
            main.ticket_worker(None)
        )

        try:
            # Give the worker time to attempt a DB connection.
            await asyncio.sleep(0.05)

            # A recoverable DB error should not kill the worker.
            assert not worker.done(), (
                "Background worker stopped after a database error"
            )

        finally:
            worker.cancel()

            with suppress(
                asyncio.CancelledError,
                sqlite3.Error
            ):
                await worker

    asyncio.run(run_test())


def test_result_write_failure_does_not_repeat_llm(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "write_failure.db"

    # Create an isolated test database.
    original_connect = sqlite3.connect

    with closing(original_connect(db_path)) as db:
        with db:
            db.execute("""
                CREATE TABLE tickets (
                    id TEXT PRIMARY KEY,
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    category TEXT,
                    priority TEXT,
                    summary TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT
                )
            """)

            db.execute("""
                INSERT INTO tickets (id, subject, body)
                VALUES (?, ?, ?)
            """, ("t-write", "API error", "Export is broken"))

    monkeypatch.setattr(main, "DB_PATH", db_path)

    failures = 0
    model_calls = 0

    # Make the first classification-result write fail.
    class FlakyConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            nonlocal failures

            normalized_sql = " ".join(sql.split())

            if (
                "SET status = 'classified'" in normalized_sql
                and failures == 0
            ):
                failures += 1
                raise sqlite3.OperationalError(
                    "Simulated temporary write failure"
                )

            return super().execute(sql, parameters)

    def flaky_connect(*args, **kwargs):
        return original_connect(
            *args, factory=FlakyConnection, **kwargs
        )

    monkeypatch.setattr(
        main.sqlite3, "connect", flaky_connect
    )

    # Replace OpenAI with a predictable response.
    async def fake_classifier(client, ticket):
        nonlocal model_calls
        model_calls += 1

        return Classification(
            category="technical",
            priority="medium",
            summary="The customer's export is broken."
        )

    monkeypatch.setattr(
        main, "classify_ticket", fake_classifier
    )

    async def run_test():
        worker = asyncio.create_task(
            main.ticket_worker(None)
        )

        try:
            for _ in range(400):
                await asyncio.sleep(0.01)

                with closing(original_connect(db_path)) as db:
                    row = db.execute("""
                        SELECT status
                        FROM tickets
                        WHERE id = 't-write'
                    """).fetchone()

                if row[0] == "classified":
                    return

                if worker.done():
                    pytest.fail(
                        "Worker terminated after a result write error"
                    )

            pytest.fail("Worker did not recover the saved result")

        finally:
            worker.cancel()

            with suppress(
                asyncio.CancelledError,
                sqlite3.Error
            ):
                await worker

    asyncio.run(run_test())

    assert failures == 1
    assert model_calls == 1
