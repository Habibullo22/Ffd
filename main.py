import asyncio
import random
import time
from dataclasses import dataclass
from typing import Dict, Optional, Set, Tuple

import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.exceptions import TelegramBadRequest

# =========================
# CONFIG
# =========================
TOKEN = "8161107014:AAGBWEYVxie7-pB4-2FoGCPjCv_sl0yHogc"
ADMIN_IDS = {5815294733}

DB_PATH = "casino.db"

CARD_HUMO = "9860 6067 5024 7151"
CARD_UZCARD = "8600 0000 0000 0000"

MIN_DEP = 20000
MAX_DEP = 2000000

# 50k deposit => 45k real (10% fee)
DEPOSIT_CREDIT_RATE = 0.90

# BONUS (chiqmaydi)
DAILY_BONUS_AMOUNT = 3000

# MINES
MINES_SIZE = 5
MINES_BOMBS = 3

# AVIATOR
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
  deposit_verified INTEGER NOT NULL DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS deposit_requests(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  amount INTEGER NOT NULL,
  method TEXT NOT NULL,
  receipt_file_id TEXT,
  status TEXT NOT NULL DEFAULT 'pending', -- pending/approved/rejected
  created_at INTEGER NOT NULL,
  handled_by INTEGER,
  handled_at INTEGER
);

CREATE TABLE IF NOT EXISTS withdraw_requests(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  amount INTEGER NOT NULL,
  card TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at INTEGER NOT NULL,
  handled_by INTEGER,
  handled_at INTEGER
);

CREATE TABLE IF NOT EXISTS house_profit(
  id INTEGER PRIMARY KEY CHECK (id=1),
  profit INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO house_profit(id, profit) VALUES(1, 0);
"""

async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_SQL)
        await db.commit()

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

async def ensure_user(uid: int, ref_by: Optional[int] = None):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM users WHERE user_id=?", (uid,))
        row = await cur.fetchone()
        if row is None:
            ref = ref_by if (ref_by and ref_by != uid) else None
            await db.execute(
                "INSERT INTO users(user_id, real_balance, bonus_balance, deposit_verified, ref_by, ref_count, last_daily_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (uid, 0, 0, 0, ref, 0, 0)
            )
            if ref:
                await db.execute("UPDATE users SET ref_count = ref_count + 1 WHERE user_id=?", (ref,))
        await db.commit()

async def get_user(uid: int) -> Tuple[int, int, int, Optional[int], int, int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT real_balance, bonus_balance, deposit_verified, ref_by, ref_count, last_daily_at "
            "FROM users WHERE user_id=?",
            (uid,)
        )
        row = await cur.fetchone()
        if not row:
            return 0, 0, 0, None, 0, 0
        return int(row[0]), int(row[1]), int(row[2]), row[3], int(row[4]), int(row[5])

async def add_real(uid: int, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET real_balance = real_balance + ? WHERE user_id=?", (amount, uid))
        await db.commit()

async def add_bonus(uid: int, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET bonus_balance = bonus_balance + ? WHERE user_id=?", (amount, uid))
        await db.commit()

async def take_real(uid: int, amount: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT real_balance FROM users WHERE user_id=?", (uid,))
        row = await cur.fetchone()
        if not row or int(row[0]) < amount:
            return False
        await db.execute("UPDATE users SET real_balance = real_balance - ? WHERE user_id=?", (amount, uid))
        await db.commit()
        return True

async def take_bonus(uid: int, amount: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT bonus_balance FROM users WHERE user_id=?", (uid,))
        row = await cur.fetchone()
        if not row or int(row[0]) < amount:
            return False
        await db.execute("UPDATE users SET bonus_balance = bonus_balance - ? WHERE user_id=?", (amount, uid))
        await db.commit()
        return True

async def set_last_daily(uid: int, ts: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET last_daily_at=? WHERE user_id=?", (ts, uid))
        await db.commit()

async def add_house_profit(amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE house_profit SET profit = profit + ? WHERE id=1", (amount,))
        await db.commit()

# =========================
# INLINE UI
# =========================
def kb_main(uid: int) -> types.InlineKeyboardMarkup:
    kb = [
        [types.InlineKeyboardButton(text="💣 Mines", callback_data="go:mines"),
         types.InlineKeyboardButton(text="✈️ Aviator", callback_data="go:aviator")],
        [types.InlineKeyboardButton(text="➕ Hisob to‘ldirish", callback_data="go:deposit"),
         types.InlineKeyboardButton(text="📤 Pul yechish", callback_data="go:withdraw")],
        [types.InlineKeyboardButton(text="💰 Balans", callback_data="go:balance"),
         types.InlineKeyboardButton(text="🎁 Promo code", callback_data="go:promo")],
        [types.InlineKeyboardButton(text="🎁 Kunlik bonus", callback_data="go:daily"),
         types.InlineKeyboardButton(text="🤝 Referal", callback_data="go:ref")],
        [types.InlineKeyboardButton(text="ℹ️ Yordam", callback_data="go:help")]
    ]
    if is_admin(uid):
        kb.append([types.InlineKeyboardButton(text="🛠 Admin panel", callback_data="adm:panel")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def kb_menu() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🔙 Menyu", callback_data="go:menu")]
    ])

def kb_deposit_amount() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="20 000", callback_data="dep:amt:20000"),
         types.InlineKeyboardButton(text="50 000", callback_data="dep:amt:50000"),
         types.InlineKeyboardButton(text="100 000", callback_data="dep:amt:100000")],
        [types.InlineKeyboardButton(text="200 000", callback_data="dep:amt:200000"),
         types.InlineKeyboardButton(text="500 000", callback_data="dep:amt:500000")],
        [types.InlineKeyboardButton(text="✍️ Boshqa summa", callback_data="dep:amt:custom")],
        [types.InlineKeyboardButton(text="🔙 Menyu", callback_data="go:menu")]
    ])

def kb_deposit_method(amount: int) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🟦 HUMO", callback_data=f"dep:m:HUMO:{amount}"),
         types.InlineKeyboardButton(text="💳 UZCARD", callback_data=f"dep:m:UZCARD:{amount}")],
        [types.InlineKeyboardButton(text="🔙 Menyu", callback_data="go:menu")]
    ])

def kb_deposit_paid(method: str, amount: int) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="✅ To‘lov qildim", callback_data=f"dep:paid:{method}:{amount}")],
        [types.InlineKeyboardButton(text="🔙 Menyu", callback_data="go:menu")]
    ])

def kb_withdraw() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="✍️ Summani yozish", callback_data="wd:amt")],
        [types.InlineKeyboardButton(text="🔙 Menyu", callback_data="go:menu")]
    ])

def kb_aviator_bet() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="1 000", callback_data="av:bet:1000"),
         types.InlineKeyboardButton(text="2 000", callback_data="av:bet:2000"),
         types.InlineKeyboardButton(text="5 000", callback_data="av:bet:5000")],
        [types.InlineKeyboardButton(text="10 000", callback_data="av:bet:10000"),
         types.InlineKeyboardButton(text="20 000", callback_data="av:bet:20000")],
        [types.InlineKeyboardButton(text="✍️ Boshqa stavka", callback_data="av:bet:custom")],
        [types.InlineKeyboardButton(text="🔙 Menyu", callback_data="go:menu")]
    ])

def kb_aviator_play() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="💸 Cashout", callback_data="av:cashout")],
        [types.InlineKeyboardButton(text="❌ Stop", callback_data="av:stop"),
         types.InlineKeyboardButton(text="🔙 Menyu", callback_data="go:menu")]
    ])

def kb_mines_bet() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="1 000", callback_data="mn:bet:1000"),
         types.InlineKeyboardButton(text="2 000", callback_data="mn:bet:2000"),
         types.InlineKeyboardButton(text="5 000", callback_data="mn:bet:5000")],
        [types.InlineKeyboardButton(text="10 000", callback_data="mn:bet:10000"),
         types.InlineKeyboardButton(text="20 000", callback_data="mn:bet:20000")],
        [types.InlineKeyboardButton(text="✍️ Boshqa stavka", callback_data="mn:bet:custom")],
        [types.InlineKeyboardButton(text="🔙 Menyu", callback_data="go:menu")]
    ])

def kb_mines_grid(opened: Set[int], dead: Optional[Set[int]] = None) -> types.InlineKeyboardMarkup:
    # dead berilsa (bomba ko‘rsatish uchun), opened va dead ko‘rinadi.
    rows = []
    for r in range(MINES_SIZE):
        row = []
        for c in range(MINES_SIZE):
            idx = r * MINES_SIZE + c
            if dead is not None and idx in dead:
                text = "💣"
            elif idx in opened:
                text = "✅"
            else:
                text = "⬜️"
            row.append(types.InlineKeyboardButton(text=text, callback_data=f"mn:pick:{idx}"))
        rows.append(row)
    rows.append([
        types.InlineKeyboardButton(text="💵 Cashout", callback_data="mn:cashout"),
        types.InlineKeyboardButton(text="❌ Stop", callback_data="mn:stop"),
    ])
    rows.append([types.InlineKeyboardButton(text="🔙 Menyu", callback_data="go:menu")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)

def kb_admin_panel() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="📥 Depositlar (pending)", callback_data="adm:deps"),
         types.InlineKeyboardButton(text="📤 Withdrawlar (pending)", callback_data="adm:wds")],
        [types.InlineKeyboardButton(text="📊 House profit", callback_data="adm:profit")],
        [types.InlineKeyboardButton(text="🔙 Menyu", callback_data="go:menu")]
    ])

def kb_admin_dep_actions(rid: int) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="✅ Tasdiq", callback_data=f"adm:dep_ok:{rid}"),
         types.InlineKeyboardButton(text="❌ Rad", callback_data=f"adm:dep_no:{rid}")],
    ])

def kb_admin_wd_actions(rid: int) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="✅ Tasdiq", callback_data=f"adm:wd_ok:{rid}"),
         types.InlineKeyboardButton(text="❌ Rad", callback_data=f"adm:wd_no:{rid}")],
    ])

# =========================
# STATES (input kerak bo'lganda)
# =========================
# steps[uid] = {"mode": "...", ...}
steps: Dict[int, Dict[str, str]] = {}

# =========================
# MINES / AVIATOR sessions
# =========================
@dataclass
class MinesSession:
    bet: int
    wallet: str  # "real" or "bonus"
    bombs: Set[int]
    opened: Set[int]
    active: bool = True

mines_sessions: Dict[int, MinesSession] = {}

def mines_multiplier(opened: int) -> float:
    # sekin ko‘tariladi (1win feel)
    table = [1.00, 1.10, 1.20, 1.35, 1.55, 1.80, 2.10, 2.50, 3.00]
    if opened < len(table):
        return table[opened]
    return round(table[-1] + (opened - (len(table)-1)) * 0.8, 2)

def gen_bombs(exclude: int) -> Set[int]:
    cells = list(range(MINES_SIZE * MINES_SIZE))
    if exclude in cells:
        cells.remove(exclude)
    return set(random.sample(cells, MINES_BOMBS))

@dataclass
class AviatorSession:
    bet: int
    wallet: str
    mult: float = 1.0
    crashed: bool = False
    cashed_out: bool = False
    task_id: int = 0

aviator_sessions: Dict[int, AviatorSession] = {}
aviator_task_counter = 0

async def aviator_loop(bot: Bot, uid: int, my_task_id: int):
    while True:
        await asyncio.sleep(AVIATOR_TICK_SEC)
        s = aviator_sessions.get(uid)
        if not s or s.task_id != my_task_id or s.cashed_out or s.crashed:
            return

        s.mult = round(s.mult * (1.0 + AVIATOR_GROWTH), 2)

        # halol risk: koef oshgani sari crash ehtimoli oshadi
        crash_prob = min(0.02 + (s.mult - 1.0) * 0.015, 0.45)
        if random.random() < crash_prob:
            s.crashed = True
            aviator_sessions[uid] = s
            try:
                await bot.send_message(
                    uid,
                    f"💥 Crash! x{s.mult:.2f}\n😅 Keyingi safar omad keladi!",
                    reply_markup=kb_main(uid)
                )
            except:
                pass
            return

        aviator_sessions[uid] = s
        # vaqti-vaqti bilan update
        if int(s.mult * 100) % 50 == 0:
            try:
                await bot.send_message(uid, f"✈️ Koef: x{s.mult:.2f}", reply_markup=kb_aviator_play())
            except:
                pass

# =========================
# Helpers
# =========================
async def edit_or_send(q: types.CallbackQuery, text: str, kb: Optional[types.InlineKeyboardMarkup] = None):
    try:
        await q.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await q.message.answer(text, reply_markup=kb)

# =========================
# START
# =========================
@dp.message(CommandStart())
async def start(m: types.Message):
    uid = m.from_user.id
    parts = (m.text or "").split()
    ref_by = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else None
    await ensure_user(uid, ref_by=ref_by)
    await m.answer("Menyu 👇", reply_markup=kb_main(uid))

# =========================
# MAIN NAV
# =========================
@dp.callback_query(F.data == "go:menu")
async def go_menu(q: types.CallbackQuery):
    uid = q.from_user.id
    steps.pop(uid, None)
    mines_sessions.pop(uid, None)
    aviator_sessions.pop(uid, None)
    await edit_or_send(q, "Menyu 👇", kb_main(uid))
    await q.answer()

@dp.callback_query(F.data == "go:help")
async def go_help(q: types.CallbackQuery):
    uid = q.from_user.id
    txt = (
        "ℹ️ Qoidalar:\n"
        f"• Deposit tasdiqlansa balansga {int(DEPOSIT_CREDIT_RATE*100)}% tushadi (qolgan service fee)\n"
        "• Promo/Kunlik/Referal — BONUS balans (chiqmaydi)\n"
        "• Withdraw: faqat deposit tasdiqlangan user va faqat REAL balansdan\n"
        "• O‘yinlar: halol random + kuchli risk\n"
    )
    await edit_or_send(q, txt, kb_menu())
    await q.answer()

@dp.callback_query(F.data == "go:balance")
async def go_balance(q: types.CallbackQuery):
    uid = q.from_user.id
    await ensure_user(uid)
    real, bonus, dep, _, refc, _ = await get_user(uid)
    await edit_or_send(
        q,
        f"💰 Balans\n✅ Real: {real}\n🎁 Bonus: {bonus}\n📌 Deposit: {'✅' if dep else '❌'}\n👥 Referal: {refc}",
        kb_menu()
    )
    await q.answer()

@dp.callback_query(F.data == "go:ref")
async def go_ref(q: types.CallbackQuery):
    uid = q.from_user.id
    await ensure_user(uid)
    me = await q.bot.get_me()
    _, _, _, _, refc, _ = await get_user(uid)
    link = f"https://t.me/{me.username}?start={uid}"
    await edit_or_send(q, f"🤝 Referal link:\n{link}\n👥 Taklif qilganlar: {refc}", kb_menu())
    await q.answer()

@dp.callback_query(F.data == "go:daily")
async def go_daily(q: types.CallbackQuery):
    uid = q.from_user.id
    await ensure_user(uid)
    _, _, _, _, _, last_daily = await get_user(uid)
    now = int(time.time())
    if now - int(last_daily) < 86400:
        left = 86400 - (now - int(last_daily))
        h = left // 3600
        mi = (left % 3600) // 60
        await edit_or_send(q, f"⏳ Kunlik bonus hali tayyor emas.\nQolgan: {h} soat {mi} daqiqa", kb_menu())
        await q.answer()
        return
    await add_bonus(uid, DAILY_BONUS_AMOUNT)
    await set_last_daily(uid, now)
    await edit_or_send(q, f"✅ Kunlik bonus: +{DAILY_BONUS_AMOUNT} (BONUS balans)", kb_menu())
    await q.answer()

# =========================
# PROMO
# =========================
@dp.callback_query(F.data == "go:promo")
async def go_promo(q: types.CallbackQuery):
    uid = q.from_user.id
    steps[uid] = {"mode": "promo_enter"}
    await edit_or_send(q, "🎁 Promo kodni yozib yuboring (masalan: BONUS10)", kb_menu())
    await q.answer()

@dp.message(Command("mkpromo"))
async def mkpromo(m: types.Message):
    if m.from_user.id not in ADMIN_IDS:
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
    await m.answer(f"✅ Promo yaratildi: {code} | +{amount} (BONUS) | max={maxuses}")

# =========================
# DEPOSIT
# =========================
@dp.callback_query(F.data == "go:deposit")
async def go_deposit(q: types.CallbackQuery):
    uid = q.from_user.id
    steps[uid] = {"mode": "dep_amount"}
    txt = (
        f"➕ Hisob to‘ldirish\n"
        f"Min: {MIN_DEP} / Max: {MAX_DEP}\n"
        f"⚠️ Deposit tasdiqlansa balansga {int(DEPOSIT_CREDIT_RATE*100)}% tushadi.\n"
        f"Summani tanlang:"
    )
    await edit_or_send(q, txt, kb_deposit_amount())
    await q.answer()

@dp.callback_query(F.data.startswith("dep:amt:"))
async def dep_amount_pick(q: types.CallbackQuery):
    uid = q.from_user.id
    await ensure_user(uid)
    parts = q.data.split(":")
    choice = parts[2]

    if choice == "custom":
        steps[uid] = {"mode": "dep_amount_custom"}
        await edit_or_send(q, f"✍️ Summani yozing (son bilan).\nMin {MIN_DEP} / Max {MAX_DEP}", kb_menu())
        await q.answer()
        return

    amount = int(choice)
    steps[uid] = {"mode": "dep_method", "amount": str(amount)}
    await edit_or_send(q, f"Summa: {amount}\nTo‘lov turini tanlang:", kb_deposit_method(amount))
    await q.answer()

@dp.callback_query(F.data.startswith("dep:m:"))
async def dep_method_pick(q: types.CallbackQuery):
    uid = q.from_user.id
    _, _, method, amount_s = q.data.split(":")
    amount = int(amount_s)

    if method == "HUMO":
        card = CARD_HUMO
    else:
        card = CARD_UZCARD

    txt = (
        f"💳 To‘lov\n"
        f"Metod: {method}\n"
        f"Karta: {card}\n"
        f"Summa: {amount}\n\n"
        f"Pul yuborgach ✅ To‘lov qildim bosing."
    )
    steps[uid] = {"mode": "dep_wait_paid", "amount": str(amount), "method": method}
    await edit_or_send(q, txt, kb_deposit_paid(method, amount))
    await q.answer()

@dp.callback_query(F.data.startswith("dep:paid:"))
async def dep_paid(q: types.CallbackQuery):
    uid = q.from_user.id
    _, _, method, amount_s = q.data.split(":")
    amount = int(amount_s)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO deposit_requests(user_id,amount,method,receipt_file_id,status,created_at) VALUES(?,?,?,?,?,?)",
            (uid, amount, method, None, "pending", int(time.time()))
        )
        await db.commit()
        cur = await db.execute("SELECT last_insert_rowid()")
        rid = (await cur.fetchone())[0]

    steps[uid] = {"mode": "dep_wait_receipt", "rid": str(rid), "amount": str(amount), "method": method}
    await edit_or_send(q, f"📸 Chek rasmini yuboring.\nDeposit ID: #{rid}", kb_menu())
    await q.answer()

# =========================
# WITHDRAW
# =========================
@dp.callback_query(F.data == "go:withdraw")
async def go_withdraw(q: types.CallbackQuery):
    uid = q.from_user.id
    await ensure_user(uid)
    real, _, dep, *_ = await get_user(uid)
    if not dep:
        await edit_or_send(q, "❌ Pul yechish uchun avval deposit qiling va admin tasdiqlasin.", kb_menu())
        await q.answer()
        return
    if real <= 0:
        await edit_or_send(q, "❌ REAL balans 0. BONUS balans chiqmaydi.", kb_menu())
        await q.answer()
        return
    steps[uid] = {"mode": "wd_amount"}
    await edit_or_send(q, "📤 Pul yechish\nSummani yozish uchun tugmani bosing:", kb_withdraw())
    await q.answer()

@dp.callback_query(F.data == "wd:amt")
async def wd_amount_begin(q: types.CallbackQuery):
    uid = q.from_user.id
    steps[uid] = {"mode": "wd_amount_input"}
    await edit_or_send(q, "✍️ Summani yozib yuboring (faqat REAL balansdan).", kb_menu())
    await q.answer()

# =========================
# GAMES
# =========================
@dp.callback_query(F.data == "go:aviator")
async def go_aviator(q: types.CallbackQuery):
    uid = q.from_user.id
    await ensure_user(uid)
    steps[uid] = {"mode": "av_bet"}
    await edit_or_send(q, "✈️ Aviator\nStavkani tanlang:", kb_aviator_bet())
    await q.answer()

@dp.callback_query(F.data.startswith("av:bet:"))
async def av_bet_pick(q: types.CallbackQuery):
    uid = q.from_user.id
    choice = q.data.split(":")[2]
    if choice == "custom":
        steps[uid] = {"mode": "av_bet_custom"}
        await edit_or_send(q, "✍️ Stavkani yozing (son bilan).", kb_menu())
        await q.answer()
        return
    bet = int(choice)
    await start_aviator(q, bet)

async def start_aviator(q: types.CallbackQuery, bet: int):
    global aviator_task_counter
    uid = q.from_user.id
    await ensure_user(uid)

    real, bonus, *_ = await get_user(uid)
    wallet = "real" if real >= bet else ("bonus" if bonus >= bet else "")
    if wallet == "":
        await edit_or_send(q, "❌ Balans yetarli emas (REAL yoki BONUS).", kb_menu())
        await q.answer()
        return

    if wallet == "real":
        if not await take_real(uid, bet):
            await edit_or_send(q, "❌ REAL balans yetarli emas.", kb_menu()); await q.answer(); return
    else:
        if not await take_bonus(uid, bet):
            await edit_or_send(q, "❌ BONUS balans yetarli emas.", kb_menu()); await q.answer(); return

    aviator_task_counter += 1
    task_id = aviator_task_counter
    aviator_sessions[uid] = AviatorSession(bet=bet, wallet=wallet, mult=1.0, crashed=False, cashed_out=False, task_id=task_id)

    await edit_or_send(q, f"✈️ Aviator boshlandi!\nBet: {bet} ({wallet.upper()})\nKoef oshyapti...", kb_aviator_play())
    asyncio.create_task(aviator_loop(q.bot, uid, task_id))
    await q.answer()

@dp.callback_query(F.data == "av:cashout")
async def av_cashout(q: types.CallbackQuery):
    uid = q.from_user.id
    s = aviator_sessions.get(uid)
    if not s or s.cashed_out or s.crashed:
        await q.answer("O‘yin yo‘q.", show_alert=False)
        return
    s.cashed_out = True
    aviator_sessions[uid] = s
    win = int(round(s.bet * s.mult))

    if s.wallet == "real":
        await add_real(uid, win)
    else:
        await add_bonus(uid, win)

    await edit_or_send(q, f"✅ Cashout!\nKoef: x{s.mult:.2f}\nYutuq: {win} ({s.wallet.upper()})", kb_main(uid))
    await q.answer()

@dp.callback_query(F.data == "av:stop")
async def av_stop(q: types.CallbackQuery):
    uid = q.from_user.id
    aviator_sessions.pop(uid, None)
    await edit_or_send(q, "❌ Aviator to‘xtatildi.", kb_main(uid))
    await q.answer()

@dp.callback_query(F.data == "go:mines")
async def go_mines(q: types.CallbackQuery):
    uid = q.from_user.id
    await ensure_user(uid)
    steps[uid] = {"mode": "mn_bet"}
    await edit_or_send(q, "💣 Mines (5x5, 3 bomba)\nStavkani tanlang:", kb_mines_bet())
    await q.answer()

@dp.callback_query(F.data.startswith("mn:bet:"))
async def mn_bet_pick(q: types.CallbackQuery):
    uid = q.from_user.id
    choice = q.data.split(":")[2]
    if choice == "custom":
        steps[uid] = {"mode": "mn_bet_custom"}
        await edit_or_send(q, "✍️ Stavkani yozing (son bilan).", kb_menu())
        await q.answer()
        return
    bet = int(choice)
    await start_mines(q, bet)

async def start_mines(q: types.CallbackQuery, bet: int):
    uid = q.from_user.id
    await ensure_user(uid)

    real, bonus, *_ = await get_user(uid)
    wallet = "real" if real >= bet else ("bonus" if bonus >= bet else "")
    if wallet == "":
        await edit_or_send(q, "❌ Balans yetarli emas (REAL yoki BONUS).", kb_menu())
        await q.answer()
        return

    if wallet == "real":
        if not await take_real(uid, bet):
            await edit_or_send(q, "❌ REAL balans yetarli emas.", kb_menu()); await q.answer(); return
    else:
        if not await take_bonus(uid, bet):
            await edit_or_send(q, "❌ BONUS balans yetarli emas.", kb_menu()); await q.answer(); return

    mines_sessions[uid] = MinesSession(bet=bet, wallet=wallet, bombs=set(), opened=set(), active=True)
    await edit_or_send(q, f"💣 Mines boshlandi!\nBet: {bet} ({wallet.upper()})\nKatak tanlang:", kb_mines_grid(set()))
    await q.answer()

@dp.callback_query(F.data.startswith("mn:pick:"))
async def mn_pick(q: types.CallbackQuery):
    uid = q.from_user.id
    s = mines_sessions.get(uid)
    if not s or not s.active:
        await q.answer("O‘yin yo‘q.", show_alert=False)
        return

    idx = int(q.data.split(":")[2])
    if idx in s.opened:
        await q.answer("Ochilgan.", show_alert=False)
        return

    # bombs birinchi bosishda generatsiya (bosilgan katak bomba bo‘lmaydi)
    if not s.bombs:
        s.bombs = gen_bombs(exclude=idx)

    # bomb
    if idx in s.bombs:
        s.active = False
        mines_sessions[uid] = s
        # bombalarni ko‘rsatamiz
        await edit_or_send(q, f"💥 Bomb!\nYutqazding.\n😅 Keyingi safar omad keladi!", kb_mines_grid(s.opened, dead=s.bombs))
        await q.answer()
        return

    s.opened.add(idx)
    mines_sessions[uid] = s
    opened = len(s.opened)
    mult = mines_multiplier(opened)
    await edit_or_send(q, f"💣 Mines\nOchilgan: {opened}\nKoef: x{mult:.2f}\n💵 Cashout istagan payt.", kb_mines_grid(s.opened))
    await q.answer()

@dp.callback_query(F.data == "mn:cashout")
async def mn_cashout(q: types.CallbackQuery):
    uid = q.from_user.id
    s = mines_sessions.get(uid)
    if not s or not s.active:
        await q.answer("O‘yin yo‘q.", show_alert=False)
        return
    opened = len(s.opened)
    if opened == 0:
        await q.answer("Avval katak oching.", show_alert=True)
        return

    s.active = False
    mines_sessions[uid] = s
    mult = mines_multiplier(opened)
    win = int(round(s.bet * mult))

    if s.wallet == "real":
        await add_real(uid, win)
    else:
        await add_bonus(uid, win)

    await edit_or_send(q, f"✅ Cashout!\nKoef: x{mult:.2f}\nYutuq: {win} ({s.wallet.upper()})", kb_main(uid))
    await q.answer()

@dp.callback_query(F.data == "mn:stop")
async def mn_stop(q: types.CallbackQuery):
    uid = q.from_user.id
    mines_sessions.pop(uid, None)
    await edit_or_send(q, "❌ Mines to‘xtatildi.", kb_main(uid))
    await q.answer()

# =========================
# ADMIN PANEL
# =========================
@dp.callback_query(F.data == "adm:panel")
async def adm_panel(q: types.CallbackQuery):
    if not is_admin(q.from_user.id):
        await q.answer("Ruxsat yo‘q.", show_alert=True)
        return
    await edit_or_send(q, "🛠 Admin panel", kb_admin_panel())
    await q.answer()

@dp.callback_query(F.data == "adm:profit")
async def adm_profit(q: types.CallbackQuery):
    if not is_admin(q.from_user.id):
        await q.answer("Ruxsat yo‘q.", show_alert=True); return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT profit FROM house_profit WHERE id=1")
        profit = (await cur.fetchone())[0]
    await edit_or_send(q, f"📊 House profit: {profit}", kb_admin_panel())
    await q.answer()

@dp.callback_query(F.data == "adm:deps")
async def adm_deps(q: types.CallbackQuery):
    if not is_admin(q.from_user.id):
        await q.answer("Ruxsat yo‘q.", show_alert=True); return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id,user_id,amount,method,created_at FROM deposit_requests WHERE status='pending' ORDER BY id DESC LIMIT 10"
        )
        rows = await cur.fetchall()
    if not rows:
        await edit_or_send(q, "📥 Pending deposit yo‘q.", kb_admin_panel()); await q.answer(); return
    txt = "📥 Pending deposit (oxirgi 10):\n"
    for r in rows:
        txt += f"#{r[0]} | uid={r[1]} | {r[2]} | {r[3]}\n"
    await edit_or_send(q, txt + "\nID tasdiqlash/rad qilish cheklar orqali keladi.", kb_admin_panel())
    await q.answer()

@dp.callback_query(F.data == "adm:wds")
async def adm_wds(q: types.CallbackQuery):
    if not is_admin(q.from_user.id):
        await q.answer("Ruxsat yo‘q.", show_alert=True); return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id,user_id,amount,card,created_at FROM withdraw_requests WHERE status='pending' ORDER BY id DESC LIMIT 10"
        )
        rows = await cur.fetchall()
    if not rows:
        await edit_or_send(q, "📤 Pending withdraw yo‘q.", kb_admin_panel()); await q.answer(); return
    txt = "📤 Pending withdraw (oxirgi 10):\n"
    for r in rows:
        txt += f"#{r[0]} | uid={r[1]} | {r[2]} | {r[3]}\n"
    await edit_or_send(q, txt + "\nID tasdiqlash/rad qilish admin xabarlarida.", kb_admin_panel())
    await q.answer()

@dp.callback_query(F.data.startswith("adm:dep_ok:"))
async def adm_dep_ok(q: types.CallbackQuery):
    if not is_admin(q.from_user.id):
        await q.answer("Ruxsat yo‘q.", show_alert=True); return
    rid = int(q.data.split(":")[2])

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id,amount,status FROM deposit_requests WHERE id=?", (rid,))
        row = await cur.fetchone()
        if not row or row[2] != "pending":
            await q.answer("Topilmadi/pending emas.", show_alert=True); return
        uid, amount = int(row[0]), int(row[1])

        credited = int(round(amount * DEPOSIT_CREDIT_RATE))
        fee = amount - credited

        await db.execute(
            "UPDATE deposit_requests SET status='approved', handled_by=?, handled_at=? WHERE id=?",
            (q.from_user.id, int(time.time()), rid)
        )
        await db.execute(
            "UPDATE users SET real_balance=real_balance+?, deposit_verified=1 WHERE user_id=?",
            (credited, uid)
        )
        await db.execute("UPDATE house_profit SET profit = profit + ? WHERE id=1", (fee,))
        await db.commit()

    try:
        await q.bot.send_message(uid, f"✅ Deposit tasdiqlandi!\nKiritildi: {amount}\nBalansga: {credited}\nService fee: {fee}", reply_markup=kb_main(uid))
    except:
        pass

    await q.answer("Tasdiqlandi.")
    try:
        await q.message.edit_reply_markup(reply_markup=None)
    except:
        pass

@dp.callback_query(F.data.startswith("adm:dep_no:"))
async def adm_dep_no(q: types.CallbackQuery):
    if not is_admin(q.from_user.id):
        await q.answer("Ruxsat yo‘q.", show_alert=True); return
    rid = int(q.data.split(":")[2])

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id,amount,status FROM deposit_requests WHERE id=?", (rid,))
        row = await cur.fetchone()
        if not row or row[2] != "pending":
            await q.answer("Topilmadi/pending emas.", show_alert=True); return
        uid, amount = int(row[0]), int(row[1])

        await db.execute(
            "UPDATE deposit_requests SET status='rejected', handled_by=?, handled_at=? WHERE id=?",
            (q.from_user.id, int(time.time()), rid)
        )
        await db.commit()

    try:
        await q.bot.send_message(uid, f"❌ Deposit rad etildi.\nSumma: {amount}", reply_markup=kb_main(uid))
    except:
        pass

    await q.answer("Rad etildi.")
    try:
        await q.message.edit_reply_markup(reply_markup=None)
    except:
        pass

@dp.callback_query(F.data.startswith("adm:wd_ok:"))
async def adm_wd_ok(q: types.CallbackQuery):
    if not is_admin(q.from_user.id):
        await q.answer("Ruxsat yo‘q.", show_alert=True); return
    rid = int(q.data.split(":")[2])

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id,amount,card,status FROM withdraw_requests WHERE id=?", (rid,))
        row = await cur.fetchone()
        if not row or row[3] != "pending":
            await q.answer("Topilmadi/pending emas.", show_alert=True); return
        uid, amount, card = int(row[0]), int(row[1]), row[2]

        await db.execute(
            "UPDATE withdraw_requests SET status='approved', handled_by=?, handled_at=? WHERE id=?",
            (q.from_user.id, int(time.time()), rid)
        )
        await db.commit()

    try:
        await q.bot.send_message(uid, f"✅ Withdraw tasdiqlandi!\nSumma: {amount}\nKarta: {card}", reply_markup=kb_main(uid))
    except:
        pass

    await q.answer("Tasdiqlandi.")
    try:
        await q.message.edit_reply_markup(reply_markup=None)
    except:
        pass

@dp.callback_query(F.data.startswith("adm:wd_no:"))
async def adm_wd_no(q: types.CallbackQuery):
    if not is_admin(q.from_user.id):
        await q.answer("Ruxsat yo‘q.", show_alert=True); return
    rid = int(q.data.split(":")[2])

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id,amount,status FROM withdraw_requests WHERE id=?", (rid,))
        row = await cur.fetchone()
        if not row or row[2] != "pending":
            await q.answer("Topilmadi/pending emas.", show_alert=True); return
        uid, amount = int(row[0]), int(row[1])

        # reject -> pulni qaytarish
        await db.execute(
            "UPDATE withdraw_requests SET status='rejected', handled_by=?, handled_at=? WHERE id=?",
            (q.from_user.id, int(time.time()), rid)
        )
        await db.execute("UPDATE users SET real_balance = real_balance + ? WHERE user_id=?", (amount, uid))
        await db.commit()

    try:
        await q.bot.send_message(uid, f"❌ Withdraw rad etildi.\nPul qaytarildi: {amount}", reply_markup=kb_main(uid))
    except:
        pass

    await q.answer("Rad etildi.")
    try:
        await q.message.edit_reply_markup(reply_markup=None)
    except:
        pass

# =========================
# PHOTO: deposit receipt
# =========================
@dp.message(F.photo)
async def on_photo(m: types.Message):
    uid = m.from_user.id
    await ensure_user(uid)

    st = steps.get(uid)
    if not st or st.get("mode") != "dep_wait_receipt":
        return

    file_id = m.photo[-1].file_id
    rid = int(st["rid"])
    amount = int(st["amount"])
    method = st["method"]

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE deposit_requests SET receipt_file_id=? WHERE id=?", (file_id, rid))
        await db.commit()

    steps.pop(uid, None)
    await m.answer(f"✅ Chek qabul qilindi. Admin tekshiradi. (ID #{rid})", reply_markup=kb_main(uid))

    # Adminlarga yuboramiz (approve/reject inline)
    for a in ADMIN_IDS:
        try:
            await m.bot.send_photo(
                a,
                photo=file_id,
                caption=f"📥 Deposit chek\nID: #{rid}\nUser: {uid}\nAmount: {amount}\nMethod: {method}\n\nTasdiq/Rad tugmasi👇",
                reply_markup=kb_admin_dep_actions(rid)
            )
        except:
            pass

# =========================
# TEXT INPUT FLOWS
# =========================
@dp.message(F.text)
async def on_text(m: types.Message):
    uid = m.from_user.id
    await ensure_user(uid)
    txt = (m.text or "").strip()
    st = steps.get(uid)

    # deposit custom amount
    if st and st.get("mode") == "dep_amount_custom":
        if not txt.isdigit():
            return await m.answer("Summani SON bilan yozing (masalan 50000).", reply_markup=kb_main(uid))
        amount = int(txt)
        if amount < MIN_DEP or amount > MAX_DEP:
            return await m.answer(f"Min {MIN_DEP} / Max {MAX_DEP}", reply_markup=kb_main(uid))

        steps[uid] = {"mode": "dep_method", "amount": str(amount)}
        return await m.answer(f"Summa: {amount}\nEndi /menu bosib qayta kirmang, tugmadan tanlang 👇",
                              reply_markup=kb_deposit_method(amount))

    # promo enter
    if st and st.get("mode") == "promo_enter":
        code = txt.upper()
        now = int(time.time())
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT 1 FROM promo_uses WHERE user_id=? AND code=?", (uid, code))
            if await cur.fetchone():
                steps.pop(uid, None)
                return await m.answer("❌ Bu promo sizda ishlatilgan.", reply_markup=kb_main(uid))

            cur = await db.execute("SELECT amount,max_uses,used_count FROM promo_codes WHERE code=?", (code,))
            row = await cur.fetchone()
            if not row:
                steps.pop(uid, None)
                return await m.answer("❌ Promo topilmadi.", reply_markup=kb_main(uid))

            amount, max_uses, used_count = int(row[0]), int(row[1]), int(row[2])
            if used_count >= max_uses:
                steps.pop(uid, None)
                return await m.answer("❌ Promo limiti tugagan.", reply_markup=kb_main(uid))

            await db.execute("INSERT INTO promo_uses(user_id,code,used_at) VALUES(?,?,?)", (uid, code, now))
            await db.execute("UPDATE promo_codes SET used_count=used_count+1 WHERE code=?", (code,))
            await db.execute("UPDATE users SET bonus_balance=bonus_balance+? WHERE user_id=?", (amount, uid))
            await db.commit()

        steps.pop(uid, None)
        return await m.answer(f"✅ Promo qabul qilindi: +{amount} (BONUS balans)", reply_markup=kb_main(uid))

    # withdraw amount input
    if st and st.get("mode") == "wd_amount_input":
        if not txt.isdigit():
            return await m.answer("Summani SON bilan yozing.", reply_markup=kb_main(uid))
        amt = int(txt)
        real, _, dep, *_ = await get_user(uid)
        if not dep:
            steps.pop(uid, None)
            return await m.answer("❌ Deposit bo‘lmasa withdraw bo‘lmaydi.", reply_markup=kb_main(uid))
        if amt <= 0 or amt > real:
            return await m.answer("❌ Noto‘g‘ri summa yoki REAL balans yetarli emas.", reply_markup=kb_main(uid))

        steps[uid] = {"mode": "wd_card_input", "amount": str(amt)}
        return await m.answer("💳 Kartangizni yozing (masalan HUMO 9860.... yoki UZCARD 8600....).", reply_markup=kb_main(uid))

    # withdraw card input -> create request and notify admin
    if st and st.get("mode") == "wd_card_input":
        amount = int(st["amount"])
        card = txt[:120]

        ok = await take_real(uid, amount)
        if not ok:
            steps.pop(uid, None)
            return await m.answer("❌ REAL balans yetarli emas.", reply_markup=kb_main(uid))

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO withdraw_requests(user_id,amount,card,status,created_at) VALUES(?,?,?,?,?)",
                (uid, amount, card, "pending", int(time.time()))
            )
            await db.commit()
            cur = await db.execute("SELECT last_insert_rowid()")
            rid = (await cur.fetchone())[0]

        steps.pop(uid, None)
        await m.answer(f"✅ Withdraw so‘rovi yuborildi. (ID #{rid})", reply_markup=kb_main(uid))

        for a in ADMIN_IDS:
            try:
                await m.bot.send_message(
                    a,
                    f"📤 Withdraw so‘rovi\nID: #{rid}\nUser: {uid}\nAmount: {amount}\nCard: {card}\nTasdiq/Rad👇",
                    reply_markup=kb_admin_wd_actions(rid)
                )
            except:
                pass
        return

    # aviator custom bet
    if st and st.get("mode") == "av_bet_custom":
        if not txt.isdigit():
            return await m.answer("Stavkani SON bilan yozing.", reply_markup=kb_main(uid))
        bet = int(txt)
        fake_q = types.CallbackQuery(id="0", from_user=m.from_user, chat_instance="0", message=m)  # only to reuse start_aviator
        # We'll call directly via a small wrapper:
        # Deduct + start
        await ensure_user(uid)
        real, bonus, *_ = await get_user(uid)
        wallet = "real" if real >= bet else ("bonus" if bonus >= bet else "")
        if wallet == "":
            steps.pop(uid, None)
            return await m.answer("❌ Balans yetarli emas (REAL yoki BONUS).", reply_markup=kb_main(uid))

        if wallet == "real":
            if not await take_real(uid, bet):
                steps.pop(uid, None)
                return await m.answer("❌ REAL balans yetarli emas.", reply_markup=kb_main(uid))
        else:
            if not await take_bonus(uid, bet):
                steps.pop(uid, None)
                return await m.answer("❌ BONUS balans yetarli emas.", reply_markup=kb_main(uid))

        global aviator_task_counter
        aviator_task_counter += 1
        task_id = aviator_task_counter
        aviator_sessions[uid] = AviatorSession(bet=bet, wallet=wallet, mult=1.0, crashed=False, cashed_out=False, task_id=task_id)
        steps.pop(uid, None)
        await m.answer(f"✈️ Aviator boshlandi!\nBet: {bet} ({wallet.upper()})", reply_markup=kb_aviator_play())
        asyncio.create_task(aviator_loop(m.bot, uid, task_id))
        return

    # mines custom bet
    if st and st.get("mode") == "mn_bet_custom":
        if not txt.isdigit():
            return await m.answer("Stavkani SON bilan yozing.", reply_markup=kb_main(uid))
        bet = int(txt)
        # Start mines
        await ensure_user(uid)
        real, bonus, *_ = await get_user(uid)
        wallet = "real" if real >= bet else ("bonus" if bonus >= bet else "")
        if wallet == "":
            steps.pop(uid, None)
            return await m.answer("❌ Balans yetarli emas (REAL yoki BONUS).", reply_markup=kb_main(uid))

        if wallet == "real":
            if not await take_real(uid, bet):
                steps.pop(uid, None)
                return await m.answer("❌ REAL balans yetarli emas.", reply_markup=kb_main(uid))
        else:
            if not await take_bonus(uid, bet):
                steps.pop(uid, None)
                return await m.answer("❌ BONUS balans yetarli emas.", reply_markup=kb_main(uid))

        mines_sessions[uid] = MinesSession(bet=bet, wallet=wallet, bombs=set(), opened=set(), active=True)
        steps.pop(uid, None)
        return await m.answer(f"💣 Mines boshlandi!\nBet: {bet} ({wallet.upper()})", reply_markup=kb_mines_grid(set()))

# =========================
# RUN
# =========================
async def main():
    await db_init()
    bot = Bot(TOKEN)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
