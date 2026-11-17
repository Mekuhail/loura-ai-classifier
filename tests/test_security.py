
import sqlite3
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

import main


@pytest.mark.parametrize("malicious_body", [
    "'; DROP TABLE tickets; --",
    "<script>alert('injected')</script>",
    "__import__('os').system('echo injected')"
])
def test_malicious_input_is_treated_as_data(
    tmp_path, monkeypatch, malicious_body
):
    db_path = tmp_path / "security_test.db"
    monkeypatch.setattr(main, "DB_PATH", db_path)

    # Isolated database: never touch the real tickets.db.
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


    # Without a context manager, TestClient does not
    # run FastAPI lifespan or start the OpenAI worker.
    client = TestClient(main.app)

    response = client.post("/tickets", json={
        "id": "security-001",
        "subject": "Security test",
        "body": malicious_body
    })

    assert response.status_code == 202

    # Retrieve the stored value through the actual API.
    retrieved = client.get("/tickets/security-001")

    assert retrieved.status_code == 200
    assert retrieved.json()["body"] == malicious_body

    # Verify that the database table still exists.
    with closing(sqlite3.connect(db_path)) as db:
        table = db.execute("""
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'tickets'
        """).fetchone()

    assert table is not None
