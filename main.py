
import asyncio
import logging
import os
import sqlite3

from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
)
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from classifier import classify_ticket
from rate_limit import RateLimiter



# Configuration of constants and logging.
DB_PATH = Path(__file__).with_name("tickets.db")
MAX_ATTEMPTS = 3
logger = logging.getLogger(__name__)
limiter = RateLimiter(limit=20, window_seconds=60)

# Rate Limiting -small preventaion of abuse of the ticket submission endpoint.-
def enforce_rate_limit(request: Request):
    client_ip = (
        request.client.host
        if request.client
        else "unknown"
    )

    retry_after = limiter.check(client_ip)

    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail="Too many ticket submissions",
            headers={"Retry-After": str(retry_after)}
        )

# Asynchronous Background Worker:
# differentt from synchrounous workers,
# it allows the application to handle other tasks while waiting for I/O operations to complete. CRUCIAL POINT
async def ticket_worker(client: AsyncOpenAI):

    while True:

        # Step 1: Find pending work and record the attempt.
        try:
            with closing(sqlite3.connect(DB_PATH)) as db:
                db.row_factory = sqlite3.Row

                with db:
                    ticket = db.execute(
                        """
                        SELECT id, subject, body, attempts
                        FROM tickets
                        WHERE status = 'pending'
                          AND attempts < ?
                        ORDER BY rowid
                        LIMIT 1
                        """,
                        (MAX_ATTEMPTS,)
                    ).fetchone()

                    if ticket is not None:
                        db.execute(
                            """
                            UPDATE tickets
                            SET attempts = attempts + 1
                            WHERE id = ?
                            """,
                            (ticket["id"],)
                        )

        except sqlite3.Error as exc:
            logger.warning(
                "Worker database operation failed: %s",
                type(exc).__name__
            )

            # Wait before attempting database access again.
            await asyncio.sleep(2)
            continue

        # Wait if there are no pending tickets.
        if ticket is None:
            await asyncio.sleep(1)
            continue

        ticket_id = ticket["id"]
        attempt = ticket["attempts"] + 1

        logger.info(
            "Classifying ticket %s, attempt %s",
            ticket_id,
            attempt
        )

        # Call the LLM external from the HTTP request.
        # separating the classification from the request to avoid blocking the API.
        try:
            result = await classify_ticket(
                client,
                dict(ticket)
            )

        #Handle classification failures.
        except Exception as exc:
            status = (
                "failed"
                if attempt >= MAX_ATTEMPTS
                else "pending"
            )

            with closing(sqlite3.connect(DB_PATH)) as db:
                with db:
                    db.execute(
                        """
                        UPDATE tickets
                        SET status = ?,
                            last_error = ?
                        WHERE id = ?
                        """,
                        (
                            status,
                            type(exc).__name__,
                            ticket_id
                        )
                    )

            logger.warning(
                "Classification failed for %s: %s",
                ticket_id,
                type(exc).__name__
            )

            # Exponential backoff while retries remain.
            if status == "pending":
                await asyncio.sleep(2 ** attempt)

        # Save validated classification results.

        else:
            # The classification is valid.
            # Retry saving without calling OpenAI again.
            write_failures = 0

            while True:
                try:
                    with closing(sqlite3.connect(DB_PATH)) as db:
                        with db:
                            db.execute(
                                """
                                UPDATE tickets
                                SET status = 'classified',
                                    category = ?,
                                    priority = ?,
                                    summary = ?,
                                    last_error = NULL
                                WHERE id = ?
                                """,
                                (
                                    result.category,
                                    result.priority,
                                    result.summary,
                                    ticket_id
                                )
                            )

                except sqlite3.Error as exc:
                    write_failures += 1

                    # Exponential backoff, capped at 5 seconds.
                    delay = min(
                        0.5 * (2 ** (write_failures - 1)),
                        5.0
                    )

                    logger.warning(
                        "Could not save classification for %s: "
                        "%s; retrying in %.1f seconds",
                        ticket_id,
                        type(exc).__name__,
                        delay
                    )

                    await asyncio.sleep(delay)

                else:
                    logger.info(
                        "Ticket %s classified successfully",
                        ticket_id
                    )
                    break


            logger.info(
                "Ticket %s classified successfully",
                ticket_id
            )


# Lifespan is basically Startup and Shutdown of application.
#it is used to initialize the database, load environment variables, and start the background worker.
@asynccontextmanager
async def lifespan(app: FastAPI):

    # Initialize or migrate the SQLite database.
    with closing(sqlite3.connect(DB_PATH)) as db:
        with db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS tickets (
                    id TEXT PRIMARY KEY,
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                )
                """
            )

            # Inspect existing database columns.
            columns = {
                row[1]
                for row in db.execute(
                    "PRAGMA table_info(tickets)"
                )
            }

            # Add the classification columns.
            new_columns = {
                "category": "TEXT",
                "priority": "TEXT",
                "summary": "TEXT",
                "attempts": "INTEGER NOT NULL DEFAULT 0",
                "last_error": "TEXT"
            }

            for name, definition in new_columns.items():
                if name not in columns:
                    db.execute(
                        f"ALTER TABLE tickets "
                        f"ADD COLUMN {name} {definition}"
                    )

            # Recover tickets that exhausted their attempts.
            # This runs on every application start or restart.
            db.execute(
                """
                UPDATE tickets
                SET status = 'failed',
                    last_error = 'Retry limit reached'
                WHERE status = 'pending'
                  AND attempts >= ?
                """,
                (MAX_ATTEMPTS,)
            )

    #Load the OpenAI API key.
    load_dotenv()

    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError(
            "OPENAI_API_KEY is not configured"
        )

    # Wworker manages retries.
    client = AsyncOpenAI(
        max_retries=0,
        timeout=20.0
    )

    # Start single background worker.
    worker_task = asyncio.create_task(
        ticket_worker(client)
    )

    try:
        yield

    finally:
        # Stop the worker during application shutdown.
        worker_task.cancel()

        try:
            await worker_task
        except asyncio.CancelledError:
            pass

        await client.close()


app = FastAPI(lifespan=lifespan)

# Ticket Input Model
class TicketCreate(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    subject: str = Field(max_length=200)
    body: str = Field(max_length=5000)


@app.get("/health")
def health_check():
    return {"status": "ok"}


# Create Ticket

@app.post("/tickets", status_code=202)
def create_ticket(
    ticket: TicketCreate,
    response: Response,
    _: None = Depends(enforce_rate_limit)
):

    with closing(sqlite3.connect(DB_PATH)) as db:
        with db:
            cursor = db.execute(
                """
                INSERT INTO tickets (id, subject, body)
                VALUES (?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    ticket.id,
                    ticket.subject,
                    ticket.body
                )
            )

            # The primary key prevented a duplicate insert.
            if cursor.rowcount == 0:

                existing = db.execute(
                    """
                    SELECT subject, body, status
                    FROM tickets
                    WHERE id = ?
                    """,
                    (ticket.id,)
                ).fetchone()

                if existing is None:
                    raise HTTPException(
                        status_code=503,
                        detail="Could not retrieve existing ticket"
                    )

                # Reject the same id POST with different content.
                if (existing[0], existing[1]) != (
                    ticket.subject,
                    ticket.body
                ):
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Ticket ID already exists "
                            "with different content"
                        )
                    )

                # Identical duplicate: no reclassification.
                response.status_code = 200

                return {
                    "id": ticket.id,
                    "status": existing[2],
                    "created": False
                }

    # New ticket is persisted as pending.
    return {
        "id": ticket.id,
        "status": "pending",
        "created": True
    }

# Retrieve Single Ticket

@app.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: str):

    with closing(sqlite3.connect(DB_PATH)) as db:
        db.row_factory = sqlite3.Row

        ticket = db.execute(
            """
            SELECT id, subject, body, status,
                   category, priority, summary,
                   attempts, last_error
            FROM tickets
            WHERE id = ?
            """,
            (ticket_id,)
        ).fetchone()

    if ticket is None:
        raise HTTPException(
            status_code=404,
            detail="Ticket not found"
        )

    return dict(ticket)

# List Tickets with Filtering and Pagination
@app.get("/tickets")
def list_tickets(
    category: Literal[
        "billing",
        "technical",
        "account",
        "other"
    ] | None = None,

    priority: Literal[
        "low",
        "medium",
        "high"
    ] | None = None,

    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):

    conditions = []
    parameters = []

    # Optional category filter.
    if category is not None:
        conditions.append("category = ?")
        parameters.append(category)

    # Optional priority filter.
    if priority is not None:
        conditions.append("priority = ?")
        parameters.append(priority)

    where_clause = (
        " WHERE " + " AND ".join(conditions)
        if conditions
        else ""
    )

    with closing(sqlite3.connect(DB_PATH)) as db:
        db.row_factory = sqlite3.Row

        # Total matching records before pagination.
        total = db.execute(
            "SELECT COUNT(*) FROM tickets" + where_clause,
            parameters
        ).fetchone()[0]

        # Retrieve a single page of matching records.
        rows = db.execute(
            """
            SELECT id, subject, body, status,
                   category, priority, summary,
                   attempts, last_error
            FROM tickets
            """
            + where_clause
            + " ORDER BY id ASC LIMIT ? OFFSET ?",
            [*parameters, limit, offset]
        ).fetchall()

    return {
        "items": [dict(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset
    }
