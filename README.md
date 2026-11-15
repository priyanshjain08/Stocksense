# StockSense — Intelligent Inventory. Smarter Decisions.

Multi-tenant inventory intelligence for retail: an immutable stock ledger, sales/returns, purchasing and
receiving, statistical demand forecasting (with backtesting), and explainable replenishment recommendations.

## Build status — read this first

| Area | Status |
|---|---|
| Forecasting models, backtesting, demand prep, replenishment math, analytics definitions, stock-count variance logic, Excel report builder, CSV-injection guard | **Implemented and tested** (29 tests pass: `cd backend && python run_domain_tests.py`) |
| API (FastAPI), ORM models, services (inventory ledger, sales, returns, purchasing, forecasting, replenishment, analytics, auth, RBAC, tenancy) | **Written, NOT yet executed** — the build sandbox had no network, so FastAPI/SQLAlchemy/pytest could not be installed. Syntax-checked only. |
| API integration tests (`backend/tests/test_api.py`) | **Written, never run.** Expect some first-run fixes. |
| React frontend (login, onboarding, products + CSV import, stock, sales, purchasing, forecasting, replenishment, health) | **Written, never built or type-checked.** |
| Docker Compose (Postgres, Redis, API, Celery worker + beat, frontend) | Written, never started |
| Alembic | Scaffolded; the initial revision is not generated (see below) |
| Added since first drop (unrun API layer) | Stock counts (snapshot → count → submit → separate approval → audited adjustments), Excel inventory export, race-safe document numbering |
| Not built | AI assistant, batch/serial/expiry tracking, reservations workflow, notifications, PDF reports and other Excel reports, supplier-comparison UI, forecast accuracy history over time, scheduled report jobs, E2E (Playwright) tests, Tailwind/shadcn UI (plain CSS used) |

## Run it

```bash
cp .env.example .env          # set SECRET_KEY and POSTGRES_PASSWORD (see comments in the file)
docker compose up --build
docker compose exec api alembic revision --autogenerate -m "initial" && docker compose exec api alembic upgrade head
docker compose exec api python -m scripts.seed_demo     # prints the demo login
```
API docs: http://localhost:8000/docs (non-production) · App: http://localhost:5173

Without Docker (SQLite, inline forecasts):
```bash
cd backend && pip install -r requirements.txt
export SECRET_KEY=$(python -c "import secrets;print(secrets.token_urlsafe(48))")
pytest                      # API + domain tests
uvicorn app.main:app --reload
```
Docker/Windows note: the `db`/`redis` services need Docker Desktop with the WSL2 backend.
In production use PostgreSQL via Alembic; `create_all` runs only when `ENVIRONMENT != production`.

## Key design decisions

* **Ledger first.** `stock_movements` is append-only; `inventory_balances` change only inside
  `inventory.service.apply_movement`, with row locks (`SELECT … FOR UPDATE`), non-negative CHECK constraints
  and per-movement idempotency keys. `GET /inventory/reconciliation` proves balances equal ledger sums.
* **Sellable vs unavailable.** Damaged/quarantined/non-restocked returns go to `unavailable_quantity`;
  only sellable stock feeds sales, forecasts and replenishment.
* **Tenancy.** Every row has `organization_id`. The active org comes from `X-Organization-Id` and is verified
  against the caller’s membership on every request; every lookup is filtered by it (cross-org IDs return 404).
* **RBAC + branch scope.** Roles map to permissions (`core/permissions.py`), checked in services.
  Non-global roles only reach branches assigned in their membership.
* **Forecasting honesty.** Models: seasonal-naive/naive baseline, moving average, SES, Holt-Winters additive,
  Croston-SBA. Rolling-origin backtests pick a model only if it beats the baseline by ≥5% error; otherwise the
  baseline is used. <28 days of data → moving average/mean with an explicit warning. Stock-out zero-sale days are
  treated as censored demand. Intervals are approximate (residual-based). Metrics: WAPE, MAE, RMSE, bias (no MAPE).
* **Replenishment is advice.** `Q = max(0, target − IP)` with `ROP = lead-time demand + z·σ·√L`; MOQ, pack size,
  shelf life, capacity and optional budget applied, with a plain-English reason and warnings per recommendation.
  Accepted recommendations become **draft** POs only; submit/approve are separate human steps.

## Known limitations / things to verify first

1. Run the test suite and fix whatever the first execution surfaces (most likely in the API layer).
2. Document numbers now come from a row-locked per-organization counter (numbers can skip after a rollback, never duplicate); unverified on Postgres.
3. Concurrency tests are only meaningful on PostgreSQL; SQLite ignores row locks.
4. Historical sales import does not move current stock; stock-out detection only sees ledger history, so demo/imported
   history has no stock-outs unless movements exist.
5. Inventory turnover uses current valuation as a proxy for average inventory (no valuation snapshots yet).
6. Rate limiting is in-process (single replica). Token is stored in `sessionStorage` in the frontend (XSS-exposed);
   move to httpOnly cookies + CSRF for production.
7. Dependency versions in `requirements.txt`/`package.json` were chosen from memory and are unverified against current releases.
