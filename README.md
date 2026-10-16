# pokemaker-api

Async Python backend for **PokeMaker**, a family trading-card creator. FastAPI +
Pydantic v2 on top of DynamoDB. It runs as one long-lived Uvicorn process.

Sibling repositories: [`pokemaker-app`](../pokemaker-app) (React frontend) and
[`pokemaker-infra`](../pokemaker-infra) (Terraform).

---

## Quick start (local, no AWS account needed)

Prerequisites: Python 3.12+, Docker with Compose.

```bash
# 1. Python environment
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 2. Configuration (defaults point at the local containers)
cp .env.example .env

# 3. Local AWS stand-in: DynamoDB Local on :8001
docker compose up -d

# 4. Create the table (idempotent)
python scripts/init_local.py

# 5. Run the API on http://localhost:8000 (docs at /api/docs)
uvicorn pokemaker.main:app --reload
```

### Tests

```bash
pytest                    # moto emulates AWS in-process, no Docker needed
ruff check . && ruff format --check .
```

To run the same suite against DynamoDB Local instead of moto:

```bash
TEST_AWS_ENDPOINT_URL=http://localhost:8001 pytest
```

Each test creates its own `test-*` table.

---

## API

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/health` | Checks DynamoDB |

Errors always look like:

```json
{"error": {"code": "VALIDATION_ERROR", "message": "...", "details": {...}}}
```

---

## Layout

```text
src/pokemaker/         The `pokemaker` package (src layout: imports never mention `src`)
├── main.py            FastAPI app, lifespan, error handlers
├── config.py          Settings from env
├── dependencies.py    build_services(): opens/closes all clients; FastAPI deps
├── integrations/      aioboto3 DynamoDB client
└── utils/             Errors, JSON logging
scripts/
└── init_local.py      Create the local table
```

---

## Async design notes

This codebase is also a place to practise `asyncio`. The patterns used, and why:

| Where | Pattern | Why |
| --- | --- | --- |
| All DynamoDB calls | `aioboto3`/`aiobotocore` | I/O-bound: while waiting on the network, the event loop serves other requests |
| `dependencies.build_services` | `AsyncExitStack` + lifespan | Every client is closed in reverse order, even if startup fails halfway |

**Concurrency vs. parallelism.** `asyncio` interleaves many waiting tasks on one
thread. That makes I/O-heavy code fast, but it never runs two pieces of Python at
the same moment. CPU-heavy work needs processes.

**Where it pays off.** Production runs a single Uvicorn process. Its lifespan runs
once, so every client and connection pool is created once and shared, and the
event loop interleaves many requests that are mostly waiting on the network.

---

## Configuration

See `.env.example`. The most important variables:

| Variable | Default | Notes |
| --- | --- | --- |
| `FAMILY_ID` | **required** | Server-configured tenant (e.g. `belchers`); no default, so a missing value fails at startup |
| `TABLE_NAME` | `pokemaker` | |
| `DYNAMODB_ENDPOINT_URL` | unset | Local container only |
| `CORS_ALLOW_ORIGINS` | `["http://localhost:5173"]` | Leave empty in AWS (same origin via CloudFront) |
