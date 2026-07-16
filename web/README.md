# SocioBurp Web — Authentication System

Next.js (App Router, TypeScript) auth system for SocioBurp: login, signup,
forgot/reset password, JWT sessions, and a role-protected dashboard
(Admin / Team Member / Client). It's a separate deployable that lives
alongside the existing FastAPI WhatsApp bot backend (`../app`) and can
share the same Postgres instance without touching its tables.

## Stack

- Next.js App Router + TypeScript, Tailwind CSS v4
- PostgreSQL via Prisma ORM (driver adapter: `@prisma/adapter-pg`)
- JWT access tokens (`jose`) + opaque, hashed, rotating refresh tokens
- `bcryptjs` for password hashing
- `nodemailer` for password-reset emails (real SMTP, no mocks)
- `zod` for input validation

## Database

This app owns three tables in your Postgres database, all prefixed
`auth_` so they never collide with the WhatsApp bot's schema
(`businesses`, `brand_profiles`, `generations`, `credit_ledger`,
`conversation_state`):

- `auth_users` — id, email, password_hash, name, role, timestamps
- `auth_refresh_tokens` — hashed refresh tokens, rotation chain, revocation
- `auth_password_reset_tokens` — hashed, single-use, time-limited reset tokens

`role` is a Postgres enum: `ADMIN`, `TEAM_MEMBER`, `CLIENT`. New
self-service signups always get `CLIENT` — promote a user to `TEAM_MEMBER`
or `ADMIN` directly in the database (or build an admin-only endpoint for
this later; none is exposed publicly on purpose).

## Local setup

```bash
cd web
npm install
cp .env.example .env
# edit .env: set DATABASE_URL and JWT_SECRET at minimum

# apply the schema to your database
npx prisma migrate deploy

npm run dev
```

Open http://localhost:3000 — it redirects to `/login`.

If `SMTP_*` isn't configured, `forgot-password` still works: the reset
link is logged to the server console instead of emailed, so you can test
the full flow locally without a mail provider.

### Linting, type checking, and build

```bash
npm run lint        # eslint
npm run typecheck   # tsc --noEmit
npm run build       # production build (next build)
```

`npx prisma generate` must run before `typecheck`/`build` in a fresh
checkout (it writes the generated client to `src/generated/prisma`, which
is gitignored). `npm run dev`/`build` don't need `DATABASE_URL` to be a
reachable database to succeed — `src/lib/prisma.ts` only connects lazily,
on first actual query — but `prisma migrate deploy`/`dev` do need a real
one.

## Environment variables

See `.env.example` for the full list. Required:

- `DATABASE_URL` — Postgres connection string
- `JWT_SECRET` — 32+ char random secret signing access tokens (`openssl rand -base64 48`)

Optional (for real emails):

- `APP_URL`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `EMAIL_FROM`

## Auth model

- **Access token**: JWT, 15 min TTL, httpOnly cookie (`sb_access_token`), scoped to `/`.
- **Refresh token**: random 256-bit token, 30 day TTL, httpOnly cookie
  (`sb_refresh_token`), also scoped to `/` (not just `/api/auth`) so
  `src/proxy.ts` receives it on `/dashboard/**` requests and can silently
  refresh an expired access token; httpOnly already prevents JS from
  reading it regardless of path. Only its SHA-256 hash is stored in
  Postgres. Rotated on every use (old token revoked, new one issued);
  reuse of an already-revoked token revokes the user's entire session
  chain as a theft signal.
- **Route protection**: `src/proxy.ts` (Next.js 16 renamed `middleware.ts`
  to `proxy.ts`) guards `/dashboard/**`, verifying the access token and
  silently refreshing it via `/api/auth/refresh` when expired but the
  refresh token is still valid. `/dashboard/admin/**` additionally
  requires the `ADMIN` role. Every Server Component and API route also
  re-verifies independently — Proxy is defense-in-depth, not the only gate.
- **Password reset**: single-use, 30-minute token; resetting a password
  revokes every active session for that user.
- **Rate limiting**: in-memory, per-process, on `login`/`signup`/
  `forgot-password`/`reset-password`. Fine for a single instance; swap
  `src/lib/rate-limit.ts` for a shared store (Redis/Upstash) before
  scaling horizontally.

## API routes

| Route                          | Method | Description                              |
| ------------------------------- | ------ | ----------------------------------------- |
| `/api/auth/signup`             | POST   | Create a `CLIENT` account, start session  |
| `/api/auth/login`               | POST   | Verify credentials, start session         |
| `/api/auth/logout`              | POST   | Revoke refresh token, clear cookies       |
| `/api/auth/refresh`             | POST   | Rotate refresh token, mint new access token |
| `/api/auth/forgot-password`     | POST   | Email a password reset link (if account exists) |
| `/api/auth/reset-password`      | POST   | Consume reset token, set new password     |
| `/api/auth/me`                  | GET    | Current user from the access token        |
| `/api/dashboard`                | GET    | Role-aware dashboard data (protected)     |

## CI

`../.github/workflows/frontend.yml` runs on every push/PR touching `web/`:

- **lint** — `eslint`
- **typecheck** — `tsc --noEmit`
- **build** — spins up a real Postgres service container, runs
  `prisma migrate deploy` against it, then `next build` — the same
  sequence Render's `buildCommand` runs (see below)

There's no frontend test suite yet, so no "test" job is defined — add one
(e.g. Vitest/Playwright) and a matching CI job when tests are introduced.

## Deploy to Render

This service is defined in `../render.yaml` as `socioburp-web`, alongside
the FastAPI backend as one Render Blueprint. See the root `README.md`'s
[Deploy to Render](../README.md#deploy-to-render) section for the full
walkthrough. In short:

- `buildCommand`: `npm ci && npx prisma generate && npx prisma migrate deploy && npm run build`
- `startCommand`: `npm run start`
- `healthCheckPath`: `/login` (a static 200 page — the app's own `/`
  redirects based on auth state, which isn't health-check-friendly)
- Required env vars in the Render dashboard (all `sync: false` in
  `render.yaml`, so Render won't set them for you): `DATABASE_URL`,
  `JWT_SECRET`, `APP_URL`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`,
  `SMTP_PASS`, `EMAIL_FROM`

## Production checklist

- Migrations run automatically on every Render deploy (`buildCommand`
  includes `prisma migrate deploy`) — no manual step needed here, unlike
  the FastAPI backend's Alembic migrations.
- Set `JWT_SECRET` and `DATABASE_URL` as real secrets in your host's env config — never commit `.env`.
- Configure real SMTP credentials so password resets actually deliver.
- Deployed behind HTTPS (cookies are marked `secure` in production).
- Replace the in-memory rate limiter if running more than one instance.
