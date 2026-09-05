# SocioBurp Creative AI — Week 1 Codebase

WhatsApp bot backend: webhook receive/send, message router, and the full
5-step onboarding flow (business name → industry → logo → color → tone),
with a credit ledger and signup bonus. Generation itself is stubbed —
that's the Week 2 build.

This repo has two independently deployable apps:

| App | Path | Stack | Purpose |
| --- | --- | --- | --- |
| WhatsApp bot backend | `/` (`app/`) | FastAPI + SQLAlchemy + Alembic | Onboarding flow, credits, webhook |
| Web dashboard & auth | `web/` | Next.js (App Router) + Prisma | Login/signup, JWT sessions, role-protected dashboard |

Both can share one Postgres instance — the web app only owns tables
prefixed `auth_*`, so it never collides with the bot's schema
(`businesses`, `brand_profiles`, `generations`, `credit_ledger`,
`conversation_state`). See `web/README.md` for the web app's own setup
and env vars; this file covers the FastAPI backend plus whole-repo CI/deploy.

## CI

GitHub Actions run on every push/PR that touches the relevant path:

- `.github/workflows/backend.yml` — lint (`ruff`), type check (`mypy`),
  build (install deps, apply Alembic migrations against a real Postgres
  service container, boot the app and hit the health check), test
  (`test_smoke.py`, the onboarding flow end-to-end on SQLite)
- `.github/workflows/frontend.yml` — lint (`eslint`), type check (`tsc`),
  build (apply Prisma migrations against a real Postgres service
  container, then `next build`) — see `web/README.md` for details
- `.github/workflows/render-deploy-check.yml` — validates `render.yaml` is
  well-formed and defines both expected services; optionally pings Render
  Deploy Hooks if configured (see [Deploy to Render](#deploy-to-render))

## What's included

- `app/main.py` — FastAPI app entry point
- `app/config.py` — env var loader
- `app/db.py` + `app/models.py` — SQLAlchemy setup + all 5 tables
- `migrations/versions/0001_initial.py` — Alembic migration (Postgres)
- `app/whatsapp/client.py` — send text / image / buttons, download media
- `app/whatsapp/webhook.py` — verify handshake + receive with fast-ack pattern
- `app/router.py` — decision tree: onboarding vs keywords vs generation
- `app/onboarding.py` — the 5-step state machine
- `app/credits.py` — append-only ledger, balance computed on read
- `app/storage.py` — Cloudflare R2 upload helpers
- `app/payments.py`, `app/engine/orchestrator.py` — stubs for Week 2/3
- `app/instagram_oauth.py` — Instagram account linking via Facebook Login
  for Business (see below)
- `migrations/versions/0002_instagram_connections.py` — Alembic migration
  for the Instagram connections table
- `test_smoke.py` — local end-to-end test (SQLite, no real APIs) — proof the
  onboarding flow works before you deploy

## Instagram account linking

A WhatsApp user types **"connect instagram"** (or "instagram") -> the bot
replies with a Facebook Login for Business OAuth link (state carries their
phone number, same pattern as every other WhatsApp-triggered flow — no
parallel session store) -> they authorize in their browser -> Meta
redirects to `GET /instagram/oauth/callback` -> the backend exchanges the
code for a long-lived Page access token, finds the connected Instagram
professional account, saves it to the new `instagram_connections` table,
and confirms over WhatsApp.

Requires `META_APP_ID`, `META_APP_SECRET`, and `META_OAUTH_REDIRECT_URI`
(see `.env.example`) — until all three are set, the bot replies that
linking isn't available yet rather than sending a broken link.
`META_OAUTH_REDIRECT_URI` must exactly match a URI registered in your Meta
App Dashboard under Facebook Login for Business -> Valid OAuth Redirect
URIs; it's read from the env var directly (not derived from a domain), so
whatever you set in Render is automatically what the generated OAuth URLs
use.

There's also a two-part gate for Meta App Review: `META_APP_REVIEW_DEMO_ENABLED`
(default `false`) turns the reviewer path on at all, and `APP_REVIEW_DEMO_TOKEN`
(a random secret you generate, e.g. `python3 -c "import uuid; print(uuid.uuid4())"`)
is required alongside it — knowing the fixed `app_review_demo` state string
isn't enough on its own. The token travels inside the OAuth `state` value
itself rather than as a separate query param, since Meta's redirect only
reliably round-trips `code` and `state`. Generate the one-off reviewer link
with `app.instagram_oauth.build_app_review_demo_url()` (raises if the token
isn't set) and paste it into the "Instructions to reproduce" field — it
completes the same real OAuth handshake but skips the WhatsApp notify/DB-save
step and just renders a static success page, since a reviewer has no
WhatsApp session in our system. Leave both env vars unset outside of an
active review submission.

## Local setup

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# add -r requirements-dev.txt too if you want to run ruff/mypy locally

cp .env.example .env
# edit .env with your real values (Neon DATABASE_URL, WA tokens, etc.)

# sanity check the whole onboarding flow without touching real APIs:
python3 test_smoke.py
```

### Linting and type checking

```bash
pip install -r requirements-dev.txt
ruff check .      # lint
mypy app/         # type check
```

Both are configured in `pyproject.toml`. `mypy` scopes a few disabled
error codes to `app/models.py`, `app/router.py`, `app/credits.py`, and
`app/onboarding.py` — these are known false positives from SQLAlchemy's
classic `Column()`-style declarative models (not `Mapped[]`-annotated),
not real bugs; see the comment in `pyproject.toml`.

## Deploy to Render

`render.yaml` is a Render **Blueprint** defining both services in this
repo. In the Render dashboard: **New +** → **Blueprint** → connect this
GitHub repo → Render detects `render.yaml` and proposes both services:

- `socioburp-creative-ai` — the FastAPI backend
- `socioburp-web` — the Next.js dashboard (see `web/README.md`)

After the blueprint creates both services, fill in their env vars
(anything marked `sync: false` in `render.yaml` isn't set automatically):

**`socioburp-creative-ai`** (backend):
- `DATABASE_URL` — from Neon (neon.tech → create project → connection
  string; use the "pooled connection" string for serverless-friendly
  behavior)
- `WA_VERIFY_TOKEN` — invent any random string, you'll reuse it in Meta's
  webhook config below
- `WA_ACCESS_TOKEN` — your permanent System User token from Meta
- `WA_PHONE_NUMBER_ID` — from the Meta App Dashboard → WhatsApp → API Setup
- `ANTHROPIC_API_KEY`
- `R2_ACCOUNT_ID`, `R2_ACCESS_KEY`, `R2_SECRET_KEY`, `R2_BUCKET`,
  `R2_PUBLIC_BASE_URL` — from Cloudflare R2 (see comment in
  `app/storage.py` for the one-time bucket setup steps)

**`socioburp-web`** (dashboard/auth — see `web/README.md` for details):
- `DATABASE_URL` — same Postgres instance as above works fine
- `JWT_SECRET` — 32+ char random secret (`openssl rand -base64 48`)
- `APP_URL` — the service's public Render URL, e.g. `https://socioburp-web.onrender.com`
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `EMAIL_FROM` — for
  password reset emails

Then:

1. Deploy. Confirm `https://<backend-service>.onrender.com/` returns
   `{"status": "ok", ...}`, and `https://<web-service>.onrender.com/login`
   loads.
2. Run the Alembic migration for the backend (see below) and the Prisma
   migration for the web app (`render.yaml`'s `buildCommand` for
   `socioburp-web` already runs `prisma migrate deploy` on every deploy).
3. In Meta App Dashboard → WhatsApp → Configuration → Webhook:
   - Callback URL: `https://<backend-service>.onrender.com/webhook`
   - Verify token: same string as `WA_VERIFY_TOKEN`
   - Subscribe to the `messages` field
4. Send "hi" to your WhatsApp number from your phone. You should get the
   onboarding welcome message back within a couple seconds.

Render's Blueprint auto-deploys both services on every push to the
connected branch — no extra CI step is required to trigger a deploy.
`.github/workflows/render-deploy-check.yml` is a pre-deploy gate that
validates `render.yaml`; it can optionally ping Render Deploy Hooks too if
you add `RENDER_DEPLOY_HOOK_BACKEND` / `RENDER_DEPLOY_HOOK_WEB` as repo
secrets (Render dashboard → service → Settings → Deploy Hook), but this is
just an extra status signal — leave them unset and Render's native
auto-deploy still works.

## Running the Alembic migration

Once `DATABASE_URL` points at your real Neon database:

```bash
alembic upgrade head
```

Note: `app/db.py`'s `init_db()` also auto-creates tables on startup as a
convenience for the very first deploy. Once you've run the real migration
once and have real data, remove the `init_db()` call from `main.py`'s
startup event so all future schema changes go through Alembic properly
instead of the auto-create shortcut. Because of this, `render.yaml`'s
`buildCommand` for the backend does **not** run `alembic upgrade head`
automatically — run it manually the first time (and after any future
migration) so an already-`init_db()`-bootstrapped database doesn't hit a
"relation already exists" error from Alembic trying to create tables that
already exist outside its migration history.

## What's NOT here yet (by design — later sessions)

- Image generation, prompt builder, captions, quality checker (Week 2)
- Razorpay payment links + webhook (Week 3)
- Rate limiting, uptime monitoring (Week 4)

See the full 30-day build guide doc for the complete roadmap.
