# SocioBurp Creative AI — Week 1 Codebase

WhatsApp bot backend: webhook receive/send, message router, and the full
5-step onboarding flow (business name → industry → logo → color → tone),
with a credit ledger and signup bonus. Generation itself is stubbed —
that's the Week 2 build.

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
- `test_smoke.py` — local end-to-end test (SQLite, no real APIs) — proof the
  onboarding flow works before you deploy

## Local setup

```bash
pip install -r requirements.txt --break-system-packages
cp .env.example .env
# edit .env with your real values (Neon DATABASE_URL, WA tokens, etc.)

# sanity check the whole onboarding flow without touching real APIs:
python3 test_smoke.py
```

## Deploy to Render

1. Push this folder to a GitHub repo
2. Render dashboard → New → Web Service → connect the repo
3. It will detect `render.yaml` — review the env var list, click through
4. In the service's Environment tab, paste in the real values:
   - `DATABASE_URL` — from Neon (neon.tech → create project → connection string,
     use the "pooled connection" string for serverless-friendly behavior)
   - `WA_VERIFY_TOKEN` — invent any random string, you'll reuse it in Meta's
     webhook config in step 6 below
   - `WA_ACCESS_TOKEN` — your permanent System User token from Meta
   - `WA_PHONE_NUMBER_ID` — from the Meta App Dashboard → WhatsApp → API Setup
   - `ANTHROPIC_API_KEY`
   - `R2_ACCOUNT_ID`, `R2_ACCESS_KEY`, `R2_SECRET_KEY`, `R2_BUCKET`,
     `R2_PUBLIC_BASE_URL` — from Cloudflare R2 (see comment in `app/storage.py`
     for the one-time bucket setup steps)
   - `META_APP_ID`, `META_APP_SECRET` — from the Meta App Dashboard → App
     Settings → Basic. Only needed for the Instagram Insights connect flow
     (see below); leave blank if you don't need performance tracking yet.
   - `META_OAUTH_REDIRECT_URI` — `https://<your-service>.onrender.com/oauth/instagram/callback`
5. Deploy. Confirm `https://<your-service>.onrender.com/` returns
   `{"status": "ok", ...}`
6. In Meta App Dashboard → WhatsApp → Configuration → Webhook:
   - Callback URL: `https://<your-service>.onrender.com/webhook`
   - Verify token: same string as `WA_VERIFY_TOKEN`
   - Subscribe to the `messages` field
7. Send "hi" to your WhatsApp number from your phone. You should get the
   onboarding welcome message back within a couple seconds.

## Instagram Insights connect flow (ads-engine performance tracking)

Separate from auto-posting (`app/instagram.py`, via Make.com) and the public
profile fetch (`app/engine/instagram_analysis.py`) — this is the only piece
that reads a business's own private Insights data (reach, saves,
engagement), so it's the only one that needs the client's own per-business
OAuth consent. See `app/instagram_insights_oauth.py` for the full flow.

Setup, in addition to `META_APP_ID`/`META_APP_SECRET`/`META_OAUTH_REDIRECT_URI` above:

1. In Meta App Dashboard → Facebook Login for Business → Settings, add
   `META_OAUTH_REDIRECT_URI`'s value to "Valid OAuth Redirect URIs".
2. Add `instagram_basic`, `instagram_manage_insights`, `pages_show_list`,
   and `pages_read_engagement` as permissions the app requests.
3. **`instagram_manage_insights` is a restricted permission.** Until Meta
   grants App Review + Business Verification for it, the flow only works
   for Instagram accounts added as testers/admins on the Meta app — not
   real clients. Kick off App Review early; it's a multi-day-to-multi-week
   external turnaround, independent of anything in this codebase.
4. A client connects by texting "connect instagram" on WhatsApp — they get
   a signed, 30-minute link that starts the Facebook Login flow, then
   land back on WhatsApp with a confirmation once approved.

Once connected, `app/engine/instagram_insights.py` reads raw Insights
metrics using the stored per-business token. It's intentionally just a
read client — no readiness-advisory scoring, best-performer ranking, or
historical tracking yet; those are separate, later pieces of the ads
engine.

## Running the Alembic migration

Once `DATABASE_URL` points at your real Neon database:

```bash
alembic upgrade head
```

Note: `app/db.py`'s `init_db()` also auto-creates tables on startup as a
convenience for the very first deploy. Once you've run the real migration
once and have real data, remove the `init_db()` call from `main.py`'s
startup event so all future schema changes go through Alembic properly
instead of the auto-create shortcut.

## What's NOT here yet (by design — later sessions)

- Image generation, prompt builder, captions, quality checker (Week 2)
- Razorpay payment links + webhook (Week 3)
- Rate limiting, uptime monitoring (Week 4)

See the full 30-day build guide doc for the complete roadmap.
