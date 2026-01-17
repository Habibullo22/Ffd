import asyncio
import time
import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)

# ================== SOZLAMALAR ==================
TOKEN = "8478058553:AAGR0eMotTJy5_zM-65bHGGsm2ImcOKKfeE"

# Adminlar (o'zingni ID'yingni qo'y)
ADMINS = {5815294733}  # masalan: {123456789, 987654321}

# Majburiy obuna: 2 ta kanal + 1 ta guruh (username yoki -100... ID)
REQUIRED_CHATS = [
    {"name": "📢 Kanal 1", "chat": "@KANAL1", "link": "https://t.me/KANAL1"},
    {"name": "📢 Kanal 2", "chat": "@KANAL2", "link": "https://t.me/KANAL2"},
    {"name": "👥 Guruh",   "chat": "@GURUH1", "link": "https://t.me/GURUH1"},
]

# Referral (boshqa bot)
REF_LINK = "https://t.me/saudia_konkurs_bot?start=ref_8034655906"

DB = "kino.db"

bot = Bot(TOKEN)
dp = Dispatcher()

# ================ DB INIT =================
async def db_init():
    async with aiosqlite.connect(DB) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            first_name TEXT,
            username TEXT,
            joined_at INTEGER,
            ref_checked INTEGER DEFAULT 0
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS movies(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE,
            title TEXT,
            description TEXT,
            file_id TEXT,        -- telegram file_id (video/doc)
            link TEXT,           -- yoki link
            created_at INTEGER
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS admins(
            user_id INTEGER PRIMARY KEY
        )
        """)
        # adminlar ro'yxatini DBga yozib qo'yamiz
        for a in ADMINS:
            await db.execute("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (a,))
        await db.commit()

async def is_admin(user_id: int) -> bool:
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        return row is not None

async def upsert_user(m: Message):
    async with aiosqlite.connect(DB) as db:
        await db.execute("""
        INSERT INTO users(user_id, first_name, username, joined_at)
        VALUES(?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            first_name=excluded.first_name,
            username=excluded.username
        """, (
            m.from_user.id,
            (m.from_user.first_name or ""),
            (m.from_user.username or ""),
            int(time.time())
        ))
        await db.commit()

async def set_ref_checked(user_id: int, val: int):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE users SET ref_checked=? WHERE user_id=?", (val, user_id))
        await db.commit()

async def get_ref_checked(user_id: int) -> int:
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT ref_checked FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        return int(row[0]) if row else 0

async def add_movie(code: str, title: str, description: str, file_id: str | None, link: str | None):
    async with aiosqlite.connect(DB) as db:
        await db.execute("""
        INSERT INTO movies(code, title, description, file_id, link, created_at)
        VALUES(?,?,?,?,?,?)
        """, (code, title, description, file_id, link, int(time.time())))
        await db.commit()

async def get_movie_by_code(code: str):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("""
        SELECT code, title, description, file_id, link FROM movies WHERE code=?
        """, (code,))
        return await cur.fetchone()

async def search_movies(q: str, limit: int = 10):
    like = f"%{q}%"
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("""
        SELECT code, title FROM movies
        WHERE title LIKE ? OR description LIKE ? OR code LIKE ?
        ORDER BY id DESC LIMIT ?
        """, (like, like, like, limit))
        return await cur.fetchall()

async def stats():
    async with aiosqlite.connect(DB) as db:
        cur1 = await db.execute("SELECT COUNT(*) FROM users")
        u = (await cur1.fetchone())[0]
        cur2 = await db.execute("SELECT COUNT(*) FROM movies")
        k = (await cur2.fetchone())[0]
        return int(u), int(k)

async def all_user_ids(limit=5000):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT user_id FROM users LIMIT ?", (limit,))
        rows = await cur.fetchall()
        return [int(r[0]) for r in rows]

# ================ MAJBURIY OBUNA =================
def join_kb():
    rows = []
    # referral bot
    rows.append([InlineKeyboardButton(text="🎁 Konkurs botga kirish", url=REF_LINK)])
    # kanallar/guruh
    for c in REQUIRED_CHATS:
        rows.append([InlineKeyboardButton(text=f"➕ {c['name']}", url=c["link"])])
    rows.append([InlineKeyboardButton(text="✅ Tekshirish", callback_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def is_member(chat_id_or_username: str, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(chat_id_or_username, user_id)
        return m.status in ("member", "administrator", "creator", "restricted")
    except Exception:
        return False

async def not_joined_chats(user_id: int):
    missing = []
    for c in REQUIRED_CHATS:
        ok = await is_member(c["chat"], user_id)
        if not ok:
            missing.append(c)
    return missing

async def gate_check(user_id: int) -> tuple[bool, str]:
    """
    True bo'lsa bot ishlaydi.
    """
    missing = await not_joined_chats(user_id)
    if missing:
        txt = "⚠️ Botdan foydalanish uchun quyidagilarga obuna bo‘ling:\n\n"
        txt += "🎁 Konkurs bot: (tugma orqali kiring)\n"
        txt += "✅ Kanal(lar) + Guruh\n\n"
        txt += "Obuna bo‘lgach «✅ Tekshirish» bosing."
        return False, txt

    # Referral botni API bilan tekshirib bo'lmaydi, shuning uchun user "tekshirdim" qilganini flag qilamiz
    ref_ok = await get_ref_checked(user_id)
    if ref_ok != 1:
        txt = ("🎁 1-qadam: Konkurs botga kirib /start bosing.\n"
               "Keyin bu yerga qaytib «✅ Tekshirish»ni bosing.\n\n"
               "Eslatma: bu qadam Telegram cheklovi sabab 100% avtomatik tekshirilmaydi.")
        return False, txt

    return True, "OK"

# ================== MENYU ==================
def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 Kino qidirish", callback_data="m_search")],
        [InlineKeyboardButton(text="🎬 Kod bilan olish", callback_data="m_code")],
        [InlineKeyboardButton(text="📢 Reklama", callback_data="m_ads")],
    ])

def admin_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Kino qo‘shish", callback_data="a_add")],
        [InlineKeyboardButton(text="📊 Statistika", callback_data="a_stats")],
        [InlineKeyboardButton(text="📣 Broadcast", callback_data="a_broadcast")],
        [InlineKeyboardButton(text="🔙 Menyu", callback_data="back_menu")],
    ])

# ================== STATES (oddiy) ==================
# user_id -> dict
STATE = {}

def set_state(uid: int, name: str, data: dict | None = None):
    STATE[uid] = {"name": name, "data": data or {}}

def get_state(uid: int):
    return STATE.get(uid)

def clear_state(uid: int):
    if uid in STATE:
        del STATE[uid]

# ================== HANDLERS ==================
@dp.message(Command("start"))
async def start(m: Message):
    await upsert_user(m)

    ok, msg = await gate_check(m.from_user.id)
    if not ok:
        await m.answer(msg, reply_markup=join_kb())
        return

    await m.answer("🎬 Kino botga xush kelibsiz!\nMenyudan tanlang 👇", reply_markup=main_menu())

@dp.callback_query(F.data == "check_sub")
async def check_sub(c: CallbackQuery):
    uid = c.from_user.id

    # Kanal/guruh tekshiruv
    missing = await not_joined_chats(uid)
    if missing:
        txt = "❌ Hali obuna bo‘lmagansiz:\n\n"
        for x in missing:
            txt += f"• {x['name']} — {x['link']}\n"
        txt += "\nObuna bo‘lib, yana «✅ Tekshirish» bosing."
        await c.message.answer(txt, reply_markup=join_kb())
        await c.answer()
        return

    # Referral qadam: user tugmani bosib kirganini “tasdiqlaydi”
    # (buni tekshiradigan yo'l yo'q, shuning uchun flag qo'yamiz)
    await set_ref_checked(uid, 1)

    await c.message.answer("✅ Hammasi joyida! Endi bot ishlaydi 👇", reply_markup=main_menu())
    await c.answer("Tasdiqlandi ✅")

@dp.message(Command("admin"))
async def admin(m: Message):
    if not await is_admin(m.from_user.id):
        await m.answer("❌ Siz admin emassiz.")
        return
    await m.answer("🛠 Admin panel:", reply_markup=admin_menu())

@dp.callback_query(F.data == "back_menu")
async def back_menu(c: CallbackQuery):
    await c.message.answer("🎬 Menyu:", reply_markup=main_menu())
    await c.answer()

# ====== USER MENU ======
@dp.callback_query(F.data == "m_search")
async def m_search(c: CallbackQuery):
    ok, msg = await gate_check(c.from_user.id)
    if not ok:
        await c.message.answer(msg, reply_markup=join_kb())
        await c.answer()
        return

    set_state(c.from_user.id, "search")
    await c.message.answer("🔎 Kino nomi yoki so‘z yozing:")
    await c.answer()

@dp.callback_query(F.data == "m_code")
async def m_code(c: CallbackQuery):
    ok, msg = await gate_check(c.from_user.id)
    if not ok:
        await c.message.answer(msg, reply_markup=join_kb())
        await c.answer()
        return

    set_state(c.from_user.id, "code")
    await c.message.answer("🎬 Kino kodini yuboring (masalan: 105):")
    await c.answer()

@dp.callback_query(F.data == "m_ads")
async def m_ads(c: CallbackQuery):
    await c.message.answer("📢 Reklama uchun: /admin → Broadcast (faqat admin).")
    await c.answer()

# ====== ADMIN PANEL ======
@dp.callback_query(F.data == "a_add")
async def a_add(c: CallbackQuery):
    if not await is_admin(c.from_user.id):
        await c.answer("Admin emassiz", show_alert=True)
        return
    set_state(c.from_user.id, "add_code", {})
    await c.message.answer("➕ Kino qo‘shish\n1/4: Kino KOD yuboring (masalan: 105):")
    await c.answer()

@dp.callback_query(F.data == "a_stats")
async def a_stats(c: CallbackQuery):
    if not await is_admin(c.from_user.id):
        await c.answer("Admin emassiz", show_alert=True)
        return
    u, k = await stats()
    await c.message.answer(f"📊 Statistika:\n👤 Userlar: {u}\n🎬 Kinolar: {k}", reply_markup=admin_menu())
    await c.answer()

@dp.callback_query(F.data == "a_broadcast")
async def a_broadcast(c: CallbackQuery):
    if not await is_admin(c.from_user.id):
        await c.answer("Admin emassiz", show_alert=True)
        return
    set_state(c.from_user.id, "broadcast")
    await c.message.answer("📣 Broadcast\nHamma userlarga yuboriladigan matnni yozing:")
    await c.answer()

# ====== TEXT/CONTENT FLOW ======
@dp.message()
async def on_message(m: Message):
    await upsert_user(m)

    st = get_state(m.from_user.id)
    text = (m.text or "").strip()

    # Har doim gate (user keyin chiqib ketsa ham)
    ok, msg_gate = await gate_check(m.from_user.id)
    if not ok:
        await m.answer(msg_gate, reply_markup=join_kb())
        return

    if not st:
        await m.answer("Menyudan foydalaning: /start")
        return

    name = st["name"]
    data = st["data"]

    # ===== USER SEARCH =====
    if name == "search":
        clear_state(m.from_user.id)
        rows = await search_movies(text, limit=10)
        if not rows:
            await m.answer("❌ Hech narsa topilmadi. Boshqa so‘z yozing yoki /start.")
            return
        msg = "✅ Topilganlar:\n\n"
        for code, title in rows:
            msg += f"🎬 {title}\n🔢 Kod: {code}\n\n"
        msg += "Kod yuborsangiz kino chiqadi 👇"
        await m.answer(msg)
        return

    if name == "code":
        row = await get_movie_by_code(text)
        clear_state(m.from_user.id)
        if not row:
            await m.answer("❌ Bu kod bo‘yicha kino topilmadi.")
            return
        code, title, desc, file_id, link = row
        caption = f"🎬 {title}\n🔢 Kod: {code}\n\n📝 {desc or ''}"
        if file_id:
            # video/doc file_id bo'lishi mumkin
            try:
                await m.answer_document(file_id, caption=caption)
            except Exception:
                try:
                    await m.answer_video(file_id, caption=caption)
                except Exception:
                    await m.answer(caption + f"\n\n🔗 {link or 'Fayl yuborib bo‘lmadi'}")
        else:
            await m.answer(caption + f"\n\n🔗 {link or 'Link yo‘q'}")
        return

    # ===== ADMIN ADD MOVIE FLOW =====
    if name == "add_code":
        if not await is_admin(m.from_user.id):
            clear_state(m.from_user.id)
            await m.answer("❌ Admin emassiz.")
            return
        if not text:
            await m.answer("KOD matn bo‘lishi kerak.")
            return
        data["code"] = text
        set_state(m.from_user.id, "add_title", data)
        await m.answer("2/4: Kino NOM yuboring:")
        return

    if name == "add_title":
        if not await is_admin(m.from_user.id):
            clear_state(m.from_user.id)
            await m.answer("❌ Admin emassiz.")
            return
        data["title"] = text
        set_state(m.from_user.id, "add_desc", data)
        await m.answer("3/4: Tavsif (description) yuboring (xohlasangiz qisqa):")
        return

    if name == "add_desc":
        if not await is_admin(m.from_user.id):
            clear_state(m.from_user.id)
            await m.answer("❌ Admin emassiz.")
            return
        data["desc"] = text
        set_state(m.from_user.id, "add_file_or_link", data)
        await m.answer("4/4: Endi kino faylini yuboring (video yoki document) YOKI kino linkini yuboring:")
        return

    if name == "add_file_or_link":
        if not await is_admin(m.from_user.id):
            clear_state(m.from_user.id)
            await m.answer("❌ Admin emassiz.")
            return

        file_id = None
        link = None

        # Video/document kelgan bo'lsa
        if m.video:
            file_id = m.video.file_id
        elif m.document:
            file_id = m.document.file_id
        else:
            # link deb qabul qilamiz
            link = text if text else None

        try:
            await add_movie(
                code=data["code"],
                title=data["title"],
                description=data.get("desc", ""),
                file_id=file_id,
                link=link
            )
        except Exception:
            clear_state(m.from_user.id)
            await m.answer("❌ Bu kod oldin ishlatilgan bo‘lishi mumkin. Boshqa KOD qo‘ying.")
            return

        clear_state(m.from_user.id)
        await m.answer(
            f"✅ Kino qo‘shildi!\n🎬 {data['title']}\n🔢 Kod: {data['code']}",
            reply_markup=admin_menu()
        )
        return

    # ===== ADMIN BROADCAST =====
    if name == "broadcast":
        if not await is_admin(m.from_user.id):
            clear_state(m.from_user.id)
            await m.answer("❌ Admin emassiz.")
            return

        clear_state(m.from_user.id)
        user_ids = await all_user_ids()
        sent = 0
        failed = 0
        for uid in user_ids:
            try:
                await bot.send_message(uid, text)
                sent += 1
            except Exception:
                failed += 1

        await m.answer(f"📣 Broadcast tugadi.\n✅ Yuborildi: {sent}\n❌ Xato: {failed}", reply_markup=admin_menu())
        return

    # fallback
    clear_state(m.from_user.id)
    await m.answer("✅ Tayyor. /start bosing.")

async def main():
    await db_init()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
