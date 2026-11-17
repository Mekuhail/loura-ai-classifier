
import sqlite3
from contextlib import closing

from fastapi.testclient import TestClient

import main


def test_duplicate_preserves_classified_ticket(
    tmp_path, monkeypatch
):
    # Use an isolated SQLite database.
    db_path = tmp_path / "api_test.db"
    monkeypatch.setattr(main, "DB_PATH", db_path)

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

    # Do not start the background worker.
    client = TestClient(main.app)

    payload = {
        "id": "t-duplicate",
        "subject": "Charged twice",
        "body": "Please refund one charge."
    }

    # First request creates the ticket.
    first = client.post("/tickets", json=payload)

    assert first.status_code == 202
    assert first.json()["created"] is True

    # Simulate an already-classified ticket.
    with closing(sqlite3.connect(db_path)) as db:
        with db:
            db.execute("""
                UPDATE tickets
                SET status = 'classified',
                    category = 'billing',
                    priority = 'medium',
                    summary = 'Customer requests a refund.',
                    attempts = 1
                WHERE id = ?
            """, ("t-duplicate",))

    # Re-submit the identical ticket.
    second = client.post("/tickets", json=payload)

    assert second.status_code == 200
    assert second.json()["created"] is False
    assert second.json()["status"] == "classified"

    # Same ID with different content is rejected.
    conflicting = {
        **payload,
        "body": "Different message"
    }

    conflict = client.post(
        "/tickets", json=conflicting
    )

    assert conflict.status_code == 409

    # Inspect the persisted database state.
    with closing(sqlite3.connect(db_path)) as db:
        count = db.execute(
            "SELECT COUNT(*) FROM tickets"
        ).fetchone()[0]

        result = db.execute("""
            SELECT status, attempts, category, priority
            FROM tickets
            WHERE id = ?
        """, ("t-duplicate",)).fetchone()

    assert count == 1

    assert result == (
        "classified",
        1,
        "billing",
        "medium"
    )
