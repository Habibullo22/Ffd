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
DB_PATH = "inline_casino_real.db"

# Virtual topup/withdraw rules (DEMO)
MIN_TOPUP = 20000
MAX_TOPUP = 2000000
TOPUP_CREDIT_RATE = 0.90  # 50k -> 45k (10% fee)
DAILY_BONUS_AMOUNT = 3000  # BONUS (withdraw qilinmaydi)

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

async def add_house_profit(amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE house_profit SET profit=profit+? WHERE id=1", (amount,))
        await db.commit()

# =========================
# INLINE UI (2-rasm style)
# =========================
def kb_main(uid: int) -> types.InlineKeyboardMarkup:
    rows = [
        [types.InlineKeyboardButton(text="💣 Mines", callback_data="go:mines"),
         types.InlineKeyboardButton(text="✈️ Aviator", callback_data="go:aviator")],
        [types.InlineKeyboardButton(text="➕ Hisob to‘ldirish", callback_data="go:topup"),
         types.InlineKeyboardButton(text="📤 Pul yechish", callback_data="go:withdraw")],
        [types.InlineKeyboardButton(text="💰 Balans", callback_data="go:balance"),
         types.InlineKeyboardButton(text="🎁 Promo code", callback_data="go:promo")],
        [types.InlineKeyboardButton(text="🎁 Kunlik bonus", callback_data="go:daily"),
         types.InlineKeyboardButton(text="🤝 Referal", callback_data="go:ref")],
        [types.InlineKeyboardButton(text="ℹ️ Yordam", callback_data="go:help")],
    ]
    if is_admin(uid):
        rows.append([types.InlineKeyboardButton(text="🛠 Admin panel", callback_data="adm:panel")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)

def kb_menu_only() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🔙 Menyu", callback_data="go:menu")]
    ])

def kb_topup_amount() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="20 000", callback_data="top:amt:20000"),
         types.InlineKeyboardButton(text="50 000", callback_data="top:amt:50000"),
         types.InlineKeyboardButton(text="100 000", callback_data="top:amt:100000")],
        [types.InlineKeyboardButton(text="200 000", callback_data="top:amt:200000"),
         types.InlineKeyboardButton(text="500 000", callback_data="top:amt:500000")],
        [types.InlineKeyboardButton(text="✍️ Boshqa summa", callback_data="top:amt:custom")],
        [types.InlineKeyboardButton(text="🔙 Menyu", callback_data="go:menu")]
    ])

def kb_bet_pick(prefix: str) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="1 000", callback_data=f"{prefix}:bet:1000"),
         types.InlineKeyboardButton(text="2 000", callback_data=f"{prefix}:bet:2000"),
         types.InlineKeyboardButton(text="5 000", callback_data=f"{prefix}:bet:5000")],
        [types.InlineKeyboardButton(text="10 000", callback_data=f"{prefix}:bet:10000"),
         types.InlineKeyboardButton(text="20 000", callback_data=f"{prefix}:bet:20000")],
        [types.InlineKeyboardButton(text="✍️ Boshqa stavka", callback_data=f"{prefix}:bet:custom")],
        [types.InlineKeyboardButton(text="🔙 Menyu", callback_data="go:menu")]
    ])

# =========================
# Admin inline
# =========================
def kb_admin_panel() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="📥 Topup pending", callback_data="adm:tops"),
         types.InlineKeyboardButton(text="📤 Withdraw pending", callback_data="adm:wds")],
        [types.InlineKeyboardButton(text="📊 House profit", callback_data="adm:profit")],
        [types.InlineKeyboardButton(text="🔙 Menyu", callback_data="go:menu")]
    ])

def kb_admin_actions(prefix: str, rid: int) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="✅ Tasdiq", callback_data=f"{prefix}_ok:{rid}"),
         types.InlineKeyboardButton(text="❌ Rad", callback_data=f"{prefix}_no:{rid}")]
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

def mines_multiplier(opened: int) -> float:
    # pastroq koef (sekin)
    table = [1.00, 1.06, 1.12, 1.20, 1.30, 1.45, 1.60, 1.80, 2.05, 2.35, 2.70]
    if opened < len(table):
        return table[opened]
    return round(table[-1] + (opened - (len(table)-1)) * 0.35, 2)

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
    rows.append([types.InlineKeyboardButton(text="🔙 Menyu", callback_data="go:menu")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)

# =========================
# AVIATOR (edit anim)
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

def kb_aviator_play() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="💸 Cashout", callback_data="av:cashout")],
        [types.InlineKeyboardButton(text="❌ Stop", callback_data="av:stop")],
        [types.InlineKeyboardButton(text="🔙 Menyu", callback_data="go:menu")]
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
                    reply_markup=kb_main(uid)
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
                reply_markup=kb_aviator_play()
            )
        except:
            pass

# =========================
# STATES (text input for custom / promo / withdraw note)
# =========================
steps: Dict[int, Dict[str, str]] = {}

# =========================
# SAFE EDIT (2-rasm style: bitta xabar)
# =========================
async def safe_edit(q: types.CallbackQuery, text: str, kb: Optional[types.InlineKeyboardMarkup] = None):
    try:
        await q.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        # agar edit bo'lmasa (masalan eski xabar), yangi jo'natib yuboramiz
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
# MENU NAV
# =========================
@dp.callback_query(F.data == "go:menu")
async def go_menu(q: types.CallbackQuery):
    uid = q.from_user.id
    steps.pop(uid, None)
    mines_sessions.pop(uid, None)
    aviator_sessions.pop(uid, None)
    await safe_edit(q, "Menyu 👇", kb_main(uid))
    await q.answer()

@dp.callback_query(F.data == "go:help")
async def go_help(q: types.CallbackQuery):
    txt = (
        "ℹ️ Yordam\n"
        "• Bu demo bot: balans = virtual coin.\n"
        f"• Topup tasdiqlansa balansga {int(TOPUP_CREDIT_RATE*100)}% tushadi.\n"
        "• Promo/Kunlik/Referal — BONUS (withdraw bo‘lmaydi).\n"
        "• Mines/Aviator — halol random.\n"
    )
    await safe_edit(q, txt, kb_menu_only())
    await q.answer()

@dp.callback_query(F.data == "go:balance")
async def go_balance(q: types.CallbackQuery):
    uid = q.from_user.id
    await ensure_user(uid)
    real, bonus, top_ok, _, refc, _ = await get_user(uid)
    await safe_edit(q, f"💰 Balans\n✅ Real: {real}\n🎁 Bonus: {bonus}\n📌 Topup: {'✅' if top_ok else '❌'}\n👥 Referal: {refc}", kb_menu_only())
    await q.answer()

@dp.callback_query(F.data == "go:ref")
async def go_ref(q: types.CallbackQuery):
    uid = q.from_user.id
    me = await q.bot.get_me()
    _, _, _, _, refc, _ = await get_user(uid)
    link = f"https://t.me/{me.username}?start={uid}"
    await safe_edit(q, f"🤝 Referal link:\n{link}\n👥 Taklif qilganlar: {refc}", kb_menu_only())
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
        m = (left % 3600) // 60
        await safe_edit(q, f"⏳ Kunlik bonus hali tayyor emas.\nQolgan: {h} soat {m} daqiqa", kb_menu_only())
        await q.answer()
        return
    await add_bonus(uid, DAILY_BONUS_AMOUNT)
    await set_last_daily(uid, now)
    await safe_edit(q, f"✅ Kunlik bonus: +{DAILY_BONUS_AMOUNT} (BONUS balans)", kb_menu_only())
    await q.answer()

# =========================
# PROMO (admin creates via /mkpromo)
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

@dp.callback_query(F.data == "go:promo")
async def go_promo(q: types.CallbackQuery):
    steps[q.from_user.id] = {"mode": "promo_enter"}
    await safe_edit(q, "🎁 Promo kodni yozib yuboring (masalan: BONUS10)", kb_menu_only())
    await q.answer()

# =========================
# TOPUP (DEMO request -> admin approve)
# =========================
@dp.callback_query(F.data == "go:topup")
async def go_topup(q: types.CallbackQuery):
    txt = (
        "➕ Hisob to‘ldirish (DEMO coin)\n"
        f"Min: {MIN_TOPUP} / Max: {MAX_TOPUP}\n"
        f"⚠️ Tasdiqlansa balansga {int(TOPUP_CREDIT_RATE*100)}% tushadi.\n"
        "Summani tanlang:"
    )
    await safe_edit(q, txt, kb_topup_amount())
    await q.answer()

@dp.callback_query(F.data.startswith("top:amt:"))
async def top_amt(q: types.CallbackQuery):
    uid = q.from_user.id
    choice = q.data.split(":")[2]
    if choice == "custom":
        steps[uid] = {"mode": "topup_custom"}
        await safe_edit(q, f"✍️ Summani yozing (son).\nMin {MIN_TOPUP} / Max {MAX_TOPUP}", kb_menu_only())
        await q.answer()
        return

    amount = int(choice)
    await create_topup_request(q, amount)

async def create_topup_request(q: types.CallbackQuery, amount: int):
    uid = q.from_user.id
    if amount < MIN_TOPUP or amount > MAX_TOPUP:
        await safe_edit(q, "❌ Noto‘g‘ri summa.", kb_menu_only())
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO topup_requests(user_id,amount,status,created_at) VALUES(?,?,?,?)",
            (uid, amount, "pending", int(time.time()))
        )
        await db.commit()
        cur = await db.execute("SELECT last_insert_rowid()")
        rid = (await cur.fetchone())[0]

    await safe_edit(q, f"✅ Topup so‘rovi yuborildi (ID #{rid}).\nAdmin tasdiqlaydi.", kb_main(uid))

    # Admin notify with approve/reject buttons
    for a in ADMIN_IDS:
        try:
            await q.bot.send_message(
                a,
                f"📥 TOPUP REQUEST\nID: #{rid}\nUser: {uid}\nAmount: {amount}\n\nApprove/Reject👇",
                reply_markup=kb_admin_actions("adm:top", rid)
            )
        except:
            pass

# =========================
# WITHDRAW (DEMO request -> admin approve)
# =========================
@dp.callback_query(F.data == "go:withdraw")
async def go_withdraw(q: types.CallbackQuery):
    uid = q.from_user.id
    real, _, top_ok, *_ = await get_user(uid)
    if not top_ok:
        await safe_edit(q, "❌ Pul yechish uchun avval topup tasdiqlangan bo‘lishi kerak.", kb_menu_only())
        await q.answer()
        return
    if real <= 0:
        await safe_edit(q, "❌ REAL balans 0. BONUS chiqmaydi.", kb_menu_only())
        await q.answer()
        return
    steps[uid] = {"mode": "wd_amount"}
    await safe_edit(q, "📤 Pul yechish (DEMO)\nSummani yozing (son bilan):", kb_menu_only())
    await q.answer()

# =========================
# GAMES
# =========================
@dp.callback_query(F.data == "go:mines")
async def go_mines(q: types.CallbackQuery):
    steps[q.from_user.id] = {"mode": "mn_bet"}
    await safe_edit(q, "💣 Mines (5x5, 3 bomba)\nStavkani tanlang:", kb_bet_pick("mn"))
    await q.answer()

@dp.callback_query(F.data == "go:aviator")
async def go_aviator(q: types.CallbackQuery):
    steps[q.from_user.id] = {"mode": "av_bet"}
    await safe_edit(q, "✈️ Aviator\nStavkani tanlang:", kb_bet_pick("av"))
    await q.answer()

@dp.callback_query(F.data.startswith("mn:bet:"))
async def mn_bet(q: types.CallbackQuery):
    uid = q.from_user.id
    choice = q.data.split(":")[2]
    if choice == "custom":
        steps[uid] = {"mode": "mn_bet_custom"}
        await safe_edit(q, "✍️ Mines stavka summasini yozing (son).", kb_menu_only())
        await q.answer()
        return
    bet = int(choice)
    await start_mines(q, bet)
    await q.answer()

async def start_mines(q: types.CallbackQuery, bet: int):
    uid = q.from_user.id
    real, bonus, *_ = await get_user(uid)

    wallet = "real" if real >= bet else ("bonus" if bonus >= bet else "")
    if wallet == "":
        await safe_edit(q, "❌ Balans yetarli emas (REAL yoki BONUS).", kb_main(uid))
        return

    ok = await (take_real(uid, bet) if wallet == "real" else take_bonus(uid, bet))
    if not ok:
        await safe_edit(q, "❌ Balans yetarli emas.", kb_main(uid))
        return

    mines_sessions[uid] = MinesSession(bet=bet, wallet=wallet, bombs=set(), opened=set(), active=True)
    await safe_edit(q, f"💣 Mines boshlandi!\nBet: {bet} ({wallet.upper()})\nKatak tanlang:", kb_mines_grid(set()))

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

    if not s.bombs:
        s.bombs = gen_bombs(exclude=idx)

    if idx in s.bombs:
        s.active = False
        mines_sessions[uid] = s
        await safe_edit(q, "💥 Bomb!\n😅 Keyingi safar omad!", kb_mines_grid(s.opened, bombs=s.bombs))
        await q.answer()
        return

    s.opened.add(idx)
    mines_sessions[uid] = s
    opened = len(s.opened)
    mult = mines_multiplier(opened)
    await safe_edit(q, f"💣 Mines\nOchilgan: {opened}\nKoef: x{mult:.2f}\nCashout bosib olasiz.", kb_mines_grid(s.opened))
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

    await safe_edit(q, f"✅ Cashout!\nKoef: x{mult:.2f}\nYutuq: {win} ({s.wallet.upper()})", kb_main(uid))
    await q.answer()

@dp.callback_query(F.data == "mn:stop")
async def mn_stop(q: types.CallbackQuery):
    mines_sessions.pop(q.from_user.id, None)
    await safe_edit(q, "❌ Mines to‘xtatildi.", kb_main(q.from_user.id))
    await q.answer()

# AVIATOR
@dp.callback_query(F.data.startswith("av:bet:"))
async def av_bet(q: types.CallbackQuery):
    uid = q.from_user.id
    choice = q.data.split(":")[2]
    if choice == "custom":
        steps[uid] = {"mode": "av_bet_custom"}
        await safe_edit(q, "✍️ Aviator stavka summasini yozing (son).", kb_menu_only())
        await q.answer()
        return
    bet = int(choice)
    await start_aviator(q, bet)
    await q.answer()

async def start_aviator(q: types.CallbackQuery, bet: int):
    uid = q.from_user.id
    real, bonus, *_ = await get_user(uid)
    wallet = "real" if real >= bet else ("bonus" if bonus >= bet else "")
    if wallet == "":
        await safe_edit(q, "❌ Balans yetarli emas (REAL yoki BONUS).", kb_main(uid))
        return

    ok = await (take_real(uid, bet) if wallet == "real" else take_bonus(uid, bet))
    if not ok:
        await safe_edit(q, "❌ Balans yetarli emas.", kb_main(uid))
        return

    # animatsion xabar yuboramiz (bitta message edit bo'ladi)
    sent = await q.message.answer(
        render_plane(1.00, 0, bet, wallet),
        reply_markup=kb_aviator_play()
    )
    aviator_sessions[uid] = AviatorSession(bet=bet, wallet=wallet, mult=1.0, msg_id=sent.message_id, frame=0)
    asyncio.create_task(aviator_loop(q.bot, uid))

    # menyu xabarini o'zgartiramiz (spam bo'lmasin)
    await safe_edit(q, "✈️ Aviator boshlandi! Pastdagi animatsion xabarda koef ketadi.", kb_main(uid))

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

    # aviator message ni ham yangilaymiz
    try:
        await q.bot.edit_message_text(
            chat_id=uid,
            message_id=s.msg_id,
            text=render_plane(s.mult, s.frame, s.bet, s.wallet) + f"\n\n✅ CASHOUT!\nYutuq: {win} ({s.wallet.upper()})",
            reply_markup=kb_main(uid)
        )
    except:
        pass

    await safe_edit(q, "✅ Cashout qilindi.", kb_main(uid))
    await q.answer()

@dp.callback_query(F.data == "av:stop")
async def av_stop(q: types.CallbackQuery):
    aviator_sessions.pop(q.from_user.id, None)
    await safe_edit(q, "❌ Aviator to‘xtatildi.", kb_main(q.from_user.id))
    await q.answer()

# =========================
# ADMIN PANEL + APPROVE/REJECT
# =========================
@dp.callback_query(F.data == "adm:panel")
async def adm_panel(q: types.CallbackQuery):
    if not is_admin(q.from_user.id):
        await q.answer("Ruxsat yo‘q.", show_alert=True); return
    await safe_edit(q, "🛠 Admin panel", kb_admin_panel())
    await q.answer()

@dp.callback_query(F.data == "adm:profit")
async def adm_profit(q: types.CallbackQuery):
    if not is_admin(q.from_user.id):
        await q.answer("Ruxsat yo‘q.", show_alert=True); return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT profit FROM house_profit WHERE id=1")
        profit = (await cur.fetchone())[0]
    await safe_edit(q, f"📊 House profit: {profit}", kb_admin_panel())
    await q.answer()

@dp.callback_query(F.data == "adm:tops")
async def adm_tops(q: types.CallbackQuery):
    if not is_admin(q.from_user.id):
        await q.answer("Ruxsat yo‘q.", show_alert=True); return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id,user_id,amount FROM topup_requests WHERE status='pending' ORDER BY id DESC LIMIT 10"
        )
        rows = await cur.fetchall()
    if not rows:
        await safe_edit(q, "📥 Pending topup yo‘q.", kb_admin_panel()); await q.answer(); return
    txt = "📥 Pending topup (oxirgi 10):\n" + "\n".join([f"#{r[0]} | uid={r[1]} | {r[2]}" for r in rows])
    await safe_edit(q, txt, kb_admin_panel())
    await q.answer()

@dp.callback_query(F.data == "adm:wds")
async def adm_wds(q: types.CallbackQuery):
    if not is_admin(q.from_user.id):
        await q.answer("Ruxsat yo‘q.", show_alert=True); return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id,user_id,amount,note FROM withdraw_requests WHERE status='pending' ORDER BY id DESC LIMIT 10"
        )
        rows = await cur.fetchall()
    if not rows:
        await safe_edit(q, "📤 Pending withdraw yo‘q.", kb_admin_panel()); await q.answer(); return
    txt = "📤 Pending withdraw (oxirgi 10):\n" + "\n".join([f"#{r[0]} | uid={r[1]} | {r[2]} | {r[3][:25]}" for r in rows])
    await safe_edit(q, txt, kb_admin_panel())
    await q.answer()

@dp.callback_query(F.data.startswith("adm:top_ok:"))
async def adm_top_ok(q: types.CallbackQuery):
    if not is_admin(q.from_user.id):
        await q.answer("Ruxsat yo‘q.", show_alert=True); return
    rid = int(q.data.split(":")[2])

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id,amount,status FROM topup_requests WHERE id=?", (rid,))
        row = await cur.fetchone()
        if not row or row[2] != "pending":
            await q.answer("Topilmadi/pending emas.", show_alert=True); return
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
        await q.bot.send_message(uid, f"✅ Topup tasdiqlandi!\nSo‘rov: {amount}\nBalansga: {credited}\nFee: {fee}", reply_markup=kb_main(uid))
    except:
        pass

    await q.answer("Tasdiqlandi.")
    try:
        await q.message.edit_reply_markup(reply_markup=None)
    except:
        pass

@dp.callback_query(F.data.startswith("adm:top_no:"))
async def adm_top_no(q: types.CallbackQuery):
    if not is_admin(q.from_user.id):
        await q.answer("Ruxsat yo‘q.", show_alert=True); return
    rid = int(q.data.split(":")[2])

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id,amount,status FROM topup_requests WHERE id=?", (rid,))
        row = await cur.fetchone()
        if not row or row[2] != "pending":
            await q.answer("Topilmadi/pending emas.", show_alert=True); return
        uid, amount = int(row[0]), int(row[1])

        await db.execute("UPDATE topup_requests SET status='rejected', handled_by=?, handled_at=? WHERE id=?",
                         (q.from_user.id, int(time.time()), rid))
        await db.commit()

    try:
        await q.bot.send_message(uid, f"❌ Topup rad etildi.\nSo‘rov: {amount}", reply_markup=kb_main(uid))
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
        cur = await db.execute("SELECT user_id,amount,status FROM withdraw_requests WHERE id=?", (rid,))
        row = await cur.fetchone()
        if not row or row[2] != "pending":
            await q.answer("Topilmadi/pending emas.", show_alert=True); return
        uid, amount = int(row[0]), int(row[1])

        await db.execute("UPDATE withdraw_requests SET status='approved', handled_by=?, handled_at=? WHERE id=?",
                         (q.from_user.id, int(time.time()), rid))
        await db.commit()

    try:
        await q.bot.send_message(uid, f"✅ Withdraw tasdiqlandi!\nSumma: {amount}", reply_markup=kb_main(uid))
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

        await db.execute("UPDATE withdraw_requests SET status='rejected', handled_by=?, handled_at=? WHERE id=?",
                         (q.from_user.id, int(time.time()), rid))
        # reject -> coin qaytadi
        await db.execute("UPDATE users SET real_balance=real_balance+? WHERE user_id=?", (amount, uid))
        await db.commit()

    try:
        await q.bot.send_message(uid, f"❌ Withdraw rad.\nPul qaytarildi: {amount}", reply_markup=kb_main(uid))
    except:
        pass

    await q.answer("Rad etildi.")
    try:
        await q.message.edit_reply_markup(reply_markup=None)
    except:
        pass

# =========================
# TEXT INPUT HANDLER (custom / promo / withdraw)
# =========================
@dp.message(F.text)
async def on_text(m: types.Message):
    uid = m.from_user.id
    await ensure_user(uid)
    txt = (m.text or "").strip()
    st = steps.get(uid)

    if not st:
        return

    # custom topup
    if st.get("mode") == "topup_custom":
        if not txt.isdigit():
            return await m.answer("Summani SON bilan yozing (masalan 50000).")
        amount = int(txt)
        if amount < MIN_TOPUP or amount > MAX_TOPUP:
            return await m.answer(f"Min {MIN_TOPUP} / Max {MAX_TOPUP}")
        steps.pop(uid, None)

        # Fake CallbackQuery edit yo‘q: oddiy xabar yuboramiz, lekin menyu inline qoladi.
        # (xohlasang keyin /start xabarini "pinned" qilib ishlatasan)
        # Shuning uchun bu yerda adminga so‘rov yuboramiz:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO topup_requests(user_id,amount,status,created_at) VALUES(?,?,?,?)",
                (uid, amount, "pending", int(time.time()))
            )
            await db.commit()
            cur = await db.execute("SELECT last_insert_rowid()")
            rid = (await cur.fetchone())[0]

        await m.answer(f"✅ Topup so‘rovi yuborildi (ID #{rid}). Admin tasdiqlaydi.", reply_markup=kb_main(uid))
        for a in ADMIN_IDS:
            try:
                await m.bot.send_message(
                    a,
                    f"📥 TOPUP REQUEST\nID: #{rid}\nUser: {uid}\nAmount: {amount}\nApprove/Reject👇",
                    reply_markup=kb_admin_actions("adm:top", rid)
                )
            except:
                pass
        return

    # promo enter
    if st.get("mode") == "promo_enter":
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
        return await m.answer(f"✅ Promo qabul qilindi: +{amount} (BONUS)", reply_markup=kb_main(uid))

    # mines custom bet
    if st.get("mode") == "mn_bet_custom":
        if not txt.isdigit():
            return await m.answer("Stavkani SON bilan yozing.", reply_markup=kb_main(uid))
        bet = int(txt)
        steps.pop(uid, None)
        # start mines from message context: send menu again
        fake_q = types.CallbackQuery(id="0", from_user=m.from_user, chat_instance="0", message=m)
        await start_mines(fake_q, bet)
        return

    # aviator custom bet
    if st.get("mode") == "av_bet_custom":
        if not txt.isdigit():
            return await m.answer("Stavkani SON bilan yozing.", reply_markup=kb_main(uid))
        bet = int(txt)
        steps.pop(uid, None)
        fake_q = types.CallbackQuery(id="0", from_user=m.from_user, chat_instance="0", message=m)
        await start_aviator(fake_q, bet)
        return

    # withdraw amount -> ask note
    if st.get("mode") == "wd_amount":
        if not txt.isdigit():
            return await m.answer("Summani SON bilan yozing.", reply_markup=kb_main(uid))
        amount = int(txt)
        real, _, top_ok, *_ = await get_user(uid)
        if not top_ok:
            steps.pop(uid, None)
            return await m.answer("❌ Avval topup tasdiqlansin.", reply_markup=kb_main(uid))
        if amount <= 0 or amount > real:
            return await m.answer("❌ Noto‘g‘ri summa yoki REAL yetarli emas.", reply_markup=kb_main(uid))

        # reserve money now (reject -> return)
        ok = await take_real(uid, amount)
        if not ok:
            steps.pop(uid, None)
            return await m.answer("❌ REAL yetarli emas.", reply_markup=kb_main(uid))

        steps[uid] = {"mode": "wd_note", "amount": str(amount)}
        return await m.answer("📝 Qayerga yechilsin? (izoh yozing, masalan: karta/rekvizit)", reply_markup=kb_main(uid))

    # withdraw note -> create request
    if st.get("mode") == "wd_note":
        amount = int(st["amount"])
        note = txt[:120]

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO withdraw_requests(user_id,amount,note,status,created_at) VALUES(?,?,?,?,?)",
                (uid, amount, note, "pending", int(time.time()))
            )
            await db.commit()
            cur = await db.execute("SELECT last_insert_rowid()")
            rid = (await cur.fetchone())[0]

        steps.pop(uid, None)
        await m.answer(f"✅ Withdraw so‘rovi yuborildi (ID #{rid}). Admin ko‘rib chiqadi.", reply_markup=kb_main(uid))

        for a in ADMIN_IDS:
            try:
                await m.bot.send_message(
                    a,
                    f"📤 WITHDRAW REQUEST\nID: #{rid}\nUser: {uid}\nAmount: {amount}\nNote: {note}\nApprove/Reject👇",
                    reply_markup=kb_admin_actions("adm:wd", rid)
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
