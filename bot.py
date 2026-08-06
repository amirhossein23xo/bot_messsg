import logging
import re
import html
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, CopyTextButton
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from config import BOT_TOKEN, MONGO_URI, ADMIN_IDS, BIO_CHECK_INTERVAL_SECONDS, PORT
from database import Database

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

db = Database()

# Only Telegram t.me links / @mentions are candidates — no generic websites.
INVITE_LINK_RE = re.compile(r"(?:https?://)?t\.me/(?:joinchat/|\+)([\w-]{4,})", re.IGNORECASE)
USERNAME_LINK_RE = re.compile(r"(?:https?://)?t\.me/([a-zA-Z]\w{3,31})\b", re.IGNORECASE)
MENTION_RE = re.compile(r"@([a-zA-Z]\w{3,31})\b")

WAITING_LOG_CHANNEL = "waiting_log_channel"


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def job_name(user_id: int, chat_id: int) -> str:
    return f"biocheck_{chat_id}_{user_id}"


async def extract_group_links(bot, text: str):
    """Return only links that point to an actual Telegram GROUP/SUPERGROUP —
    never channels, bots, or ordinary websites.

    - t.me/+xxxx and t.me/joinchat/xxxx (private invite links) can't be
      verified by a bot without joining them, so they're kept as-is
      (this is the standard way private groups get shared).
    - t.me/username and @username are verified live via get_chat: only kept
      if the resolved chat.type is "group" or "supergroup" — this
      automatically excludes channels (type "channel") and bots/regular
      users (type "private").
    """
    if not text:
        return []

    seen = set()
    results = []

    for m in INVITE_LINK_RE.finditer(text):
        link = f"https://t.me/+{m.group(1)}"
        if link not in seen:
            seen.add(link)
            results.append(link)

    candidate_usernames = []
    for m in USERNAME_LINK_RE.finditer(text):
        uname = m.group(1)
        if uname.lower() == "joinchat":
            continue
        candidate_usernames.append(uname)
    for m in MENTION_RE.finditer(text):
        candidate_usernames.append(m.group(1))

    for uname in dict.fromkeys(candidate_usernames):  # dedupe, keep order
        link = f"https://t.me/{uname}"
        if link in seen:
            continue
        seen.add(link)
        try:
            chat = await bot.get_chat(f"@{uname}")
        except Exception:
            continue  # can't resolve -> skip rather than guess
        if chat.type in ("group", "supergroup"):
            results.append(link)

    return results




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
        links = await extract_group_links(context.bot, bio)
        lines.append(f"🔗 لینک‌های گروه تشخیص‌داده‌شده: {links if links else 'هیچی پیدا نشد'}")

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
            lines.append(f"— گروه {r.get('chat_id')}: last_links ذخیره‌شده = {r.get('last_links')}")
        global_sent = await db.get_sent_links(target_id)
        lines.append(f"📤 لینک‌هایی که تا الان (توی هر گروهی) براش ارسال شده: {global_sent if global_sent else 'هیچی'}")

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
    current_links = await extract_group_links(context.bot, bio)

    last_links = await db.get_last_links(user_id, chat_id)

    new_links = [l for l in current_links if l not in last_links]
    if new_links:
        # Atomically claim each candidate link. If two bot processes are
        # somehow running at once (leftover Railway deployment, etc.) and
        # both wake up on this same user at the same time, only one of
        # them will win the claim for a given link — the other will get
        # False back and skip it, so it's physically impossible for the
        # same link to be sent twice, even under a race.
        to_announce = [l for l in new_links if await db.claim_link(user_id, l)]

        if to_announce:
            log_channel = await db.get_log_channel()
            if log_channel:
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

                def esc(s):
                    return html.escape(str(s))

                links_block = "\n".join(f"• {esc(l)}" for l in to_announce)

                text = (
                    "🔔 <b>لینک گروه جدید در بیوگرافی یک کاربر پیدا شد</b>\n"
                    "———————————————\n"
                    f"👤 کاربر: <b>{esc(full_name)}</b>\n"
                    f"🔗 یوزرنیم: {esc(username)}\n"
                    f"🆔 آیدی عددی: <code>{user_id}</code>\n"
                    f"👥 گروه: {esc(group_title)}\n"
                    f"🕐 زمان: {esc(now_str)}\n"
                    "———————————————\n"
                    f"🆕 <b>لینک‌های جدید:</b>\n{links_block}"
                )

                keyboard_rows = []
                for idx, link in enumerate(to_announce, start=1):
                    label = "📋 کپی لینک" if len(to_announce) == 1 else f"📋 کپی لینک {idx}"
                    keyboard_rows.append([InlineKeyboardButton(label, copy_text=CopyTextButton(text=link))])
                reply_markup = InlineKeyboardMarkup(keyboard_rows)

                try:
                    await context.bot.send_message(log_channel, text, parse_mode="HTML", reply_markup=reply_markup)
                except Exception as e:
                    # NOTE: the links in `to_announce` were already atomically
                    # claimed above, so even though this particular send
                    # failed, they will NOT be retried on the next check —
                    # this keeps the "never send the same link twice"
                    # guarantee airtight instead of trading it for retries.
                    logger.warning(f"failed to send to log channel (links were already claimed, will not retry): {e}")
        # else: every link we just found was already announced for this user
        # in some other group before — say nothing, don't re-send.

    if current_links != last_links:
        await db.update_last_links(user_id, chat_id, current_links)


async def schedule_bio_job(app: Application, user_id: int, chat_id: int):
    name = job_name(user_id, chat_id)
    if app.job_queue.get_jobs_by_name(name):
        return  # already scheduled, don't duplicate
    job = app.job_queue.run_repeating(
        check_bio_job,
        interval=BIO_CHECK_INTERVAL_SECONDS,
        data={"user_id": user_id, "chat_id": chat_id},
        name=name,
    )
    # NOTE: passing first=0 to run_repeating does NOT run the job immediately
    # (this is a documented APScheduler limitation), so we force the first
    # run explicitly here instead of waiting for the first 2-hour interval.
    await job.run(app)


async def track_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or update.effective_chat.type not in ("group", "supergroup"):
        return
    user = update.effective_user
    if not user or user.is_bot:
        return

    chat_id = update.effective_chat.id
    if not await db.is_monitored(user.id, chat_id):
        await db.add_monitored(user.id, chat_id)
        await schedule_bio_job(context.application, user.id, chat_id)


async def restore_jobs(app: Application):
    monitored = await db.get_all_monitored()
    for doc in monitored:
        await schedule_bio_job(app, doc["user_id"], doc["chat_id"])
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
