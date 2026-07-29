import logging
import re
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

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
    origin = update.message.forward_origin
    if origin and getattr(origin, "type", None) == "channel":
        channel_id = origin.chat.id
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

    try:
        await db.set_log_channel(channel_id)
    except Exception as e:
        await update.message.reply_text(
            f"❌ کانال لاگ توی تلگرام تایید شد ولی ذخیره‌ش توی دیتابیس با خطا مواجه شد.\nخطا: {e}"
        )
        return

    context.user_data[WAITING_LOG_CHANNEL] = False
    await update.message.reply_text(
        f"✅ کانال لاگ با موفقیت روی «{chat.title or channel_id}» تنظیم شد.",
        reply_markup=main_menu_keyboard(),
    )


# ---------------- Diagnostics ----------------

async def checkbio_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "استفاده:\n/checkbio <آیدی عددی کاربر>\n\n"
            "این دستور همین الان بیوی اون کاربر رو می‌خونه و گزارش کامل میده، "
            "بدون اینکه منتظر چک دوره‌ای بمونیم."
        )
        return

    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("آیدی عددی معتبر نیست.")
        return

    lines = [f"🔍 دیباگ کاربر {target_id}"]

    # 1. Can we fetch the chat at all?
    try:
        chat = await context.bot.get_chat(target_id)
    except Exception as e:
        lines.append(f"❌ get_chat شکست خورد: {e}")
        lines.append("→ یعنی ربات هیچ اطلاعاتی از این کاربر نداره (نه پیامی ازش دیده نه چتی باهاش داشته).")
        await update.message.reply_text("\n".join(lines))
        return

    lines.append(f"✅ get_chat موفق بود. نام: {chat.full_name or '—'}")

    bio = getattr(chat, "bio", None)
    if bio is None:
        lines.append(
            "⚠️ فیلد bio اصلاً برنگشت (None). طبق محدودیت تلگرام، این یعنی این کاربر "
            "هنوز خصوصاً و مستقیم با ربات چت (/start) نزده. تا وقتی این کارو نکنه، "
            "بیوش برای ربات قابل‌خوندن نیست — فرقی نمی‌کنه چقدر توی گروه پیام بده."
        )
    else:
        lines.append(f"📄 متن بیو: {bio!r}")
        links = extract_links(bio)
        lines.append(f"🔗 لینک‌های استخراج‌شده: {links if links else 'هیچی پیدا نشد'}")

    # 2. Log channel status
    log_channel = await db.get_log_channel()
    if not log_channel:
        lines.append("❌ کانال لاگ توی دیتابیس تنظیم نشده.")
    else:
        lines.append(f"✅ کانال لاگ: {log_channel}")
        try:
            member = await context.bot.get_chat_member(log_channel, context.bot.id)
            lines.append(f"✅ وضعیت ربات توی کانال لاگ: {member.status}")
        except Exception as e:
            lines.append(f"❌ نتونستم وضعیت ربات توی کانال لاگ رو چک کنم: {e}")
        try:
            await context.bot.send_message(log_channel, "🧪 پیام تست از /checkbio — اگه اینو می‌بینی، ارسال به کانال لاگ سالمه.")
            lines.append("✅ پیام تست با موفقیت به کانال لاگ ارسال شد.")
        except Exception as e:
            lines.append(f"❌ ارسال پیام تست به کانال لاگ شکست خورد: {e}")

    # 3. Monitoring records for this user (across all chats)
    all_monitored = await db.get_all_monitored()
    records = [d for d in all_monitored if d.get("user_id") == target_id]
    if not records:
        lines.append("ℹ️ این کاربر توی هیچ گروهی هنوز به‌عنوان مانیتورشونده ثبت نشده (یعنی هنوز پیامی توی گروه‌های تحت پوشش نداده).")
    else:
        for r in records:
            lines.append(
                f"— گروه {r.get('chat_id')}: last_links ذخیره‌شده = {r.get('last_links')}, "
                f"sent_links = {r.get('sent_links', [])}"
            )

    await update.message.reply_text("\n".join(lines))


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
    current_links = extract_links(bio)

    last_links = await db.get_last_links(user_id, chat_id)

    new_links = [l for l in current_links if l not in last_links]
    if new_links:
        log_channel = await db.get_log_channel()
        if log_channel:
            sent_before = set(await db.get_sent_links(user_id, chat_id))

            full_name = chat.full_name if getattr(chat, "full_name", None) else str(user_id)
            username = f"@{chat.username}" if chat.username else "ندارد"

            try:
                now_str = datetime.now(ZoneInfo("Asia/Tehran")).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

            group_title = str(chat_id)
            try:
                group_chat = await context.bot.get_chat(chat_id)
                group_title = group_chat.title or group_title
            except Exception as e:
                logger.warning(f"could not fetch group title for {chat_id}: {e}")

            links_lines = []
            for link in new_links:
                status = "\U0001F195 جدید" if link not in sent_before else "\u267B\uFE0F تکراری (قبلا هم ارسال شده)"
                links_lines.append(f"\u2022 {link}\n  {status}")

            text = (
                "\U0001F514 لینک جدید در بیوگرافی یک کاربر پیدا شد\n\n"
                f"\U0001F464 کاربر: {full_name}\n"
                f"\U0001F517 یوزرنیم: {username}\n"
                f"\U0001F194 آیدی عددی: {user_id}\n"
                f"\U0001F465 گروه: {group_title}\n"
                f"\U0001F550 زمان: {now_str}\n\n"
                f"\U0001F517 لینک(های) یافت‌شده در بیو:\n" + "\n".join(links_lines)
            )
            try:
                await context.bot.send_message(log_channel, text)
                await db.add_sent_links(user_id, chat_id, new_links)
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
    app.add_handler(CommandHandler("checkbio", checkbio_command))
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
