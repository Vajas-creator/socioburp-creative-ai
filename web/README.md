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

## Environment variables

See `.env.example` for the full list. Required:

- `DATABASE_URL` — Postgres connection string
- `JWT_SECRET` — 32+ char random secret signing access tokens (`openssl rand -base64 48`)

Optional (for real emails):

- `APP_URL`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `EMAIL_FROM`

## Auth model

- **Access token**: JWT, 15 min TTL, httpOnly cookie (`sb_access_token`), scoped to `/`.
- **Refresh token**: random 256-bit token, 30 day TTL, httpOnly cookie
  (`sb_refresh_token`) scoped to `/api/auth`. Only its SHA-256 hash is
  stored in Postgres. Rotated on every use (old token revoked, new one
  issued); reuse of an already-revoked token revokes the user's entire
  session chain as a theft signal.
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

## Production checklist

- Run `npx prisma migrate deploy` against production `DATABASE_URL` as part of your deploy step.
- Set `JWT_SECRET` and `DATABASE_URL` as real secrets in your host's env config — never commit `.env`.
- Configure real SMTP credentials so password resets actually deliver.
- Deployed behind HTTPS (cookies are marked `secure` in production).
- Replace the in-memory rate limiter if running more than one instance.
