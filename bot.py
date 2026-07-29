import logging
import re
import asyncio

from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from config import BOT_TOKEN, MONGO_URI, ADMIN_IDS, BIO_CHECK_INTERVAL_SECONDS, LINK_PATTERN, PORT
from database import Database

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

db = Database()
LINK_RE = re.compile(LINK_PATTERN)

WAITING_LOG_CHANNEL = "waiting_log_channel"


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def job_name(user_id: int, chat_id: int) -> str:
    return f"biocheck_{chat_id}_{user_id}"


def extract_links(text: str):
    if not text:
        return []
    seen = list(dict.fromkeys(LINK_RE.findall(text)))  # dedupe, keep order
    return seen


PRIVATE_INVITE_RE = re.compile(r"t\.me/(\+|joinchat/)", re.IGNORECASE)
TME_USERNAME_RE = re.compile(r"t\.me/([A-Za-z0-9_]{4,32})\b", re.IGNORECASE)


async def is_group_link(bot, link: str) -> bool:
    """
    Returns True if the link should be treated as a group (public or private),
    False if it resolves to a channel (or can't be confirmed as a group).
    Non-Telegram links (not t.me / @username) are always kept, since they
    can't be a Telegram channel.
    """
    link = link.strip()

    # Private invite links (t.me/+xxxx or t.me/joinchat/xxxx) can't be resolved
    # by the bot API without joining, so we can't tell group vs channel here.
    # We keep them by default (best effort) since most shared invite links of
    # this kind in a bio tend to be groups.
    if PRIVATE_INVITE_RE.search(link):
        return True

    username = None
    m = TME_USERNAME_RE.search(link)
    if m:
        username = m.group(1)
    elif link.startswith("@"):
        username = link[1:]

    if username is None:
        # Not a t.me/@username style link at all -> can't be a Telegram channel
        return True

    try:
        chat = await bot.get_chat(f"@{username}")
    except Exception:
        # Can't resolve (private user, doesn't exist, etc.) -> can't confirm it's a group
        return False

    return chat.type in ("group", "supergroup")


# ---------------- Panel ----------------

def main_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("\U0001F4E1 تنظیم کانال لاگ", callback_data="set_log_channel")],
        [InlineKeyboardButton("\U0001F4CA وضعیت پایش‌ها", callback_data="status")],
    ]
    return InlineKeyboardMarkup(keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return  # silently ignore non-admins, no reply at all
    await update.message.reply_text(
        "پنل مدیریت ربات پایش بیو \U0001F447",
        reply_markup=main_menu_keyboard(),
    )


async def panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer()
        return
    await query.answer()

    if query.data == "set_log_channel":
        context.user_data[WAITING_LOG_CHANNEL] = True
        await query.edit_message_text(
            "یک پیام از کانال لاگ موردنظر برام فوروارد کن، یا آیدی عددی کانال "
            "(مثل -1001234567890) رو بفرست.\n\n"
            "⚠️ ربات باید از قبل ادمین همون کانال باشه."
        )
    elif query.data == "status":
        log_channel = await db.get_log_channel()
        monitored = await db.get_all_monitored()
        text = (
            f"\U0001F4E1 کانال لاگ: {log_channel if log_channel else 'تنظیم نشده'}\n"
            f"\U0001F465 تعداد کاربران تحت پایش: {len(monitored)}"
        )
        await query.edit_message_text(text, reply_markup=main_menu_keyboard())


async def admin_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    if not context.user_data.get(WAITING_LOG_CHANNEL):
        return

    channel_id = None
    if update.message.forward_from_chat:
        channel_id = update.message.forward_from_chat.id
    elif update.message.text:
        try:
            channel_id = int(update.message.text.strip())
        except ValueError:
            await update.message.reply_text(
                "این یه آیدی عددی معتبر یا پیام فورواردی نبود، دوباره امتحان کن."
            )
            return

    try:
        chat = await context.bot.get_chat(channel_id)
        bot_member = await context.bot.get_chat_member(channel_id, context.bot.id)
        if bot_member.status not in ("administrator", "creator"):
            await update.message.reply_text(
                "ربات توی این کانال ادمین نیست. اول ربات رو ادمین کانال کن، بعد دوباره امتحان کن."
            )
            return
    except Exception as e:
        await update.message.reply_text(
            f"نتونستم به این کانال دسترسی پیدا کنم. مطمئن شو آیدی درسته و ربات عضو/ادمین کانال هست.\nخطا: {e}"
        )
        return

    await db.set_log_channel(channel_id)
    context.user_data[WAITING_LOG_CHANNEL] = False
    await update.message.reply_text(
        f"✅ کانال لاگ با موفقیت روی «{chat.title or channel_id}» تنظیم شد.",
        reply_markup=main_menu_keyboard(),
    )


# ---------------- Bio monitoring ----------------

async def check_bio_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    user_id = data["user_id"]
    chat_id = data["chat_id"]

    try:
        chat = await context.bot.get_chat(user_id)
    except Exception as e:
        logger.warning(f"get_chat failed for {user_id}: {e}")
        return

    bio = getattr(chat, "bio", None) or ""
    raw_links = extract_links(bio)

    current_links = []
    for link in raw_links:
        try:
            if await is_group_link(context.bot, link):
                current_links.append(link)
        except Exception as e:
            logger.warning(f"link classification failed for {link}: {e}")

    last_links = await db.get_last_links(user_id, chat_id)

    new_links = [l for l in current_links if l not in last_links]
    if new_links:
        log_channel = await db.get_log_channel()
        if log_channel:
            username = f"@{chat.username}" if chat.username else str(user_id)
            text = f"\U0001F517 لینک جدید در بیوی {username}:\n" + "\n".join(new_links)
            try:
                await context.bot.send_message(log_channel, text)
            except Exception as e:
                logger.warning(f"failed to send to log channel: {e}")

    if current_links != last_links:
        await db.update_last_links(user_id, chat_id, current_links)


def schedule_bio_job(app: Application, user_id: int, chat_id: int):
    name = job_name(user_id, chat_id)
    if app.job_queue.get_jobs_by_name(name):
        return  # already scheduled, don't duplicate
    app.job_queue.run_repeating(
        check_bio_job,
        interval=BIO_CHECK_INTERVAL_SECONDS,
        first=0,  # check once immediately, then every interval
        data={"user_id": user_id, "chat_id": chat_id},
        name=name,
    )


async def track_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or update.effective_chat.type not in ("group", "supergroup"):
        return
    user = update.effective_user
    if not user or user.is_bot:
        return

    chat_id = update.effective_chat.id
    if not await db.is_monitored(user.id, chat_id):
        await db.add_monitored(user.id, chat_id)
        schedule_bio_job(context.application, user.id, chat_id)


async def restore_jobs(app: Application):
    monitored = await db.get_all_monitored()
    for doc in monitored:
        schedule_bio_job(app, doc["user_id"], doc["chat_id"])
    logger.info(f"Restored {len(monitored)} bio-check jobs after restart.")


# ---------------- Tiny health server (Railway sometimes expects a bound port) ----------------

async def run_health_server():
    web_app = web.Application()
    web_app.router.add_get("/", lambda r: web.Response(text="ok"))
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()


async def post_init(app: Application):
    await restore_jobs(app)
    asyncio.create_task(run_health_server())


async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled exception while processing update:", exc_info=context.error)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN env var is not set")
    if not MONGO_URI:
        raise RuntimeError("MONGO_URI env var is not set")

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("panel", start))
    app.add_handler(CallbackQueryHandler(panel_callback))
    app.add_handler(
        MessageHandler(
            filters.User(user_id=ADMIN_IDS)
            & filters.TEXT
            & ~filters.COMMAND
            & filters.ChatType.PRIVATE,
            admin_text_input,
        )
    )
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, track_group_message)
    )

    logger.info("Bot starting (polling mode)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
