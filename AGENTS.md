# AGENTS.md

Self-hosted algorithmic crypto trading platform (DepthSight) — FastAPI + async
Python trading bot, React/Vite frontend, Docker Compose on Elestio.

## Setup commands

- Install deps: `pip install -r requirements.txt` · `cd frontend && npm install`
- Test:         `python -m pytest tests/`
- Frontend:     `cd frontend && npm run lint` · `npm run build`
- Migrations:   `alembic upgrade head`

There is no Python linter configured. `pytest.ini` sets `pythonpath = .` and
`asyncio_mode = auto` (use bare `async def` tests, no `@pytest.mark.asyncio`).

## Project layout

- `api/` — FastAPI routers, schemas, Pydantic models, auth, onboarding
- `bot_module/` — trading core: `controller.py` (orchestration), `risk_manager.py`, `paper_executor.py`, `exchanges/` (ccxt adapters + factory), `data_consumer.py`, `strategy.py`
- `market_data_service.py` — standalone OKX market-data WebSocket service
- `bot_runner.py` — process entrypoint; spawns a `TradingController` per user
- `alembic/` — DB migrations · `tests/` — pytest · `frontend/` — Vite/React/TS
- `infra/`, `ops/`, `scripts/` — deployment and ops tooling · `pwa/` — installable web app
- `slack_agent/` — separate app, NOT imported by the trading path

## Architecture invariants

- **Redis 2 containers with ACL users** (`depthsight_redis`, `depthsight_redis_market`),
  both `default OFF`. Auth needs `--user <name> -a $PW`; a bare `-a` gives `WRONGPASS`.
  Auth with the *connecting client's* password, not `redis-server`'s env.
- **`_testnet` suffix on an exchange id is the single source of truth** for sandbox
  mode, shared by frontend, `exchanges/factory.py` and the executors. Do not add a
  parallel `is_testnet` flag.
- **`create_exchange_executor` is shared by 4+ call sites.** A guard added for the
  trading path must be checked against the public market-data path, which builds
  executors with *empty* credentials.
- **When a predicate is duplicated across functions, all copies must handle the
  same special cases.** `_update_monitored_symbols()` treats a hardcoded
  `config_data["symbol"]` as always-required (it drives the subscription); the
  SignalCheck matcher must agree, or strategies subscribe and never match.
- **`dict.get(k, {})` does not protect against a null value** — it returns `None`
  when the key exists with a null value. Use `get(k) or {}`.
- **Guards on real-money paths must fail in the same direction as the thing they
  guard.** If unrecognised input means "live executor" downstream, it must mean
  "live and tightly capped" upstream. Prefer `if mode == "paper": <loose>` over
  `if mode == "live": <tight>`.
- **A filter chain needs a logged `else`.** An `if/elif` with no `else` silently
  discards instances. Aggregate "nothing matched" logs must name per-item reasons.
- **A live task is not a working task.** A coroutine blocked in `await socket.read()`
  is alive and not `done()`, and delivering nothing. Never gate recovery on task
  *liveness* — gate it on **ownership**: `entry.get("task") is asyncio.current_task()`.
  A liveness check evaluated from inside the task itself is never false, so the
  recovery path silently becomes dead code. See `AGENTS.md` history for the commit
  where a self-heal shipped, passed its tests, and could never execute.
- **Two things failing at the same instant are one cause, not two coincidences.**
  Check the timestamps before blaming an upstream dependency. A ccxt client comes
  from `executor._exchange_pro`, so **one socket serves every stream on that
  executor** — any code that closes it to force a reconnect kills all of them
  silently. Recycled shared resources need a generation counter so every holder
  notices and re-subscribes.
- **Anything that always reports OK is worse than nothing** — it converts "I don't
  know" into "it's fine", and sends the user hunting for a cause that does not
  exist. Health endpoints must be able to go red, must say *why*, and must
  distinguish `unknown`/`idle` from healthy. See `api/routes/diagnostics.py`.
- **Strategies auto-rehydrate on bot restart.** Do not tell users to manually
  restart strategies after a deploy — verify the automatic path first.

## Trading safety rules

- **Testnet first, always. Never create a mainnet OKX key without explicit approval.**
- API keys are **Trade-only. Never Withdraw.**
- Never log or commit credentials; never paste them into chat.
- Paper trading must model the failure modes that lose money: liquidation, funding,
  commission, slippage. A demo that cannot fail the way reality fails is misleading.
- A guard that silently disables a risk control (blacklist, stop-loss, emergency
  stop) is itself a critical bug — fail closed and log loudly.

## Deploying to Elestio

The maintainer has no SSH; they paste into the Elestio web terminal.

```bash
cd /opt/app/depthsight
git config --global url."https://github.com/".insteadOf "git@github.com:"
git remote set-url origin https://github.com/DiverseInc/DepthSight.git
git pull origin main
git log -1 --oneline          # MUST show the expected commit, else the pull no-opped
bash ops/scripts/record_deployed_commit.sh   # stamps the commit; REFUSES a dirty checkout
docker compose build --no-cache <changed services>
docker compose up -d --no-deps <changed services>
docker compose up -d --no-deps --force-recreate api   # the stamp lives in the api's environment
docker compose restart caddy                           # caddy.config is bind-mounted; caddy reads it only at startup
```

- **`bash ops/scripts/record_deployed_commit.sh` is not optional.** It writes
  `DEPLOYED_COMMIT=<sha>` into `.env`, which `docker-compose.yml` hands to the
  `api` service as `env_file`; the running service publishes it as the
  `commit` field of `GET /api/v1/status`, and the AGPL-3.0 section 13 offer at
  `/source` points a reader at the archive of exactly that commit. **Run it
  AFTER the pull and BEFORE the build**: the value travels with the container,
  so a failed build leaves the old container reporting the old commit, which
  is the truth.
- **The `api` service is ALWAYS recreated, even when nothing in it changed.**
  `env_file` is read when the container is CREATED, so a container that is left
  running keeps the environment it was created with. On a bot-only deploy the
  `api` would otherwise keep publishing the PREVIOUS commit while the `bot`
  already runs the new code -- and the archive the offer points at would not
  contain the running bot. `--force-recreate` rebuilds nothing; it re-creates
  the container so it picks up the stamp that was just written.
- **`caddy` is ALWAYS restarted.** `caddy.config` is bind-mounted
  (`docker-compose.yml:283`), and caddy reads its Caddyfile only at startup,
  so `docker compose up -d` does not activate a changed route. `frontend/` is
  bind-mounted too (`docker-compose.yml:289`), so `welcome.html` goes live the
  moment the pull lands. Without the restart the site advertises the source
  offer while `/source` still falls through to the app shell. It is restarted
  LAST on purpose: the route appears at the same moment the published commit
  becomes correct, never before.
- It also **refuses to continue when the checkout is not the committed tree**
  (a modified or untracked file). The image is built from the Docker build
  context while the published source is the committed tree, so a dirty
  checkout would put code in the running containers that is not in the
  published source. It deletes nothing — it prints the offending paths and
  stops. Park them with `git stash -u` and deploy again.

- Services: `api bot market_data celery_worker websocket frontend pwa ops`;
  infra is `postgres redis redis-market caddy`. **Rebuild only the services whose
  files changed** — a `bot_module/` change means `bot`, not all five.
- The SSH remote resets between sessions; the `insteadOf` rewrite in step 2 is the
  permanent fix. Run it first, every time.
- `docker compose up -d` will NOT recreate a container for a bind-mounted file's
  changed *contents*; use `docker compose restart <svc>` in that case.
- Health check: `docker compose ps` (all ages should match, no crash loop), then
  the service log. Strategies rehydrate automatically within ~30s.

## Testing instructions

- Unit tests: `python -m pytest tests/` (CI runs exactly this on push/PR to
  `main`/`master`/`dev`). The frontend has no test suite.
- **Verify against the real function, not a reimplementation.** Bind it to a stub
  `self` (`Real.method.__get__(stub)`); mind `@staticmethod` — binding passes `self`
  as a spurious arg. A copied implementation tests the copy.
- **Cross-check closed-form math against an independent derivation** (e.g. a
  bisection solve), never against itself.
- Read the real call path before writing a stub — guessed command keys, payload
  field names and container types produce confident false failures.
- Prefer extracting a pure helper out of a large method so the rules are testable.

## PR & commit conventions

- Default branch is `main`. Never force-push it.
- Conventional commits (`fix:`, `feat:`, `docs:`, `refactor:`). In the body,
  state the root cause, the evidence, and the verification performed.
- Never amend or force-push a commit that may already be deployed.

## Security

- Never commit secrets — `.env` is gitignored.
- Do not modify or redirect the `diverseinc.net` / `diverseindustriesinc.com` domains.
