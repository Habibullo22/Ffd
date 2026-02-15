import asyncio
import random
import time
from dataclasses import dataclass
from typing import Dict, Optional, Set, Tuple

import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

# =========================
# CONFIG
# =========================
TOKEN = "8161107014:AAGBWEYVxie7-pB4-2FoGCPjCv_sl0yHogc"
ADMIN_IDS = {5815294733}
DB_PATH = "casino_demo.db"

# DEMO topup/withdraw
MIN_TOPUP = 20000
MAX_TOPUP = 2000000
TOPUP_CREDIT_RATE = 0.90   # 50k -> 45k real
DAILY_BONUS_AMOUNT = 3000  # bonus balance (o'yinda ishlaydi)

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

CREATE TABLE IF NOT EXISTS topup_requests(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  amount INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at INTEGER NOT NULL,
  handled_by INTEGER,
  handled_at INTEGER
);

CREATE TABLE IF NOT EXISTS withdraw_requests(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  amount INTEGER NOT NULL,
  note TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at INTEGER NOT NULL,
  handled_by INTEGER,
  handled_at INTEGER
);

CREATE TABLE IF NOT EXISTS house_profit(
  id INTEGER PRIMARY KEY CHECK(id=1),
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
                "INSERT INTO users(user_id, real_balance, bonus_balance, topup_verified, ref_by, ref_count, last_daily_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (uid, 0, 0, 0, ref, 0, 0)
            )
            if ref:
                await db.execute("UPDATE users SET ref_count = ref_count + 1 WHERE user_id=?", (ref,))
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

async def set_topup_verified(uid: int, v: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET topup_verified=? WHERE user_id=?", (v, uid))
        await db.commit()

async def add_house_profit(amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE house_profit SET profit=profit+? WHERE id=1", (amount,))
        await db.commit()

# =========================
# UI: REPLY MENU (pastda)
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
    kb.button(text="📊 Profit")
    kb.button(text="🔙 Menyu")
    kb.adjust(2, 1, 1)
    return kb.as_markup(resize_keyboard=True)

# =========================
# MINES (inline grid)
# =========================
@dataclass
class MinesSession:
    bet: int
    wallet: str          # "real" or "bonus"
    bombs: Set[int]
    opened: Set[int]
    active: bool = True

mines_sessions: Dict[int, MinesSession] = {}

def mines_multiplier(opened: int) -> float:
    table = [1.00, 1.06, 1.12, 1.20, 1.30, 1.45, 1.60, 1.80, 2.05, 2.35, 2.70]
    if opened < len(table):
        return table[opened]
    return round(table[-1] + (opened - (len(table) - 1)) * 0.35, 2)

def gen_bombs(exclude: int) -> Set[int]:
    cells = list(range(MINES_SIZE * MINES_SIZE))
    if exclude in cells:
        cells.remove(exclude)
    return set(random.sample(cells, MINES_BOMBS))

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
# AVIATOR (message edit anim)
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

        crash_prob = min(0.02 + (s.mult - 1.0) * 0.015, 0.45)
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
# STATES (text input)
# =========================
steps: Dict[int, Dict[str, str]] = {}  # uid -> {"mode": "...", ...}

def parse_int_like(s: str) -> Optional[int]:
    s = s.replace(" ", "").strip()
    return int(s) if s.isdigit() else None

# =========================
# START / MENU
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
    uid = m.from_user.id
    steps.pop(uid, None)
    await m.answer("Menyu 👇", reply_markup=menu_kb(uid))

@dp.message(F.text == "❌ Bekor qilish")
async def cancel(m: types.Message):
    uid = m.from_user.id
    steps.pop(uid, None)
    await m.answer("Bekor qilindi.", reply_markup=menu_kb(uid))

# =========================
# BALANCE / HELP / REF / DAILY
# =========================
@dp.message(F.text == "💰 Balans")
async def balance(m: types.Message):
    uid = m.from_user.id
    await ensure_user(uid)
    real, bonus, top_ok, _, refc, _ = await get_user(uid)
    await m.answer(
        f"💰 Balans\n✅ Real: {real}\n🎁 Bonus: {bonus}\n📌 Topup: {'✅' if top_ok else '❌'}\n👥 Referal: {refc}",
        reply_markup=menu_kb(uid)
    )

@dp.message(F.text == "ℹ️ Yordam")
async def help_(m: types.Message):
    await m.answer(
        "ℹ️ Yordam\n"
        "• Menyu pastda (tugmalar).\n"
        "• Mines: 5x5, 3 bomba.\n"
        "• Aviator: samalyot + koef (xabar edit).\n"
        "• Promo/Kunlik/Referal → BONUS.\n"
        "• ✅ BONUS bilan o‘ynab yutsa — yutuq REALga tushadi.\n",
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
    await m.answer("🎁 Promo kodni yozing (masalan: BONUS10)", reply_markup=cancel_kb(m.from_user.id))

# =========================
# TOPUP (DEMO)
# =========================
@dp.message(F.text == "➕ Hisob to‘ldirish")
async def topup_start(m: types.Message):
    uid = m.from_user.id
    steps[uid] = {"mode": "topup_choose"}
    await m.answer(
        f"➕ Hisob to‘ldirish (DEMO)\nMin {MIN_TOPUP} / Max {MAX_TOPUP}\n"
        f"⚠️ Tasdiqlansa balansga {int(TOPUP_CREDIT_RATE*100)}% tushadi.\n"
        "Summani tanlang:",
        reply_markup=topup_amount_kb(uid)
    )

# =========================
# WITHDRAW (DEMO)
# =========================
@dp.message(F.text == "📤 Pul yechish")
async def withdraw_start(m: types.Message):
    uid = m.from_user.id
    real, _, top_ok, *_ = await get_user(uid)
    if not top_ok:
        return await m.answer("❌ Pul yechish uchun avval topup tasdiqlangan bo‘lishi kerak.", reply_markup=menu_kb(uid))
    if real <= 0:
        return await m.answer("❌ REAL balans 0. BONUS chiqmaydi.", reply_markup=menu_kb(uid))
    steps[uid] = {"mode": "wd_amount"}
    await m.answer("📤 Pul yechish (DEMO)\nSummani yozing (son):", reply_markup=cancel_kb(uid))

# =========================
# GAMES START
# =========================
@dp.message(F.text == "💣 Mines")
async def mines_start(m: types.Message):
    uid = m.from_user.id
    steps[uid] = {"mode": "mn_bet"}
    await m.answer("💣 Mines\nStavkani tanlang:", reply_markup=bet_kb(uid))

@dp.message(F.text == "✈️ Aviator")
async def aviator_start(m: types.Message):
    uid = m.from_user.id
    steps[uid] = {"mode": "av_bet"}
    await m.answer("✈️ Aviator\nStavkani tanlang:", reply_markup=bet_kb(uid))

# =========================
# MINES GAME HELPERS
# =========================
async def start_mines_game(m: types.Message, bet: int):
    uid = m.from_user.id
    real, bonus, *_ = await get_user(uid)

    wallet = "real" if real >= bet else ("bonus" if bonus >= bet else "")
    if wallet == "":
        return await m.answer("❌ Balans yetarli emas (REAL yoki BONUS).", reply_markup=menu_kb(uid))

    ok = await (take_real(uid, bet) if wallet == "real" else take_bonus(uid, bet))
    if not ok:
        return await m.answer("❌ Balans yetarli emas.", reply_markup=menu_kb(uid))

    mines_sessions[uid] = MinesSession(bet=bet, wallet=wallet, bombs=set(), opened=set(), active=True)
    await m.answer(
        f"💣 Mines boshlandi!\nBet: {bet} ({wallet.upper()})\nKatak tanlang:",
        reply_markup=menu_kb(uid)
    )
    await m.answer("⬇️ O‘yin paneli:", reply_markup=kb_mines_grid(set()))

# =========================
# AVIATOR GAME HELPERS
# =========================
async def start_aviator_game(m: types.Message, bet: int):
    uid = m.from_user.id
    real, bonus, *_ = await get_user(uid)

    wallet = "real" if real >= bet else ("bonus" if bonus >= bet else "")
    if wallet == "":
        return await m.answer("❌ Balans yetarli emas (REAL yoki BONUS).", reply_markup=menu_kb(uid))

    ok = await (take_real(uid, bet) if wallet == "real" else take_bonus(uid, bet))
    if not ok:
        return await m.answer("❌ Balans yetarli emas.", reply_markup=menu_kb(uid))

    sent = await m.answer(render_plane(1.00, 0, bet, wallet), reply_markup=kb_aviator_inline())
    aviator_sessions[uid] = AviatorSession(bet=bet, wallet=wallet, mult=1.0, msg_id=sent.message_id, frame=0)
    asyncio.create_task(aviator_loop(m.bot, uid))

    await m.answer("✈️ Aviator boshlandi! Cashout bosib olasiz.", reply_markup=menu_kb(uid))

# =========================
# INLINE CALLBACKS (MINES + AVIATOR)
# =========================
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
        try:
            await q.message.edit_text("💥 Bomb!\n😅 Keyingi safar omad!", reply_markup=kb_mines_grid(s.opened, bombs=s.bombs))
        except:
            pass
        return await q.answer()

    s.opened.add(idx)
    mines_sessions[uid] = s
    opened = len(s.opened)
    mult = mines_multiplier(opened)
    try:
        await q.message.edit_text(f"💣 Mines\nOchilgan: {opened}\nKoef: x{mult:.2f}\nCashout bosib olasiz.", reply_markup=kb_mines_grid(s.opened))
    except:
        pass
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

    # ✅ SEN SO'RAGAN NARSA:
    # BONUS bilan o'ynasa ham yutuq REALga tushadi
    await add_real(uid, win)

    try:
        await q.message.edit_text(f"✅ Cashout!\nKoef: x{mult:.2f}\nYutuq: {win} → REALga tushdi.", reply_markup=None)
    except:
        pass
    await q.answer()

@dp.callback_query(F.data == "mn:stop")
async def mn_stop(q: types.CallbackQuery):
    mines_sessions.pop(q.from_user.id, None)
    try:
        await q.message.edit_text("❌ Mines to‘xtatildi.", reply_markup=None)
    except:
        pass
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

    # ✅ BONUS bilan o'ynasa ham yutuq REALga tushadi
    await add_real(uid, win)

    try:
        await q.bot.edit_message_text(
            chat_id=uid,
            message_id=s.msg_id,
            text=render_plane(s.mult, s.frame, s.bet, s.wallet) + f"\n\n✅ CASHOUT!\nYutuq: {win} → REALga tushdi.",
            reply_markup=None
        )
    except:
        pass

    await q.answer("Cashout!")
    # menyu pastda turadi — qayta yubormaymiz

@dp.callback_query(F.data == "av:stop")
async def av_stop(q: types.CallbackQuery):
    aviator_sessions.pop(q.from_user.id, None)
    try:
        await q.message.edit_reply_markup(reply_markup=None)
    except:
        pass
    await q.answer("Stop")

# =========================
# ADMIN: approve/reject buttons
# =========================
def kb_admin_req(prefix: str, rid: int) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(text="✅ Tasdiq", callback_data=f"{prefix}:ok:{rid}"),
            types.InlineKeyboardButton(text="❌ Rad", callback_data=f"{prefix}:no:{rid}"),
        ]
    ])

@dp.callback_query(F.data.startswith("top:ok:"))
async def top_ok(q: types.CallbackQuery):
    if not is_admin(q.from_user.id):
        return await q.answer("No.", show_alert=True)
    rid = int(q.data.split(":")[2])

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id,amount,status FROM topup_requests WHERE id=?", (rid,))
        row = await cur.fetchone()
        if not row or row[2] != "pending":
            return await q.answer("Topilmadi/pending emas.", show_alert=True)
        uid, amount = int(row[0]), int(row[1])

        credited = int(round(amount * TOPUP_CREDIT_RATE))
        fee = amount - credited

        await db.execute("UPDATE topup_requests SET status='approved', handled_by=?, handled_at=? WHERE id=?",
                         (q.from_user.id, int(time.time()), rid))
        await db.execute("UPDATE users SET real_balance=real_balance+?, topup_verified=1 WHERE user_id=?",
                         (credited, uid))
        await db.execute("UPDATE house_profit SET profit=profit+? WHERE id=1", (fee,))
        await db.commit()

    try:
        await q.bot.send_message(uid, f"✅ Topup tasdiqlandi!\nSo‘rov: {amount}\nBalansga: {credited}\nFee: {fee}", reply_markup=menu_kb(uid))
    except:
        pass

    await q.answer("OK")
    try:
        await q.message.edit_reply_markup(reply_markup=None)
    except:
        pass

@dp.callback_query(F.data.startswith("top:no:"))
async def top_no(q: types.CallbackQuery):
    if not is_admin(q.from_user.id):
        return await q.answer("No.", show_alert=True)
    rid = int(q.data.split(":")[2])

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id,amount,status FROM topup_requests WHERE id=?", (rid,))
        row = await cur.fetchone()
        if not row or row[2] != "pending":
            return await q.answer("Topilmadi/pending emas.", show_alert=True)
        uid, amount = int(row[0]), int(row[1])

        await db.execute("UPDATE topup_requests SET status='rejected', handled_by=?, handled_at=? WHERE id=?",
                         (q.from_user.id, int(time.time()), rid))
        await db.commit()

    try:
        await q.bot.send_message(uid, f"❌ Topup rad.\nSo‘rov: {amount}", reply_markup=menu_kb(uid))
    except:
        pass

    await q.answer("Rejected")
    try:
        await q.message.edit_reply_markup(reply_markup=None)
    except:
        pass

@dp.callback_query(F.data.startswith("wd:ok:"))
async def wd_ok(q: types.CallbackQuery):
    if not is_admin(q.from_user.id):
        return await q.answer("No.", show_alert=True)
    rid = int(q.data.split(":")[2])

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id,amount,status FROM withdraw_requests WHERE id=?", (rid,))
        row = await cur.fetchone()
        if not row or row[2] != "pending":
            return await q.answer("Topilmadi/pending emas.", show_alert=True)
        uid, amount = int(row[0]), int(row[1])

        await db.execute("UPDATE withdraw_requests SET status='approved', handled_by=?, handled_at=? WHERE id=?",
                         (q.from_user.id, int(time.time()), rid))
        await db.commit()

    try:
        await q.bot.send_message(uid, f"✅ Withdraw tasdiqlandi!\nSumma: {amount}", reply_markup=menu_kb(uid))
    except:
        pass

    await q.answer("OK")
    try:
        await q.message.edit_reply_markup(reply_markup=None)
    except:
        pass

@dp.callback_query(F.data.startswith("wd:no:"))
async def wd_no(q: types.CallbackQuery):
    if not is_admin(q.from_user.id):
        return await q.answer("No.", show_alert=True)
    rid = int(q.data.split(":")[2])

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id,amount,status FROM withdraw_requests WHERE id=?", (rid,))
        row = await cur.fetchone()
        if not row or row[2] != "pending":
            return await q.answer("Topilmadi/pending emas.", show_alert=True)
        uid, amount = int(row[0]), int(row[1])

        await db.execute("UPDATE withdraw_requests SET status='rejected', handled_by=?, handled_at=? WHERE id=?",
                         (q.from_user.id, int(time.time()), rid))
        # reject -> pul qaytariladi
        await db.execute("UPDATE users SET real_balance=real_balance+? WHERE user_id=?", (amount, uid))
        await db.commit()

    try:
        await q.bot.send_message(uid, f"❌ Withdraw rad.\nPul qaytarildi: {amount}", reply_markup=menu_kb(uid))
    except:
        pass

    await q.answer("Rejected")
    try:
        await q.message.edit_reply_markup(reply_markup=None)
    except:
        pass

# =========================
# ADMIN PANEL (reply)
# =========================
@dp.message(F.text == "🛠 Admin panel")
async def admin_panel(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    await m.answer("🛠 Admin panel", reply_markup=admin_panel_kb(m.from_user.id))

@dp.message(F.text == "📊 Profit")
async def admin_profit(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT profit FROM house_profit WHERE id=1")
        profit = (await cur.fetchone())[0]
    await m.answer(f"📊 House profit: {profit}", reply_markup=admin_panel_kb(m.from_user.id))

@dp.message(F.text == "📥 Topup pending")
async def admin_top_list(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id,user_id,amount FROM topup_requests WHERE status='pending' ORDER BY id DESC LIMIT 10")
        rows = await cur.fetchall()
    if not rows:
        return await m.answer("📥 Pending topup yo‘q.", reply_markup=admin_panel_kb(m.from_user.id))

    for rid, uid, amount in rows:
        await m.answer(
            f"📥 TOPUP\nID #{rid}\nUser: {uid}\nAmount: {amount}\nApprove/Reject:",
            reply_markup=kb_admin_req("top", rid)
        )

@dp.message(F.text == "📤 Withdraw pending")
async def admin_wd_list(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id,user_id,amount,note FROM withdraw_requests WHERE status='pending' ORDER BY id DESC LIMIT 10")
        rows = await cur.fetchall()
    if not rows:
        return await m.answer("📤 Pending withdraw yo‘q.", reply_markup=admin_panel_kb(m.from_user.id))

    for rid, uid, amount, note in rows:
        await m.answer(
            f"📤 WITHDRAW\nID #{rid}\nUser: {uid}\nAmount: {amount}\nNote: {note}\nApprove/Reject:",
            reply_markup=kb_admin_req("wd", rid)
        )

# =========================
# TEXT INPUT ROUTER (promo/custom/topup/withdraw/bets)
# =========================
@dp.message(F.text)
async def text_router(m: types.Message):
    uid = m.from_user.id
    await ensure_user(uid)
    st = steps.get(uid)
    if not st:
        return

    txt = (m.text or "").strip()

    # Promo enter
    if st.get("mode") == "promo_enter":
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
        return await m.answer(f"✅ Promo qabul qilindi: +{amount} BONUS", reply_markup=menu_kb(uid))

    # Custom bet (mines/aviator)
    if st.get("mode") in ("mn_bet_custom", "av_bet_custom"):
        bet = parse_int_like(txt)
        if bet is None or bet <= 0:
            return await m.answer("❌ Stavkani son bilan yozing (masalan 5000).", reply_markup=cancel_kb(uid))
        mode = st["mode"]
        steps.pop(uid, None)
        if mode == "mn_bet_custom":
            return await start_mines_game(m, bet)
        else:
            return await start_aviator_game(m, bet)

    # Custom topup amount
    if st.get("mode") == "topup_custom":
        amount = parse_int_like(txt)
        if amount is None:
            return await m.answer("❌ Summani son bilan yozing.", reply_markup=cancel_kb(uid))
        if amount < MIN_TOPUP or amount > MAX_TOPUP:
            return await m.answer(f"❌ Min {MIN_TOPUP} / Max {MAX_TOPUP}", reply_markup=cancel_kb(uid))

        steps.pop(uid, None)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO topup_requests(user_id,amount,status,created_at) VALUES(?,?,?,?)",
                (uid, amount, "pending", int(time.time()))
            )
            await db.commit()
            cur = await db.execute("SELECT last_insert_rowid()")
            rid = (await cur.fetchone())[0]

        await m.answer(f"✅ Topup so‘rovi yuborildi (ID #{rid}). Admin tasdiqlaydi.", reply_markup=menu_kb(uid))
        for a in ADMIN_IDS:
            try:
                await m.bot.send_message(a, f"📥 TOPUP\nID #{rid}\nUser {uid}\nAmount {amount}", reply_markup=kb_admin_req("top", rid))
            except:
                pass
        return

    # Withdraw flow
    if st.get("mode") == "wd_amount":
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

        steps[uid] = {"mode": "wd_note", "amount": str(amount)}
        return await m.answer("📝 Qayerga yechilsin? (izoh yozing)", reply_markup=cancel_kb(uid))

    if st.get("mode") == "wd_note":
        amount = int(st["amount"])
        note = txt[:120]
        steps.pop(uid, None)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO withdraw_requests(user_id,amount,note,status,created_at) VALUES(?,?,?,?,?)",
                (uid, amount, note, "pending", int(time.time()))
            )
            await db.commit()
            cur = await db.execute("SELECT last_insert_rowid()")
            rid = (await cur.fetchone())[0]

        await m.answer(f"✅ Withdraw so‘rovi yuborildi (ID #{rid}). Admin ko‘rib chiqadi.", reply_markup=menu_kb(uid))
        for a in ADMIN_IDS:
            try:
                await m.bot.send_message(a, f"📤 WITHDRAW\nID #{rid}\nUser {uid}\nAmount {amount}\nNote: {note}",
                                         reply_markup=kb_admin_req("wd", rid))
            except:
                pass
        return

# =========================
# REPLY BUTTON NUMBERS (bets / topup)
# =========================
@dp.message(F.text.in_({"1 000","2 000","5 000","10 000","20 000","✍️ Boshqa stavka"}))
async def bet_buttons(m: types.Message):
    uid = m.from_user.id
    st = steps.get(uid)
    if not st or st.get("mode") not in ("mn_bet", "av_bet"):
        return

    if m.text == "✍️ Boshqa stavka":
        steps[uid] = {"mode": "mn_bet_custom"} if st["mode"] == "mn_bet" else {"mode": "av_bet_custom"}
        return await m.answer("✍️ Stavkani son bilan yozing:", reply_markup=cancel_kb(uid))

    bet = parse_int_like(m.text)
    if bet is None:
        return
    mode = st["mode"]
    steps.pop(uid, None)
    if mode == "mn_bet":
        return await start_mines_game(m, bet)
    else:
        return await start_aviator_game(m, bet)

@dp.message(F.text.in_({"20 000","50 000","100 000","200 000","500 000","✍️ Boshqa summa"}))
async def topup_buttons(m: types.Message):
    uid = m.from_user.id
    st = steps.get(uid)
    if not st or st.get("mode") != "topup_choose":
        return

    if m.text == "✍️ Boshqa summa":
        steps[uid] = {"mode": "topup_custom"}
        return await m.answer("✍️ Summani son bilan yozing:", reply_markup=cancel_kb(uid))

    amount = parse_int_like(m.text)
    if amount is None:
        return
    steps.pop(uid, None)

    if amount < MIN_TOPUP or amount > MAX_TOPUP:
        return await m.answer("❌ Noto‘g‘ri summa.", reply_markup=menu_kb(uid))

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO topup_requests(user_id,amount,status,created_at) VALUES(?,?,?,?)",
            (uid, amount, "pending", int(time.time()))
        )
        await db.commit()
        cur = await db.execute("SELECT last_insert_rowid()")
        rid = (await cur.fetchone())[0]

    await m.answer(f"✅ Topup so‘rovi yuborildi (ID #{rid}). Admin tasdiqlaydi.", reply_markup=menu_kb(uid))
    for a in ADMIN_IDS:
        try:
            await m.bot.send_message(a, f"📥 TOPUP\nID #{rid}\nUser {uid}\nAmount {amount}", reply_markup=kb_admin_req("top", rid))
        except:
            pass

# =========================
# RUN
# =========================
async def main():
    await db_init()
    bot = Bot(TOKEN)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
