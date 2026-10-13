# pokemaker-api

Async Python backend for **PokeMaker**, a family trading-card creator. FastAPI +
Pydantic v2 on top of DynamoDB, S3 and the OpenAI Images API. It runs locally with
Uvicorn, and in AWS Lambda through Mangum, using the same code.

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

# 2. Configuration (defaults point at the local containers; mock artwork)
cp .env.example .env

# 3. Local AWS stand-ins: DynamoDB Local on :8001, MinIO on :9000 (console :9001)
docker compose up -d

# 4. Create the table and bucket, seed profiles kid1/kid2 and the sample card (idempotent)
python scripts/init_local.py

# 5. Run the API on http://localhost:8000 (docs at /api/docs)
uvicorn src.main:app --reload
```

Then start the frontend in `../pokemaker-app` (`npm install && npm run dev`) and open
http://localhost:5173.

Optional: give the profiles nicknames and avatars (avatars: `dragon`, `lightning`,
`rocket`, `star`, `fire`, `water`, `leaf`, `paw`):

```bash
python scripts/seed_profiles.py --name kid1="Captain Cards" --avatar kid1=rocket
```

The same command adds more players, e.g. a parent:
`python scripts/seed_profiles.py --name dad="Dad" --avatar dad=rocket` (ids are
lowercase letters, digits and hyphens; the avatar defaults to `star`).

Existing profiles are never overwritten, so to rename one after it was created,
`docker compose down -v` (wipes all local data) and re-run `init_local.py`.

### Real AI artwork locally

The default `ARTWORK_PROVIDER=mock` draws a free, deterministic placeholder creature.
To use OpenAI (this costs money):

```bash
# in .env
ARTWORK_PROVIDER=openai
OPENAI_API_KEY=sk-...
```

### Tests

```bash
pytest                    # ~20 s; moto emulates AWS in-process, no Docker needed
ruff check . && ruff format --check .
```

To run the same suite against DynamoDB Local and MinIO instead of moto:

```bash
TEST_AWS_ENDPOINT_URL=http://localhost:8001 TEST_S3_ENDPOINT_URL=http://localhost:9000 pytest
```

Each test creates its own `test-*` table and bucket. OpenAI is never called: the
OpenAI integration is tested against a fake HTTP transport.

---

## API

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/profiles` | The family's profiles (`role`, `hasPin`) |
| POST | `/api/profiles/{profileId}/unlock` | `{pin}` → short-lived unlock token (a parent's PIN works too) |
| PUT | `/api/profiles/{profileId}/pin` | Set the first PIN (open) or change it (needs unlock) |
| GET | `/api/profiles/{profileId}/cards` | One child's collection, by card number |
| POST | `/api/profiles/{profileId}/cards` | Create; assigns `cardNumber` 🔒 |
| GET | `/api/profiles/{profileId}/cards/{cardId}` | |
| PUT | `/api/profiles/{profileId}/cards/{cardId}` | Body includes `version` (optimistic concurrency) 🔒 |
| DELETE | `/api/profiles/{profileId}/cards/{cardId}` | Also deletes the card's artwork 🔒 |
| POST | `/api/profiles/{profileId}/cards/{cardId}/duplicate` | Copies fields and artwork; no quota 🔒 |
| POST | `/api/profiles/{profileId}/cards/{cardId}/artwork` | Requires `Idempotency-Key` header (UUID) 🔒 |
| GET | `/api/artwork/requests/{requestId}` | Status of a generation (for polling) |
| GET | `/api/artwork/quota` | `{date, used, limit, remaining, resetsAt}` |
| GET | `/api/health` | Checks DynamoDB and S3 concurrently |

🔒 marks routes that need `Authorization: Bearer <unlock token>` when the profile
has a PIN: create, update, delete, duplicate, generate artwork, and change PIN.
Reading is always open.

Errors always look like:

```json
{"error": {"code": "VERSION_CONFLICT", "message": "...", "details": {"current": {...}}}}
```

Notable codes: `VALIDATION_ERROR` 422, `PROFILE_NOT_FOUND`/`CARD_NOT_FOUND` 404,
`VERSION_CONFLICT` 409 (carries the current card), `IDEMPOTENCY_KEY_REUSED` 409,
`QUOTA_EXCEEDED` 429, `ARTWORK_REJECTED` 422, `ARTWORK_PROVIDER_ERROR` 502,
`ARTWORK_TIMEOUT` 504, `PAYLOAD_TOO_LARGE` 413, `PIN_REQUIRED` 401, `WRONG_PIN` 401
(with `attemptsLeft`), `PROFILE_LOCKED` 423 (with `retryAfterSeconds`).

---

## Layout

```text
src/
├── main.py            FastAPI app, lifespan, error handlers, Mangum `handler`
├── config.py          Settings from env (model/size/quality allowlists)
├── dependencies.py    build_services(): opens/closes all clients; FastAPI deps
├── routes/            Thin HTTP handlers
├── models/            Pydantic models (camelCase on the wire and in DynamoDB)
├── services/          Business logic: cards, quota, artwork state machine
├── repositories/      DynamoDB access: conditional writes and transactions
├── integrations/      aioboto3 DynamoDB/S3, AsyncOpenAI, mock artwork provider
└── utils/             DynamoDB keys, clock, errors, JSON logging, body-size limit
scripts/
├── init_local.py              Create local table + bucket, then seed
├── seed_profiles.py           Seed profiles and the "Layla" sample card (local or AWS)
├── reconcile_generations.py   Settle generation requests stuck mid-flight
├── reset_pin.py               Remove a profile's PIN (e.g. a parent forgot theirs)
└── build_lambda.sh            Build dist/lambda.zip (arm64, python3.12)
```

---

## Data model (single table `pokemaker`, on-demand)

| Item | PK | SK |
| --- | --- | --- |
| Profile | `FAMILY#belchers` | `PROFILE#kid1` |
| Card | `FAMILY#belchers#PROFILE#kid1` | `CARD#<uuid>` |
| Daily quota | `FAMILY#belchers` | `QUOTA#ARTWORK#2026-10-13` |
| Generation request | `FAMILY#belchers` | `GENERATION#<requestId>` |

- **Cards** carry `version` (an edit counter for optimistic concurrency), `cardNumber`
  (from an atomic per-profile counter, `cardCounter`, on the profile item),
  `artworkKey`/`artworkVersion`, and an `artworkSeq` counter that allocates S3 versions.
- **Artwork** lives in a private bucket at
  `families/belchers/profiles/kid1/cards/<cardId>/artwork/v<n>.<ext>` (`.webp` from
  OpenAI, `.png` from the mock). The API returns short-lived presigned URLs
  (`artworkUrl`). Image bytes are never stored in DynamoDB.
- **Quota and generation items** have an `expiresAt` TTL attribute, used for cleanup
  only. The quota resets because each UTC day has its own item, not because of TTL.
- **Future Cognito:** the family id is ours and is never a Cognito `sub`. Users will
  map to families with membership items (`PK = USER#<sub>, SK = FAMILY#belchers`,
  `role = OWNER`), so no card keys need migrating.

---

## Async design notes

This codebase is also a place to practise `asyncio`. The patterns used, and why:

| Where | Pattern | Why |
| --- | --- | --- |
| All DynamoDB/S3 calls | `aioboto3`/`aiobotocore` | I/O-bound: while waiting on the network, the event loop serves other requests |
| `integrations/openai.py` | `AsyncOpenAI` on an `httpx2.AsyncClient` | A 10–60 s wait that must not block the loop. openai-python 3.x uses `httpx2`, the successor to `httpx`; tests use `httpx.AsyncClient` + `ASGITransport` |
| `CardService.list_cards` | `asyncio.TaskGroup` | The profile check and the card query are independent, so latency is max(a, b) rather than a + b. TaskGroup cancels the sibling if one fails |
| `/api/health`, `init_local.py`, seeding | `asyncio.gather` | Independent checks and setup steps |
| `CardService.delete_card` | Sequential, on purpose | Delete the card (the source of truth) before its artwork. Running them concurrently could orphan a card or lose artwork |
| `ArtworkService._run` | `asyncio.timeout()` | Bounds the whole provider call, independent of HTTP-client timeouts |
| `ArtworkService._run` | `except CancelledError: await asyncio.shield(...); raise` | Records what happened without a second cancellation interrupting the bookkeeping, then re-raises. Never swallow `CancelledError` |
| Mock provider rendering | `asyncio.to_thread` | Keeps CPU work off the loop. This is concurrency, not parallelism: the GIL still runs one thread's Python at a time |
| `dependencies.build_services` | `AsyncExitStack` + lifespan | Every client is closed in reverse order, even if startup fails halfway |

**Concurrency vs. parallelism.** `asyncio` interleaves many waiting tasks on one
thread. That makes I/O-heavy code fast, but it never runs two pieces of Python at
the same moment. CPU-heavy work needs processes, or more Lambda instances.

**What asyncio does *not* solve here: cross-process races.** Two Lambda instances
share no memory, so an `asyncio.Lock` or an in-memory counter can't enforce a
quota or prevent lost updates. Every correctness rule lives in a DynamoDB
conditional write or transaction:

- Card edits: `ConditionExpression: version = :expected`. The loser gets `409` with
  the current card.
- Card numbers and artwork versions: atomic `ADD` counters.
- Quota: one transaction that inserts the generation record (`attribute_not_exists`)
  **and** increments the day's count (`generationCount < :limit`).
- Generation state changes: compare-and-swap on `status`.
- Attaching artwork: `artworkVersion < :new`, so a slow, older generation can't
  overwrite a newer one. Artwork attachment doesn't bump the card's edit `version`,
  so a generation that finishes mid-edit never causes an autosave conflict.

**Lambda specifics.** Mangum runs the lifespan around every invocation, because
async clients are bound to the event loop that created them. Botocore's parsed
service models and the SSM-fetched OpenAI key are cached at module level, purely
as performance caches. There are no `BackgroundTasks` or detached tasks: Lambda
may freeze the process the moment a response is returned. If PokeMaker ever needs
durable background work (for example batch generation), send it to **SQS**, with a
worker Lambda, rather than spawning tasks.

---

## Artwork generation: state machine and failure policy

```text
           reserve (transaction: record + quota slot)
                         │
                         ▼
   PENDING ──────► CALLING_PROVIDER ──────► STORING ──────► SUCCEEDED
      │                  │                     │
      ▼                  ▼                     ▼
   FAILED             FAILED                FAILED
  (slot RELEASED)  (RELEASED or CONSUMED)  (CONSUMED)
```

The OpenAI call and DynamoDB cannot be made atomic together, so every step is
recorded and each failure decides explicitly what happens to the quota slot:

| What happened | Status / slot | Retry? |
| --- | --- | --- |
| Invalid input, unknown card, empty description | Nothing reserved | – |
| Quota exhausted | `429`, nothing reserved | Tomorrow (UTC) |
| Same `Idempotency-Key` again | Original result returned (`202` if still running) | Never re-run |
| OpenAI refused the prompt (400) | FAILED / **RELEASED** | Child can rephrase |
| OpenAI error response (401/429/5xx) | FAILED / **RELEASED** | Child can press again |
| Timeout, dropped connection, cancellation | FAILED / **CONSUMED** (may have been billed) | **No automatic retry** |
| S3 upload failed (3 attempts; idempotent PUT) | FAILED / CONSUMED | – |
| Card deleted mid-generation | FAILED / CONSUMED; orphaned object deleted | – |
| Process died mid-flight | Reconciled once older than 5 min: PENDING → RELEASED, later states → CONSUMED | – |

The OpenAI SDK runs with `max_retries=0`, because an SDK retry after a timeout
could pay twice.

**API Gateway's 30-second limit.** HTTP APIs cut responses off at 30 s, but image
generation can take longer. The Lambda keeps running after API Gateway gives up
(Lambda timeout: 120 s), finishes, and records the result. The frontend then polls
`GET /api/artwork/requests/{requestId}` with the same id. Stale records are
reconciled whenever they are read, and `scripts/reconcile_generations.py` sweeps
all of them.

---

## Cost controls

- Server-side daily quota (`DAILY_GENERATION_LIMIT`, default 10 per family per UTC
  day), enforced atomically across all Lambda instances.
- Model, size and quality are fixed server-side and validated against allowlists
  (`gpt-image-1-mini`, medium quality, 1536×1024, `n=1`). The client can't change them.
- Descriptions are capped at 400 characters, request bodies at 32 KB, and image
  prompts are built server-side.
- Every generation is logged as JSON with token usage and
  `OPENAI_ESTIMATED_COST_USD`. This is an estimate: check
  https://openai.com/api/pricing.
- **In the OpenAI dashboard:** use a dedicated project with a low monthly budget
  and email alerts. Budgets and alerts are *not* a guaranteed hard cap, which is
  why the server-side quota exists.
- API Gateway throttling and Lambda reserved concurrency are set in `pokemaker-infra`.

---

## Profile PINs

PINs keep siblings out of each other's collections. Anyone can *look* at any
collection; *changing* one needs its 4-digit PIN.

- **Setting:** a profile's first PIN can be set by anyone (in the app: "Add a
  PIN"). After that, changing it needs the profile unlocked.
- **Parents:** profiles with `role = parent` (`seed_profiles.py --parent dad`)
  have a master PIN that unlocks any profile, which is how a forgotten kid's PIN
  gets reset. A parent who forgets their own PIN runs `python scripts/reset_pin.py dad`.
- **Unlocking** (`POST …/unlock`) returns an HMAC-signed token valid for 2 hours.
  It names the family, the profile and the profile's `pinVersion`, so changing a
  PIN signs out every device. The app keeps tokens per browser tab and drops
  them whenever someone switches player.
- **Guessing:** each attempt is counted with an atomic DynamoDB `ADD` *before*
  the PIN is checked, so even 20 simultaneous guesses get at most 5 checks.
  After 5 wrong tries the profile locks for 5 minutes.
- **Storage:** PINs are hashed with scrypt (salted, ~16 MB, ~50–80 ms). Hashing
  runs in `asyncio.to_thread`, and because `hashlib.scrypt` releases the GIL,
  checking the profile's PIN and the parents' PINs in parallel threads really
  is parallel. That's an exception to the usual "threads don't speed up CPU
  work in Python" rule.

PINs are a household guard, not account security: see below.

## Security and MVP limitations

**There is no authentication in this MVP.** Be clear-eyed about what that means:

- Anyone who learns the API's address can call it: list profiles, read, edit or
  delete any card, and use up the day's artwork quota.
- **Choosing a profile in the app is a convenience, not authorization.** The API
  checks that a profile belongs to the configured family (data integrity), not
  *who* is asking.
- An obscure URL is not a security mechanism.
- Quotas and throttling limit cost and abuse; they provide no privacy.
- Profile PINs stop a sibling from changing someone else's cards in the app,
  but they don't hide anything (reads are open). With only 10,000 possible PINs,
  they resist guessing only because of the attempt limit.

What *is* in place: strict Pydantic validation, server-side quotas, request-size
limits, private S3 with short-lived presigned URLs, least-privilege IAM, and the
OpenAI key kept in SSM Parameter Store (never in the frontend, Terraform state or
git). Collect as little about the children as possible: use nicknames, never real
names, schools or photos.

Before inviting other families, add Cognito, map users to families with membership
items, and authorize every request against the caller's family. The key design
already allows this.

---

## Configuration

See `.env.example`. The most important variables:

| Variable | Default | Notes |
| --- | --- | --- |
| `FAMILY_ID` | **required** | Server-configured tenant (e.g. `belchers`); no default, so a missing value fails at startup |
| `TABLE_NAME` / `ARTWORK_BUCKET` | `pokemaker` / `pokemaker-artwork-local` | |
| `DYNAMODB_ENDPOINT_URL`, `S3_ENDPOINT_URL`, `S3_PUBLIC_ENDPOINT_URL` | unset | Local containers only |
| `ARTWORK_PROVIDER` | `mock` | `mock` or `openai` |
| `OPENAI_API_KEY` | unset | Local only; Lambda uses `OPENAI_API_KEY_SSM_PARAMETER` |
| `OPENAI_IMAGE_MODEL` / `_QUALITY` / `_SIZE` | `gpt-image-1-mini` / `medium` / `1536x1024` | Allowlisted |
| `OPENAI_TIMEOUT_SECONDS` | `90` | |
| `DAILY_GENERATION_LIMIT` | `10` | Family-wide, per UTC day |
| `PIN_TOKEN_SECRET` | **required** | ≥ 32 chars; signs unlock tokens. Lambda uses `PIN_TOKEN_SECRET_SSM_PARAMETER` |
| `PIN_TOKEN_TTL_MINUTES` / `PIN_MAX_ATTEMPTS` / `PIN_LOCKOUT_MINUTES` | `120` / `5` / `5` | |
| `CORS_ALLOW_ORIGINS` | `["http://localhost:5173"]` | Leave empty in AWS (same origin via CloudFront) |

## Deploying

```bash
./scripts/build_lambda.sh   # → dist/lambda.zip
```

Then apply `pokemaker-infra`, which points at this zip by default. To seed profiles
in AWS, run `seed_profiles.py` without the local endpoint variables, with AWS
credentials and `TABLE_NAME` set to the deployed table.
