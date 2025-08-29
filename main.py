import asyncio
import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime, date, timezone
from typing import Dict, Optional, Tuple

import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from pytz import timezone as tz_get, all_timezones
from telegram import Update

from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

DATA_FILE = "user_data.json"
DEFAULT_TZ = os.getenv("LOCAL_TZ", "Asia/Tashkent")
CF_API = "https://codeforces.com/api"

REMINDER_TIMES = ["08:00", "12:00", "22:00"]  # HH:MM in LOCAL_TZ (default Europe/Amsterdam)

# --------------------------- Persistence ---------------------------

@dataclass
class UserRecord:
    handle: Optional[str] = None
    timezone: str = DEFAULT_TZ  # IANA name, e.g., "Europe/Amsterdam"
    # Track last reminder date (YYYY-MM-DD) per slot to avoid duplicate pings on the same day
    last_notified: Dict[str, str] = None  # key = "HH:MM", value = "YYYY-MM-DD"

    def to_dict(self):
        d = asdict(self)
        if d["last_notified"] is None:
            d["last_notified"] = {}
        return d


def load_db() -> Dict[str, UserRecord]:
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)
    out = {}
    for k, v in raw.items():
        out[k] = UserRecord(
            handle=v.get("handle"),
            timezone=v.get("timezone", DEFAULT_TZ),
            last_notified=v.get("last_notified", {}),
        )
    return out


def save_db(db: Dict[str, UserRecord]):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump({k: v.to_dict() for k, v in db.items()}, f, ensure_ascii=False, indent=2)


DB: Dict[str, UserRecord] = load_db()

# --------------------------- Helpers ---------------------------

async def cf_has_solved_today(handle: str, user_tz_name: str) -> bool:
    """
    Returns True if the user has at least one accepted submission today (in their timezone).
    """
    if not handle:
        return False

    if user_tz_name not in all_timezones:
        user_tz_name = DEFAULT_TZ

    user_tz = tz_get(user_tz_name)
    today_local = datetime.now(user_tz).date()

    # Fetch last ~200 submissions (enough for daily check) using Codeforces API
    # API returns newest first when using from=1, count=200
    url = f"{CF_API}/user.status?handle={handle}&from=1&count=200"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=20) as resp:
            data = await resp.json()
    if data.get("status") != "OK":
        return False

    for sub in data.get("result", []):
        if sub.get("verdict") == "OK":
            # creationTimeSeconds is POSIX UTC seconds
            ts = sub.get("creationTimeSeconds")
            if ts is None:
                continue
            dt_utc = datetime.fromtimestamp(ts, tz=timezone.utc)
            dt_local = dt_utc.astimezone(user_tz)
            if dt_local.date() == today_local:
                return True
    return False


def parse_time(hhmm: str) -> Tuple[int, int]:
    h, m = hhmm.split(":")
    return int(h), int(m)


def mark_notified(user_id: str, slot: str):
    rec = DB.setdefault(user_id, UserRecord())
    if rec.last_notified is None:
        rec.last_notified = {}
    rec.last_notified[slot] = date.today().isoformat()
    save_db(DB)


def already_notified_today(user_id: str, slot: str, user_tz_name: str) -> bool:
    rec = DB.get(user_id)
    if not rec:
        return False
    if rec.last_notified is None:
        return False
    if user_tz_name not in all_timezones:
        user_tz_name = DEFAULT_TZ
    user_tz = tz_get(user_tz_name)
    today_local = datetime.now(user_tz).date().isoformat()
    return rec.last_notified.get(slot) == today_local


# --------------------------- Telegram Handlers ---------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    DB.setdefault(uid, UserRecord())
    save_db(DB)

    text = (
        "Assalomu alaykum!\n\n"
        "✳️ Bu bot Codeforces streakingizni kuzatadi.\n"
        "Agar bugun hali AC qilmagan bo‘lsangiz, soat 08:00, 12:00 va 22:00 da eslatadi.\n\n"
        "⚙️ Sozlash:\n"
        "• /sethandle <handle> — Codeforces profilingiz.\n"
        "• /streak — Bugungi holatni tekshirish.\n"
        "• /settz <IANA_tz> — Vaqt zonasi (masalan, Europe/Amsterdam yoki Asia/Tashkent).\n"
        "• /whoami — Joriy sozlamalar.\n\n"
        f"Default vaqt zonasi: {DEFAULT_TZ}\n"
        "(Har bir foydalanuvchi o‘z zonasi bilan ishlashi mumkin.)"
    )

    await update.effective_message.reply_text(text)



async def cmd_sethandle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if not context.args:
        await update.effective_message.reply_text("Foydalanish: /sethandle <codeforces_handle>")
        return
    handle = context.args[0]
    rec = DB.setdefault(uid, UserRecord())
    rec.handle = handle
    save_db(DB)
    await update.effective_message.reply_text(f"✅ Handle saqlandi: {handle}")


async def cmd_settz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if not context.args:
        await update.effective_message.reply_text(
            "Foydalanish: /settz <IANA_timezone>, masalan: Europe/Amsterdam yoki Asia/Tashkent"
        )
        return
    tz_name = context.args[0]
    if tz_name not in all_timezones:
        await update.effective_message.reply_text("❌ Notog‘ri timezone. IANA ro‘yxatidan foydalaning.")
        return
    rec = DB.setdefault(uid, UserRecord())
    rec.timezone = tz_name
    save_db(DB)
    await update.effective_message.reply_text(f"✅ Timezone saqlandi: {tz_name}")


async def cmd_streak(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    rec = DB.get(uid)
    if not rec or not rec.handle:
        await update.effective_message.reply_text("Avval /sethandle buyruğini yuboring.")
        return
    ok_today = await cf_has_solved_today(rec.handle, rec.timezone)
    if ok_today:
        await update.effective_message.reply_text("🎉 Bugun allaqachon AC bor! Ajoyib!")
    else:
        await update.effective_message.reply_text("⏰ Hali AC yo‘q. Omad! (Eslatmalar ishlaydi)")


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    rec = DB.get(uid, UserRecord())
    await update.effective_message.reply_html(
        f"👤 Siz:\n"
        f"• Handle: <b>{rec.handle or 'yo‘q'}</b>\n"
        f"• Timezone: <b>{rec.timezone}</b>\n"
        f"• Reminderlar: {', '.join(REMINDER_TIMES)}"
    )


# --------------------------- Reminder Logic ---------------------------

async def send_reminders(app: Application, slot: str):
    """Check all users and ping those who haven't solved today, once per slot/day."""
    for uid, rec in DB.items():
        # Skip users without handle
        if not rec.handle:
            continue
        # Avoid duplicate reminders within the same local day
        if already_notified_today(uid, slot, rec.timezone):
            continue
        try:
            ok_today = await cf_has_solved_today(rec.handle, rec.timezone)
        except Exception:
            ok_today = False
        if not ok_today:
            try:
                await app.bot.send_message(
                    chat_id=int(uid),
                    text=(
                        f"⏰ Eslatma ({slot})\n"
                        f"Bugun hali Codeforces’da AC yo‘q. Keling, bitta yechamiz! 💪\n"
                        f"Handle: {rec.handle}"
                    ),
                )
                mark_notified(uid, slot)
            except Exception:
                pass
        else:
            # If streak already done, mark as notified to suppress further same-day pings
            mark_notified(uid, slot)


async def scheduler_job(app: Application):
    scheduler = AsyncIOScheduler()
    for slot in REMINDER_TIMES:
        h, m = parse_time(slot)
        trigger = CronTrigger(hour=h, minute=m, timezone=tz_get(DEFAULT_TZ))
        scheduler.add_job(send_reminders, trigger, args=[app, slot], id=f"reminder_{slot}")
    scheduler.start()


# --------------------------- App Bootstrap ---------------------------

tg_bot_token = "8054890903:AAHbFAvESZwCMhzV4h_naxT5ZcEmUg6pYso"

if __name__ == "__main__":
    import sys
    import asyncio

    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    app = ApplicationBuilder().token(tg_bot_token).build()

    # Telegram buyruqlarini qo‘shish
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("sethandle", cmd_sethandle))
    app.add_handler(CommandHandler("streak", cmd_streak))
    app.add_handler(CommandHandler("settz", cmd_settz))
    app.add_handler(CommandHandler("whoami", cmd_whoami))

    # Launch scheduler after bot starts
    app.post_init = lambda _: scheduler_job(app)
    # ... boshqa handlerlar ...

    print("CF Streak Bot started. Press Ctrl+C to stop.")
    app.run_polling()  # <-- shu yerda asyncio.run ishlatilmaydi
