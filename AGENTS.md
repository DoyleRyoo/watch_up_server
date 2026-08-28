# AGENTS.md — watch_up_server (backend)

```yaml
repo: watch_up_server
role: backend API
stack: FastAPI, Python 3.14, Uvicorn, Pydantic, HTTPX, Redis, Supabase (Postgres + Auth)
entrypoint: uvicorn app.main:app   (compat: main.py)
sibling_repos: [watch_up_react (frontend), watch_up_infra (compose/CI/docs)]
```

## 0. RULE ZERO — DOUBLE CHECK IS MANDATORY

- Double check is required at **every** stage: before edit, after edit, before commit, before "done".
- Never mark work done on one pass. Run the two-pass protocol in §8.
- Applies to: code, migrations, tests, docs, config. No exception.

## 1. SPEC SOURCES (priority order, GOV-01)

| rank | doc | owns |
|---|---|---|
| 1 | `../watch_up_infra/docs/planning_v2.2.md` | policy, scope (single source of truth) |
| 2 | `../watch_up_infra/docs/planning_v2.2_ai.md` | same policy, AI-parse format, tags (SCOPE-01, PRICE-01, BUY-01 ...) |
| 3 | `../watch_up_infra/docs/WatchUp_v2.2_functions.md` | API + DB contracts (FE-BE-xx, BE-DB-xx, §0.x) |
| 4 | `../watch_up_infra/docs/develop_steps_v2.2/step_*.md` | build order, per-step scope, per-step double-check gate |
| 5 | actual code + applied migrations | — |

- Conflict → higher rank wins. Record lower-rank mismatch as `implementation defect` / `migration drift`. Do not edit policy to match code.
- Do only the current `step_*.md` scope. No pulling work forward. No invented endpoints / columns / aliases.

## 2. COMMANDS

| task | command |
|---|---|
| install (dev + test) | `python -m pip install -r requirements-dev.txt` |
| format check | `python -m ruff format --check app tests main.py` |
| format apply | `python -m ruff format app tests main.py` |
| lint | `python -m ruff check app tests main.py` |
| typecheck | `python -m mypy app` |
| test | `python -m pytest -q` |
| run local | `uvicorn app.main:app --reload` |
| dep sanity | `python -m pip check` |

- CI runs: format check → lint → typecheck → test → docker build → non-root + health check. All must pass.
- Runtime deps → `requirements.txt`. Dev/test deps → `requirements-dev.txt`. No `pyproject.toml`.

## 3. LAYOUT

```
app/
  api/
    dependencies/   auth, supabase client, service injection
    routes/         health, coins, (paper — v2.2 new)
    router.py       assembles /api sub-routers
  cache/            keys.py (key+TTL), lock.py (token lock), redis.py (async cache)
  clients/          supabase.py (per-request user client), upbit.py (shared async client)
  core/             config.py, errors.py (code↔HTTP), exceptions.py, security.py (JWKS JWT)
  models/           internal domain + cache models
  repositories/     persistence layer
  schemas/          external API + Upbit response schemas (APIModel = camelCase)
  services/         market_list, price, chart, (paper_* — v2.2 new)
  main.py           FastAPI factory + lifespan
tests/              unit / API / lifecycle
supabase/migrations/  SQL migrations
```

## 4. CONTRACT RULES

### Response envelope
| kind | shape |
|---|---|
| success | `{ "data": T, "meta": null }` |
| list | `{ "data": T[], "meta": { "count": int } }` — `count == len(data)`, enforced |
| error | `{ "error": { "code": str, "message": str, "details": any\|null } }` |

- JSON keys = camelCase (via `app/schemas/base.py` `APIModel`). Python + DB identifiers = snake_case.
- Source unchanged: `app/schemas/common.py`.

### Auth (AUTH-01 / AUTH_BOUNDARY)
- Bearer JWT on every endpoint except `GET /api/health`.
- `user_id` = verified JWT `sub` only. Never from body / query / header.
- JWKS verify: `exp`, `iss`, `aud`, `sub`. `app/core/security.py`.
- Missing/invalid → `AUTH_REQUIRED` (401). Expired → `AUTH_TOKEN_EXPIRED` (401).
- `SUPABASE_SERVICE_ROLE_KEY` forbidden in paper-trading code paths.
- RLS + app-level filter both required. Never assume ownership from one alone.

### Money (MONEY-01)
| value | DB type | Python type |
|---|---|---|
| cash / grant / top-up / delta / balance-after | `BIGINT` | `int` |
| price / quantity / cost / P&L | `NUMERIC(38,18)` | `Decimal` |

- `float` / `double` forbidden for money math.
- JSON out: all `NUMERIC` / `BIGINT` → decimal string. No JSON number. No scientific notation.
- `amountKrw` in: string `^[1-9][0-9]*$`, range `1 .. 9223372036854775807`. JSON number → `INVALID_REQUEST`.
- BUY quantity → `floor18` first. Other stored `NUMERIC(38,18)` → `ROUND_HALF_EVEN`.
- `floor18(x) = floor(x*10^18)/10^18` ; `floorKrw(x) = floor(x)`.
- Decimal context precision >= 80, normalize to 18 digits at DB boundary.

### Price / trade path (PRICE-01)
- Execution price = 1 direct Upbit Quotation ticker call, `max_retries=0`, **no Redis read**, no cache write.
- Upbit Exchange API = forbidden. Real order = never.
- `marketStatus` = only trade-block axis. `priceStatus` never blocks.
- Any price-fetch failure → trade fails. Zero change to account / position / transaction. No stale fallback on trade path.

### Idempotency (IDEM-01) — `POST /api/paper/top-ups`, `POST /api/paper/trades`
- `Idempotency-Key` header, UUID v4, required. Missing/malformed → `IDEMPOTENCY_KEY_REQUIRED` (400).
- Fingerprint: normalized body → canonical JSON → SHA-256 → stored.
- same key + same fingerprint → replay stored result, HTTP 200.
- same key + different fingerprint → `IDEMPOTENCY_KEY_REUSED` (409).
- Key space shared across both endpoints, unique per `(account_id, idempotency_key)`.
- Failed request → no row, retryable with same key.

### Transaction / lock (TX-01)
- Direct PostgreSQL tx, `READ COMMITTED`. Not via PostgREST / Supabase SDK for money writes.
- `SET LOCAL request.jwt.claim.sub` = verified user id → RLS `auth.uid()` resolves.
- Connection = session-mode (not transaction-mode PgBouncer). `SET LOCAL` must persist.
- Lock order fixed: `paper_accounts` `FOR UPDATE` → then `paper_positions` `FOR UPDATE`.
- Deadlock / serialization (SQLSTATE 40001 / 40P01) → `DATABASE_UNAVAILABLE` (503), no auto-retry, full rollback.

### Data model
- Exactly 3 new tables: `paper_accounts`, `paper_transactions`, `paper_positions`. DDL source = functions §7.
- `paper_transactions` = insert-only. No app UPDATE / DELETE while account exists (RETENTION-01).
- 1 trade request → 1 price fetch → 1 DB tx → 1 immutable transaction row. (+1 `INITIAL_GRANT` only on first account creation, same tx.)

### WATCHLIST_DEPRECATED
- Keep DB table + rows. No drop, no column add, no migrate into `paper_positions`.
- Remove: router registration, route/service/repo/model modules, app read/write.
- Keep shared code (e.g. `MARKET_CODE_PATTERN`) — relocate to a shared module.
- Migration: drop v1.5 RLS policies + `revoke ... from anon, authenticated`.
- Never add favorites / watchlist / heart / star / max-count API.

## 5. ERROR CODES (v2.2 new — functions §8; Korean message in `app/core/errors.py`)

| code | HTTP | trigger |
|---|---|---|
| `INVALID_REQUEST` | 400 | bad body, unknown field, wrong-side field, JSON number money |
| `INVALID_MARKET_CODE` | 400 | bad format or unknown market |
| `IDEMPOTENCY_KEY_REQUIRED` | 400 | missing / malformed header |
| `IDEMPOTENCY_KEY_REUSED` | 409 | same key, different fingerprint |
| `MARKET_NOT_TRADABLE` | 400 | `marketStatus == UNAVAILABLE` on BUY/SELL |
| `INSUFFICIENT_CASH_BALANCE` | 400 | `amountKrw > cash_balance_krw` on BUY |
| `INSUFFICIENT_HOLDING_QUANTITY` | 400 | `quantity > held` or no position on SELL |
| `TOP_UP_AMOUNT_OUT_OF_RANGE` | 400 | outside [1, 2,100,000,000] |
| `TOP_UP_LIFETIME_LIMIT_EXCEEDED` | 400 | cumulative TOP_UP > 100,000,000,000 |
| `DATABASE_UNAVAILABLE` | 503 | Postgres 40001 / 40P01 |
| `UPBIT_UNAVAILABLE` / `UPBIT_RATE_LIMITED` / `UPBIT_TEMPORARILY_BLOCKED` | 502 / 503 / 503 | Upbit failures (trade path: no stale fallback) |

## 6. API SURFACE (final v2.2 — lock to this exact set)

| method + path | note |
|---|---|
| `GET /api/health` | no auth, excluded from op count |
| `GET /api/coins/search` | KEPT |
| `GET /api/coins/{marketCode}/chart` | MODIFIED (add name, currentPrice, marketStatus, priceStatus) |
| `GET /api/paper/account` | NEW |
| `POST /api/paper/top-ups` | NEW, `Idempotency-Key` |
| `POST /api/paper/trades` | NEW, `Idempotency-Key`, BUY/SELL |
| `GET /api/paper/portfolio` | NEW, holdings where `quantity > 0` |
| `GET /api/paper/trades` | NEW, full history, cursor `limit`+`beforeId`. No `/api/paper/transactions` alias. |

- Removed: `GET/POST /api/watchlist`, `DELETE /api/watchlist/{id}` — not registered, absent from OpenAPI.
- Op count = distinct `(method, path)`. Target = 2 coin + 5 paper.

### CORS (`app/main.py`)
| phase | methods | headers |
|---|---|---|
| Nginx-free pre-deploy (step_1) | `GET, POST, DELETE, OPTIONS` | `Authorization, Content-Type` |
| final v2.2 (step_8) | `GET, POST, OPTIONS` | `Authorization, Content-Type, Idempotency-Key` |

- No wildcard origin. Origins from `CORS_ALLOWED_ORIGINS` env.

## 7. ENV VARS (`.env.example`)

`APP_ENV`, `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_JWKS_URL`, `SUPABASE_ISSUER`, `SUPABASE_AUDIENCE`, `REDIS_URL`, `REDIS_TIMEOUT_SECONDS`, `UPBIT_BASE_URL`, `UPBIT_TIMEOUT_SECONDS`, `UPBIT_MAX_RETRIES`, `CORS_ALLOWED_ORIGINS`.
- v2.2 adds: direct-Postgres connection string + dedicated DB role (`NOBYPASSRLS`) creds. Server env only. Never commit real values.

## 8. DOUBLE-CHECK PROTOCOL (run every step — RULE ZERO)

### Pass 1 — self review (before commit)
- [ ] Re-open every spec tag the step lists. Code matches word for word.
- [ ] `git diff --stat` — every changed file is in the step's FILES list. Extra file → justify or revert.
- [ ] `python -m ruff format --check app tests main.py` → clean.
- [ ] `python -m ruff check app tests main.py` → clean.
- [ ] `python -m mypy app` → clean.
- [ ] `python -m pytest -q` → all green. No new skip / xfail hiding a failure.
- [ ] Every new/changed JSON: camelCase keys, money as string, correct envelope.
- [ ] Run each `grep` in the step's DOUBLE CHECK section. Counts match.

### Pass 2 — adversarial review (before "done")
- [ ] Assume it is wrong. Find one input that breaks a §4 rule.
- [ ] Negative cases: forbidden field, wrong-side field, JSON-number money, missing `Idempotency-Key`, cross-user access, price-fetch failure, rollback path, concurrent requests.
- [ ] No scope leak from a later step.
- [ ] Full suite green (whole repo, not only new tests). Run concurrency tests 2x for flake.
- [ ] Migration (if any): applies on fresh DB, constraints verified via `\d+` and `pg_policies`.

## 9. COMPLETION REPORT FORMAT

- **Output language: Korean (한국어).**
- No background explanation. No restating the task. Keyword / table style only.
- Template:

```
## 완료 보고

### 변경 파일
- app/... : <keyword>
- tests/... : <keyword>

### 구현 항목
- <tag> : <keyword>

### 검증 결과
- ruff format: pass
- ruff check: pass
- mypy: pass
- pytest: <count> passed

### 더블 체크
- Pass 1: 완료 / 항목 <n>개
- Pass 2: 완료 / 적대적 케이스 <n>개

### 남은 작업
- <keyword> | 없음
```
