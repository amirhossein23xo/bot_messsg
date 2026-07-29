import os

# --- Required environment variables (set these on Railway) ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
MONGO_URI = os.environ.get("MONGO_URI", "")
PORT = int(os.environ.get("PORT", "8080"))

# Only these Telegram user IDs are allowed to use bot commands / the settings panel.
# The bot silently ignores commands and panel taps from anyone else.
ADMIN_IDS = {8347908205, 6781649757}

# How often (seconds) a monitored user's bio is re-checked.
BIO_CHECK_INTERVAL_SECONDS = 1 * 60 * 60  # 1 hour
