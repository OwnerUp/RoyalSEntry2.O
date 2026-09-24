import os
import sys
import json
import random
import asyncio
import logging
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =====================================================
# CONFIGURATION
# =====================================================

TOKEN = os.getenv("TOKEN", "8932896459:AAH5Y_u8VjjdZUPACvSmDtYd0ZRZzjOcCBI")
OWNER_ID = int(os.getenv("OWNER_ID", "5059296601"))
IST = ZoneInfo("Asia/Kolkata")
DB_FILE = Path("queue_data.json")

BOT_VERSION = "v5.3-MultiChannel"
LAST_UPDATE_TIME = datetime.now(IST)

DAILY_MIN = 70
DAILY_MAX = 90
DELAY_MIN = 1
DELAY_MAX = 180

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
log = logging.getLogger("adaptive_bot")

# =====================================================
# STORAGE
# =====================================================

channel_queues: dict[str, list[dict]] = {}
channel_stats: dict[str, dict] = {}
channel_settings: dict[str, dict] = {}
admin_notices: set[str] = set()
running_worker_tasks: dict[str, asyncio.Task] = {}
owner_inputs: dict[int, tuple[str, str]] = {}
last_reset_date = datetime.now(IST).date()

# =====================================================
# HELPERS
# =====================================================

def now_local() -> datetime:
    return datetime.now(IST)

def channel_ids() -> set[str]:
    return set(channel_queues) | set(channel_stats) | set(channel_settings)

def is_owner(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id == OWNER_ID)

async def owner_notice(context: ContextTypes.DEFAULT_TYPE | Application, text: str) -> None:
    bot = getattr(context, "bot", context)
    try:
        await bot.send_message(chat_id=OWNER_ID, text=text, parse_mode="Markdown")
    except TelegramError:
        log.warning("Owner ko notification nahi bheja ja saka.")

def status_label(cid: str) -> str:
    config = channel_settings.get(cid, {})
    if not config.get("can_approve", False):
        return "⚠️ Permission Missing"
    return "🟢 ON" if config.get("enabled", True) else "🔴 OFF"

# =====================================================
# KEYBOARDS
# =====================================================

def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Channels List & Control", callback_data="channels")],
        [InlineKeyboardButton("ℹ️ Help / Commands", callback_data="help")],
    ])

def channels_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for cid in sorted(channel_ids(), key=lambda x: channel_settings.get(x, {}).get("title", x).casefold()):
        config = channel_settings.get(cid, {})
        icon = "🟢" if config.get("enabled", True) and config.get("can_approve", False) else "🔴"
        title = config.get("title", cid)
        rows.append([InlineKeyboardButton(f"{icon} {title[:28]}", callback_data=f"channel:{cid}")])
    rows.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="home")])
    return InlineKeyboardMarkup(rows)

def channel_keyboard(cid: str) -> InlineKeyboardMarkup:
    config = channel_settings.get(cid, {})
    toggle = "⏸ Stop (Turn OFF)" if config.get("enabled", True) else "▶️ Start (Turn ON)"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle, callback_data=f"toggle:{cid}")],
        [InlineKeyboardButton("🎯 Set Daily Limit", callback_data=f"limit:{cid}"),
         InlineKeyboardButton("⏱ Set Delay", callback_data=f"delay:{cid}")],
        [InlineKeyboardButton("⬅️ Back to Channels", callback_data="channels")],
    ])

def channel_details(cid: str) -> str:
    config = channel_settings.get(cid, {})
    stats = channel_stats.get(cid, {})
    return (
        f"📢 *Channel:* {config.get('title', cid)}\n"
        f"🆔 *ID:* `{cid}`\n"
        f"⚡ *Status:* {status_label(cid)}\n"
        f"📊 *Approved Today:* {stats.get('approved_today', 0)} / {stats.get('daily_limit', DAILY_MAX)}\n"
        f"🎯 *Daily Range:* {config.get('daily_min', DAILY_MIN)} – {config.get('daily_max', DAILY_MAX)}\n"
        f"⏱ *Delay Range:* {config.get('delay_min', DELAY_MIN)} – {config.get('delay_max', DELAY_MAX)} min\n"
        f"👥 *Queue:* {len(channel_queues.get(cid, []))}"
    )

def help_text() -> str:
    return (
        "🛠 *Owner Commands:*\n\n"
        "• `/channels` — Channels list aur controls\n"
        "• `/on CHANNEL_ID` — Auto-accept shuru karein\n"
        "• `/off CHANNEL_ID` — Auto-accept band karein\n"
        "• `/limit CHANNEL_ID MIN MAX` — Daily target limit set karein\n"
        "• `/delay CHANNEL_ID MIN MAX` — Delay range minutes set karein\n"
        "• `/myid` — Apna user ID check karein"
    )

# =====================================================
# DATABASE MANAGEMENT
# =====================================================

def save_database() -> None:
    try:
        DB_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "queues": channel_queues,
            "channel_stats": channel_stats,
            "channel_settings": channel_settings,
            "admin_notices": sorted(list(admin_notices)),
        }
        fd, temp_name = tempfile.mkstemp(prefix="queue_data_", suffix=".tmp", dir=str(DB_FILE.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, DB_FILE)
    except Exception as e:
        log.error(f"Save DB Error: {e}")

def ensure_channel(channel_id: str | int, title: str | None = None, *, can_approve: bool | None = None) -> str:
    cid = str(channel_id)
    channel_queues.setdefault(cid, [])
    settings = channel_settings.setdefault(cid, {})
    if title:
        settings["title"] = title
    else:
        settings.setdefault("title", cid)

    settings.setdefault("enabled", True)
    settings.setdefault("can_approve", True if can_approve is None and "can_approve" not in settings else bool(can_approve))
    settings.setdefault("daily_min", DAILY_MIN)
    settings.setdefault("daily_max", DAILY_MAX)
    settings.setdefault("delay_min", DELAY_MIN)
    settings.setdefault("delay_max", DELAY_MAX)

    if can_approve is not None:
        settings["can_approve"] = bool(can_approve)

    stats = channel_stats.setdefault(cid, {})
    stats.setdefault("approved_today", 0)
    stats.setdefault("daily_limit", random.randint(settings["daily_min"], settings["daily_max"]))
    stats.setdefault("date", now_local().date().isoformat())
    return cid

def load_database() -> None:
    global channel_queues, channel_stats, channel_settings, admin_notices
    if not DB_FILE.exists():
        save_database()
        return
    try:
        with DB_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        channel_queues = {str(k): v if isinstance(v, list) else [] for k, v in data.get("queues", {}).items()}
        channel_stats = {str(k): v if isinstance(v, dict) else {} for k, v in data.get("channel_stats", {}).items()}
        channel_settings = {str(k): v if isinstance(v, dict) else {} for k, v in data.get("channel_settings", {}).items()}
        admin_notices = set(map(str, data.get("admin_notices", [])))
        for cid in list(channel_ids()):
            old_queue = channel_queues.get(cid, [])
            old_title = next((item.get("channel_name") for item in old_queue if item.get("channel_name")), None)
            ensure_channel(cid, old_title)
        reset_daily()
        print("✅ Database Loaded")
    except Exception as e:
        print(f"❌ DB Load Error: {e}")

def reset_daily() -> None:
    today = now_local().date().isoformat()
    changed = False
    for cid in list(channel_ids()):
        ensure_channel(cid)
        stats = channel_stats[cid]
        settings = channel_settings[cid]
        if stats.get("date") != today:
            stats["approved_today"] = 0
            stats["daily_limit"] = random.randint(settings["daily_min"], settings["daily_max"])
            stats["date"] = today
            changed = True
    if changed:
        save_database()
        log.info("Daily limits reset")

def total_pending_users() -> int:
    return sum(len(queue) for queue in channel_queues.values())

# =====================================================
# DELAY CALCULATION
# =====================================================

def get_dynamic_delay(channel_id: str) -> int:
    now = now_local()
    current_hour = now.hour
    pending_users = total_pending_users()
    stats = channel_stats.get(channel_id, {})
    approved_today = stats.get("approved_today", 0)
    daily_limit = stats.get("daily_limit", DAILY_MAX)

    if current_hour < 13:
        target_now = 40
    elif current_hour < 18:
        target_now = int(daily_limit * 0.70)
    elif current_hour < 22:
        target_now = int(daily_limit * 0.90)
    else:
        target_now = daily_limit

    if approved_today < target_now:
        if pending_users > 20:
            choices = [60, 120, 180, 240, 300]
        elif pending_users > 10:
            choices = [120, 180, 240, 300, 420]
        else:
            choices = [180, 240, 300, 420, 600]
    else:
        if 6 <= current_hour < 13:
            choices = [300, 420, 600, 900]
        elif 13 <= current_hour < 24:
            choices = [900, 1200, 1800, 2400, 3600]
        else:
            choices = [3600, 5400, 7200, 10800]

    settings = channel_settings.get(channel_id, {})
    low = settings.get("delay_min", DELAY_MIN) * 60
    high = settings.get("delay_max", DELAY_MAX) * 60
    usable = [d for d in choices if low <= d <= high]
    if usable:
        return random.choice(usable)
    return min(high, max(low, random.choice(choices)))

# =====================================================
# WORKER
# =====================================================

def ensure_worker(channel_id: str | int, app: Application) -> None:
    cid = str(channel_id)
    existing_task = running_worker_tasks.get(cid)
    if existing_task and not existing_task.done():
        return

    task = asyncio.create_task(channel_worker(cid, app))
    running_worker_tasks[cid] = task

    def done_callback(t: asyncio.Task) -> None:
        if not t.cancelled() and t.exception():
            log.warning(f"Worker for {cid} exited with exception: {t.exception()}. Will revive on next update.")

    task.add_done_callback(done_callback)

async def channel_worker(channel_id: str, app: Application) -> None:
    while True:
        try:
            reset_daily()
            config = channel_settings.get(channel_id)
            if not config or not config.get("enabled", True) or not config.get("can_approve", False):
                await asyncio.sleep(20)
                continue

            stats = channel_stats.get(channel_id, {})
            queue = channel_queues.setdefault(channel_id, [])

            if stats.get("approved_today", 0) >= stats.get("daily_limit", DAILY_MAX):
                await asyncio.sleep(300)
                continue

            if not queue:
                await asyncio.sleep(25)
                continue

            item = queue[0]
            delay = get_dynamic_delay(channel_id)
            minutes = round(delay / 60, 1)

            log.info("[%s] %s waiting %s min | Queue: %s", config.get("title", channel_id), item.get("user_name", item.get("user_id")), minutes, total_pending_users())
            await asyncio.sleep(delay)

            config = channel_settings.get(channel_id)
            if not config or not config.get("enabled", True) or not config.get("can_approve", False):
                continue

            reset_daily()
            stats = channel_stats.get(channel_id, {})
            if stats.get("approved_today", 0) >= stats.get("daily_limit", DAILY_MAX):
                continue

            try:
                await app.bot.approve_chat_join_request(chat_id=int(channel_id), user_id=int(item["user_id"]))
                if queue and int(queue[0].get("user_id", 0)) == int(item["user_id"]):
                    queue.pop(0)
                stats["approved_today"] = stats.get("approved_today", 0) + 1
                save_database()
                log.info("✅ Approved %s in %s | Today: %s/%s", item.get("user_name"), config.get("title"), stats["approved_today"], stats["daily_limit"])
            except RetryAfter as exc:
                wait_time = int(exc.retry_after)
                log.warning("FloodWait %ss for %s", wait_time, channel_id)
                await asyncio.sleep(wait_time + 1)
            except Forbidden:
                config["can_approve"] = False
                save_database()
                await owner_notice(app, f"🔴 *Permission Removed!*\nChannel: {config.get('title')} (`{channel_id}`)\nBot ko admin permission wapas dein.")
                await asyncio.sleep(20)
            except (TimedOut, NetworkError):
                await asyncio.sleep(30)
            except BadRequest as e:
                if queue and int(queue[0].get("user_id", 0)) == int(item["user_id"]):
                    queue.pop(0)
                save_database()
                log.info("Skipped stale request in %s: %s", config.get("title"), e)
            except TelegramError as te:
                log.warning("Telegram error in %s: %s", channel_id, te)
                await asyncio.sleep(30)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"Worker unhandled error in {channel_id}: {e}")
            await asyncio.sleep(10)

# =====================================================
# HANDLERS
# =====================================================

async def handle_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    request = update.chat_join_request
    if not request:
        return

    cid = str(request.chat.id)
    was_unknown = cid not in channel_settings
    ensure_channel(cid, request.chat.title, can_approve=True)

    if was_unknown:
        await owner_notice(context, f"📌 *Naya Channel Request se Add Hua:*\n📢 Channel: {request.chat.title}\n🆔 ID: `{cid}`")

    queue = channel_queues[cid]
    if any(int(item.get("user_id", -1)) == request.from_user.id for item in queue):
        ensure_worker(cid, context.application)
        return

    queue.append({
        "user_id": request.from_user.id,
        "user_name": request.from_user.full_name,
        "channel_name": request.chat.title or cid,
        "created_at": now_local().isoformat(),
    })
    save_database()
    ensure_worker(cid, context.application)

async def bot_membership_changed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    change = update.my_chat_member
    if not change:
        return

    chat = change.chat
    cid = str(chat.id)
    old_status = change.old_chat_member.status
    new_member = change.new_chat_member
    new_status = new_member.status
    was_admin = old_status == "administrator"
    is_admin = new_status == "administrator"

    actor = change.from_user
    actor_name = f"@{actor.username}" if (actor and actor.username) else (actor.full_name if actor else "Unknown User")
    actor_id = actor.id if actor else "N/A"

    if not is_admin:
        if cid in channel_settings:
            channel_settings[cid]["can_approve"] = False
            save_database()
            if was_admin:
                await owner_notice(context, f"🔴 *Bot Admin Removed!*\n📢 *Channel:* {chat.title or cid}\n🆔 *ID:* `{cid}`\n👤 *Hataane Wala:* {actor_name} (`{actor_id}`)")
        return

    can_approve = bool(getattr(new_member, "can_invite_users", False))
    ensure_channel(cid, chat.title, can_approve=can_approve)
    save_database()

    if can_approve and channel_settings[cid].get("enabled", True):
        ensure_worker(cid, context.application)

    if not was_admin:
        admin_notices.add(cid)
        save_database()
        if can_approve:
            notice = (
                f"🎉 *Naya Channel Alert!*\n\n"
                f"👤 *Admin Banane Wala:* {actor_name} (`{actor_id}`)\n"
                f"📢 *Channel:* {chat.title or cid}\n"
                f"🆔 *Channel ID:* `{cid}`\n"
                f"✅ *Permission:* Approve requests OK\n"
                f"⚡ *Auto-Accept:* Chalu ho gaya"
            )
        else:
            notice = (
                f"⚠️ *Naya Channel Alert (Incomplete Permissions)!*\n\n"
                f"👤 *Admin Banane Wala:* {actor_name} (`{actor_id}`)\n"
                f"📢 *Channel:* {chat.title or cid}\n"
                f"🆔 *Channel ID:* `{cid}`\n"
                f"❌ *Note:* 'Invite Users via Link / Approve requests' permission band hai."
            )
        await owner_notice(context, notice)
    elif can_approve and not channel_settings[cid].get("can_approve"):
        await owner_notice(context, f"✅ *Permission Granted!*\n📢 *Channel:* {chat.title or cid} (`{cid}`)\nApprove requests permission mil gayi hai.")

# =====================================================
# COMMANDS & CALLBACKS
# =====================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    user_name = user.first_name if user and user.first_name else "Sir"

    diff = now_local() - LAST_UPDATE_TIME
    hours = int(diff.total_seconds() // 3600)
    minutes = int((diff.total_seconds() % 3600) // 60)

    welcome_text = (
        f"Hello {user_name} Sir 👋\n\n"
        f"🤖 Bot Version: {BOT_VERSION}\n"
        f"✅ Multi Channel System Active\n"
        f"🔄 Last Updated:\n"
        f"{hours}h {minutes}m ago"
    )

    if is_owner(update):
        welcome_text += "\n\n🎛 *Owner Control Panel:*"
        await update.effective_message.reply_text(welcome_text, reply_markup=main_keyboard(), parse_mode="Markdown")
    else:
        await update.effective_message.reply_text(welcome_text)

async def myid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user and update.effective_message:
        await update.effective_message.reply_text(f"Aapka Telegram ID: `{update.effective_user.id}`", parse_mode="Markdown")

async def channels_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    if not channel_ids():
        await update.effective_message.reply_text("Abhi tak koi channel registered nahi hai.")
        return
    await update.effective_message.reply_text("Channel chunein:", reply_markup=channels_keyboard())

async def set_enabled_command(update: Update, context: ContextTypes.DEFAULT_TYPE, enabled: bool) -> None:
    if not is_owner(update):
        return
    if len(context.args) != 1:
        await update.effective_message.reply_text("Format: `/on CHANNEL_ID` ya `/off CHANNEL_ID`", parse_mode="Markdown")
        return
    cid = str(context.args[0])
    if cid not in channel_settings:
        await update.effective_message.reply_text("Channel nahi mila. List ke liye `/channels` dekhein.", parse_mode="Markdown")
        return

    channel_settings[cid]["enabled"] = enabled
    save_database()
    if enabled and channel_settings[cid]["can_approve"]:
        ensure_worker(cid, context.application)
    status_str = "🟢 ON" if enabled else "🔴 OFF"
    await update.effective_message.reply_text(f"*{channel_settings[cid]['title']}* status: {status_str}", parse_mode="Markdown")

async def set_range_command(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str) -> None:
    if not is_owner(update):
        return
    if len(context.args) != 3:
        ex = "`/limit CHANNEL_ID 70 90`" if kind == "limit" else "`/delay CHANNEL_ID 1 180`"
        await update.effective_message.reply_text(f"Format: {ex}", parse_mode="Markdown")
        return
    cid = str(context.args[0])
    if cid not in channel_settings:
        await update.effective_message.reply_text("Channel nahi mila. `/channels` check karein.", parse_mode="Markdown")
        return
    try:
        low, high = int(context.args[1]), int(context.args[2])
    except ValueError:
        await update.effective_message.reply_text("MIN aur MAX numbers hone chahiye.")
        return
    if low < 0 or high < low:
        await update.effective_message.reply_text("Invalid range. MAX, MIN se bada hona chahiye.")
        return

    config = channel_settings[cid]
    if kind == "limit":
        config["daily_min"], config["daily_max"] = low, high
        channel_stats[cid]["daily_limit"] = random.randint(low, high)
        label = "Daily target"
    else:
        config["delay_min"], config["delay_max"] = low, high
        label = "Delay minutes"
    save_database()
    await update.effective_message.reply_text(f"✅ *{config['title']}*: {label} range `{low}`–`{high}` ho gaya.", parse_mode="Markdown")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if query.from_user.id != OWNER_ID:
        await query.answer("Sirf Owner access kar sakta hai.", show_alert=True)
        return
    await query.answer()
    data = query.data or ""

    try:
        if data == "home":
            await query.edit_message_text("🎛 Control Panel:", reply_markup=main_keyboard())
        elif data == "help":
            await query.edit_message_text(help_text(), reply_markup=main_keyboard(), parse_mode="Markdown")
        elif data == "channels":
            if channel_ids():
                await query.edit_message_text("Channel chunein:", reply_markup=channels_keyboard())
            else:
                await query.edit_message_text("Abhi tak koi channel registered nahi hai.", reply_markup=main_keyboard())
        elif data.startswith("channel:"):
            cid = data.split(":", 1)[1]
            if cid in channel_settings:
                await query.edit_message_text(channel_details(cid), reply_markup=channel_keyboard(cid), parse_mode="Markdown")
        elif data.startswith("toggle:"):
            cid = data.split(":", 1)[1]
            if cid in channel_settings:
                channel_settings[cid]["enabled"] = not channel_settings[cid]["enabled"]
                save_database()
                if channel_settings[cid]["enabled"] and channel_settings[cid]["can_approve"]:
                    ensure_worker(cid, context.application)
                await query.edit_message_text(channel_details(cid), reply_markup=channel_keyboard(cid), parse_mode="Markdown")
        elif data.startswith("limit:") or data.startswith("delay:"):
            kind, cid = data.split(":", 1)
            if cid not in channel_settings:
                return
            owner_inputs[OWNER_ID] = (kind, cid)
            sample = "70 90" if kind == "limit" else "5 30"
            await query.message.reply_text(f"*{channel_settings[cid]['title']}* ke liye MIN MAX numbers space ke saath bhejein.\nExample: `{sample}`", parse_mode="Markdown")
    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            raise e

async def owner_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update) or not update.effective_message or not update.effective_message.text:
        return
    pending = owner_inputs.get(OWNER_ID)
    if not pending:
        return
    kind, cid = pending
    try:
        low, high = map(int, update.effective_message.text.split())
    except ValueError:
        await update.effective_message.reply_text("Do numbers bhejein, jaise: `70 90`", parse_mode="Markdown")
        return
    if low < 0 or high < low:
        await update.effective_message.reply_text("Invalid range. Dobara bhejein.")
        return

    config = channel_settings.get(cid)
    if not config:
        owner_inputs.pop(OWNER_ID, None)
        return

    if kind == "limit":
        config["daily_min"], config["daily_max"] = low, high
        channel_stats[cid]["daily_limit"] = random.randint(low, high)
        label = "Daily target"
    else:
        config["delay_min"], config["delay_max"] = low, high
        label = "Delay minutes"

    owner_inputs.pop(OWNER_ID, None)
    save_database()
    await update.effective_message.reply_text(f"✅ *{config['title']}*: {label} range `{low}`–`{high}` set ho gaya.", reply_markup=channel_keyboard(cid), parse_mode="Markdown")

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, BadRequest) and "Message is not modified" in str(context.error):
        return
    log.error(f"Exception while handling update: {context.error}")

# =====================================================
# STARTUP & MAIN
# =====================================================

async def startup(app: Application) -> None:
    print("🚀 Adaptive Smart Queue Bot Started...")
    me = await app.bot.get_me()
    for cid in sorted(channel_ids()):
        config = channel_settings.get(cid, {})
        try:
            member = await app.bot.get_chat_member(int(cid), me.id)
            rights = (member.status == "administrator") and bool(getattr(member, "can_invite_users", False))
            config["can_approve"] = rights
        except Exception as e:
            log.warning(f"Startup check note for {cid}: {e}. Retaining status.")
            if "can_approve" not in config:
                config["can_approve"] = True

        save_database()
        # Pehle se save queue ko continue karne ke liye worker start karein
        if config.get("enabled", True):
            ensure_worker(cid, app)

    if OWNER_ID > 0:
        await owner_notice(app, f"🚀 *Bot Online Hai!*\nVersion: {BOT_VERSION}\nControl Panel ke liye `/start` karein.")

def main() -> None:
    load_database()

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(60)
        .pool_timeout(60)
        .post_init(startup)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("myid", myid_command))
    app.add_handler(CommandHandler("channels", channels_command))
    app.add_handler(CommandHandler("on", lambda u, c: set_enabled_command(u, c, True)))
    app.add_handler(CommandHandler("off", lambda u, c: set_enabled_command(u, c, False)))
    app.add_handler(CommandHandler("limit", lambda u, c: set_range_command(u, c, "limit")))
    app.add_handler(CommandHandler("delay", lambda u, c: set_range_command(u, c, "delay")))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(ChatJoinRequestHandler(handle_request))
    app.add_handler(ChatMemberHandler(bot_membership_changed, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, owner_text_handler))
    app.add_error_handler(global_error_handler)

    app.run_polling(
        allowed_updates=["message", "callback_query", "chat_join_request", "my_chat_member"],
        drop_pending_updates=False,
    )

if __name__ == "__main__":
    main()
