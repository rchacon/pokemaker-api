# pokemaker-api

Async Python backend for **PokeMaker**, a family trading-card creator. FastAPI +
Pydantic v2 on top of DynamoDB and S3. It runs as one long-lived Uvicorn process.

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

# 3. Local AWS stand-ins: DynamoDB Local on :8001, MinIO on :9000 (console :9001)
docker compose up -d

# 4. Create the table and bucket, seed profiles kid1/kid2 and the sample card (idempotent)
python scripts/init_local.py

# 5. Run the API on http://localhost:8000 (docs at /api/docs)
uvicorn pokemaker.main:app --reload
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

### Tests

```bash
pytest                    # ~20 s; moto emulates AWS in-process, no Docker needed
ruff check . && ruff format --check .
```

To run the same suite against DynamoDB Local and MinIO instead of moto:

```bash
TEST_AWS_ENDPOINT_URL=http://localhost:8001 TEST_S3_ENDPOINT_URL=http://localhost:9000 pytest
```

Each test creates its own `test-*` table and bucket.

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
| POST | `/api/profiles/{profileId}/cards/{cardId}/duplicate` | Copies fields and artwork 🔒 |
| GET | `/api/health` | Checks DynamoDB and S3 concurrently |

🔒 marks routes that need `Authorization: Bearer <unlock token>` when the profile
has a PIN: create, update, delete, duplicate, and change PIN.
Reading is always open.

Errors always look like:

```json
{"error": {"code": "VERSION_CONFLICT", "message": "...", "details": {"current": {...}}}}
```

Notable codes: `VALIDATION_ERROR` 422, `PROFILE_NOT_FOUND`/`CARD_NOT_FOUND` 404,
`VERSION_CONFLICT` 409 (carries the current card), `PAYLOAD_TOO_LARGE` 413,
`PIN_REQUIRED` 401, `WRONG_PIN` 401 (with `attemptsLeft`), `PROFILE_LOCKED` 423
(with `retryAfterSeconds`).

---

## Layout

```text
src/pokemaker/         The `pokemaker` package (src layout: imports never mention `src`)
├── main.py            FastAPI app, lifespan, error handlers, middleware order
├── config.py          Settings from env
├── dependencies.py    build_services(): opens/closes all clients; FastAPI deps
├── routes/            Thin HTTP handlers
├── models/            Pydantic models (camelCase on the wire and in DynamoDB)
├── services/          Business logic: cards and PINs
├── repositories/      DynamoDB access: conditional writes
├── integrations/      aioboto3 DynamoDB and S3 clients
└── utils/             DynamoDB keys, clock, errors, JSON logging, body-size limit
scripts/
├── init_local.py              Create local table + bucket, then seed
├── seed_profiles.py           Seed profiles and the "Layla" sample card (local or AWS)
└── reset_pin.py               Remove a profile's PIN (e.g. a parent forgot theirs)
```

---

## Data model (single table `pokemaker`, on-demand)

| Item | PK | SK |
| --- | --- | --- |
| Profile | `FAMILY#belchers` | `PROFILE#kid1` |
| Card | `FAMILY#belchers#PROFILE#kid1` | `CARD#<uuid>` |

- **Cards** carry `version` (an edit counter for optimistic concurrency), `cardNumber`
  (from an atomic per-profile counter, `cardCounter`, on the profile item),
  `artworkKey`/`artworkVersion`, and an `artworkSeq` counter that allocates S3 versions.
- **Artwork** lives in a private bucket at
  `families/belchers/profiles/kid1/cards/<cardId>/artwork/v<n>.<ext>`. The API returns
  short-lived presigned URLs (`artworkUrl`). Image bytes are never stored in DynamoDB.
- **Future Cognito:** the family id is ours and is never a Cognito `sub`. Users will
  map to families with membership items (`PK = USER#<sub>, SK = FAMILY#belchers`,
  `role = OWNER`), so no card keys need migrating.

---

## Async design notes

This codebase is also a place to practise `asyncio`. The patterns used, and why:

| Where | Pattern | Why |
| --- | --- | --- |
| All DynamoDB/S3 calls | `aioboto3`/`aiobotocore` | I/O-bound: while waiting on the network, the event loop serves other requests |
| `CardService.list_cards` | `asyncio.TaskGroup` | The profile check and the card query are independent, so latency is max(a, b) rather than a + b. TaskGroup cancels the sibling if one fails |
| `/api/health`, `init_local.py`, seeding | `asyncio.gather` | Independent checks and setup steps |
| `CardService.delete_card` | Sequential, on purpose | Delete the card (the source of truth) before its artwork. Running them concurrently could orphan a card or lose artwork |
| `dependencies.build_services` | `AsyncExitStack` + lifespan | Every client is closed in reverse order, even if startup fails halfway |

**Concurrency vs. parallelism.** `asyncio` interleaves many waiting tasks on one
thread. That makes I/O-heavy code fast, but it never runs two pieces of Python at
the same moment. CPU-heavy work needs processes.

**Where it pays off.** Production runs a single Uvicorn process. Its lifespan runs
once, so every client and connection pool is created once and shared, and the
event loop interleaves many requests that are mostly waiting on DynamoDB or S3:
one child's autosaves and another's gallery loads flow on the same loop.

**What asyncio does *not* solve here: cross-process races.** Two processes (a
restarted container, a second server, your dev server against the same table)
share no memory, so an `asyncio.Lock` or an in-memory counter can't prevent lost
updates or duplicate card numbers. Every correctness rule lives in a DynamoDB
conditional write or transaction:

- Card edits: `ConditionExpression: version = :expected`. The loser gets `409` with
  the current card.
- Card numbers: an atomic `ADD` counter.

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

- Anyone who learns the API's address can call it: list profiles, and read, edit
  or delete any card.
- **Choosing a profile in the app is a convenience, not authorization.** The API
  checks that a profile belongs to the configured family (data integrity), not
  *who* is asking.
- An obscure URL is not a security mechanism.
- Profile PINs stop a sibling from changing someone else's cards in the app,
  but they don't hide anything (reads are open). With only 10,000 possible PINs,
  they resist guessing only because of the attempt limit.

What *is* in place: strict Pydantic validation, request-size limits, private S3
with short-lived presigned URLs, and least-privilege IAM. Collect as little about
the children as possible: use nicknames, never real names, schools or photos.

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
| `PIN_TOKEN_SECRET` | **required** | ≥ 32 chars; signs unlock tokens |
| `PIN_TOKEN_TTL_MINUTES` / `PIN_MAX_ATTEMPTS` / `PIN_LOCKOUT_MINUTES` | `120` / `5` / `5` | |
| `CORS_ALLOW_ORIGINS` | `["http://localhost:5173"]` | Leave empty in AWS (same origin via CloudFront) |
