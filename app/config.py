"""
Central config. All secrets come from environment variables — never hardcode.

Locally: create a `.env` file (see .env.example) and this loads it via
python-dotenv. On Render: set these in the dashboard under Environment.
"""
import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    # --- Database ---
    DATABASE_URL: str = os.environ["DATABASE_URL"]

    # --- WhatsApp Cloud API ---
    WA_VERIFY_TOKEN: str = os.environ["WA_VERIFY_TOKEN"]          # you invent this string
    WA_ACCESS_TOKEN: str = os.environ["WA_ACCESS_TOKEN"]          # permanent system-user token
    WA_PHONE_NUMBER_ID: str = os.environ["WA_PHONE_NUMBER_ID"]    # from Meta dashboard
    WA_API_VERSION: str = os.environ.get("WA_API_VERSION", "v21.0")

    # --- Claude ---
    ANTHROPIC_API_KEY: str = os.environ["ANTHROPIC_API_KEY"]
    CLAUDE_INTENT_MODEL: str = os.environ.get("CLAUDE_INTENT_MODEL", "claude-haiku-4-5-20251001")
    CLAUDE_PROMPT_MODEL: str = os.environ.get("CLAUDE_PROMPT_MODEL", "claude-sonnet-4-6")
    # Marketing-consultant answers (see app/engine/marketing_advisor.py) --
    # same tier as CLAUDE_PROMPT_MODEL by default, kept as its own setting
    # so it can be tuned independently without touching the prompt-builder
    # model.
    CLAUDE_MARKETING_MODEL: str = os.environ.get("CLAUDE_MARKETING_MODEL", "claude-sonnet-4-6")

    # --- Image generation (set once you pick the benchmark winner) ---
    IMAGE_API_KEY: str = os.environ.get("IMAGE_API_KEY", "")
    IMAGE_PROVIDER: str = os.environ.get("IMAGE_PROVIDER", "openai")  # "openai" | "ideogram" | "flux"

    # --- Storage (Cloudflare R2 — S3-compatible) ---
    R2_ACCOUNT_ID: str = os.environ.get("R2_ACCOUNT_ID", "")
    R2_ACCESS_KEY: str = os.environ.get("R2_ACCESS_KEY", "")
    R2_SECRET_KEY: str = os.environ.get("R2_SECRET_KEY", "")
    R2_BUCKET: str = os.environ.get("R2_BUCKET", "socioburp-creatives")
    R2_PUBLIC_BASE_URL: str = os.environ.get("R2_PUBLIC_BASE_URL", "")  # e.g. https://cdn.socioburp.net

    # --- Razorpay ---
    RAZORPAY_KEY_ID: str = os.environ.get("RAZORPAY_KEY_ID", "")
    RAZORPAY_KEY_SECRET: str = os.environ.get("RAZORPAY_KEY_SECRET", "")
    RAZORPAY_WEBHOOK_SECRET: str = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")

    # --- Instagram auto-posting (via Make.com scenario) ---
    MAKE_INSTAGRAM_WEBHOOK_URL: str = os.environ.get("MAKE_INSTAGRAM_WEBHOOK_URL", "")

    # --- Instagram profile/content fetch (via a separate Make.com scenario,
    # "SocioBurp — Instagram Profile Fetch" -- Business Discovery API using
    # SocioBurp's own connected IG account, no per-client OAuth needed) ---
    MAKE_INSTAGRAM_PROFILE_FETCH_WEBHOOK_URL: str = os.environ.get("MAKE_INSTAGRAM_PROFILE_FETCH_WEBHOOK_URL", "")

    # --- Instagram Insights OAuth (per-business Facebook Login for Business,
    # ads-engine performance tracking only -- see app/instagram_insights_oauth.py.
    # Entirely separate from the two integrations above: this is the only one
    # that reads a CLIENT's own private Insights data (reach/saves/engagement),
    # which requires their explicit per-business OAuth consent, unlike posting
    # (SocioBurp's own Make.com connection + manual Page-admin invite) or the
    # profile/caption fetch (public Business Discovery data only). ---
    META_APP_ID: str = os.environ.get("META_APP_ID", "")
    META_APP_SECRET: str = os.environ.get("META_APP_SECRET", "")
    META_GRAPH_API_VERSION: str = os.environ.get("META_GRAPH_API_VERSION", "v21.0")
    # Must exactly match a Valid OAuth Redirect URI configured on the Meta
    # app, e.g. https://<your-render-url>/oauth/instagram/callback
    META_OAUTH_REDIRECT_URI: str = os.environ.get("META_OAUTH_REDIRECT_URI", "")

    # --- Alerts (optional but recommended) ---
    ALERT_TELEGRAM_TOKEN: str = os.environ.get("ALERT_TELEGRAM_TOKEN", "")
    ALERT_TELEGRAM_CHAT_ID: str = os.environ.get("ALERT_TELEGRAM_CHAT_ID", "")

    # --- Business rules ---
    SIGNUP_BONUS_CREDITS: int = int(os.environ.get("SIGNUP_BONUS_CREDITS", "20"))
    LOW_BALANCE_THRESHOLD: int = int(os.environ.get("LOW_BALANCE_THRESHOLD", "3"))
    MAX_GENERATIONS_PER_HOUR: int = int(os.environ.get("MAX_GENERATIONS_PER_HOUR", "10"))


settings = Settings()
