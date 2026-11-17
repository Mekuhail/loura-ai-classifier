# Loura AI Ticket Classification Service

A small Python HTTP service for accepting customer support tickets and classifying them asynchronously with the OpenAI API. Built for the Loura Engineering Take-Home, with an emphasis on correctness, a clear model boundary, and explicit trade-offs—not production-scale infrastructure.

## Quick start

Requires Python 3.13 and an OpenAI API key authorized for the configured model (`gpt-6-luna`). API requests incur charges.

```bash
python3 -m venv .venv
source .venv/bin/activate                 # macOS/Linux
python -m pip install -r requirements.txt
cp .env.example .env
# Edit .env and set OPENAI_API_KEY to your own secret key.
python -m uvicorn main:app
```

Open http://127.0.0.1:8000/docs to try the API. The local SQLite database is created/updated on startup. Run the server as **one process** (do not use multiple Uvicorn workers); it also starts the single classification worker.

## API

| Method | Route | Behavior |
| --- | --- | --- |
| `GET` | `/health` | Health response (`{"status": "ok"}`); does not independently verify worker health |
| `POST` | `/tickets` | Accept a ticket with `id`, `subject`, `body`; return `202` for a new pending ticket, `200` for identical duplicate, `409` for same ID with different content |
| `GET` | `/tickets/{ticket_id}` | Return one ticket or `404` |
| `GET` | `/tickets` | Filter by optional `category` and/or `priority`; paginate with `limit` (1–100) and `offset` |

An example request body:

```json
{"id": "t-1001", "subject": "Charged twice", "body": "I was charged twice for my subscription."}
```

The supported categories are `billing`, `technical`, `account`, `other`. Priorities are `low`, `medium`, `high`. Ticket state is `pending`, `classified`, or `failed`.

A lightweight per-client-IP in-memory sliding-window rate limiter restricts `POST /tickets` to **20 requests per 60 seconds**; excess requests receive `429` and a `Retry-After` header. It is appropriate for the single-process take-home, not distributed or proxy-aware deployment.

## Architecture and lifecycle

```text
Client POST /tickets
    -> FastAPI + Pydantic input validation
    -> SQLite insert with ID as PRIMARY KEY (duplicate-safe)
    -> HTTP 202 while ticket remains pending

Application-started background worker (one at a time)
    -> poll SQLite for pending tickets
    -> transactionally record the next attempt
    -> OpenAI Responses API with a structured Pydantic output schema
    -> validate category, priority, and nonblank single-line summary
    -> persist classified result, or record failure / bounded retry

GET /tickets... reads the persisted state; no OpenAI request is made by GET.
```

The worker is started through FastAPI's lifespan hook and runs independently of ticket creation. Classification does **not** happen inside `POST /tickets`. A maximum of **three attempts** is recorded per ticket; short backoffs prevent immediate repeated requests. Pending work survives restarts because the queue is represented by database state, not only an in-memory task. At startup, pending tickets that have already reached the limit are marked failed.

A successful model response that encounters a temporary SQLite write error is retried in-memory without repeating inference. This does not survive a process crash: the model result may be lost before it is saved. The current single-worker design makes no exactly-once inference guarantee.

## Why this stack?

- **FastAPI:** compact HTTP endpoints, request validation, and automatically generated documentation.
- **Pydantic:** strongly typed input and output contracts, with allowed category/priority values and a summary validator.
- **SQLite:** durable transactional persistence and primary-key uniqueness without an external server.
- **`asyncio`:** one background task capable of waiting on OpenAI without blocking request completion; avoids a separate broker for the take-home.
- **OpenAI Responses API:** real model integration using structured output; local validation still matters because a well-formed response can be semantically wrong.
- **pytest:** isolated regression testing with temporary databases and mocked model calls.

## Tests

```bash
python -m pytest -q
```

The tests exercise output-schema rejection, blank summaries, malformed inputs, worker failures and retry limits, recovery of pending work, duplicate submission behavior, malicious ticket strings, and HTTP rate limiting. Mocked model calls avoid paid requests for these regression checks. Some scenarios are simulated rather than full process-crash or multi-process tests.

## Sample tickets and evaluation

The ten appendix examples are in `samples/sample_tickets.json`. Developer-assigned reference labels are in `samples/reference_labels.json`. The evaluation program directly calls the classifier rather than adding evaluation tickets to SQLite:

```bash
python evaluate.py
```

**This command makes real, billable API requests.** On one run with `gpt-6-luna`, predictions matched the developer's reference labels on **10/10 categories** and **10/10 priorities**, with zero request errors. In the provided prompt-injection case (`t-1005`), the model classified the actual invoice-download question rather than following forged CEO instructions.

This small, manually labeled dataset is **not** evidence of 100% production accuracy. Summary quality was inspected manually. A larger, independently labeled evaluation set would be needed to estimate generalization or robustness to novel attacks.

## Security boundaries

- Public ticket subject/body are treated as **data**, never executed as Python/shell code.
- SQLite uses bound parameters for untrusted values rather than SQL string interpolation.
- Model instructions are separate from ticket content. The model has no tool access or direct database permissions.
- Pydantic rejects malformed or out-of-range classification fields before persistence; tests cover several invalid responses.
- `.env` holds the API secret locally and is excluded from Git; `.env.example` contains only a placeholder.
- POST rate limiting reduces accidental or abusive ingestion, but does not replace authentication or a spending cap.

This local demonstration has **no authentication or authorization**, so it must not be exposed to the public internet with real customer tickets. A future frontend must escape displayed ticket text rather than render it as executable HTML.

## Deliberate limitations and trade-offs

- **Single application process and single classifier worker.** Multiple workers could claim the same pending ticket without stronger transactional coordination.
- **At-least-once inference attempts.** A crash after OpenAI returns but before persistence can result in another paid API call; recorded attempts are bounded.
- **Database errors.** Reads retry with a delay, and successful result writes retry in memory; some failure-status persistence paths may still need additional handling. Persistent SQLite failures can stall work.
- **Synchronous SQLite access inside the async worker.** Acceptable for this small workload but not appropriate for prolonged blocking operations under heavy load.
- **Rate limiter.** In-memory per-IP limits reset on restart, are not shared across instances, and have no inactive-client eviction mechanism.
- **No authentication, authorization, or production monitoring.** These would be essential in an internet-facing service.
- **Summary validation.** The validator rejects blank/multiline text but cannot guarantee exactly one grammatical sentence or factual correctness.
- **Evaluation.** Ten developer-labeled examples, one known adversarial sample; not representative of real-world traffic.

With more time, the next steps would be structured application logging/worker health checks, centralizing database schema setup in tests, finer-grained retries for transient vs permanent errors, stronger worker leasing for multiple instances, and a larger independent evaluation.

## Repository hygiene

Commit source code, tests, sample data, this README, `.gitignore`, `.env.example`, and pinned `requirements.txt` only. Do **not** commit `.env`, `.venv/`, `tickets.db`, caches, or a raw ZIP of the development directory.
