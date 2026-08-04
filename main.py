import asyncio
import logging
import os
import random
import re
import aiosqlite
from datetime import datetime, timedelta
from urllib.parse import unquote, urlparse
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
PREMIUM_DAYS = 30
NOTIFY_BEFORE_DAYS = 3
PAGE_SIZE = 10
BOT_USERNAME = ""

API_TIMEOUT = 10
DB_PATH = "bot_data.db"

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN topilmadi! Replit Secrets bo'limiga qo'shing.")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

_db: aiosqlite.Connection = None


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        _db = await aiosqlite.connect(DB_PATH)
        _db.row_factory = aiosqlite.Row
    return _db


async def init_db():
    global _db
    _db = await aiosqlite.connect(DB_PATH)
    _db.row_factory = aiosqlite.Row

    await _db.execute("""
    CREATE TABLE IF NOT EXISTS channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id TEXT,
        title TEXT,
        link TEXT,
        type TEXT DEFAULT 'telegram'
    )""")

    await _db.execute("""
    CREATE TABLE IF NOT EXISTS manual_confirmations (
        user_id INTEGER,
        channel_id TEXT,
        confirmed_at TEXT,
        PRIMARY KEY (user_id, channel_id)
    )""")

    await _db.execute("""
    CREATE TABLE IF NOT EXISTS media (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT,
        title TEXT,
        category TEXT,
        file_id TEXT,
        part INTEGER,
        is_premium INTEGER DEFAULT 0,
        views INTEGER DEFAULT 0,
        likes INTEGER DEFAULT 0,
        dislikes INTEGER DEFAULT 0
    )""")

    await _db.execute("""
    CREATE TABLE IF NOT EXISTS premium_users (
        user_id INTEGER PRIMARY KEY,
        expire_date TEXT,
        notified INTEGER DEFAULT 0
    )""")

    await _db.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        first_seen TEXT
    )""")

    await _db.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )""")

    await _db.execute("""
    CREATE TABLE IF NOT EXISTS media_votes (
        user_id INTEGER,
        media_id INTEGER,
        vote INTEGER,
        PRIMARY KEY (user_id, media_id)
    )""")

    await _db.execute("""
    CREATE TABLE IF NOT EXISTS admins (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        added_at TEXT
    )""")

    await _db.commit()

    migrations = [
        "ALTER TABLE media ADD COLUMN is_premium INTEGER DEFAULT 0",
        "ALTER TABLE media ADD COLUMN views INTEGER DEFAULT 0",
        "ALTER TABLE media ADD COLUMN likes INTEGER DEFAULT 0",
        "ALTER TABLE media ADD COLUMN dislikes INTEGER DEFAULT 0",
        "ALTER TABLE premium_users ADD COLUMN expire_date TEXT",
        "ALTER TABLE premium_users ADD COLUMN notified INTEGER DEFAULT 0",
        "ALTER TABLE channels ADD COLUMN type TEXT DEFAULT 'telegram'",
    ]
    for sql in migrations:
        try:
            await _db.execute(sql)
            await _db.commit()
        except Exception:
            pass

    try:
        async with _db.execute("SELECT value FROM settings WHERE key='premium_price'") as cur:
            old_price = await cur.fetchone()
        if old_price:
            await _db.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES ('premium_price_30', ?)",
                (old_price[0],)
            )
            await _db.commit()
    except Exception:
        pass


# ─── FSM HOLATLARI ───────────────────────────────────────────────────────────

class MediaUpload(StatesGroup):
    category = State()
    title = State()
    code_choice = State()
    manual_code = State()
    is_premium = State()
    parts_count = State()
    waiting_for_videos = State()

class CodeSearch(StatesGroup):
    waiting_for_code = State()

class AdminChannel(StatesGroup):
    waiting_for_type = State()
    waiting_for_id = State()
    waiting_for_manual_title = State()
    waiting_for_manual_link = State()
    waiting_for_invite_title = State()
    waiting_for_invite_resolve = State()

class AdminDeleteMedia(StatesGroup):
    waiting_for_code = State()

class AdminPremium(StatesGroup):
    waiting_user_id = State()

class AdminBroadcast(StatesGroup):
    waiting_for_message = State()

class AdminPriceChange(StatesGroup):
    waiting_for_plan = State()
    waiting_for_price = State()

class PaymentReceipt(StatesGroup):
    waiting_for_receipt = State()

class AdminCardChange(StatesGroup):
    waiting_for_number = State()
    waiting_for_holder = State()

class AdminManage(StatesGroup):
    waiting_for_add_id = State()
    waiting_for_remove_id = State()


# ─── YORDAMCHI FUNKSIYALAR ───────────────────────────────────────────────────

async def is_premium_user(user_id: int) -> bool:
    db = await get_db()
    async with db.execute(
        "SELECT user_id, expire_date FROM premium_users WHERE user_id=?", (user_id,)
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return False
    if row["expire_date"]:
        expire = datetime.fromisoformat(row["expire_date"])
        if datetime.now() > expire:
            await db.execute("DELETE FROM premium_users WHERE user_id=?", (user_id,))
            await db.commit()
            return False
    return True

async def get_expire_date(user_id: int):
    db = await get_db()
    async with db.execute(
        "SELECT expire_date FROM premium_users WHERE user_id=?", (user_id,)
    ) as cur:
        row = await cur.fetchone()
    if not row or not row["expire_date"]:
        return None
    expire = datetime.fromisoformat(row["expire_date"])
    if datetime.now() > expire:
        await db.execute("DELETE FROM premium_users WHERE user_id=?", (user_id,))
        await db.commit()
        return None
    return expire

async def remove_premium(user_id: int):
    db = await get_db()
    await db.execute("DELETE FROM premium_users WHERE user_id=?", (user_id,))
    await db.commit()

async def safe_get_chat_member(chat_id, user_id: int):
    try:
        return await asyncio.wait_for(
            bot.get_chat_member(chat_id=chat_id, user_id=user_id),
            timeout=API_TIMEOUT
        )
    except asyncio.TimeoutError:
        logging.warning(f"get_chat_member timeout: chat={chat_id}, user={user_id}")
        return None
    except Exception as e:
        logging.error(f"get_chat_member xato ({chat_id}): {e}")
        return None

async def safe_get_chat(chat_id):
    try:
        return await asyncio.wait_for(
            bot.get_chat(chat_id),
            timeout=API_TIMEOUT
        )
    except asyncio.TimeoutError:
        logging.warning(f"get_chat timeout: {chat_id}")
        return None
    except Exception as e:
        logging.error(f"get_chat xato ({chat_id}): {e}")
        return None

async def get_channel_link(ch_id: str, link: str) -> str:
    """
    Kanal uchun havola qaytaradi.
    - Ochiq kanal (@username): doim https://t.me/username dan foydalanadi (eskirmaydi)
    - Yopiq kanal: kanal QO'SHILGANDA bir marta yaratilgan (member_limit va
      expire_date berilmagan) taklif havolasi qaytariladi. Bunday havolalar
      Telegram tomonidan o'z-o'zidan eskirmaydi, shuning uchun har bir
      tekshiruvda YANGI havola yaratishga hojat yo'q — aksincha, doimiy
      qayta-yaratish urinishlari Telegram tezlik cheklovi (rate limit)ga
      urilib, tasodifiy xatoliklarga sabab bo'lishi mumkin edi.
    """
    if str(ch_id).startswith("@"):
        username = ch_id.lstrip("@")
        return f"https://t.me/{username}"
    return link

def is_super_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

async def is_bot_admin(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    db = await get_db()
    async with db.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)) as cur:
        return (await cur.fetchone()) is not None

def md_escape(text) -> str:
    """Telegram Markdown v1 uchun maxsus belgilarni escape qiladi."""
    if text is None:
        return ""
    text = str(text)
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text

async def add_admin(user_id: int, username: str = ""):
    db = await get_db()
    await db.execute(
        "INSERT OR REPLACE INTO admins (user_id, username, added_at) VALUES (?, ?, ?)",
        (user_id, username, datetime.now().strftime("%d.%m.%Y %H:%M"))
    )
    await db.commit()

async def remove_admin(user_id: int):
    db = await get_db()
    await db.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
    await db.commit()

async def get_admins_list():
    db = await get_db()
    async with db.execute("SELECT user_id, username, added_at FROM admins ORDER BY added_at") as cur:
        return await cur.fetchall()

async def get_card_info():
    db = await get_db()
    async with db.execute("SELECT value FROM settings WHERE key='card_number'") as cur:
        row = await cur.fetchone()
    card_number = row["value"] if row else "Karta raqami hali kiritilmagan"
    async with db.execute("SELECT value FROM settings WHERE key='card_holder'") as cur:
        row2 = await cur.fetchone()
    card_holder = row2["value"] if row2 else ""
    return card_number, card_holder

async def set_card_info(card_number=None, card_holder=None):
    db = await get_db()
    if card_number is not None:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('card_number', ?)",
            (card_number,)
        )
    if card_holder is not None:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('card_holder', ?)",
            (card_holder,)
        )
    await db.commit()

async def add_premium(user_id: int, days: int = PREMIUM_DAYS):
    expire = datetime.now() + timedelta(days=days)
    db = await get_db()
    await db.execute(
        "INSERT OR REPLACE INTO premium_users (user_id, expire_date, notified) VALUES (?, ?, 0)",
        (user_id, expire.isoformat())
    )
    await db.commit()
    return expire

async def get_premium_price(days: int = 30) -> str:
    db = await get_db()
    async with db.execute(
        "SELECT value FROM settings WHERE key=?", (f"premium_price_{days}",)
    ) as cur:
        row = await cur.fetchone()
    if row:
        return row["value"]
    async with db.execute("SELECT value FROM settings WHERE key='premium_price'") as cur:
        row2 = await cur.fetchone()
    return row2["value"] if row2 else "50,000"

async def set_premium_price(price: str, days: int = 30):
    db = await get_db()
    await db.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (f"premium_price_{days}", price)
    )
    await db.commit()


# ─── KANAL USERNAME TOZALASH ─────────────────────────────────────────────────

def clean_tme_path(raw: str) -> str:
    """
    t.me havolasidan yoki username inputidan sof username ajratib oladi.
    URL-encoded belgilar (%20, %5F va h.k.), ortiqcha /, ? parametrlar,
    bosh-oxirdagi bo'sh joylar barchasini tozalaydi.
    """
    raw = raw.strip()
    raw = unquote(raw)
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if raw.lower().startswith(prefix.lower()):
            raw = raw[len(prefix):]
            break
    raw = raw.split("?")[0].strip("/").strip()
    return raw

def parse_channel_input(raw: str):
    """
    Admin tomonidan kiritilgan kanal input'ini tahlil qiladi.
    Qaytaradi: (chat_id_for_api, full_link_or_none, is_invite)
    """
    raw = raw.strip()
    decoded = unquote(raw)

    is_tme = any(decoded.lower().startswith(p) for p in (
        "https://t.me/", "http://t.me/", "t.me/"
    ))

    if is_tme:
        path = clean_tme_path(decoded)
        full_link = "https://t.me/" + path

        if path.startswith("+") or path.lower().startswith("joinchat/"):
            return None, full_link, True

        username = "@" + path.lstrip("@")
        return username, full_link, False

    stripped = decoded.lstrip("@").strip()
    if re.match(r'^-?\d+$', stripped):
        return int(stripped), None, False

    username = "@" + stripped.lstrip("@")
    return username, None, False


# ─── TUGMALAR ────────────────────────────────────────────────────────────────

def main_menu(user_id: int = 0):
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎬 Kinolar"), KeyboardButton(text="📺 Seriallar")],
            [KeyboardButton(text="⛩ Anime va Multfilm")],
            [KeyboardButton(text="🔍 Kod orqali qidirish"), KeyboardButton(text="🌟 Premium")],
        ],
        resize_keyboard=True
    )

def admin_menu(user_id: int = 0):
    rows = [
        [KeyboardButton(text="➕ Kino/Serial/Anime qo'shish")],
        [KeyboardButton(text="🗑 Kino/Serial/Anime o'chirish")],
        [KeyboardButton(text="📢 Kanal qo'shish"), KeyboardButton(text="🗑 Kanal o'chirish")],
        [KeyboardButton(text="📋 Kanallar ro'yxati")],
        [KeyboardButton(text="👥 Premium berish"), KeyboardButton(text="❌ Premium olish")],
        [KeyboardButton(text="📊 Premium ro'yxati")],
        [KeyboardButton(text="📊 Statistika")],
        [KeyboardButton(text="📣 Xabar yuborish")],
    ]
    if is_super_admin(user_id):
        rows.append([KeyboardButton(text="💰 Premium narxini o'zgartirish")])
        rows.append([KeyboardButton(text="💳 Karta raqamini o'zgartirish")])
        rows.append([KeyboardButton(text="👨‍💼 Adminlar")])
        rows.append([KeyboardButton(text="🗄 Zaxira olish")])
    rows.append([KeyboardButton(text="🏠 Bosh menyu")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


# ─── MAJBURIY OBUNA TEKSHIRISH ───────────────────────────────────────────────

async def check_subscriptions(user_id: int):
    """
    Foydalanuvchining barcha kanallarga obunasini tekshiradi.
    Qaytaradi: obuna bo'lmagan kanallar ro'yxati.
    """
    db = await get_db()
    async with db.execute("SELECT channel_id, link, title, type FROM channels") as cur:
        channels = await cur.fetchall()

    unsubscribed = []
    for ch in channels:
        ch_id = ch["channel_id"]
        link = ch["link"]
        title = ch["title"]
        ch_type = ch["type"]

        if ch_type in ("bot", "instagram", "manual"):
            async with db.execute(
                "SELECT 1 FROM manual_confirmations WHERE user_id=? AND channel_id=?",
                (user_id, ch_id)
            ) as cur2:
                confirmed = await cur2.fetchone()
            if not confirmed:
                unsubscribed.append((ch_id, link, title, ch_type))
            continue

        # Telegram kanal/guruh — API orqali tekshirish
        if str(ch_id).lstrip("-").isdigit():
            chat_id = int(ch_id)
        else:
            chat_id = ch_id  # @username ko'rinishida

        member = await safe_get_chat_member(chat_id, user_id)
        is_subscribed = member is not None and member.status not in ("left", "kicked")

        if not is_subscribed:
            # Kanalda "Yangi a'zolarni tasdiqlash" yoqilgan bo'lishi mumkin —
            # bunday holda foydalanuvchi hali rasman a'zo emas (admin
            # tasdiqlashini kutmoqda), lekin qo'shilish SO'ROVINI yuborgan
            # bo'lsa, buni yetarli deb hisoblaymiz.
            async with db.execute(
                "SELECT 1 FROM manual_confirmations WHERE user_id=? AND channel_id=?",
                (user_id, ch_id)
            ) as cur2:
                requested = await cur2.fetchone()
            if requested:
                is_subscribed = True

        if not is_subscribed:
            unsubscribed.append((ch_id, link, title, ch_type))

    return unsubscribed

async def build_subscription_keyboard(unsub) -> InlineKeyboardMarkup:
    """
    Faqat obuna bo'linmagan kanallar ko'rsatiladi.
    Faqat "✅ Tekshirish" va "🌟 Premium" tugmalari qoladi — "Bosdim" yo'q.
    """
    buttons = []
    for ch_id, link, title, ch_type in unsub:
        if ch_type in ("bot", "instagram", "manual"):
            icon = "🤖" if ch_type == "bot" else ("📸" if ch_type == "instagram" else "🔗")
            if link:
                buttons.append([InlineKeyboardButton(text=f"{icon} {title}", url=link)])
            else:
                buttons.append([InlineKeyboardButton(
                    text=f"{icon} {title}", callback_data="noop"
                )])
        else:
            # Telegram kanal: havola olish (public => username URL, private => yangi invite)
            real_link = await get_channel_link(ch_id, link)
            if real_link:
                buttons.append([InlineKeyboardButton(text=f"📢 {title}", url=real_link)])
            else:
                buttons.append([InlineKeyboardButton(
                    text=f"📢 {title}", callback_data="noop"
                )])
    # Faqat "Tekshirish" va "Premium" tugmalari
    buttons.append([InlineKeyboardButton(text="✅ Tekshirish", callback_data="check_sub")])
    buttons.append([InlineKeyboardButton(
        text="🌟 Premium tarifga obuna bo'lish", callback_data="req_premium"
    )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ─── FON VAZIFALAR ───────────────────────────────────────────────────────────

async def premium_checker():
    while True:
        await asyncio.sleep(12 * 3600)
        try:
            db = await get_db()
            async with db.execute(
                "SELECT user_id, expire_date FROM premium_users"
            ) as cur:
                all_users = await cur.fetchall()
            now = datetime.now()
            notify_threshold = now + timedelta(days=NOTIFY_BEFORE_DAYS)
            for row in all_users:
                uid = row["user_id"]
                expire_str = row["expire_date"]
                if not expire_str:
                    continue
                expire = datetime.fromisoformat(expire_str)
                if now > expire:
                    await db.execute("DELETE FROM premium_users WHERE user_id=?", (uid,))
                    await db.commit()
                    try:
                        await bot.send_message(
                            uid,
                            "⏰ *Premium obunangiz muddati tugadi.*\n\n"
                            "Davom ettirish uchun 🌟 *Premium* bo'limiga o'ting.",
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass
                elif expire <= notify_threshold:
                    async with db.execute(
                        "SELECT notified FROM premium_users WHERE user_id=?", (uid,)
                    ) as cur2:
                        nrow = await cur2.fetchone()
                    if nrow and nrow["notified"] == 0:
                        days_left = (expire - now).days + 1
                        await db.execute(
                            "UPDATE premium_users SET notified=1 WHERE user_id=?", (uid,)
                        )
                        await db.commit()
                        try:
                            await bot.send_message(
                                uid,
                                f"⚠️ *Diqqat!* Premium obunangiz *{days_left} kun* ichida tugaydi.\n\n"
                                f"📅 Tugash sanasi: *{expire.strftime('%d.%m.%Y')}*\n\n"
                                f"Uzaytirish uchun @{ADMIN_USERNAME} bilan bog'laning.",
                                parse_mode="Markdown"
                            )
                        except Exception:
                            pass
        except Exception as e:
            logging.error(f"Premium checker xatosi: {e}")

async def backup_scheduler():
    while True:
        await asyncio.sleep(24 * 3600)
        try:
            db = await get_db()
            await db.commit()
            backup_name = f"backup_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.db"
            await bot.send_document(
                chat_id=ADMIN_ID,
                document=types.FSInputFile(DB_PATH, filename=backup_name),
                caption=f"🗄 Avtomatik zaxira nusxa\n📅 {datetime.now().strftime('%d.%m.%Y %H:%M')}"
            )
        except Exception as e:
            logging.error(f"Zaxira yuborishda xato: {e}")


# ─── ZAXIRA OLISH ────────────────────────────────────────────────────────────

@dp.message(F.text == "🗄 Zaxira olish", StateFilter("*"))
async def manual_backup(message: types.Message, state: FSMContext):
    if not is_super_admin(message.from_user.id):
        return
    await state.clear()
    try:
        db = await get_db()
        await db.commit()
        backup_name = f"backup_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.db"
        await message.answer_document(
            document=types.FSInputFile(DB_PATH, filename=backup_name),
            caption=f"🗄 Qo'lda olingan zaxira\n📅 {datetime.now().strftime('%d.%m.%Y %H:%M')}"
        )
    except Exception as e:
        await message.answer(f"❌ Zaxira olishda xato: {e}")


# ─── START ───────────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    args = message.text.split(maxsplit=1)
    payload = args[1].strip() if len(args) > 1 else None
    user_id = message.from_user.id
    db = await get_db()

    await db.execute(
        "INSERT OR IGNORE INTO users (user_id, first_seen) VALUES (?, ?)",
        (user_id, datetime.now().isoformat())
    )
    await db.commit()

    is_prem = await is_premium_user(user_id)

    if payload:
        if not is_prem:
            unsub = await check_subscriptions(user_id)
            if unsub:
                kb = await build_subscription_keyboard(unsub)
                await message.answer(
                    "⚠️ Botdan foydalanish uchun quyidagi kanal(lar)ga obuna bo'ling:\n\n"
                    "ℹ️ _Premium a'zolarga majburiy kanal obunasi talab qilinmaydi!_",
                    reply_markup=kb, parse_mode="Markdown"
                )
                return
        await message.answer(
            "Xush kelibsiz! Kerakli bo'limni tanlang:",
            reply_markup=main_menu(user_id)
        )
        await deliver_media_by_code(message, user_id, payload)
        return

    if is_prem:
        await message.answer(
            "Xush kelibsiz! Kerakli bo'limni tanlang:",
            reply_markup=main_menu(user_id)
        )
        return

    unsub = await check_subscriptions(user_id)
    if unsub:
        kb = await build_subscription_keyboard(unsub)
        await message.answer(
            "⚠️ Botdan foydalanish uchun quyidagi kanal(lar)ga obuna bo'ling:\n\n"
            "ℹ️ _Premium a'zolarga majburiy kanal obunasi talab qilinmaydi!_",
            reply_markup=kb, parse_mode="Markdown"
        )
        return
    await message.answer(
        "Xush kelibsiz! Kerakli bo'limni tanlang:",
        reply_markup=main_menu(user_id)
    )

@dp.callback_query(F.data == "noop")
async def noop_cb(call: types.CallbackQuery):
    await call.answer("🔒 Bu maxfiy kanal. Admin orqali qo'shiling.", show_alert=True)

@dp.callback_query(F.data == "check_sub")
async def check_sub_cb(call: types.CallbackQuery):
    user_id = call.from_user.id
    if await is_premium_user(user_id):
        try:
            await call.message.delete()
        except Exception:
            pass
        await call.message.answer(
            "✅ Siz Premium a'zosiz! Menyudan foydalanishingiz mumkin:",
            reply_markup=main_menu(user_id)
        )
        await call.answer()
        return

    # bot/instagram/manual kanallar uchun manual tasdiq (API orqali tekshirib bo'lmaydi)
    db = await get_db()
    async with db.execute("SELECT channel_id, type FROM channels") as cur:
        all_channels = await cur.fetchall()
    for ch in all_channels:
        if ch["type"] in ("bot", "instagram", "manual"):
            await db.execute(
                "INSERT OR REPLACE INTO manual_confirmations "
                "(user_id, channel_id, confirmed_at) VALUES (?, ?, ?)",
                (user_id, ch["channel_id"], datetime.now().isoformat())
            )
    await db.commit()

    unsub = await check_subscriptions(user_id)
    if unsub:
        # Faqat hali obuna bo'linmagan kanallarni ko'rsat
        kb = await build_subscription_keyboard(unsub)
        try:
            await call.message.edit_text(
                "⚠️ Quyidagi kanal(lar)ga hali obuna bo'lmadingiz:\n\n"
                "ℹ️ _Premium a'zolarga majburiy kanal obunasi talab qilinmaydi!_",
                reply_markup=kb,
                parse_mode="Markdown"
            )
        except Exception:
            await call.message.answer(
                "⚠️ Quyidagi kanal(lar)ga hali obuna bo'lmadingiz:",
                reply_markup=kb
            )
        await call.answer("❌ Hali barcha kanallarga obuna bo'lmadingiz!", show_alert=True)
    else:
        try:
            await call.message.delete()
        except Exception:
            pass
        await call.message.answer(
            "✅ Obuna tasdiqlandi! Menyudan foydalanishingiz mumkin:",
            reply_markup=main_menu(user_id)
        )
        await call.answer()


# ─── MEDIA YETKAZIB BERISH ───────────────────────────────────────────────────

async def deliver_media_by_code(sendable, user_id: int, code: str):
    db = await get_db()
    async with db.execute(
        "SELECT id, file_id, title, part, is_premium, category, views, likes, dislikes "
        "FROM media WHERE code=? ORDER BY part ASC",
        (code,)
    ) as cur:
        results = await cur.fetchall()

    if not results:
        await sendable.answer("❌ Bunday kodli kino, serial yoki anime topilmadi.")
        return

    is_prem_content = results[0]["is_premium"]
    if is_prem_content and not await is_premium_user(user_id):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🌟 Premium olish", callback_data="req_premium")],
            [InlineKeyboardButton(
                text="👨‍💻 Admin bilan bog'lanish",
                url=f"https://t.me/{ADMIN_USERNAME}"
            )]
        ])
        await sendable.answer(
            "🔒 Bu *Premium* kontent!\n\nTomosha qilish uchun premium a'zo bo'ling.",
            reply_markup=kb, parse_mode="Markdown"
        )
        return

    user_is_admin = await is_bot_admin(user_id)

    for row in results:
        media_id = row["id"]
        file_id = row["file_id"]
        title = row["title"]
        part = row["part"]
        category = row["category"]
        views = (row["views"] or 0) + 1
        likes = row["likes"] or 0
        dislikes = row["dislikes"] or 0

        await db.execute("UPDATE media SET views=? WHERE id=?", (views, media_id))
        await db.commit()

        cat_icon = {"kino": "🎬", "serial": "📺", "anime": "⛩"}.get(category, "🎬")
        caption = (
            f"{cat_icon} {title}\n"
            f"📌 Qism: {part}\n"
            f"🔑 Kod: {code}\n"
            f"👁 Ko'rildi: {views:,} marta"
        )
        if not user_is_admin:
            caption += (
                "\n\n🔒 _Bu videoni saqlash yoki boshqa joyga uzatish uchun "
                "🌟 Premium tarifga o'ting._"
            )

        rating_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=f"👍 {likes}", callback_data=f"vote_{media_id}_1"
            ),
            InlineKeyboardButton(
                text=f"👎 {dislikes}", callback_data=f"vote_{media_id}_0"
            ),
        ]])
        try:
            await sendable.answer_video(
                video=file_id,
                caption=caption,
                parse_mode="Markdown",
                protect_content=not user_is_admin,
                reply_markup=rating_kb
            )
        except Exception as e:
            logging.error(f"Video yuborishda xato (media_id={media_id}): {e}")
            await sendable.answer(f"❌ '{title}' videoni yuborib bo'lmadi.")


# ─── RO'YXATLAR ──────────────────────────────────────────────────────────────

CATEGORY_INFO = {
    "kino":   {"icon": "🎬", "label": "Kinolar",
               "desc": "Eng saralangan va o'zbek tiliga tarjima qilingan kinolar!"},
    "serial": {"icon": "📺", "label": "Seriallar",
               "desc": "Eng mashhur va qiziqarli seriallar!"},
    "anime":  {"icon": "⛩", "label": "Anime va Multfilmlar",
               "desc": "Afsonaviy anime seriyalar va qiziqarli multfilmlar!"},
}

def build_media_list_kb(items, category: str, page: int):
    start = page * PAGE_SIZE
    page_items = items[start:start + PAGE_SIZE]
    buttons = [
        [InlineKeyboardButton(
            text=f"🎞 {title}", callback_data=f"getmedia_{code}"
        )]
        for code, title in page_items
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            text="⬅️ Oldingi", callback_data=f"page_{category}_{page - 1}"
        ))
    if start + PAGE_SIZE < len(items):
        nav.append(InlineKeyboardButton(
            text="Keyingi ➡️", callback_data=f"page_{category}_{page + 1}"
        ))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(
        text="⬅️ Orqaga", callback_data="close_list"
    )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def render_media_list(category: str, page: int = 0):
    db = await get_db()
    async with db.execute(
        "SELECT DISTINCT code, title FROM media "
        "WHERE category=? AND is_premium=0 ORDER BY id DESC",
        (category,)
    ) as cur:
        items = await cur.fetchall()
    items = [(row["code"], row["title"]) for row in items]
    info = CATEGORY_INFO[category]

    if not items:
        text = (
            f"{info['icon']} ✦ *{info['label']} Bo'limi* ✦\n"
            f"ℹ️ _{info['desc']}_\n\n"
            f"😔 Hozircha ochiq {info['label'].lower()} mavjud emas."
        )
        return text, None

    total_pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    text = (
        f"{info['icon']} ✦ *{info['label']} Bo'limi* ✦\n"
        f"ℹ️ _{info['desc']}_\n"
        f"📄 Sahifa {page + 1}/{total_pages}\n\n"
        f"👇 Kerakli tanlang:"
    )
    return text, build_media_list_kb(items, category, page)

@dp.message(F.text == "🎬 Kinolar")
async def list_movies(message: types.Message):
    unsub = await check_subscriptions(message.from_user.id)
    if unsub and not await is_premium_user(message.from_user.id):
        kb = await build_subscription_keyboard(unsub)
        await message.answer(
            "⚠️ Avval kanallarga obuna bo'ling:", reply_markup=kb
        )
        return
    text, kb = await render_media_list("kino", 0)
    await message.answer(text, parse_mode="Markdown", reply_markup=kb)

@dp.message(F.text == "📺 Seriallar")
async def list_serials(message: types.Message):
    unsub = await check_subscriptions(message.from_user.id)
    if unsub and not await is_premium_user(message.from_user.id):
        kb = await build_subscription_keyboard(unsub)
        await message.answer(
            "⚠️ Avval kanallarga obuna bo'ling:", reply_markup=kb
        )
        return
    text, kb = await render_media_list("serial", 0)
    await message.answer(text, parse_mode="Markdown", reply_markup=kb)

@dp.message(F.text == "⛩ Anime va Multfilm")
async def list_anime(message: types.Message):
    unsub = await check_subscriptions(message.from_user.id)
    if unsub and not await is_premium_user(message.from_user.id):
        kb = await build_subscription_keyboard(unsub)
        await message.answer(
            "⚠️ Avval kanallarga obuna bo'ling:", reply_markup=kb
        )
        return
    text, kb = await render_media_list("anime", 0)
    await message.answer(text, parse_mode="Markdown", reply_markup=kb)

@dp.callback_query(F.data.startswith("prempage_"))
async def prempage_cb(call: types.CallbackQuery):
    page = int(call.data.split("_")[1])
    text, kb = await render_premium_list(page)
    try:
        await call.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    except Exception:
        pass
    await call.answer()

@dp.callback_query(F.data.startswith("page_"))
async def page_cb(call: types.CallbackQuery):
    parts = call.data.split("_")
    category = parts[1]
    page = int(parts[2])
    text, kb = await render_media_list(category, page)
    try:
        await call.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    except Exception:
        pass
    await call.answer()

@dp.callback_query(F.data.startswith("getmedia_"))
async def getmedia_cb(call: types.CallbackQuery):
    code = call.data[len("getmedia_"):]
    user_id = call.from_user.id
    unsub = await check_subscriptions(user_id)
    if unsub and not await is_premium_user(user_id):
        kb = await build_subscription_keyboard(unsub)
        await call.message.answer("⚠️ Avval kanallarga obuna bo'ling:", reply_markup=kb)
        await call.answer()
        return
    await deliver_media_by_code(call.message, user_id, code)
    await call.answer()

@dp.callback_query(F.data == "close_list")
async def close_list_cb(call: types.CallbackQuery):
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.answer()


# ─── REYTING OVOZ ────────────────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("vote_"))
async def vote_cb(call: types.CallbackQuery):
    parts = call.data.split("_")
    media_id = int(parts[1])
    vote_value = int(parts[2])
    user_id = call.from_user.id
    db = await get_db()

    async with db.execute(
        "SELECT vote FROM media_votes WHERE user_id=? AND media_id=?",
        (user_id, media_id)
    ) as cur:
        existing = await cur.fetchone()

    if existing and existing["vote"] == vote_value:
        await call.answer("Siz allaqachon shu ovozni bergansiz.")
        return

    if existing:
        old_col = "likes" if existing["vote"] == 1 else "dislikes"
        await db.execute(
            f"UPDATE media SET {old_col} = MAX({old_col} - 1, 0) WHERE id=?", (media_id,)
        )
        await db.execute(
            "UPDATE media_votes SET vote=? WHERE user_id=? AND media_id=?",
            (vote_value, user_id, media_id)
        )
    else:
        await db.execute(
            "INSERT INTO media_votes (user_id, media_id, vote) VALUES (?, ?, ?)",
            (user_id, media_id, vote_value)
        )

    new_col = "likes" if vote_value == 1 else "dislikes"
    await db.execute(
        f"UPDATE media SET {new_col} = {new_col} + 1 WHERE id=?", (media_id,)
    )
    await db.commit()

    async with db.execute(
        "SELECT likes, dislikes FROM media WHERE id=?", (media_id,)
    ) as cur:
        row = await cur.fetchone()
    likes = row["likes"] or 0
    dislikes = row["dislikes"] or 0
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=f"👍 {likes}", callback_data=f"vote_{media_id}_1"
        ),
        InlineKeyboardButton(
            text=f"👎 {dislikes}", callback_data=f"vote_{media_id}_0"
        ),
    ]])
    try:
        await call.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        pass
    await call.answer("✅ Ovozingiz qabul qilindi!")


# ─── QIDIRUV ─────────────────────────────────────────────────────────────────

@dp.message(F.text == "🔍 Kod orqali qidirish")
async def ask_code(message: types.Message, state: FSMContext):
    unsub = await check_subscriptions(message.from_user.id)
    if unsub and not await is_premium_user(message.from_user.id):
        kb = await build_subscription_keyboard(unsub)
        await message.answer("⚠️ Avval kanallarga obuna bo'ling:", reply_markup=kb)
        return
    await state.set_state(CodeSearch.waiting_for_code)
    await message.answer(
        "🔍 *Kod orqali qidiruv*\n"
        "ℹ️ _TikTok/Reels da ko'rgan kino kodini kiriting!_\n\n"
        "Kino, Serial yoki Anime kodini kiriting:",
        parse_mode="Markdown"
    )

@dp.message(CodeSearch.waiting_for_code)
async def search_by_code(message: types.Message, state: FSMContext):
    code = message.text.strip()
    await deliver_media_by_code(message, message.from_user.id, code)
    await state.clear()


# ─── PREMIUM BO'LIM ──────────────────────────────────────────────────────────

def build_premium_list_kb(items, page: int):
    start = page * PAGE_SIZE
    page_items = items[start:start + PAGE_SIZE]
    icon_map = {"kino": "🎬", "serial": "📺", "anime": "⛩"}
    buttons = [
        [InlineKeyboardButton(
            text=f"{icon_map.get(cat, '🌟')} {title}",
            callback_data=f"getmedia_{code}"
        )]
        for code, title, cat in page_items
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            text="⬅️ Oldingi", callback_data=f"prempage_{page - 1}"
        ))
    if start + PAGE_SIZE < len(items):
        nav.append(InlineKeyboardButton(
            text="Keyingi ➡️", callback_data=f"prempage_{page + 1}"
        ))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(
        text="⬅️ Orqaga", callback_data="close_list"
    )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def render_premium_list(page: int = 0):
    db = await get_db()
    async with db.execute(
        "SELECT DISTINCT code, title, category FROM media "
        "WHERE is_premium=1 ORDER BY id DESC"
    ) as cur:
        rows = await cur.fetchall()
    items = [(row["code"], row["title"], row["category"]) for row in rows]

    if not items:
        return "Hozircha premium kontent joylanmagan.", None

    total_pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    text = (
        "🔒 *Premium kontent ro'yxati:*\n"
        f"📄 Sahifa {page + 1}/{total_pages}\n\n"
        "👇 Kerakli tanlang:"
    )
    return text, build_premium_list_kb(items, page)

@dp.message(F.text == "🌟 Premium", StateFilter(None))
async def premium_info(message: types.Message):
    user_id = message.from_user.id
    price = await get_premium_price(30)

    if await is_premium_user(user_id):
        expire = await get_expire_date(user_id)
        expire_str = expire.strftime("%d.%m.%Y") if expire else "Noma'lum"
        text, kb = await render_premium_list(0)
        await message.answer(
            f"🌟 *Siz Premium a'zosiz!*\n📅 Muddat: *{expire_str}* gacha\n\n{text}",
            parse_mode="Markdown", reply_markup=kb
        )
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"💳 Karta orqali to'lash ({price} so'm)",
            callback_data="pay_card"
        )],
        [InlineKeyboardButton(
            text="👨‍💻 Admin bilan bog'lanish",
            url=f"https://t.me/{ADMIN_USERNAME}"
        )],
    ])
    await message.answer(
        f"🌟 *Premium a'zolik*\n\n"
        f"✅ *Afzalliklar:*\n"
        f"• Barcha HD kinolar va seriallar\n"
        f"• Reklamasiz tomosha\n"
        f"• Video saqlash va uzatish huquqi\n"
        f"• Majburiy kanal obunasisiz foydalanish\n\n"
        f"💰 *Narxi:* {price} so'm / {PREMIUM_DAYS} kun\n\n"
        f"👨‍💻 *Admin:* @{ADMIN_USERNAME}\n\n"
        f"To'lov usulini tanlang:",
        parse_mode="Markdown", reply_markup=kb
    )


# ─── TO'LOV ───────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "req_premium")
async def req_premium_cb(call: types.CallbackQuery):
    price = await get_premium_price(30)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"💳 Karta orqali to'lash ({price} so'm)",
            callback_data="pay_card"
        )],
        [InlineKeyboardButton(
            text="👨‍💻 Admin bilan bog'lanish",
            url=f"https://t.me/{ADMIN_USERNAME}"
        )],
    ])
    await call.message.answer(
        f"🌟 *Premium a'zolik — {price} so'm / {PREMIUM_DAYS} kun*\n\n"
        f"Qaysi usul orqali to'lamoqchisiz?",
        parse_mode="Markdown", reply_markup=kb
    )
    await call.answer()

@dp.callback_query(F.data == "pay_card")
async def pay_card_cb(call: types.CallbackQuery, state: FSMContext):
    card_number, card_holder = await get_card_info()
    holder_line = f"\n👤 *Karta egasi:* {card_holder}" if card_holder else ""
    price = await get_premium_price(30)
    await state.set_state(PaymentReceipt.waiting_for_receipt)
    await call.message.answer(
        f"💳 *Premium uchun to'lov*\n\n"
        f"💳 Karta raqami: `{card_number}`{holder_line}\n"
        f"💰 Summasi: *{price} so'm* / {PREMIUM_DAYS} kun\n\n"
        f"1️⃣ Yuqoridagi kartaga to'lovni amalga oshiring.\n"
        f"2️⃣ To'lov chekining *rasmini (screenshot)* shu yerga yuboring.\n\n"
        f"⚠️ *Eslatma:* Chekni tashlamasangiz, Premium berilmaydi!",
        parse_mode="Markdown"
    )
    await call.answer()

@dp.message(PaymentReceipt.waiting_for_receipt, F.photo)
async def receive_payment_receipt(message: types.Message, state: FSMContext):
    user = message.from_user
    uname = f"@{user.username}" if user.username else "username yo'q"
    sent_time = datetime.now().strftime("%d.%m.%Y %H:%M")
    kb_admin = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Tasdiqlash", callback_data=f"approve_prem_{user.id}"
        ),
        InlineKeyboardButton(
            text="❌ Rad etish", callback_data=f"reject_prem_{user.id}"
        )
    ]])
    try:
        await bot.send_photo(
            chat_id=ADMIN_ID,
            photo=message.photo[-1].file_id,
            caption=(
                f"🧾 *Yangi to'lov cheki!*\n\n"
                f"👤 Ism: {user.full_name}\n"
                f"🔗 Username: {uname}\n"
                f"🆔 ID: `{user.id}`\n"
                f"🕒 Yuborilgan vaqt: {sent_time}\n\n"
                f"✅ Tasdiqlasangiz *{PREMIUM_DAYS} kun* premium beriladi."
            ),
            reply_markup=kb_admin,
            parse_mode="Markdown"
        )
        await message.answer(
            "✅ Chekingiz qabul qilindi va adminga yuborildi!\nTekshirilgach, Premium tasdiqlanadi."
        )
    except Exception:
        await message.answer(
            f"❌ Xatolik yuz berdi. Iltimos, chekni to'g'ridan-to'g'ri adminga yuboring: "
            f"@{ADMIN_USERNAME}"
        )
    await state.clear()

@dp.message(PaymentReceipt.waiting_for_receipt)
async def receipt_wrong_format(message: types.Message):
    await message.answer(
        "⚠️ Iltimos, to'lov chekining *rasmini (screenshot)* yuboring — "
        "matn qabul qilinmaydi.",
        parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("approve_prem_"))
async def approve_premium(call: types.CallbackQuery):
    target_id = int(call.data.split("_")[2])
    expire = await add_premium(target_id, PREMIUM_DAYS)
    expire_str = expire.strftime("%d.%m.%Y")
    await call.message.edit_text(
        f"✅ Foydalanuvchi `{target_id}` Premium a'zolikka qo'shildi!\n"
        f"📅 Muddat: *{expire_str}* gacha",
        parse_mode="Markdown"
    )
    try:
        await bot.send_message(
            target_id,
            f"🎉 *Tabriklaymiz!* Premium obunangiz tasdiqlandi.\n\n"
            f"📅 *Muddat:* {PREMIUM_DAYS} kun ({expire_str} gacha)\n\n"
            f"🌟 *Premium* tugmasini bosib barcha eksklyuziv kinolarni tomosha qiling!",
            parse_mode="Markdown"
        )
    except Exception:
        pass

@dp.callback_query(F.data.startswith("reject_prem_"))
async def reject_premium(call: types.CallbackQuery):
    target_id = int(call.data.split("_")[2])
    await call.message.edit_text(
        f"❌ Foydalanuvchi `{target_id}` so'rovi rad etildi.",
        parse_mode="Markdown"
    )
    try:
        await bot.send_message(
            target_id,
            "❌ Afsuski, to'lovingiz tasdiqlanmadi.\n\n"
            "Muammo bo'lsa, adminimizga murojaat qiling."
        )
    except Exception:
        pass


# ─── ADMIN PANEL ─────────────────────────────────────────────────────────────

@dp.message(Command("admin"), StateFilter("*"))
async def admin_panel(message: types.Message, state: FSMContext):
    if not await is_bot_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer(
        "👨‍💻 Admin panelga xush kelibsiz!",
        reply_markup=admin_menu(message.from_user.id)
    )

@dp.message(Command("bekor"), StateFilter("*"))
async def cancel_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    is_adm = await is_bot_admin(message.from_user.id)
    menu = admin_menu(message.from_user.id) if is_adm else main_menu(message.from_user.id)
    await message.answer("❌ Amal bekor qilindi.", reply_markup=menu)

@dp.message(F.text == "🏠 Bosh menyu", StateFilter("*"))
async def back_to_main(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Bosh menyu:", reply_markup=main_menu(message.from_user.id))


# ─── PREMIUM BERISH (ADMIN) ───────────────────────────────────────────────────

@dp.message(F.text == "👥 Premium berish", StateFilter("*"))
async def admin_give_premium(message: types.Message, state: FSMContext):
    if not await is_bot_admin(message.from_user.id):
        return
    await state.clear()
    await state.update_data(action="give")
    await state.set_state(AdminPremium.waiting_user_id)
    await message.answer(
        "Premium bermoqchi bo'lgan foydalanuvchining *Telegram ID* sini kiriting:\n\n"
        "_(ID ni bilish uchun foydalanuvchi @userinfobot ga yozsin)_",
        parse_mode="Markdown",
        reply_markup=types.ReplyKeyboardRemove()
    )

@dp.message(F.text == "❌ Premium olish", StateFilter("*"))
async def admin_remove_premium_cmd(message: types.Message, state: FSMContext):
    if not await is_bot_admin(message.from_user.id):
        return
    await state.clear()
    await state.update_data(action="remove")
    await state.set_state(AdminPremium.waiting_user_id)
    await message.answer(
        "Premium *olmoqchi* bo'lgan foydalanuvchining *Telegram ID* sini kiriting:",
        parse_mode="Markdown",
        reply_markup=types.ReplyKeyboardRemove()
    )

@dp.message(AdminPremium.waiting_user_id)
async def process_premium_action(message: types.Message, state: FSMContext):
    try:
        target_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Noto'g'ri ID. Faqat raqam kiriting.")
        return

    data = await state.get_data()
    action = data.get("action", "give")
    if action == "give":
        expire = await add_premium(target_id, PREMIUM_DAYS)
        expire_str = expire.strftime("%d.%m.%Y")
        await message.answer(
            f"✅ Foydalanuvchi `{target_id}` ga {PREMIUM_DAYS} kunlik Premium berildi.\n"
            f"📅 Tugash sanasi: *{expire_str}*",
            reply_markup=admin_menu(message.from_user.id), parse_mode="Markdown"
        )
        try:
            await bot.send_message(
                target_id,
                f"🎉 *Tabriklaymiz!* Sizga {PREMIUM_DAYS} kunlik Premium berildi.\n"
                f"📅 *Muddat:* {expire_str} gacha\n\n"
                f"🌟 *Premium* tugmasini bosib tomosha qiling!",
                parse_mode="Markdown"
            )
        except Exception:
            pass
    else:
        if await is_premium_user(target_id):
            await remove_premium(target_id)
            await message.answer(
                f"✅ Foydalanuvchi `{target_id}` ning Premium obunasi bekor qilindi.",
                reply_markup=admin_menu(message.from_user.id), parse_mode="Markdown"
            )
            try:
                await bot.send_message(
                    target_id,
                    "❌ Sizning Premium obunangiz admin tomonidan bekor qilindi."
                )
            except Exception:
                pass
        else:
            await message.answer(
                f"⚠️ Foydalanuvchi `{target_id}` premium a'zo emas.",
                reply_markup=admin_menu(message.from_user.id), parse_mode="Markdown"
            )
    await state.clear()

@dp.message(F.text == "📊 Premium ro'yxati", StateFilter("*"))
async def premium_list_admin(message: types.Message, state: FSMContext):
    if not await is_bot_admin(message.from_user.id):
        return
    await state.clear()
    db = await get_db()
    async with db.execute(
        "SELECT user_id, expire_date FROM premium_users ORDER BY expire_date DESC"
    ) as cur:
        rows = await cur.fetchall()
    if not rows:
        await message.answer("Hozircha premium a'zolar yo'q.")
        return
    text = "🌟 *Premium a'zolar:*\n\n"
    for row in rows:
        exp = datetime.fromisoformat(row["expire_date"]) if row["expire_date"] else None
        exp_str = exp.strftime("%d.%m.%Y") if exp else "Noma'lum"
        status = "✅" if exp and exp > datetime.now() else "❌"
        text += f"{status} `{row['user_id']}` — {exp_str}\n"
    await message.answer(text, parse_mode="Markdown")


# ─── STATISTIKA ───────────────────────────────────────────────────────────────

@dp.message(F.text == "📊 Statistika", StateFilter("*"))
async def statistics(message: types.Message, state: FSMContext):
    if not await is_bot_admin(message.from_user.id):
        return
    await state.clear()
    db = await get_db()
    async with db.execute("SELECT COUNT(*) as cnt FROM users") as cur:
        users_count = (await cur.fetchone())["cnt"]
    async with db.execute(
        "SELECT COUNT(*) as cnt FROM premium_users WHERE expire_date > ?",
        (datetime.now().isoformat(),)
    ) as cur:
        prem_count = (await cur.fetchone())["cnt"]
    async with db.execute(
        "SELECT COUNT(DISTINCT code) as cnt FROM media WHERE is_premium=0"
    ) as cur:
        free_media = (await cur.fetchone())["cnt"]
    async with db.execute(
        "SELECT COUNT(DISTINCT code) as cnt FROM media WHERE is_premium=1"
    ) as cur:
        prem_media = (await cur.fetchone())["cnt"]
    async with db.execute("SELECT COUNT(*) as cnt FROM channels") as cur:
        channels_count = (await cur.fetchone())["cnt"]

    await message.answer(
        f"📊 *Bot statistikasi:*\n\n"
        f"👥 Jami foydalanuvchilar: *{users_count:,}*\n"
        f"🌟 Faol premium a'zolar: *{prem_count:,}*\n"
        f"🎬 Ochiq kontentlar: *{free_media:,}*\n"
        f"🔒 Premium kontentlar: *{prem_media:,}*\n"
        f"📢 Kanallar: *{channels_count:,}*",
        parse_mode="Markdown"
    )


# ─── XABAR YUBORISH ───────────────────────────────────────────────────────────

@dp.message(F.text == "📣 Xabar yuborish", StateFilter("*"))
async def start_broadcast(message: types.Message, state: FSMContext):
    if not await is_bot_admin(message.from_user.id):
        return
    await state.set_state(AdminBroadcast.waiting_for_message)
    await message.answer(
        "📣 Barcha foydalanuvchilarga yubormoqchi bo'lgan xabaringizni yuboring.\n"
        "(Matn, rasm yoki video bo'lishi mumkin)\n\n"
        "Bekor qilish uchun /bekor",
        reply_markup=types.ReplyKeyboardRemove()
    )

@dp.message(AdminBroadcast.waiting_for_message)
async def send_broadcast(message: types.Message, state: FSMContext):
    await state.clear()
    db = await get_db()
    async with db.execute("SELECT user_id FROM users") as cur:
        all_users = await cur.fetchall()

    sent, failed = 0, 0
    for row in all_users:
        uid = row["user_id"]
        try:
            await message.copy_to(uid)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    await message.answer(
        f"📣 Xabar yuborildi!\n\n✅ Muvaffaqiyatli: *{sent}*\n❌ Yuborilmadi: *{failed}*",
        reply_markup=admin_menu(message.from_user.id), parse_mode="Markdown"
    )


# ─── KINO/SERIAL/ANIME QO'SHISH ──────────────────────────────────────────────

@dp.message(F.text == "➕ Kino/Serial/Anime qo'shish", StateFilter("*"))
async def add_media_start(message: types.Message, state: FSMContext):
    if not await is_bot_admin(message.from_user.id):
        return
    await state.clear()
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎬 Kino"), KeyboardButton(text="📺 Serial")],
            [KeyboardButton(text="⛩ Anime/Multfilm")],
        ],
        resize_keyboard=True
    )
    await state.set_state(MediaUpload.category)
    await message.answer("Kategoriyani tanlang:", reply_markup=kb)

@dp.message(MediaUpload.category)
async def process_category(message: types.Message, state: FSMContext):
    text = message.text
    if "Kino" in text:
        cat = "kino"
    elif "Serial" in text:
        cat = "serial"
    elif "Anime" in text or "Multfilm" in text:
        cat = "anime"
    else:
        await message.answer("Iltimos, ro'yxatdan birini tanlang.")
        return
    await state.update_data(category=cat)
    await state.set_state(MediaUpload.title)
    await message.answer(
        "Nom kiriting (masalan: Avengers):",
        reply_markup=types.ReplyKeyboardRemove()
    )

@dp.message(MediaUpload.title)
async def process_title(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎲 Random kod"),
             KeyboardButton(text="✏️ Qo'lda kiritish")]
        ],
        resize_keyboard=True
    )
    await state.set_state(MediaUpload.code_choice)
    await message.answer("Kodni qanday belgilash kerak?", reply_markup=kb)

@dp.message(MediaUpload.code_choice)
async def process_code_choice(message: types.Message, state: FSMContext):
    if message.text == "🎲 Random kod":
        db = await get_db()
        code = str(random.randint(100, 9999))
        async with db.execute("SELECT id FROM media WHERE code=?", (code,)) as cur:
            while await cur.fetchone():
                code = str(random.randint(100, 9999))
        await state.update_data(code=code)
        kb = ReplyKeyboardMarkup(
            keyboard=[[
                KeyboardButton(text="🌐 Oddiy (Ochiq)"),
                KeyboardButton(text="🌟 Premium")
            ]],
            resize_keyboard=True
        )
        await state.set_state(MediaUpload.is_premium)
        await message.answer(
            f"✅ Generatsiya qilingan kod: *{code}*\n\nKino turini tanlang:",
            reply_markup=kb, parse_mode="Markdown"
        )
    else:
        await state.set_state(MediaUpload.manual_code)
        await message.answer(
            "Kod kiriting (raqam):", reply_markup=types.ReplyKeyboardRemove()
        )

@dp.message(MediaUpload.manual_code)
async def process_manual_code(message: types.Message, state: FSMContext):
    code = message.text.strip()
    await state.update_data(code=code)
    kb = ReplyKeyboardMarkup(
        keyboard=[[
            KeyboardButton(text="🌐 Oddiy (Ochiq)"),
            KeyboardButton(text="🌟 Premium")
        ]],
        resize_keyboard=True
    )
    await state.set_state(MediaUpload.is_premium)
    await message.answer(
        f"Kod: *{code}*\n\nKino turini tanlang:", reply_markup=kb, parse_mode="Markdown"
    )

@dp.message(MediaUpload.is_premium)
async def process_is_premium(message: types.Message, state: FSMContext):
    is_prem = 1 if "Premium" in message.text else 0
    await state.update_data(is_premium=is_prem)
    await state.set_state(MediaUpload.parts_count)
    await message.answer(
        "Necha qismdan iborat? (masalan: 1, 5, 12):",
        reply_markup=types.ReplyKeyboardRemove()
    )

@dp.message(MediaUpload.parts_count)
async def process_parts_count(message: types.Message, state: FSMContext):
    try:
        count = int(message.text.strip())
        if count < 1:
            raise ValueError
        await state.update_data(parts_count=count, current_part=1)
        await state.set_state(MediaUpload.waiting_for_videos)
        await message.answer("1-qism videoni yuboring:")
    except ValueError:
        await message.answer("Iltimos, faqat musbat raqam kiriting!")

@dp.message(MediaUpload.waiting_for_videos, F.video)
async def process_video_upload(message: types.Message, state: FSMContext):
    data = await state.get_data()
    current_part = data["current_part"]
    total_parts = data["parts_count"]
    db = await get_db()

    await db.execute(
        "INSERT INTO media (code, title, category, file_id, part, is_premium) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (data["code"], data["title"], data["category"],
         message.video.file_id, current_part, data["is_premium"])
    )
    await db.commit()

    if current_part < total_parts:
        await state.update_data(current_part=current_part + 1)
        await message.answer(
            f"✅ {current_part}-qism saqlandi. {current_part + 1}-qism videoni yuboring:"
        )
    else:
        cat_icon = {"kino": "🎬", "serial": "📺", "anime": "⛩"}.get(
            data["category"], "🎬"
        )
        status_str = "🌟 Premium" if data["is_premium"] else "🌐 Oddiy"
        await message.answer(
            f"🎉 Barcha *{total_parts}* ta qism saqlandi!\n\n"
            f"{cat_icon} *{data['title']}*\n"
            f"🔑 Kod: `{data['code']}`\n"
            f"📌 Turi: {status_str}",
            reply_markup=admin_menu(message.from_user.id), parse_mode="Markdown"
        )
        await state.clear()


# ─── MEDIA O'CHIRISH ──────────────────────────────────────────────────────────

@dp.message(F.text == "🗑 Kino/Serial/Anime o'chirish", StateFilter("*"))
async def delete_media_start(message: types.Message, state: FSMContext):
    if not await is_bot_admin(message.from_user.id):
        return
    await state.clear()
    db = await get_db()
    async with db.execute(
        "SELECT DISTINCT code, title, category, is_premium FROM media ORDER BY category, id DESC"
    ) as cur:
        items = await cur.fetchall()
    if not items:
        await message.answer("Hozircha hech qanday kontent yo'q.")
        return
    text = "🗑 *O'chirish uchun kodni yuboring:*\n\n"
    for row in items:
        icon = {"kino": "🎬", "serial": "📺", "anime": "⛩"}.get(row["category"], "🎬")
        prem = " 🌟" if row["is_premium"] else ""
        text += f"{icon}{prem} {row['title']} — `{row['code']}`\n"
    await message.answer(text, parse_mode="Markdown")
    await state.set_state(AdminDeleteMedia.waiting_for_code)

@dp.message(AdminDeleteMedia.waiting_for_code)
async def delete_media_finish(message: types.Message, state: FSMContext):
    code = message.text.strip()
    db = await get_db()
    async with db.execute(
        "SELECT title FROM media WHERE code=? LIMIT 1", (code,)
    ) as cur:
        row = await cur.fetchone()
    if not row:
        await message.answer("❌ Bunday kod topilmadi.")
        await state.clear()
        return
    await db.execute("DELETE FROM media WHERE code=?", (code,))
    await db.commit()
    await message.answer(
        f"✅ *{row['title']}* (kod: `{code}`) o'chirildi.",
        reply_markup=admin_menu(message.from_user.id), parse_mode="Markdown"
    )
    await state.clear()


# ─── KANAL QO'SHISH ──────────────────────────────────────────────────────────

@dp.message(F.text == "📢 Kanal qo'shish", StateFilter("*"))
async def add_channel_start(message: types.Message, state: FSMContext):
    if not await is_bot_admin(message.from_user.id):
        return
    await state.clear()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📢 Telegram kanal/guruh",
            callback_data="chtype_telegram"
        )],
        [InlineKeyboardButton(
            text="📸 Instagram / boshqa tashqi havola",
            callback_data="chtype_instagram"
        )],
    ])
    await state.set_state(AdminChannel.waiting_for_type)
    await message.answer(
        "Qanday turdagi kanal/havola qo'shmoqchisiz?",
        reply_markup=kb
    )

@dp.callback_query(F.data == "chtype_telegram", AdminChannel.waiting_for_type)
async def add_channel_type_telegram(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(ch_type="telegram")
    await state.set_state(AdminChannel.waiting_for_id)
    await call.message.answer(
        "📢 *Telegram kanal yoki guruh qo'shish*\n\n"
        "Quyidagilardan birini yuboring:\n"
        "• Kanal username: @mening\\_kanalim\n"
        "• Kanal ID raqami: -1001234567890\n"
        "• t.me havolasi: https://t.me/mening\\_kanalim\n"
        "• Yopiq kanal taklifi: https://t.me/+AbCdEfGh1234\n\n"
        "ℹ️ _Bot kanalga admin qilib qo'shilgan bo'lishi shart!_\n\n"
        "Bekor qilish: /bekor",
        parse_mode="Markdown",
        reply_markup=types.ReplyKeyboardRemove()
    )
    await call.answer()

@dp.message(AdminChannel.waiting_for_id)
async def add_channel_get_id(message: types.Message, state: FSMContext):
    raw = message.text.strip()

    chat_id_for_api, full_link, is_invite = parse_channel_input(raw)

    # ── Yopiq kanal invite havolasi ──
    if is_invite:
        # Faqat havola matnini saqlash YETARLI EMAS: statik taklif havolasi
        # vaqt o'tishi, foydalanish limiti yoki qayta generatsiya qilinishi
        # sababli "eskirgan havola" bo'lib qoladi va bot a'zolikni Telegram
        # API orqali hech qachon tekshira olmaydi. Shuning uchun kanalning
        # HAQIQIY (raqamli) ID'ini aniqlashimiz kerak — shunda bot: (1) har
        # safar yangi, eskirmaydigan taklif havolasi yaratadi, (2) a'zolikni
        # real vaqtda tekshiradi.
        await state.update_data(invite_link=full_link)
        await state.set_state(AdminChannel.waiting_for_invite_resolve)
        await message.answer(
            "🔒 Bu — yopiq kanalning *taklif havolasi*.\n\n"
            "Yopiq kanallar uchun bot kanalning *haqiqiy ID raqamini* bilishi shart "
            "— aks holda havola vaqt o'tib \"eskirgan\" bo'lib qoladi va "
            "a'zolikni tekshira olmaydi.\n\n"
            "Iltimos quyidagilardan *birini* bajaring:\n"
            "1️⃣ Botni shu kanalga *administrator* qilib qo'shing, so'ng o'sha "
            "kanaldan istalgan xabarni shu yerga *forward* (uzatib) yuboring — "
            "ID avtomatik aniqlanadi.\n"
            "2️⃣ Yoki kanal ID raqamini qo'lda yuboring (masalan: `-1001234567890`).\n\n"
            "Bekor qilish: /bekor",
            parse_mode="Markdown"
        )
        return

    # ── Telegram kanal/guruh: API orqali tekshirish ──
    chat = await safe_get_chat(chat_id_for_api)

    if not chat:
        if isinstance(chat_id_for_api, str) and chat_id_for_api.startswith("@"):
            alt = chat_id_for_api.lstrip("@")
            chat = await safe_get_chat(alt)

    if not chat:
        await message.answer(
            "❌ Bot bu kanal/guruhni topa olmadi.\n\n"
            "*Tekshiring:*\n"
            "1. Username yoki havola to'g'ri yozilganmi?\n"
            "2. Bot kanalga admin qilib qo'shilganmi?\n"
            "3. Maxsus belgilar bo'lsa, ID raqamini (-100...) ishlating\n\n"
            "Qaytadan yuboring yoki /bekor deb yozing.",
            parse_mode="Markdown"
        )
        return

    ch_type_detected = getattr(chat, "type", None)

    if ch_type_detected in ("channel", "group", "supergroup"):
        title = chat.title or str(chat.id)
        uname = f"@{chat.username}" if getattr(chat, "username", None) else None

        type_label = {
            "channel": "📢 Kanal",
            "group": "👥 Guruh",
            "supergroup": "👥 Guruh (supergroup)"
        }.get(ch_type_detected, "Chat")

        uname_safe = md_escape(uname) if uname else "_(username yo'q)_"
        title_safe = md_escape(title)

        # Botning admin holati va "havola orqali qo'shish" huquqini tekshiramiz
        is_admin_here = False
        can_invite = False
        try:
            me_member = await asyncio.wait_for(
                bot.get_chat_member(chat.id, (await bot.get_me()).id),
                timeout=API_TIMEOUT
            )
            is_admin_here = me_member.status in ("administrator", "creator")
            if me_member.status == "creator":
                can_invite = True
            else:
                can_invite = bool(getattr(me_member, "can_invite_users", False))
        except Exception as e:
            logging.warning(f"Bot admin holatini tekshirib bo'lmadi ({chat.id}): {e}")

        if not is_admin_here:
            await state.clear()
            await message.answer(
                f"✅ *Aniqlandi!*\n\n"
                f"🏷 Nomi: *{title_safe}*\n"
                f"📌 Turi: {type_label}\n"
                f"👤 Username: {uname_safe}\n\n"
                "⚠️ *DIQQAT:* Bot bu kanalda hali *administrator* emas!\n"
                "Botni admin qilib qo'shing, so'ng \"📢 Kanal qo'shish\" dan "
                "qaytadan urinib ko'ring.",
                parse_mode="Markdown",
                reply_markup=admin_menu(message.from_user.id)
            )
            return

        if uname:
            # Ochiq kanal: o'zgarmas, hech qachon eskirmaydigan t.me/username havolasi
            link = f"https://t.me/{uname.lstrip('@')}"
            save_ch_id = uname
        else:
            # Yopiq kanal: bot HAR DOIM o'zi taklif havolasi yaratadi.
            # Qo'lda kiritilgan/nusxalangan havolalarga ISHONILMAYDI, chunki
            # ular istalgan payt bekor qilinishi yoki eskirishi mumkin va
            # bot buni kuzatib bora olmaydi — aynan shu "eskirgan havola"
            # xatosining sababi shu edi.
            if not can_invite:
                await state.clear()
                await message.answer(
                    f"✅ *Aniqlandi!*\n\n"
                    f"🏷 Nomi: *{title_safe}*\n"
                    f"📌 Turi: {type_label}\n\n"
                    "⛔ Lekin botda *\"Foydalanuvchilarni havola orqali qo'shish\"* "
                    "huquqi yo'q, shuning uchun ishonchli (eskirmaydigan) havola "
                    "yarata olmayapman.\n\n"
                    "🔧 *Tuzatish:* Telegram'da kanal → Administratorlar → botni "
                    "tanlang → shu huquqni yoqing, so'ng \"📢 Kanal qo'shish\" dan "
                    "qaytadan urinib ko'ring.",
                    parse_mode="Markdown",
                    reply_markup=admin_menu(message.from_user.id)
                )
                return
            try:
                invite = await asyncio.wait_for(
                    bot.create_chat_invite_link(chat.id),
                    timeout=API_TIMEOUT
                )
                link = invite.invite_link
            except Exception as e:
                logging.error(f"Invite link yaratib bo'lmadi ({chat.id}): {e}")
                await state.clear()
                await message.answer(
                    f"❌ Taklif havolasini yaratib bo'lmadi: `{e}`\n\n"
                    "\"📢 Kanal qo'shish\" dan qaytadan urinib ko'ring.",
                    parse_mode="Markdown",
                    reply_markup=admin_menu(message.from_user.id)
                )
                return
            save_ch_id = str(chat.id)

        db = await get_db()
        await db.execute(
            "INSERT OR IGNORE INTO channels (channel_id, title, link, type) "
            "VALUES (?, ?, ?, 'telegram')",
            (save_ch_id, title, link)
        )
        await db.commit()
        await state.clear()
        await message.answer(
            f"✅ *Kanal/guruh qo'shildi!*\n\n"
            f"🏷 Nomi: *{title_safe}*\n"
            f"📌 Turi: {type_label}\n"
            f"🆔 ID: `{save_ch_id}`\n"
            f"🔗 Havola: {link}",
            parse_mode="Markdown",
            reply_markup=admin_menu(message.from_user.id)
        )

    else:
        # Bot yoki oddiy foydalanuvchi
        uname = getattr(chat, "username", None)
        if not uname and isinstance(chat_id_for_api, str):
            uname = chat_id_for_api.lstrip("@")

        is_real_bot = bool(uname) and uname.lower().endswith("bot")

        if not is_real_bot:
            await message.answer(
                "❌ Bu — bot emas, oddiy foydalanuvchi profili ko'rinadi.\n\n"
                "Majburiy obuna faqat *kanal*, *guruh* yoki *bot* uchun qo'shiladi.\n"
                "Agar bu chindan ham bot bo'lsa, uning username doim "
                "`...bot` bilan tugashi kerak.\n\n"
                "Qaytadan yuboring yoki /bekor deb yozing.",
                parse_mode="Markdown"
            )
            return

        title = (
            getattr(chat, "full_name", None)
            or getattr(chat, "first_name", None)
            or uname
            or str(chat.id)
        )
        link = f"https://t.me/{uname}" if uname else ""
        ch_id = f"bot_{uname or int(datetime.now().timestamp())}"

        db = await get_db()
        await db.execute(
            "INSERT OR IGNORE INTO channels (channel_id, title, link, type) "
            "VALUES (?, ?, ?, 'bot')",
            (ch_id, title, link)
        )
        await db.commit()
        await state.clear()
        uname_safe = md_escape(uname) if uname else "yo'q"
        title_safe = md_escape(title)
        no_link = "_(yo'q)_"
        await message.answer(
            f"✅ *Telegram bot qo'shildi!*\n\n"
            f"🤖 Nomi: *{title_safe}*\n"
            f"👤 Username: @{uname_safe}\n"
            f"🔗 Havola: {link if link else no_link}\n\n"
            f"ℹ️ Foydalanuvchilar botga o'tib, keyin "
            f"\"✅ Tekshirish\" tugmasini bosib tasdiqlaydi.",
            parse_mode="Markdown",
            reply_markup=admin_menu(message.from_user.id)
        )

@dp.message(AdminChannel.waiting_for_invite_resolve)
async def add_channel_invite_resolve(message: types.Message, state: FSMContext):
    data = await state.get_data()
    invite_link = data.get("invite_link", "")

    chat_id_int = None

    # 1) Kanaldan forward qilingan xabar bo'lsa — undan chat ID olamiz
    fwd_chat = getattr(message, "forward_from_chat", None)
    if fwd_chat is not None:
        chat_id_int = fwd_chat.id
    else:
        # 2) Yoki admin to'g'ridan-to'g'ri raqamli ID yuborgan bo'lishi mumkin
        raw = (message.text or "").strip()
        if re.match(r'^-?\d+$', raw):
            chat_id_int = int(raw)

    if chat_id_int is None:
        await message.answer(
            "❌ Kanal ID'ini aniqlab bo'lmadi.\n\n"
            "• Kanaldan xabar *forward* qiling (bot o'sha kanalda admin bo'lishi shart), "
            "yoki\n"
            "• Kanal ID raqamini yuboring (masalan: `-1001234567890`).\n\n"
            "Bekor qilish: /bekor",
            parse_mode="Markdown"
        )
        return

    # ID topilgach — bot haqiqatan ham shu kanalni ko'ra oladimi, tekshiramiz
    chat = await safe_get_chat(chat_id_int)
    if not chat:
        await message.answer(
            "❌ Bot bu kanalni topa olmadi.\n\n"
            "Bot kanalga *administrator* qilib qo'shilganini tekshirib, "
            "qaytadan urinib ko'ring yoki /bekor deb yozing.",
            parse_mode="Markdown"
        )
        return

    # Bot shu kanalda admin ekanini VA aniq "havola orqali qo'shish" huquqiga
    # ega ekanini tekshiramiz — aks holda create_chat_invite_link ishlamaydi
    is_admin_here = False
    can_invite = False
    try:
        me_member = await asyncio.wait_for(
            bot.get_chat_member(chat.id, (await bot.get_me()).id),
            timeout=API_TIMEOUT
        )
        is_admin_here = me_member.status in ("administrator", "creator")
        # Creator uchun bu huquq har doim bor; administrator uchun aniq
        # can_invite_users maydoni tekshiriladi
        if me_member.status == "creator":
            can_invite = True
        else:
            can_invite = bool(getattr(me_member, "can_invite_users", False))
    except Exception as e:
        logging.warning(f"Bot admin holatini tekshirib bo'lmadi ({chat.id}): {e}")

    if not is_admin_here:
        await message.answer(
            "⚠️ Bot bu kanalda hali *administrator* emas.\n\n"
            "Botni kanalga admin qilib qo'shing (\"Taklif havolalari orqali qo'shish\" "
            "huquqi bilan), so'ng shu xabarni qaytadan yuboring yoki forward qiling.",
            parse_mode="Markdown"
        )
        return

    title = chat.title or invite_link or str(chat.id)
    uname = f"@{chat.username}" if getattr(chat, "username", None) else None
    save_ch_id = uname if uname else str(chat.id)

    # Havolani saqlaymiz: username bo'lsa doim yangilanadigan t.me/username
    # ishlatiladi; bo'lmasa, bot o'zi YANGI taklif havolasi yaratishi SHART —
    # aks holda admin qo'lda bergan eski/eskirgan havola saqlanib qolib,
    # "eskirgan havola" xatosi qaytadan chiqadi.
    link_to_store = f"https://t.me/{uname.lstrip('@')}" if uname else None

    if not uname:
        if not can_invite:
            await message.answer(
                "⛔ Bot kanalda admin, lekin unda *\"Foydalanuvchilarni havola orqali "
                "qo'shish\" (Invite Users via Link)* huquqi yo'q.\n\n"
                "Shu sabab bot o'zi yangi, eskirmaydigan taklif havolasini yarata olmayapti "
                "— sizning qo'lda bergan eski havolangiz esa allaqachon eskirgan bo'lishi mumkin.\n\n"
                "🔧 *Tuzatish:* Telegram'da kanal → Administratorlar → botni tanlang → "
                "\"Foydalanuvchilarni havola orqali qo'shish\" huquqini yoqing, "
                "so'ng shu xabarni qaytadan yuboring yoki forward qiling.",
                parse_mode="Markdown"
            )
            return
        try:
            invite = await asyncio.wait_for(
                bot.create_chat_invite_link(chat.id),
                timeout=API_TIMEOUT
            )
            link_to_store = invite.invite_link
        except Exception as e:
            logging.error(f"Invite link yaratib bo'lmadi ({chat.id}): {e}")
            await message.answer(
                f"❌ Bot avtomatik taklif havolasini yarata olmadi: `{e}`\n\n"
                "Iltimos, botning admin huquqlarini tekshirib, qaytadan urinib ko'ring "
                "yoki /bekor deb yozing.",
                parse_mode="Markdown"
            )
            return

    db = await get_db()
    await db.execute(
        "INSERT OR IGNORE INTO channels (channel_id, title, link, type) "
        "VALUES (?, ?, ?, 'telegram')",
        (save_ch_id, title, link_to_store)
    )
    await db.commit()
    await state.clear()
    title_safe = md_escape(title)
    await message.answer(
        f"✅ *Yopiq kanal qo'shildi!*\n\n"
        f"📢 Nomi: *{title_safe}*\n"
        f"🆔 ID: `{save_ch_id}`\n"
        f"🔗 Havola: {link_to_store}\n\n"
        f"ℹ️ Endi bot foydalanuvchi a'zoligini Telegram API orqali *real vaqtda* "
        f"tekshiradi va kerak bo'lganda har safar *yangi* taklif havolasi yaratadi — "
        f"\"eskirgan havola\" xatosi endi chiqmaydi.",
        parse_mode="Markdown",
        reply_markup=admin_menu(message.from_user.id)
    )

@dp.callback_query(F.data == "chtype_instagram", AdminChannel.waiting_for_type)
async def add_channel_type_instagram(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(ch_type="instagram")
    await state.set_state(AdminChannel.waiting_for_manual_title)
    await call.message.answer(
        "📸 *Instagram yoki boshqa tashqi havola qo'shish*\n\n"
        "1. Bu havola uchun nom kiriting:\n"
        "Masalan: Instagram sahifamiz yoki TikTok kanalimiz",
        parse_mode="Markdown"
    )
    await call.answer()

@dp.message(AdminChannel.waiting_for_manual_title)
async def add_channel_manual_title(message: types.Message, state: FSMContext):
    await state.update_data(manual_title=message.text.strip())
    await state.set_state(AdminChannel.waiting_for_manual_link)
    await message.answer(
        "2. Havolani (link) yuboring:\n"
        "Masalan: https://instagram.com/mening\\_sahifam\n\n"
        "ℹ️ *Eslatma:* Instagram havolalarga a'zolikni Telegram orqali tekshirib bo'lmaydi.\n"
        "Foydalanuvchi havolani ochib, keyin \"✅ Tekshirish\" tugmasini bosadi.",
        parse_mode="Markdown"
    )

@dp.message(AdminChannel.waiting_for_manual_link)
async def add_channel_manual_finish(message: types.Message, state: FSMContext):
    data = await state.get_data()
    title = data.get("manual_title", "Havola")
    link = message.text.strip()
    ch_type = data.get("ch_type", "instagram")

    if not (link.startswith("http://") or link.startswith("https://")):
        await message.answer(
            "❌ Iltimos to'liq havola yuboring:\nMasalan: https://instagram.com/...",
            parse_mode="Markdown"
        )
        return

    ch_id = f"{ch_type}_{int(datetime.now().timestamp())}"
    db = await get_db()
    await db.execute(
        "INSERT INTO channels (channel_id, title, link, type) VALUES (?, ?, ?, ?)",
        (ch_id, title, link, ch_type)
    )
    await db.commit()
    icon = "📸" if ch_type == "instagram" else "🔗"
    await state.clear()
    title_safe = md_escape(title)
    await message.answer(
        f"✅ *Havola qo'shildi!*\n\n"
        f"{icon} Nomi: *{title_safe}*\n"
        f"🔗 {link}\n\n"
        f"ℹ️ Foydalanuvchilar havolani ochib, \"✅ Tekshirish\" tugmasini bosib tasdiqlaydi.",
        parse_mode="Markdown",
        reply_markup=admin_menu(message.from_user.id)
    )


# ─── KANAL O'CHIRISH ──────────────────────────────────────────────────────────

@dp.message(F.text == "🗑 Kanal o'chirish", StateFilter("*"))
async def list_del_channels(message: types.Message, state: FSMContext):
    if not await is_bot_admin(message.from_user.id):
        return
    await state.clear()
    db = await get_db()
    async with db.execute(
        "SELECT id, title, channel_id, type FROM channels"
    ) as cur:
        channels = await cur.fetchall()
    if not channels:
        await message.answer("Kanallar mavjud emas.")
        return

    type_icons = {"telegram": "📢", "bot": "🤖", "instagram": "📸", "manual": "🔗"}
    buttons = [
        [InlineKeyboardButton(
            text=f"❌ {type_icons.get(row['type'], '🔗')} {row['title']}",
            callback_data=f"del_ch_{row['id']}"
        )]
        for row in channels
    ]
    await message.answer(
        "O'chirmoqchi bo'lgan kanalni tanlang:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )

@dp.callback_query(F.data.startswith("del_ch_"))
async def delete_channel_cb(call: types.CallbackQuery):
    if not await is_bot_admin(call.from_user.id):
        await call.answer("⛔ Ruxsat yo'q.", show_alert=True)
        return
    ch_db_id = int(call.data.split("_")[2])
    db = await get_db()
    async with db.execute(
        "SELECT title FROM channels WHERE id=?", (ch_db_id,)
    ) as cur:
        row = await cur.fetchone()
    if not row:
        await call.answer("Kanal topilmadi.", show_alert=True)
        return
    await db.execute("DELETE FROM channels WHERE id=?", (ch_db_id,))
    await db.commit()
    await call.message.answer(
        f"✅ *{md_escape(row['title'])}* kanali o'chirildi.",
        parse_mode="Markdown",
        reply_markup=admin_menu(call.from_user.id)
    )
    await call.answer()

@dp.message(F.text == "📋 Kanallar ro'yxati", StateFilter("*"))
async def list_channels(message: types.Message, state: FSMContext):
    if not await is_bot_admin(message.from_user.id):
        return
    await state.clear()
    db = await get_db()
    async with db.execute(
        "SELECT channel_id, title, link, type FROM channels"
    ) as cur:
        channels = await cur.fetchall()
    if not channels:
        await message.answer("Hozircha kanallar qo'shilmagan.")
        return

    type_icons = {"telegram": "📢", "bot": "🤖", "instagram": "📸", "manual": "🔗"}
    text = "📋 *Kanallar ro'yxati:*\n\n"
    refresh_buttons = []
    for ch in channels:
        icon = type_icons.get(ch["type"], "🔗")
        link_str = ch["link"] if ch["link"] else "_(havola yo'q)_"
        ch_id_str = md_escape(str(ch["channel_id"]))
        title_str = md_escape(ch["title"])
        text += (
            f"{icon} *{title_str}*\n"
            f"  🆔 `{ch_id_str}`\n"
            f"  🔗 {link_str}\n\n"
        )
        # Faqat yopiq (raqamli ID) Telegram kanallar uchun havolani
        # yangilash imkoniyati beramiz — @username kanallarga kerak emas
        if ch["type"] == "telegram" and not str(ch["channel_id"]).startswith("@"):
            short_title = ch["title"][:28]
            refresh_buttons.append([InlineKeyboardButton(
                text=f"🔄 {short_title}",
                callback_data=f"refresh_link_{ch['channel_id']}"
            )])

    kb = None
    if refresh_buttons:
        text += "ℹ️ _Yopiq kanal havolasi ishlamay qolsa, quyidan yangilang:_"
        kb = InlineKeyboardMarkup(inline_keyboard=refresh_buttons)
    await message.answer(text, parse_mode="Markdown", reply_markup=kb)

@dp.callback_query(F.data.startswith("refresh_link_"))
async def refresh_channel_link_cb(call: types.CallbackQuery):
    if not await is_bot_admin(call.from_user.id):
        await call.answer("⛔ Ruxsat yo'q.", show_alert=True)
        return
    ch_id = call.data[len("refresh_link_"):]
    try:
        chat_id_int = int(ch_id)
    except ValueError:
        await call.answer("❌ Noto'g'ri kanal ID.", show_alert=True)
        return

    try:
        invite = await asyncio.wait_for(
            bot.create_chat_invite_link(chat_id_int),
            timeout=API_TIMEOUT
        )
        new_link = invite.invite_link
    except Exception as e:
        logging.error(f"Havolani yangilashda xato ({ch_id}): {e}")
        await call.answer(
            "❌ Yangi havola yaratib bo'lmadi. Botga kanalda \"Foydalanuvchilarni "
            "havola orqali qo'shish\" huquqi berilganini tekshiring.",
            show_alert=True
        )
        return

    db = await get_db()
    await db.execute("UPDATE channels SET link=? WHERE channel_id=?", (new_link, ch_id))
    await db.commit()
    await call.answer("✅ Yangi havola yaratildi!", show_alert=True)
    await call.message.answer(f"🔗 Yangi havola:\n{new_link}")


# ─── NARX O'ZGARTIRISH ───────────────────────────────────────────────────────

@dp.message(F.text == "💰 Premium narxini o'zgartirish", StateFilter("*"))
async def start_price_change(message: types.Message, state: FSMContext):
    if not is_super_admin(message.from_user.id):
        return
    await state.clear()
    price = await get_premium_price(30)
    await state.set_state(AdminPriceChange.waiting_for_price)
    await message.answer(
        f"💰 Hozirgi narx: *{price}* so'm\n\nYangi narxni kiriting (masalan: 25000):",
        parse_mode="Markdown",
        reply_markup=types.ReplyKeyboardRemove()
    )

@dp.message(AdminPriceChange.waiting_for_price)
async def finish_price_change(message: types.Message, state: FSMContext):
    new_price = message.text.strip().replace(" ", "")
    if not new_price.isdigit():
        await message.answer("❌ Iltimos faqat raqam kiriting (masalan: 25000).")
        return
    formatted = f"{int(new_price):,}"
    await set_premium_price(formatted, 30)
    await state.clear()
    await message.answer(
        f"✅ Premium narxi *{formatted} so'm* qilib o'zgartirildi.",
        reply_markup=admin_menu(message.from_user.id), parse_mode="Markdown"
    )


# ─── KARTA O'ZGARTIRISH ───────────────────────────────────────────────────────

@dp.message(F.text == "💳 Karta raqamini o'zgartirish", StateFilter("*"))
async def start_card_change(message: types.Message, state: FSMContext):
    if not is_super_admin(message.from_user.id):
        return
    await state.clear()
    card_number, card_holder = await get_card_info()
    holder_line = f"\n👤 Hozirgi egasi: {card_holder}" if card_holder else ""
    await state.set_state(AdminCardChange.waiting_for_number)
    await message.answer(
        f"💳 Hozirgi karta raqami: `{card_number}`{holder_line}\n\n"
        "Yangi karta raqamini kiriting (masalan: 8600 1234 5678 9012):",
        parse_mode="Markdown",
        reply_markup=types.ReplyKeyboardRemove()
    )

@dp.message(AdminCardChange.waiting_for_number)
async def process_card_number(message: types.Message, state: FSMContext):
    await state.update_data(card_number=message.text.strip())
    await state.set_state(AdminCardChange.waiting_for_holder)
    await message.answer("👤 Endi karta egasining F.I.Sh (ism-familiyasi)ni kiriting:")

@dp.message(AdminCardChange.waiting_for_holder)
async def process_card_holder(message: types.Message, state: FSMContext):
    data = await state.get_data()
    card_number = data.get("card_number")
    card_holder = message.text.strip()
    await set_card_info(card_number, card_holder)
    await state.clear()
    await message.answer(
        f"✅ Karta ma'lumotlari yangilandi!\n\n"
        f"💳 `{card_number}`\n"
        f"👤 {card_holder}",
        parse_mode="Markdown",
        reply_markup=admin_menu(message.from_user.id)
    )


# ─── ADMINLARNI BOSHQARISH ────────────────────────────────────────────────────

@dp.message(F.text == "👨‍💼 Adminlar", StateFilter("*"))
async def admins_panel(message: types.Message, state: FSMContext):
    if not is_super_admin(message.from_user.id):
        return
    await state.clear()
    admins = await get_admins_list()
    text = "👨‍💼 *Adminlar ro'yxati:*\n\n"
    text += f"👑 Asosiy admin: `{ADMIN_ID}`\n\n"
    if admins:
        for row in admins:
            uname_part = f"@{row['username']}" if row["username"] else "username yo'q"
            text += f"🔹 `{row['user_id']}` — {uname_part} ({row['added_at']})\n"
    else:
        text += "Qo'shimcha adminlar yo'q."

    if message.from_user.id == ADMIN_ID:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="➕ Admin qo'shish", callback_data="add_admin_start"
            )],
            [InlineKeyboardButton(
                text="➖ Admin olib tashlash", callback_data="remove_admin_start"
            )]
        ])
        await message.answer(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await message.answer(text, parse_mode="Markdown")

@dp.callback_query(F.data == "add_admin_start")
async def add_admin_start_cb(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer(
            "⛔ Faqat asosiy admin yangi admin qo'sha oladi.", show_alert=True
        )
        return
    await state.set_state(AdminManage.waiting_for_add_id)
    await call.message.answer("➕ Yangi adminning Telegram ID raqamini yuboring:")
    await call.answer()

@dp.message(AdminManage.waiting_for_add_id)
async def process_add_admin(message: types.Message, state: FSMContext):
    try:
        new_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Noto'g'ri format. Faqat raqam (ID) yuboring.")
        return
    if new_id == ADMIN_ID or await is_bot_admin(new_id):
        await message.answer("⚠️ Bu foydalanuvchi allaqachon admin.")
        await state.clear()
        return
    chat = await safe_get_chat(new_id)
    uname = getattr(chat, "username", "") if chat else ""
    await add_admin(new_id, uname or "")
    await state.clear()
    await message.answer(
        f"✅ `{new_id}` endi admin!",
        parse_mode="Markdown",
        reply_markup=admin_menu(message.from_user.id)
    )
    try:
        await bot.send_message(
            new_id, "🎉 Sizga bot admin huquqi berildi! /admin buyrug'ini yuboring."
        )
    except Exception:
        pass

@dp.callback_query(F.data == "remove_admin_start")
async def remove_admin_start_cb(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer(
            "⛔ Faqat asosiy admin admin olib tashlay oladi.", show_alert=True
        )
        return
    admins = await get_admins_list()
    if not admins:
        await call.answer("Qo'shimcha adminlar yo'q.", show_alert=True)
        return
    kb_rows = []
    for row in admins:
        label = f"@{row['username']}" if row["username"] else str(row["user_id"])
        kb_rows.append([InlineKeyboardButton(
            text=f"❌ {label}",
            callback_data=f"rm_admin_{row['user_id']}"
        )])
    await call.message.answer(
        "Olib tashlamoqchi bo'lgan adminni tanlang:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows)
    )
    await call.answer()

@dp.callback_query(F.data.startswith("rm_admin_"))
async def process_remove_admin(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔ Ruxsat yo'q.", show_alert=True)
        return
    target_id = int(call.data.split("_")[2])
    await remove_admin(target_id)
    await call.message.answer(
        f"✅ `{target_id}` admin huquqidan olib tashlandi.", parse_mode="Markdown"
    )
    await call.answer()
    try:
        await bot.send_message(
            target_id, "ℹ️ Sizning bot admin huquqingiz olib tashlandi."
        )
    except Exception:
        pass


# ─── MAXFIY KANAL: A'ZOLIKKA SO'ROV (JOIN REQUEST) ───────────────────────────

async def resolve_tracked_channel_id(chat_id: int, username: str = None):
    """
    Berilgan chat botning `channels` bazasida (type='telegram') qanday
    channel_id bilan saqlanganini topadi (raqamli ID yoki @username
    ko'rinishida) va shuni qaytaradi. Topilmasa None qaytaradi — bu holda
    kanal botga aloqasi yo'q, aralashmaymiz.
    """
    db = await get_db()
    async with db.execute(
        "SELECT channel_id FROM channels WHERE type='telegram'"
    ) as cur:
        rows = await cur.fetchall()
    ids = {row["channel_id"] for row in rows}
    if str(chat_id) in ids:
        return str(chat_id)
    if username and f"@{username}" in ids:
        return f"@{username}"
    return None

@dp.chat_join_request()
async def handle_join_request(join_request: types.ChatJoinRequest):
    """
    Kanalda "Yangi a'zolarni tasdiqlash" (Approve New Members) yoqilgan
    bo'lsa, taklif havolasini bosgan foydalanuvchi darhol a'zo bo'lmaydi —
    Telegram "qo'shilish so'rovi" yaratadi va kanal administratori buni
    QO'LDA tasdiqlaydi (bot AVTOMATIK tasdiqlamaydi).

    Lekin botdan foydalanish uchun buni kutish shart emas: foydalanuvchi
    so'rov YUBORGANI botga yetarli dalil sifatida hisoblanadi va
    check_subscriptions uni "obuna bo'lgan" deb qabul qiladi.
    """
    chat = join_request.chat
    user = join_request.from_user

    ch_id = await resolve_tracked_channel_id(chat.id, getattr(chat, "username", None))
    if not ch_id:
        return  # Botga aloqasi bo'lmagan kanal — aralashmaymiz

    db = await get_db()
    await db.execute(
        "INSERT OR REPLACE INTO manual_confirmations (user_id, channel_id, confirmed_at) "
        "VALUES (?, ?, ?)",
        (user.id, ch_id, datetime.now().isoformat())
    )
    await db.commit()
    logging.info(f"Join so'rovi qayd etildi (avtomatik tasdiqlanmadi): chat={chat.id} user={user.id}")

    # Foydalanuvchiga botdan qisqa xabar (ixtiyoriy, botni bloklagan bo'lsa xato bermaydi)
    try:
        await bot.send_message(
            user.id,
            f"✅ *{md_escape(chat.title or 'Kanal')}* ga qo'shilish so'rovingiz qabul qilindi!\n\n"
            f"Administrator tez orada tasdiqlaydi. Hozircha botdan foydalanishingiz mumkin — "
            f"\"✅ Tekshirish\" tugmasini bosing.",
            parse_mode="Markdown"
        )
    except Exception:
        pass
    except Exception:
        pass


# ─── COMMANDS ────────────────────────────────────────────────────────────────

@dp.message(Command("kino"))
async def cmd_kino(message: types.Message):
    text, kb = await render_media_list("kino", 0)
    await message.answer(text, parse_mode="Markdown", reply_markup=kb)

@dp.message(Command("serial"))
async def cmd_serial(message: types.Message):
    text, kb = await render_media_list("serial", 0)
    await message.answer(text, parse_mode="Markdown", reply_markup=kb)

@dp.message(Command("anime"))
async def cmd_anime(message: types.Message):
    text, kb = await render_media_list("anime", 0)
    await message.answer(text, parse_mode="Markdown", reply_markup=kb)

@dp.message(Command("search"))
async def cmd_search(message: types.Message, state: FSMContext):
    args = message.text.split(maxsplit=1)
    if len(args) > 1:
        code = args[1].strip()
        await deliver_media_by_code(message, message.from_user.id, code)
    else:
        await state.set_state(CodeSearch.waiting_for_code)
        await message.answer("🔍 Kino kodini kiriting:")

@dp.message(Command("premium"))
async def cmd_premium(message: types.Message):
    await premium_info(message)


# ─── TANILMAGAN XABARLAR (CATCH-ALL) ─────────────────────────────────────────

@dp.message(StateFilter(None))
async def unknown_message(message: types.Message):
    """
    Hech qaysi handlerga tushmaydigan xabarlarga darhol javob beradi.
    """
    user_id = message.from_user.id

    if await is_bot_admin(user_id):
        await message.answer(
            "ℹ️ Noma'lum buyruq. Menyudan foydalaning:",
            reply_markup=admin_menu(user_id)
        )
        return

    if not await is_premium_user(user_id):
        unsub = await check_subscriptions(user_id)
        if unsub:
            kb = await build_subscription_keyboard(unsub)
            await message.answer(
                "⚠️ Botdan foydalanish uchun quyidagi kanal(lar)ga obuna bo'ling:",
                reply_markup=kb
            )
            return

    await message.answer(
        "ℹ️ Noma'lum buyruq. Quyidagi menyudan foydalaning:",
        reply_markup=main_menu(user_id)
    )


# ─── ISHGA TUSHIRISH ─────────────────────────────────────────────────────────

async def set_commands():
    user_commands = [
        types.BotCommand(command="start",   description="🚀 Botni ishga tushirish"),
        types.BotCommand(command="kino",    description="🎬 Kinolar ro'yxati"),
        types.BotCommand(command="serial",  description="📺 Seriallar ro'yxati"),
        types.BotCommand(command="anime",   description="⛩ Anime va Multfilmlar"),
        types.BotCommand(command="search",  description="🔍 Kod orqali qidirish"),
        types.BotCommand(command="premium", description="🌟 Premium bo'lim"),
    ]
    admin_commands = user_commands + [
        types.BotCommand(command="admin",  description="👨‍💻 Admin panel"),
        types.BotCommand(command="bekor",  description="❌ Amalni bekor qilish"),
    ]
    await bot.set_my_commands(user_commands)
    try:
        await bot.set_my_commands(
            admin_commands,
            scope=types.BotCommandScopeChat(chat_id=ADMIN_ID)
        )
    except Exception as e:
        logging.warning(f"Admin buyruqlari o'rnatilmadi: {e}")

async def run_bot():
    global BOT_USERNAME

    await init_db()

    try:
        me = await bot.get_me()
        BOT_USERNAME = me.username
        logging.info(f"Bot: @{BOT_USERNAME} (id={me.id})")
    except Exception as e:
        logging.error(f"Bot username olishda xato: {e}")

    try:
        await bot.set_my_short_description("Kino, Serial va Anime botga xush kelibsiz!")
        await bot.set_my_description(
            "🚀 Kino, serial va animelarni tezkor tomosha qiling!\n"
            "🌟 Premium a'zolik: eksklyuziv HD kinolar!"
        )
        await set_commands()
    except Exception as e:
        logging.error(f"Bot ma'lumotlarini o'rnatishda xato: {e}")

    asyncio.create_task(premium_checker())
    asyncio.create_task(backup_scheduler())

    first_start = True
    while True:
        try:
            logging.info("Bot ishga tushmoqda (polling)...")
            await dp.start_polling(
                bot,
                allowed_updates=dp.resolve_used_update_types(),
                drop_pending_updates=first_start,
            )
        except Exception as e:
            logging.error(f"Polling to'xtadi: {e}. 5 soniyadan keyin qayta uriniladi...")
            await asyncio.sleep(5)
        finally:
            first_start = False

if __name__ == "__main__":
    asyncio.run(run_bot())
