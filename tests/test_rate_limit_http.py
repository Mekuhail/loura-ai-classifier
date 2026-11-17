
import sqlite3
from contextlib import closing

from fastapi.testclient import TestClient

import main
from rate_limit import RateLimiter


def test_post_tickets_returns_429(tmp_path, monkeypatch):
    # Use a temporary database.
    db_path = tmp_path / "rate_limit_test.db"

    monkeypatch.setattr(main, "DB_PATH", db_path)

    # Use a small limit to keep the test fast.
    monkeypatch.setattr(
        main,
        "limiter",
        RateLimiter(limit=2, window_seconds=60)
    )

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

    # No lifespan: don't start the OpenAI worker.
    client = TestClient(main.app)

    def submit(ticket_id):
        return client.post(
            "/tickets",
            json={
                "id": ticket_id,
                "subject": "Test",
                "body": "Testing rate limiting."
            }
        )

    first = submit("rate-001")
    second = submit("rate-002")
    third = submit("rate-003")

    assert first.status_code == 202
    assert second.status_code == 202
    assert third.status_code == 429

    assert "Retry-After" in third.headers

    # Verify the rejected ticket was never stored.
    with closing(sqlite3.connect(db_path)) as db:
        count = db.execute(
            "SELECT COUNT(*) FROM tickets"
        ).fetchone()[0]

    assert count == 2
