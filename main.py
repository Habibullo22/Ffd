import asyncio
import random
import time
from dataclasses import dataclass
from typing import Dict, Optional, Set, Tuple

import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.utils.keyboard import ReplyKeyboardBuilder

# =========================
# CONFIG
# =========================
TOKEN = "8161107014:AAGBWEYVxie7-pB4-2FoGCPjCv_sl0yHogc"
ADMIN_IDS = {5815294733}
DB_PATH = "bot.db"

# Kartalar (ko'rsatish uchun)
PAYMENT_CARDS = {
    "humo":   {"title": "🟦 HUMO",   "card": "9860 XXXX XXXX XXXX", "name": "H. (Ism)"},
    "uzcard": {"title": "🟩 UZCARD", "card": "8600 XXXX XXXX XXXX", "name": "H. (Ism)"},
}

MIN_TOPUP = 20000
MAX_TOPUP = 2000000
TOPUP_TIMEOUT_MIN = 10  # 10 daqiqa ichida chek kelmasa expired
DAILY_BONUS_AMOUNT = 3000

# Mines
MINES_SIZE = 5
MINES_BOMBS = 3

# Aviator
AVIATOR_TICK_SEC = 0.8
AVIATOR_GROWTH = 0.05

dp = Dispatcher()

# =========================
# DB
# =========================
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS users(
  user_id INTEGER PRIMARY KEY,
  real_balance INTEGER NOT NULL DEFAULT 0,
  bonus_balance INTEGER NOT NULL DEFAULT 0,
  topup_verified INTEGER NOT NULL DEFAULT 0,
  ref_by INTEGER,
  ref_count INTEGER NOT NULL DEFAULT 0,
  last_daily_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS promo_codes(
  code TEXT PRIMARY KEY,
  amount INTEGER NOT NULL,
  max_uses INTEGER NOT NULL,
  used_count INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS promo_uses(
  user_id INTEGER NOT NULL,
  code TEXT NOT NULL,
  used_at INTEGER NOT NULL,
  PRIMARY KEY(user_id, code)
);

-- TOPUP: summa -> method -> "to'lov qildim" -> receipt photo -> admin approve/reject
CREATE TABLE IF NOT EXISTS topup_requests(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  amount INTEGER NOT NULL,
  method TEXT NOT NULL DEFAULT '',
  receipt_file_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'waiting_method',  -- waiting_method, waiting_receipt, waiting_admin, approved, rejected, expired, cancelled
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  handled_by INTEGER,
  handled_at INTEGER
);

-- WITHDRAW: summa -> method -> card_number -> admin approve/reject
CREATE TABLE IF NOT EXISTS withdraw_requests(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  amount INTEGER NOT NULL,
  method TEXT NOT NULL DEFAULT '',
  card_number TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending', -- pending, approved, rejected, cancelled
  created_at INTEGER NOT NULL,
  handled_by INTEGER,
  handled_at INTEGER
);
"""

async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_SQL)
        await db.commit()

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def parse_int_like(s: str) -> Optional[int]:
    s = (s or "").replace(" ", "").strip()
    return int(s) if s.isdigit() else None

async def ensure_user(uid: int, ref_by: Optional[int] = None):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM users WHERE user_id=?", (uid,))
        row = await cur.fetchone()
        if row is None:
            ref = ref_by if (ref_by and ref_by != uid) else None
            await db.execute(
                "INSERT INTO users(user_id, real_balance, bonus_balance, topup_verified, ref_by, ref_count, last_daily_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (uid, 0, 0, 0, ref, 0, 0)
            )
            if ref:
                await db.execute("UPDATE users SET ref_count=ref_count+1 WHERE user_id=?", (ref,))
        await db.commit()

async def get_user(uid: int) -> Tuple[int, int, int, Optional[int], int, int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT real_balance, bonus_balance, topup_verified, ref_by, ref_count, last_daily_at "
            "FROM users WHERE user_id=?",
            (uid,)
        )
        row = await cur.fetchone()
        if not row:
            return 0, 0, 0, None, 0, 0
        return int(row[0]), int(row[1]), int(row[2]), row[3], int(row[4]), int(row[5])

async def add_real(uid: int, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET real_balance=real_balance+? WHERE user_id=?", (amount, uid))
        await db.commit()

async def add_bonus(uid: int, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET bonus_balance=bonus_balance+? WHERE user_id=?", (amount, uid))
        await db.commit()

async def take_real(uid: int, amount: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT real_balance FROM users WHERE user_id=?", (uid,))
        row = await cur.fetchone()
        if not row or int(row[0]) < amount:
            return False
        await db.execute("UPDATE users SET real_balance=real_balance-? WHERE user_id=?", (amount, uid))
        await db.commit()
        return True

async def take_bonus(uid: int, amount: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT bonus_balance FROM users WHERE user_id=?", (uid,))
        row = await cur.fetchone()
        if not row or int(row[0]) < amount:
            return False
        await db.execute("UPDATE users SET bonus_balance=bonus_balance-? WHERE user_id=?", (amount, uid))
        await db.commit()
        return True

async def set_last_daily(uid: int, ts: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET last_daily_at=? WHERE user_id=?", (ts, uid))
        await db.commit()

# =========================
# REPLY MENU (PASTDA)
# =========================
def menu_kb(uid: int) -> types.ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="💣 Mines")
    kb.button(text="✈️ Aviator")
    kb.button(text="➕ Hisob to‘ldirish")
    kb.button(text="📤 Pul yechish")
    kb.button(text="💰 Balans")
    kb.button(text="🎁 Promo code")
    kb.button(text="🎁 Kunlik bonus")
    kb.button(text="🤝 Referal")
    kb.button(text="ℹ️ Yordam")
    if is_admin(uid):
        kb.button(text="🛠 Admin panel")
    kb.adjust(2, 2, 2, 2, 1, 1)
    return kb.as_markup(resize_keyboard=True)

def cancel_kb(uid: int) -> types.ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="❌ Bekor qilish")
    kb.button(text="🔙 Menyu")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)

def bet_kb(uid: int) -> types.ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="1 000")
    kb.button(text="2 000")
    kb.button(text="5 000")
    kb.button(text="10 000")
    kb.button(text="20 000")
    kb.button(text="✍️ Boshqa stavka")
    kb.button(text="❌ Bekor qilish")
    kb.button(text="🔙 Menyu")
    kb.adjust(3, 2, 1, 2)
    return kb.as_markup(resize_keyboard=True)

def topup_amount_kb(uid: int) -> types.ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="20 000")
    kb.button(text="50 000")
    kb.button(text="100 000")
    kb.button(text="200 000")
    kb.button(text="500 000")
    kb.button(text="✍️ Boshqa summa")
    kb.button(text="❌ Bekor qilish")
    kb.button(text="🔙 Menyu")
    kb.adjust(3, 2, 1, 2)
    return kb.as_markup(resize_keyboard=True)

def admin_panel_kb(uid: int) -> types.ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="📥 Topup pending")
    kb.button(text="📤 Withdraw pending")
    kb.button(text="🔙 Menyu")
    kb.adjust(2, 1)
    return kb.as_markup(resize_keyboard=True)

# =========================
# INLINE: TOPUP UI
# =========================
def kb_pay_methods(amount: int) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(text="🟦 HUMO", callback_data=f"pay:method:humo:{amount}"),
            types.InlineKeyboardButton(text="🟩 UZCARD", callback_data=f"pay:method:uzcard:{amount}"),
        ],
        [types.InlineKeyboardButton(text="❌ Bekor", callback_data="pay:cancel")]
    ])

def kb_paid_btn(topup_id: int) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="✅ To‘lov qildim", callback_data=f"pay:paid:{topup_id}")],
        [types.InlineKeyboardButton(text="❌ Bekor", callback_data="pay:cancel")]
    ])

def kb_admin_topup(rid: int) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(text="✅ Tasdiq", callback_data=f"top:ok:{rid}"),
            types.InlineKeyboardButton(text="❌ Rad", callback_data=f"top:no:{rid}"),
        ]
    ])

# =========================
# INLINE: WITHDRAW UI
# =========================
def kb_wd_methods(amount: int) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(text="🟦 HUMO", callback_data=f"wd:method:humo:{amount}"),
            types.InlineKeyboardButton(text="🟩 UZCARD", callback_data=f"wd:method:uzcard:{amount}"),
        ],
        [types.InlineKeyboardButton(text="❌ Bekor", callback_data="wd:cancel")]
    ])

def kb_admin_withdraw(rid: int) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(text="✅ Tasdiq", callback_data=f"wda:ok:{rid}"),
            types.InlineKeyboardButton(text="❌ Rad", callback_data=f"wda:no:{rid}"),
        ]
    ])

# =========================
# MINES
# =========================
@dataclass
class MinesSession:
    bet: int
    wallet: str
    bombs: Set[int]
    opened: Set[int]
    active: bool = True

mines_sessions: Dict[int, MinesSession] = {}

def gen_bombs(exclude: int) -> Set[int]:
    cells = list(range(MINES_SIZE * MINES_SIZE))
    if exclude in cells:
        cells.remove(exclude)
    return set(random.sample(cells, MINES_BOMBS))

def mines_multiplier(opened: int) -> float:
    # o'sishi sekinroq
    table = [1.00, 1.06, 1.12, 1.20, 1.30, 1.45, 1.60, 1.80, 2.05, 2.35, 2.70]
    if opened < len(table):
        return table[opened]
    return round(table[-1] + (opened - (len(table) - 1)) * 0.30, 2)

def kb_mines_grid(opened: Set[int], bombs: Optional[Set[int]] = None) -> types.InlineKeyboardMarkup:
    rows = []
    for r in range(MINES_SIZE):
        row = []
        for c in range(MINES_SIZE):
            idx = r * MINES_SIZE + c
            if bombs is not None and idx in bombs:
                txt = "💣"
            elif idx in opened:
                txt = "✅"
            else:
                txt = "⬜️"
            row.append(types.InlineKeyboardButton(text=txt, callback_data=f"mn:pick:{idx}"))
        rows.append(row)
    rows.append([
        types.InlineKeyboardButton(text="💵 Cashout", callback_data="mn:cashout"),
        types.InlineKeyboardButton(text="❌ Stop", callback_data="mn:stop"),
    ])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)

# =========================
# AVIATOR
# =========================
@dataclass
class AviatorSession:
    bet: int
    wallet: str
    mult: float = 1.0
    crashed: bool = False
    cashed_out: bool = False
    msg_id: int = 0
    frame: int = 0

aviator_sessions: Dict[int, AviatorSession] = {}

def render_plane(mult: float, frame: int, bet: int, wallet: str) -> str:
    pos = frame % 19
    track = ["·"] * 19
    track[pos] = "✈️"
    line = "".join(track)
    return (
        "✈️ AVIATOR\n"
        f"Koef: x{mult:.2f}\n"
        f"Bet: {bet} ({wallet.upper()})\n\n"
        f"{line}  ☁️"
    )

def kb_aviator_inline() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="💸 Cashout", callback_data="av:cashout")],
        [types.InlineKeyboardButton(text="❌ Stop", callback_data="av:stop")]
    ])

async def aviator_loop(bot: Bot, uid: int):
    while True:
        await asyncio.sleep(AVIATOR_TICK_SEC)
        s = aviator_sessions.get(uid)
        if not s or s.cashed_out or s.crashed:
            return

        s.frame += 1
        s.mult = round(s.mult * (1.0 + AVIATOR_GROWTH), 2)

        # oddiy crash ehtimoli (ko'p to'lamasligi uchun)
        crash_prob = min(0.03 + (s.mult - 1.0) * 0.020, 0.55)
        if random.random() < crash_prob:
            s.crashed = True
            aviator_sessions[uid] = s
            try:
                await bot.edit_message_text(
                    chat_id=uid,
                    message_id=s.msg_id,
                    text=render_plane(s.mult, s.frame, s.bet, s.wallet) + f"\n\n💥 CRASH! x{s.mult:.2f}\n😅 Keyingi safar omad!",
                    reply_markup=None
                )
            except:
                pass
            return

        aviator_sessions[uid] = s
        try:
            await bot.edit_message_text(
                chat_id=uid,
                message_id=s.msg_id,
                text=render_plane(s.mult, s.frame, s.bet, s.wallet),
                reply_markup=kb_aviator_inline()
            )
        except:
            pass

# =========================
# STATES
# =========================
steps: Dict[int, Dict[str, str]] = {}

# =========================
# START
# =========================
@dp.message(CommandStart())
async def start(m: types.Message):
    uid = m.from_user.id
    parts = (m.text or "").split()
    ref_by = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else None
    await ensure_user(uid, ref_by=ref_by)
    await m.answer("Menyu 👇", reply_markup=menu_kb(uid))

@dp.message(F.text == "🔙 Menyu")
async def menu_back(m: types.Message):
    steps.pop(m.from_user.id, None)
    await m.answer("Menyu 👇", reply_markup=menu_kb(m.from_user.id))

@dp.message(F.text == "❌ Bekor qilish")
async def cancel(m: types.Message):
    steps.pop(m.from_user.id, None)
    await m.answer("Bekor qilindi.", reply_markup=menu_kb(m.from_user.id))

# =========================
# INFO
# =========================
@dp.message(F.text == "💰 Balans")
async def balance(m: types.Message):
    uid = m.from_user.id
    real, bonus, top_ok, _, refc, _ = await get_user(uid)
    await m.answer(
        f"💰 Balans\n✅ Real: {real}\n🎁 Bonus: {bonus}\n📌 Topup: {'✅' if top_ok else '❌'}\n👥 Referal: {refc}",
        reply_markup=menu_kb(uid)
    )

@dp.message(F.text == "ℹ️ Yordam")
async def help_(m: types.Message):
    await m.answer(
        "ℹ️ Yordam\n"
        "• Menyu pastda turadi.\n"
        "• Hisob to‘ldirish: summa → HUMO/UZCARD → karta → To‘lov qildim → chek rasm → admin tasdiq.\n"
        "• 10 daqiqada chek yuborilmasa bekor bo‘ladi.\n",
        reply_markup=menu_kb(m.from_user.id)
    )

@dp.message(F.text == "🤝 Referal")
async def ref(m: types.Message):
    uid = m.from_user.id
    me = await m.bot.get_me()
    _, _, _, _, refc, _ = await get_user(uid)
    link = f"https://t.me/{me.username}?start={uid}"
    await m.answer(f"🤝 Referal link:\n{link}\n👥 Taklif qilganlar: {refc}", reply_markup=menu_kb(uid))

@dp.message(F.text == "🎁 Kunlik bonus")
async def daily(m: types.Message):
    uid = m.from_user.id
    _, _, _, _, _, last_daily = await get_user(uid)
    now = int(time.time())
    if now - int(last_daily) < 86400:
        left = 86400 - (now - int(last_daily))
        h = left // 3600
        mm = (left % 3600) // 60
        return await m.answer(f"⏳ Hali tayyor emas.\nQolgan: {h} soat {mm} daqiqa", reply_markup=menu_kb(uid))

    await add_bonus(uid, DAILY_BONUS_AMOUNT)
    await set_last_daily(uid, now)
    await m.answer(f"✅ +{DAILY_BONUS_AMOUNT} BONUS qo‘shildi.", reply_markup=menu_kb(uid))

# =========================
# PROMO
# =========================
@dp.message(Command("mkpromo"))
async def mkpromo(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) != 4:
        return await m.answer("Format: /mkpromo CODE AMOUNT MAXUSES\nMasalan: /mkpromo BONUS10 10000 50")
    code = parts[1].upper()
    if not parts[2].isdigit() or not parts[3].isdigit():
        return await m.answer("AMOUNT va MAXUSES son bo‘lsin.")
    amount = int(parts[2]); maxuses = int(parts[3])

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO promo_codes(code,amount,max_uses,used_count,created_at) VALUES(?,?,?,?,?)",
            (code, amount, maxuses, 0, int(time.time()))
        )
        await db.commit()
    await m.answer(f"✅ Promo yaratildi: {code} | +{amount} BONUS | max={maxuses}")

@dp.message(F.text == "🎁 Promo code")
async def promo_start(m: types.Message):
    steps[m.from_user.id] = {"mode": "promo_enter"}
    await m.answer("🎁 Promo kodni yozing:", reply_markup=cancel_kb(m.from_user.id))

# =========================
# TOPUP FLOW
# =========================
@dp.message(F.text == "➕ Hisob to‘ldirish")
async def topup_start(m: types.Message):
    uid = m.from_user.id
    steps[uid] = {"mode": "topup_amount"}
    await m.answer(
        f"➕ Hisob to‘ldirish\nMin {MIN_TOPUP} / Max {MAX_TOPUP}\nSummani yozing yoki tugmadan tanlang:",
        reply_markup=topup_amount_kb(uid)
    )

@dp.message(F.text.in_({"20 000","50 000","100 000","200 000","500 000","✍️ Boshqa summa"}))
async def topup_buttons(m: types.Message):
    uid = m.from_user.id
    st = steps.get(uid)
    if not st or st.get("mode") != "topup_amount":
        return

    if m.text == "✍️ Boshqa summa":
        steps[uid] = {"mode": "topup_amount_custom"}
        return await m.answer("✍️ Summani son bilan yozing:", reply_markup=cancel_kb(uid))

    amount = parse_int_like(m.text)
    if amount is None:
        return
    steps.pop(uid, None)
    await m.answer(f"Summa: {amount}\nQaysi karta bilan to‘laysiz?", reply_markup=kb_pay_methods(amount))

@dp.callback_query(F.data.startswith("pay:method:"))
async def pay_choose_method(q: types.CallbackQuery):
    uid = q.from_user.id
    _, _, _, _, _, _ = await get_user(uid)

    _, _, method, amount_s = q.data.split(":")
    amount = int(amount_s)

    if amount < MIN_TOPUP or amount > MAX_TOPUP:
        return await q.answer("Summa noto‘g‘ri.", show_alert=True)

    now = int(time.time())
    expires_at = now + TOPUP_TIMEOUT_MIN * 60

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO topup_requests(user_id,amount,method,receipt_file_id,status,created_at,expires_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (uid, amount, method, "", "waiting_receipt", now, expires_at)
        )
        await db.commit()
        cur = await db.execute("SELECT last_insert_rowid()")
        rid = (await cur.fetchone())[0]

    card = PAYMENT_CARDS[method]
    text = (
        f"{card['title']} orqali to‘lov\n\n"
        f"💳 Karta: {card['card']}\n"
        f"👤 Ism: {card['name']}\n"
        f"💰 Summa: {amount}\n\n"
        f"⏳ {TOPUP_TIMEOUT_MIN} daqiqada chek yuborilmasa hisobga olinmaydi.\n"
        f"To‘lov qilgan bo‘lsangiz ✅ To‘lov qildim ni bosing."
    )
    await q.message.edit_text(text, reply_markup=kb_paid_btn(rid))
    await q.answer()

@dp.callback_query(F.data.startswith("pay:paid:"))
async def pay_paid(q: types.CallbackQuery):
    uid = q.from_user.id
    rid = int(q.data.split(":")[2])

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT status,expires_at FROM topup_requests WHERE id=? AND user_id=?", (rid, uid))
        row = await cur.fetchone()
        if not row:
            return await q.answer("So‘rov topilmadi.", show_alert=True)

        status, expires_at = row[0], int(row[1])
        if status not in ("waiting_receipt",):
            return await q.answer("Status noto‘g‘ri.", show_alert=True)

        if int(time.time()) > expires_at:
            await db.execute("UPDATE topup_requests SET status='expired' WHERE id=?", (rid,))
            await db.commit()
            return await q.answer("⏳ Vaqt tugadi.", show_alert=True)

    steps[uid] = {"mode": "topup_receipt", "rid": str(rid)}
    await q.message.edit_text("✅ Endi chekni rasm qilib yuboring (photo).", reply_markup=None)
    await q.answer()

@dp.callback_query(F.data == "pay:cancel")
async def pay_cancel(q: types.CallbackQuery):
    await q.answer("Bekor.")
    try:
        await q.message.edit_reply_markup(reply_markup=None)
    except:
        pass

@dp.message(F.photo)
async def on_photo(m: types.Message):
    uid = m.from_user.id
    st = steps.get(uid)
    if not st or st.get("mode") != "topup_receipt":
        return

    rid = int(st["rid"])
    file_id = m.photo[-1].file_id

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT amount,method,status,expires_at FROM topup_requests WHERE id=? AND user_id=?", (rid, uid))
        row = await cur.fetchone()
        if not row:
            steps.pop(uid, None)
            return await m.answer("So‘rov topilmadi.", reply_markup=menu_kb(uid))

        amount, method, status, expires_at = int(row[0]), row[1], row[2], int(row[3])

        if int(time.time()) > expires_at:
            await db.execute("UPDATE topup_requests SET status='expired' WHERE id=?", (rid,))
            await db.commit()
            steps.pop(uid, None)
            return await m.answer("⏳ Vaqt tugadi. Qayta urinib ko‘ring.", reply_markup=menu_kb(uid))

        await db.execute(
            "UPDATE topup_requests SET receipt_file_id=?, status='waiting_admin' WHERE id=?",
            (file_id, rid)
        )
        await db.commit()

    steps.pop(uid, None)
    await m.answer("✅ Chek yuborildi. Admin tekshiradi.", reply_markup=menu_kb(uid))

    card = PAYMENT_CARDS.get(method, {"title": method})
    for a in ADMIN_IDS:
        try:
            await m.bot.send_photo(
                a,
                photo=file_id,
                caption=(
                    f"📥 TOPUP\n"
                    f"ID #{rid}\n"
                    f"User: {uid}\n"
                    f"Method: {card['title']}\n"
                    f"Amount: {amount}\n\n"
                    f"Tasdiqlaysizmi?"
                ),
                reply_markup=kb_admin_topup(rid)
            )
        except:
            pass

@dp.callback_query(F.data.startswith("top:ok:"))
async def admin_top_ok(q: types.CallbackQuery):
    if not is_admin(q.from_user.id):
        return await q.answer("Yo‘q.", show_alert=True)

    rid = int(q.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id,amount,status FROM topup_requests WHERE id=?", (rid,))
        row = await cur.fetchone()
        if not row or row[2] != "waiting_admin":
            return await q.answer("Status noto‘g‘ri.", show_alert=True)

        uid, amount = int(row[0]), int(row[1])

        await db.execute("UPDATE topup_requests SET status='approved', handled_by=?, handled_at=? WHERE id=?",
                         (q.from_user.id, int(time.time()), rid))
        await db.execute("UPDATE users SET real_balance=real_balance+?, topup_verified=1 WHERE user_id=?", (amount, uid))
        await db.commit()

    try:
        await q.bot.send_message(uid, f"✅ To‘lov tasdiqlandi! +{amount} REAL", reply_markup=menu_kb(uid))
    except:
        pass

    await q.answer("OK")
    try:
        await q.message.edit_reply_markup(reply_markup=None)
    except:
        pass

@dp.callback_query(F.data.startswith("top:no:"))
async def admin_top_no(q: types.CallbackQuery):
    if not is_admin(q.from_user.id):
        return await q.answer("Yo‘q.", show_alert=True)

    rid = int(q.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id,amount,status FROM topup_requests WHERE id=?", (rid,))
        row = await cur.fetchone()
        if not row or row[2] != "waiting_admin":
            return await q.answer("Status noto‘g‘ri.", show_alert=True)
        uid, amount = int(row[0]), int(row[1])

        await db.execute("UPDATE topup_requests SET status='rejected', handled_by=?, handled_at=? WHERE id=?",
                         (q.from_user.id, int(time.time()), rid))
        await db.commit()

    try:
        await q.bot.send_message(uid, f"❌ To‘lov rad qilindi. (Summa: {amount})", reply_markup=menu_kb(uid))
    except:
        pass

    await q.answer("Rejected")
    try:
        await q.message.edit_reply_markup(reply_markup=None)
    except:
        pass

# =========================
# WITHDRAW FLOW
# =========================
@dp.message(F.text == "📤 Pul yechish")
async def withdraw_start(m: types.Message):
    uid = m.from_user.id
    real, _, top_ok, *_ = await get_user(uid)
    if not top_ok:
        return await m.answer("❌ Pul yechish uchun avval TOPUP tasdiqlangan bo‘lishi kerak.", reply_markup=menu_kb(uid))
    if real <= 0:
        return await m.answer("❌ REAL balans 0.", reply_markup=menu_kb(uid))

    steps[uid] = {"mode": "wd_amount"}
    await m.answer("📤 Pul yechish\nSummani yozing (son):", reply_markup=cancel_kb(uid))

@dp.callback_query(F.data.startswith("wd:method:"))
async def wd_method(q: types.CallbackQuery):
    uid = q.from_user.id
    _, _, method, amount_s = q.data.split(":")
    amount = int(amount_s)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO withdraw_requests(user_id,amount,method,card_number,status,created_at) VALUES(?,?,?,?,?,?)",
            (uid, amount, method, "", "pending", int(time.time()))
        )
        await db.commit()
        cur = await db.execute("SELECT last_insert_rowid()")
        rid = (await cur.fetchone())[0]

    steps[uid] = {"mode": "wd_card", "rid": str(rid)}
    await q.message.edit_text("💳 Karta raqamingizni yozing (faqat raqam):", reply_markup=None)
    await q.answer()

@dp.callback_query(F.data == "wd:cancel")
async def wd_cancel(q: types.CallbackQuery):
    await q.answer("Bekor.")
    try:
        await q.message.edit_reply_markup(reply_markup=None)
    except:
        pass

@dp.callback_query(F.data.startswith("wda:ok:"))
async def admin_wd_ok(q: types.CallbackQuery):
    if not is_admin(q.from_user.id):
        return await q.answer("Yo‘q.", show_alert=True)
    rid = int(q.data.split(":")[2])

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id,amount,status FROM withdraw_requests WHERE id=?", (rid,))
        row = await cur.fetchone()
        if not row or row[2] != "pending":
            return await q.answer("Status noto‘g‘ri.", show_alert=True)

        uid, amount = int(row[0]), int(row[1])

        await db.execute("UPDATE withdraw_requests SET status='approved', handled_by=?, handled_at=? WHERE id=?",
                         (q.from_user.id, int(time.time()), rid))
        await db.commit()

    try:
        await q.bot.send_message(uid, f"✅ Withdraw tasdiqlandi! (Summa: {amount})", reply_markup=menu_kb(uid))
    except:
        pass

    await q.answer("OK")
    try:
        await q.message.edit_reply_markup(reply_markup=None)
    except:
        pass

@dp.callback_query(F.data.startswith("wda:no:"))
async def admin_wd_no(q: types.CallbackQuery):
    if not is_admin(q.from_user.id):
        return await q.answer("Yo‘q.", show_alert=True)
    rid = int(q.data.split(":")[2])

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id,amount,status FROM withdraw_requests WHERE id=?", (rid,))
        row = await cur.fetchone()
        if not row or row[2] != "pending":
            return await q.answer("Status noto‘g‘ri.", show_alert=True)
        uid, amount = int(row[0]), int(row[1])

        # rad bo'lsa pulni qaytaramiz
        await db.execute("UPDATE withdraw_requests SET status='rejected', handled_by=?, handled_at=? WHERE id=?",
                         (q.from_user.id, int(time.time()), rid))
        await db.execute("UPDATE users SET real_balance=real_balance+? WHERE user_id=?", (amount, uid))
        await db.commit()

    try:
        await q.bot.send_message(uid, f"❌ Withdraw rad qilindi. Pul qaytarildi: {amount}", reply_markup=menu_kb(uid))
    except:
        pass

    await q.answer("Rejected")
    try:
        await q.message.edit_reply_markup(reply_markup=None)
    except:
        pass

# =========================
# ADMIN PANEL
# =========================
@dp.message(F.text == "🛠 Admin panel")
async def admin_panel(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    await m.answer("🛠 Admin panel", reply_markup=admin_panel_kb(m.from_user.id))

@dp.message(F.text == "📥 Topup pending")
async def admin_top_list(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id,user_id,amount,method,status FROM topup_requests WHERE status='waiting_admin' ORDER BY id DESC LIMIT 10"
        )
        rows = await cur.fetchall()
    if not rows:
        return await m.answer("Pending topup yo‘q.", reply_markup=admin_panel_kb(m.from_user.id))
    for rid, uid, amount, method, status in rows:
        await m.answer(f"📥 TOPUP #{rid}\nUser: {uid}\nAmount: {amount}\nMethod: {method}\nStatus: {status}")

@dp.message(F.text == "📤 Withdraw pending")
async def admin_wd_list(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id,user_id,amount,method,card_number,status FROM withdraw_requests WHERE status='pending' ORDER BY id DESC LIMIT 10"
        )
        rows = await cur.fetchall()
    if not rows:
        return await m.answer("Pending withdraw yo‘q.", reply_markup=admin_panel_kb(m.from_user.id))
    for rid, uid, amount, method, card, status in rows:
        await m.answer(
            f"📤 WITHDRAW #{rid}\nUser: {uid}\nAmount: {amount}\nMethod: {method}\nCard: {card}\nStatus: {status}",
            reply_markup=kb_admin_withdraw(rid)
        )

# =========================
# GAMES START
# =========================
@dp.message(F.text == "💣 Mines")
async def mines_start(m: types.Message):
    steps[m.from_user.id] = {"mode": "mn_bet"}
    await m.answer("💣 Mines\nStavkani tanlang:", reply_markup=bet_kb(m.from_user.id))

@dp.message(F.text == "✈️ Aviator")
async def aviator_start(m: types.Message):
    steps[m.from_user.id] = {"mode": "av_bet"}
    await m.answer("✈️ Aviator\nStavkani tanlang:", reply_markup=bet_kb(m.from_user.id))

@dp.message(F.text.in_({"1 000","2 000","5 000","10 000","20 000","✍️ Boshqa stavka"}))
async def bet_buttons(m: types.Message):
    uid = m.from_user.id
    st = steps.get(uid)
    if not st or st.get("mode") not in ("mn_bet","av_bet"):
        return
    if m.text == "✍️ Boshqa stavka":
        steps[uid] = {"mode": "mn_bet_custom"} if st["mode"] == "mn_bet" else {"mode": "av_bet_custom"}
        return await m.answer("✍️ Stavkani son bilan yozing:", reply_markup=cancel_kb(uid))

    bet = parse_int_like(m.text)
    mode = st["mode"]
    steps.pop(uid, None)
    if mode == "mn_bet":
        return await start_mines_game(m, bet)
    return await start_aviator_game(m, bet)

async def start_mines_game(m: types.Message, bet: int):
    uid = m.from_user.id
    real, bonus, *_ = await get_user(uid)
    wallet = "real" if real >= bet else ("bonus" if bonus >= bet else "")
    if not wallet:
        return await m.answer("❌ Balans yetarli emas.", reply_markup=menu_kb(uid))

    ok = await (take_real(uid, bet) if wallet == "real" else take_bonus(uid, bet))
    if not ok:
        return await m.answer("❌ Balans yetarli emas.", reply_markup=menu_kb(uid))

    mines_sessions[uid] = MinesSession(bet=bet, wallet=wallet, bombs=set(), opened=set(), active=True)
    await m.answer(f"💣 Mines boshlandi! Bet: {bet} ({wallet.upper()})", reply_markup=menu_kb(uid))
    await m.answer("⬇️ O‘yin:", reply_markup=kb_mines_grid(set()))

async def start_aviator_game(m: types.Message, bet: int):
    uid = m.from_user.id
    real, bonus, *_ = await get_user(uid)
    wallet = "real" if real >= bet else ("bonus" if bonus >= bet else "")
    if not wallet:
        return await m.answer("❌ Balans yetarli emas.", reply_markup=menu_kb(uid))

    ok = await (take_real(uid, bet) if wallet == "real" else take_bonus(uid, bet))
    if not ok:
        return await m.answer("❌ Balans yetarli emas.", reply_markup=menu_kb(uid))

    sent = await m.answer(render_plane(1.00, 0, bet, wallet), reply_markup=kb_aviator_inline())
    aviator_sessions[uid] = AviatorSession(bet=bet, wallet=wallet, msg_id=sent.message_id)
    asyncio.create_task(aviator_loop(m.bot, uid))
    await m.answer("✈️ Aviator boshlandi!", reply_markup=menu_kb(uid))

@dp.callback_query(F.data.startswith("mn:pick:"))
async def mn_pick(q: types.CallbackQuery):
    uid = q.from_user.id
    s = mines_sessions.get(uid)
    if not s or not s.active:
        return await q.answer("O‘yin yo‘q.", show_alert=False)

    idx = int(q.data.split(":")[2])
    if idx in s.opened:
        return await q.answer("Ochilgan.", show_alert=False)

    if not s.bombs:
        s.bombs = gen_bombs(exclude=idx)

    if idx in s.bombs:
        s.active = False
        mines_sessions[uid] = s
        await q.message.edit_text("💥 Bomb!\n😅 Keyingi safar omad!", reply_markup=kb_mines_grid(s.opened, bombs=s.bombs))
        return await q.answer()

    s.opened.add(idx)
    mines_sessions[uid] = s
    opened = len(s.opened)
    mult = mines_multiplier(opened)
    await q.message.edit_text(f"💣 Mines\nOchilgan: {opened}\nKoef: x{mult:.2f}", reply_markup=kb_mines_grid(s.opened))
    await q.answer()

@dp.callback_query(F.data == "mn:cashout")
async def mn_cashout(q: types.CallbackQuery):
    uid = q.from_user.id
    s = mines_sessions.get(uid)
    if not s or not s.active:
        return await q.answer("O‘yin yo‘q.", show_alert=False)

    opened = len(s.opened)
    if opened == 0:
        return await q.answer("Avval katak oching.", show_alert=True)

    s.active = False
    mines_sessions[uid] = s
    mult = mines_multiplier(opened)
    win = int(round(s.bet * mult))

    # BONUS bilan o'ynasa ham yutuq REALga o'tadi (sen so'ragan)
    await add_real(uid, win)

    await q.message.edit_text(f"✅ Cashout!\nKoef: x{mult:.2f}\nYutuq: {win} → REAL", reply_markup=None)
    await q.answer()

@dp.callback_query(F.data == "mn:stop")
async def mn_stop(q: types.CallbackQuery):
    mines_sessions.pop(q.from_user.id, None)
    await q.message.edit_text("❌ Mines to‘xtatildi.", reply_markup=None)
    await q.answer()

@dp.callback_query(F.data == "av:cashout")
async def av_cashout(q: types.CallbackQuery):
    uid = q.from_user.id
    s = aviator_sessions.get(uid)
    if not s or s.cashed_out or s.crashed:
        return await q.answer("O‘yin yo‘q.", show_alert=False)

    s.cashed_out = True
    aviator_sessions[uid] = s
    win = int(round(s.bet * s.mult))

    await add_real(uid, win)

    await q.bot.edit_message_text(
        chat_id=uid,
        message_id=s.msg_id,
        text=render_plane(s.mult, s.frame, s.bet, s.wallet) + f"\n\n✅ CASHOUT!\nYutuq: {win} → REAL",
        reply_markup=None
    )
    await q.answer("Cashout!")

@dp.callback_query(F.data == "av:stop")
async def av_stop(q: types.CallbackQuery):
    aviator_sessions.pop(q.from_user.id, None)
    try:
        await q.message.edit_reply_markup(reply_markup=None)
    except:
        pass
    await q.answer("Stop")

# =========================
# TEXT ROUTER (ENG OXIRIDA)
# =========================
@dp.message(F.text)
async def text_router(m: types.Message):
    uid = m.from_user.id
    await ensure_user(uid)
    st = steps.get(uid)
    if not st:
        return

    txt = (m.text or "").strip()
    mode = st.get("mode")

    # promo
    if mode == "promo_enter":
        code = txt.upper()
        now = int(time.time())
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT 1 FROM promo_uses WHERE user_id=? AND code=?", (uid, code))
            if await cur.fetchone():
                steps.pop(uid, None)
                return await m.answer("❌ Bu promo sizda ishlatilgan.", reply_markup=menu_kb(uid))

            cur = await db.execute("SELECT amount,max_uses,used_count FROM promo_codes WHERE code=?", (code,))
            row = await cur.fetchone()
            if not row:
                steps.pop(uid, None)
                return await m.answer("❌ Promo topilmadi.", reply_markup=menu_kb(uid))

            amount, max_uses, used_count = int(row[0]), int(row[1]), int(row[2])
            if used_count >= max_uses:
                steps.pop(uid, None)
                return await m.answer("❌ Promo limiti tugagan.", reply_markup=menu_kb(uid))

            await db.execute("INSERT INTO promo_uses(user_id,code,used_at) VALUES(?,?,?)", (uid, code, now))
            await db.execute("UPDATE promo_codes SET used_count=used_count+1 WHERE code=?", (code,))
            await db.execute("UPDATE users SET bonus_balance=bonus_balance+? WHERE user_id=?", (amount, uid))
            await db.commit()

        steps.pop(uid, None)
        return await m.answer(f"✅ Promo: +{amount} BONUS", reply_markup=menu_kb(uid))

    # topup custom amount
    if mode == "topup_amount_custom":
        amount = parse_int_like(txt)
        if amount is None:
            return await m.answer("❌ Summani son bilan yozing.", reply_markup=cancel_kb(uid))
        steps.pop(uid, None)
        return await m.answer(f"Summa: {amount}\nQaysi karta bilan to‘laysiz?", reply_markup=kb_pay_methods(amount))

    # mines/aviator custom bet
    if mode in ("mn_bet_custom","av_bet_custom"):
        bet = parse_int_like(txt)
        if bet is None or bet <= 0:
            return await m.answer("❌ Stavkani son bilan yozing.", reply_markup=cancel_kb(uid))
        steps.pop(uid, None)
        if mode == "mn_bet_custom":
            return await start_mines_game(m, bet)
        return await start_aviator_game(m, bet)

    # withdraw flow: amount
    if mode == "wd_amount":
        amount = parse_int_like(txt)
        if amount is None or amount <= 0:
            return await m.answer("❌ Summani son bilan yozing.", reply_markup=cancel_kb(uid))
        real, _, top_ok, *_ = await get_user(uid)
        if not top_ok:
            steps.pop(uid, None)
            return await m.answer("❌ Avval topup tasdiqlansin.", reply_markup=menu_kb(uid))
        if amount > real:
            return await m.answer("❌ REAL yetarli emas.", reply_markup=cancel_kb(uid))

        ok = await take_real(uid, amount)  # reserve
        if not ok:
            steps.pop(uid, None)
            return await m.answer("❌ REAL yetarli emas.", reply_markup=menu_kb(uid))

        steps[uid] = {"mode": "wd_choose_method", "amount": str(amount)}
        return await m.answer("Qaysi karta turiga yechiladi?", reply_markup=menu_kb(uid)) or \
               await m.answer("⬇️ Tanlang:", reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                   [
                       types.InlineKeyboardButton(text="🟦 HUMO", callback_data=f"wd:method:humo:{amount}"),
                       types.InlineKeyboardButton(text="🟩 UZCARD", callback_data=f"wd:method:uzcard:{amount}")
                   ],
                   [types.InlineKeyboardButton(text="❌ Bekor", callback_data="wd:cancel")]
               ]))

    # withdraw flow: card number
    if mode == "wd_card":
        rid = int(st["rid"])
        card = txt.replace(" ", "")
        if not card.isdigit() or len(card) < 12:
            return await m.answer("❌ Karta raqam noto‘g‘ri. Qayta yozing (faqat raqam).", reply_markup=cancel_kb(uid))

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE withdraw_requests SET card_number=? WHERE id=? AND user_id=?", (card, rid, uid))
            cur = await db.execute("SELECT amount,method,status FROM withdraw_requests WHERE id=?", (rid,))
            row = await cur.fetchone()
            await db.commit()

        steps.pop(uid, None)
        await m.answer("✅ So‘rov yuborildi. Admin ko‘rib chiqadi.", reply_markup=menu_kb(uid))

        amount, method, status = int(row[0]), row[1], row[2]
        for a in ADMIN_IDS:
            try:
                await m.bot.send_message(
                    a,
                    f"📤 WITHDRAW\nID #{rid}\nUser: {uid}\nAmount: {amount}\nMethod: {method}\nCard: {card}",
                    reply_markup=kb_admin_withdraw(rid)
                )
            except:
                pass
        return

# =========================
# RUN
# =========================
async def main():
    await db_init()
    bot = Bot(TOKEN)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
