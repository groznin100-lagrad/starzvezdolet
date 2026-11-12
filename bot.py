import asyncio
import hashlib
import hmac
import html
import json
import logging
import math
import os
import random
import re
import sqlite3
import time

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from i18n import DEFAULT_LANG, LANG_NAMES, t

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("stars")

# ---------------------------------------------------------------- настройки
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
PRICE_PER_STAR = float(os.getenv("PRICE_PER_STAR", "1.7"))  # рублей за 1 звезду
MIN_STARS = int(os.getenv("MIN_STARS", "50"))
MAX_STARS = int(os.getenv("MAX_STARS", "10000"))
ORDER_TTL = int(os.getenv("ORDER_TTL_MIN", "30")) * 60

CRYPTO_PAY_TOKEN = os.getenv("CRYPTO_PAY_TOKEN", "")
PARITY_SECRET = os.getenv("PARITY_SECRET", "")  # секретный ключ кассы №1 из кабинета ParityPay
PUBLIC_URL = os.getenv("PUBLIC_URL", "")  # адрес сервиса на Render, например https://stars-bot-xxxx.onrender.com
SBP_ENABLED = bool(PARITY_SECRET)

# Подтверждение домена: файл, который попросит ParityPay
VERIFY_PATH = os.getenv("VERIFY_PATH", "")  # например /parity-verify.txt
VERIFY_CONTENT = os.getenv("VERIFY_CONTENT", "")

PORT = int(os.getenv("PORT", "10000"))
DB_PATH = os.getenv("DB_PATH", "orders.db")

PACKAGES = [50, 100, 250, 500, 1000]

# Какие языки показывать клиентам. Меняется переменной LANGUAGES в Render, например: ru,en,uk
ACTIVE_LANGS = [x.strip() for x in os.getenv("LANGUAGES", "ru,en").split(",") if x.strip() in LANG_NAMES] or ["ru", "en"]

router = Router()


# ---------------------------------------------------------------- база
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, user_name TEXT, recipient TEXT,
            stars INTEGER, amount REAL, method TEXT,
            status TEXT, ext_id TEXT,
            created INTEGER, paid_at INTEGER, done_at INTEGER)"""
        )
        c.execute("CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, lang TEXT)")


def get_lang(uid):
    with db() as c:
        r = c.execute("SELECT lang FROM users WHERE user_id=?", (uid,)).fetchone()
    return r["lang"] if r and r["lang"] in ACTIVE_LANGS else None


def set_lang(uid, lang):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO users(user_id, lang) VALUES(?,?)", (uid, lang))


def L(uid):
    return get_lang(uid) or DEFAULT_LANG


def get_order(oid):
    with db() as c:
        return c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()


def calc_price(stars: int) -> int:
    return math.ceil(stars * PRICE_PER_STAR)


def mark_paid(oid: int) -> bool:
    """True только при первой успешной отметке (защита от повторов)."""
    with db() as c:
        cur = c.execute(
            "UPDATE orders SET status='paid', paid_at=? "
            "WHERE id=? AND status IN ('pending','review','expired')",
            (int(time.time()), oid),
        )
        return cur.rowcount == 1


# ---------------------------------------------------------------- клавиатуры
def kb(rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=tx, callback_data=d) for tx, d in row]
            for row in rows
        ]
    )


def lang_kb():
    return kb([[(LANG_NAMES[code], f"lang:{code}")] for code in ACTIVE_LANGS])


def main_menu(lang):
    return kb([
        [(t(lang, "btn_buy"), "buy")],
        [(t(lang, "btn_orders"), "my")],
        [(t(lang, "btn_lang"), "chooselang")],
    ])


def packages_kb(lang):
    rows, row = [], []
    for p in PACKAGES:
        row.append((f"{p} ⭐ — {calc_price(p)} ₽", f"pk:{p}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([(t(lang, "btn_custom"), "pk:custom")])
    rows.append([(t(lang, "btn_back"), "menu")])
    return kb(rows)


def methods_kb(lang):
    rows = []
    if CRYPTO_PAY_TOKEN:
        rows.append([(t(lang, "pay_crypto"), "pay:crypto")])
    if SBP_ENABLED:
        rows.append([(t(lang, "pay_sbp"), "pay:sbp")])
    rows.append([(t(lang, "btn_cancel"), "menu")])
    return kb(rows)


# ---------------------------------------------------------------- состояния
class Order(StatesGroup):
    custom = State()
    username = State()


# ---------------------------------------------------------------- язык и меню
@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    lang = get_lang(m.from_user.id)
    if lang is None:
        await m.answer("🌐 Выберите язык / Choose language / Оберіть мову", reply_markup=lang_kb())
        return
    await m.answer(t(lang, "welcome"), reply_markup=main_menu(lang))


@router.message(Command("lang"))
async def lang_cmd(m: Message):
    await m.answer("🌐 Выберите язык / Choose language / Оберіть мову", reply_markup=lang_kb())


@router.callback_query(F.data == "chooselang")
async def choose_lang(c: CallbackQuery):
    await c.message.edit_text("🌐 Выберите язык / Choose language / Оберіть мову", reply_markup=lang_kb())
    await c.answer()


@router.callback_query(F.data.startswith("lang:"))
async def set_lang_cb(c: CallbackQuery, state: FSMContext):
    code = c.data.split(":")[1]
    if code not in ACTIVE_LANGS:
        return await c.answer()
    set_lang(c.from_user.id, code)
    await state.clear()
    await c.message.edit_text(t(code, "welcome"), reply_markup=main_menu(code))
    await c.answer()


@router.callback_query(F.data == "menu")
async def menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    lang = L(c.from_user.id)
    await c.message.edit_text(t(lang, "menu"), reply_markup=main_menu(lang))
    await c.answer()


# ---------------------------------------------------------------- заказ
@router.callback_query(F.data == "buy")
async def buy(c: CallbackQuery, state: FSMContext):
    await state.clear()
    lang = L(c.from_user.id)
    await c.message.edit_text(t(lang, "ask_stars"), reply_markup=packages_kb(lang))
    await c.answer()


@router.callback_query(F.data.startswith("pk:"))
async def pick(c: CallbackQuery, state: FSMContext):
    lang = L(c.from_user.id)
    val = c.data.split(":")[1]
    if val == "custom":
        await state.set_state(Order.custom)
        await c.message.edit_text(t(lang, "ask_custom", min=MIN_STARS, max=MAX_STARS))
    else:
        await state.update_data(stars=int(val))
        await state.set_state(Order.username)
        await c.message.edit_text(t(lang, "ask_username"))
    await c.answer()


@router.message(Order.custom)
async def custom(m: Message, state: FSMContext):
    lang = L(m.from_user.id)
    txt = (m.text or "").strip()
    if not txt.isdigit() or not (MIN_STARS <= int(txt) <= MAX_STARS):
        await m.answer(t(lang, "bad_number", min=MIN_STARS, max=MAX_STARS))
        return
    await state.update_data(stars=int(txt))
    await state.set_state(Order.username)
    await m.answer(t(lang, "ask_username"))


@router.message(Order.username)
async def username(m: Message, state: FSMContext):
    lang = L(m.from_user.id)
    txt = (m.text or "").strip()
    if not re.fullmatch(r"@?[A-Za-z0-9_]{5,32}", txt):
        await m.answer(t(lang, "bad_username"))
        return
    uname = txt.lstrip("@")
    data = await state.get_data()
    stars = data["stars"]
    await state.update_data(recipient=uname)
    await m.answer(
        t(lang, "confirm", stars=stars, user=html.escape(uname), price=calc_price(stars)),
        reply_markup=methods_kb(lang),
    )


@router.callback_query(F.data.startswith("pay:"))
async def pay(c: CallbackQuery, state: FSMContext):
    lang = L(c.from_user.id)
    method = c.data.split(":")[1]
    data = await state.get_data()
    if "stars" not in data or "recipient" not in data:
        await c.message.edit_text(t(lang, "order_stale"), reply_markup=main_menu(lang))
        await c.answer()
        return
    stars, rec = data["stars"], data["recipient"]
    amount = float(calc_price(stars))

    with db() as conn:
        cur = conn.execute(
            "INSERT INTO orders(user_id,user_name,recipient,stars,amount,method,status,created)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (c.from_user.id, c.from_user.username or "", rec, stars, amount, method,
             "pending", int(time.time())),
        )
        oid = cur.lastrowid

    try:
        if method == "crypto":
            url, ext = await crypto_create(oid, amount, stars)
            with db() as conn:
                conn.execute("UPDATE orders SET ext_id=? WHERE id=?", (ext, oid))
            await c.message.edit_text(
                t(lang, "crypto_msg", oid=oid, amount=f"{amount:.0f}"),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=t(lang, "btn_pay_crypto"), url=url)],
                    [InlineKeyboardButton(text=t(lang, "btn_check"), callback_data=f"chk:{oid}")],
                ]),
            )
        elif method == "sbp":
            url, ext = await sbp_create(oid, amount, stars, rec)
            with db() as conn:
                conn.execute("UPDATE orders SET ext_id=? WHERE id=?", (ext, oid))
            await c.message.edit_text(
                t(lang, "sbp_msg", oid=oid, amount=f"{amount:.0f}"),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=t(lang, "btn_pay_sbp"), url=url)],
                    [InlineKeyboardButton(text=t(lang, "btn_check"), callback_data=f"chk:{oid}")],
                ]),
            )
    except Exception as e:
        log.exception("payment create failed")
        with db() as conn:
            conn.execute("UPDATE orders SET status='canceled' WHERE id=?", (oid,))
        await c.message.edit_text(
            t(lang, "pay_fail", err=html.escape(str(e))), reply_markup=main_menu(lang)
        )
    await c.answer()


@router.callback_query(F.data.startswith("chk:"))
async def check(c: CallbackQuery):
    lang = L(c.from_user.id)
    oid = int(c.data.split(":")[1])
    o = get_order(oid)
    if not o or o["user_id"] != c.from_user.id:
        await c.answer(t(lang, "not_found"), show_alert=True)
        return
    if o["status"] in ("paid", "done"):
        await c.answer(t(lang, "already_paid"), show_alert=True)
        return
    if o["method"] == "crypto" and o["ext_id"]:
        if await crypto_is_paid(o["ext_id"]):
            await on_paid(c.bot, oid)
            await c.answer(t(lang, "paid_alert"), show_alert=True)
            return
    if o["method"] == "sbp" and o["ext_id"]:
        if await parity_verified(o["ext_id"], oid):
            await on_paid(c.bot, oid)
            await c.answer(t(lang, "paid_alert"), show_alert=True)
            return
    await c.answer(t(lang, "not_paid_yet"), show_alert=True)


@router.callback_query(F.data == "my")
async def my_orders(c: CallbackQuery):
    lang = L(c.from_user.id)
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10", (c.from_user.id,)
        ).fetchall()
    txt = "\n".join(
        t(lang, "my_line", oid=r["id"], stars=r["stars"], user=r["recipient"],
          status=t(lang, f"st_{r['status']}"))
        for r in rows
    ) or t(lang, "my_empty")
    if SUPPORT:
        txt += "\n\n" + t(lang, "support", support=SUPPORT)
    await c.message.edit_text(txt, reply_markup=kb([[(t(lang, "btn_back"), "menu")]]))
    await c.answer()


# ---------------------------------------------------------------- админ (на русском)
def is_admin(uid):
    return uid == ADMIN_ID


@router.callback_query(F.data.startswith("done:"))
async def adm_done(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer()
    oid = int(c.data.split(":")[1])
    o = get_order(oid)
    with db() as conn:
        cur = conn.execute(
            "UPDATE orders SET status='done', done_at=? WHERE id=? AND status='paid'",
            (int(time.time()), oid),
        )
    if cur.rowcount == 1 and o:
        await c.bot.send_message(
            o["user_id"],
            t(L(o["user_id"]), "done_client", oid=oid, stars=o["stars"], user=o["recipient"]),
        )
        await c.message.edit_reply_markup(reply_markup=None)
        await c.answer("Клиент уведомлён")
    else:
        await c.answer("Уже выполнен", show_alert=True)


@router.message(Command("orders"))
async def adm_orders(m: Message):
    if not is_admin(m.from_user.id):
        return
    with db() as conn:
        rows = conn.execute("SELECT * FROM orders WHERE status='paid' ORDER BY id").fetchall()
    if not rows:
        return await m.answer("Нет заказов, ожидающих отправки.")
    for o in rows:
        await m.answer(
            f"№{o['id']}: {o['stars']}⭐ → @{o['recipient']} ({o['amount']:.2f} ₽)",
            reply_markup=kb([[("✅ Выполнено", f"done:{o['id']}")]]),
        )


@router.message(Command("stats"))
async def adm_stats(m: Message):
    if not is_admin(m.from_user.id):
        return
    with db() as conn:
        r = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(stars),0) s, COALESCE(SUM(amount),0) a "
            "FROM orders WHERE status IN ('paid','done')"
        ).fetchone()
        today = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(amount),0) a FROM orders "
            "WHERE status IN ('paid','done') AND paid_at>?", (int(time.time()) - 86400,)
        ).fetchone()
    await m.answer(
        f"Всего оплачено: {r['n']} заказов, {r['s']} ⭐, {r['a']:.2f} ₽\n"
        f"За 24 часа: {today['n']} заказов, {today['a']:.2f} ₽"
    )


async def on_paid(bot: Bot, oid: int) -> bool:
    if not mark_paid(oid):
        return False
    o = get_order(oid)
    await bot.send_message(
        o["user_id"],
        t(L(o["user_id"]), "paid_client", oid=oid, stars=o["stars"], user=o["recipient"]),
    )
    await bot.send_message(
        ADMIN_ID,
        f"💰 ОПЛАЧЕН заказ №{oid}\n{o['stars']} ⭐ → @{o['recipient']}\n"
        f"Сумма: {o['amount']:.2f} ₽ ({o['method']})",
        reply_markup=kb([[("✅ Выполнено", f"done:{oid}")]]),
    )
    return True


# ---------------------------------------------------------------- CryptoBot
CRYPTO_API = "https://pay.crypt.bot/api"


async def crypto_call(method: str, params: dict):
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"{CRYPTO_API}/{method}",
            json=params,
            headers={"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as r:
            data = await r.json()
    if not data.get("ok"):
        raise RuntimeError(f"CryptoBot: {data.get('error')}")
    return data["result"]


async def crypto_create(oid: int, amount: float, stars: int):
    res = await crypto_call("createInvoice", {
        "currency_type": "fiat",
        "fiat": "RUB",
        "accepted_assets": "USDT,TON",
        "amount": f"{amount:.2f}",
        "description": f"{stars} Telegram Stars, order {oid}",
        "payload": str(oid),
        "expires_in": ORDER_TTL,
    })
    return res["bot_invoice_url"], str(res["invoice_id"])


async def crypto_is_paid(invoice_id: str) -> bool:
    res = await crypto_call("getInvoices", {"invoice_ids": invoice_id})
    items = res.get("items", [])
    return bool(items) and items[0].get("status") == "paid"


async def crypto_webhook(request: web.Request):
    body = await request.read()
    secret = hashlib.sha256(CRYPTO_PAY_TOKEN.encode()).digest()
    sign = hmac.new(secret, body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sign, request.headers.get("crypto-pay-api-signature", "")):
        return web.Response(status=403, text="bad signature")
    data = json.loads(body)
    if data.get("update_type") == "invoice_paid":
        inv = data["payload"]
        try:
            await on_paid(request.app["bot"], int(inv["payload"]))
        except Exception:
            log.exception("crypto webhook error")
    return web.Response(text="ok")


# ---------------------------------------------------------------- СБП через ParityPay
PARITY_API = "https://api.paritypay.net"


def find_key(obj, key):
    """Ищет ключ в ответе API на любой глубине (формат ответа может быть вложенным)."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, key)
            if r is not None:
                return r
    return None


async def parity_call(method: str, path: str, body=None, params=None):
    async with aiohttp.ClientSession() as s:
        async with s.request(
            method, PARITY_API + path, json=body, params=params,
            headers={"X-SecretKey": PARITY_SECRET},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as r:
            text = await r.text()
            try:
                data = json.loads(text)
            except ValueError:
                raise RuntimeError(f"ParityPay HTTP {r.status}")
            if r.status >= 400:
                raise RuntimeError(f"ParityPay {r.status}: {str(data)[:200]}")
            return data


async def sbp_create(oid: int, amount: float, stars: int, recipient: str):
    # order_id в ParityPay уникален в рамках кассы, поэтому добавляем время:
    # если база на Render сбросится, номера заказов начнутся заново и не пересекутся со старыми.
    ext = f"{oid}-{int(time.time())}"
    body = {
        "order_id": ext,
        "amount": amount,
        "comment": f"Оплата покупки для {recipient}",
        "service": "sbp",
    }
    if PUBLIC_URL:
        body["callback_url"] = PUBLIC_URL.rstrip("/") + "/parity"
    data = await parity_call("POST", "/v2/invoice/create", body)
    link = find_key(data, "link")
    if not link:
        raise RuntimeError("ParityPay: в ответе нет ссылки на оплату")
    return link, ext


async def parity_verified(ext: str, oid: int) -> bool:
    """Оплату признаём только по ответу API ParityPay, а не по телу уведомления."""
    o = get_order(oid)
    if not o or o["ext_id"] != ext:
        return False
    data = await parity_call("GET", "/v2/invoice/list", params={"order_id": ext, "status": "PAID"})
    items = find_key(data, "invoices") or []
    for it in items:
        if not isinstance(it, dict) or it.get("status") != "PAID":
            continue
        if it.get("order_id") is not None and str(it["order_id"]) != ext:
            continue
        amt = it.get("amount")
        if amt is not None and float(amt) + 0.01 < float(o["amount"]):
            continue
        return True
    return False


async def parity_webhook(request: web.Request):
    body = await request.read()
    log.info("parity webhook: %s", body[:500])
    try:
        data = json.loads(body)
        ext = find_key(data, "order_id")
        if ext:
            ext = str(ext)
            oid = int(ext.split("-")[0])
            if await parity_verified(ext, oid):
                await on_paid(request.app["bot"], oid)
    except Exception:
        log.exception("parity webhook error")
    return web.Response(text="ok")


# ---------------------------------------------------------------- веб-сервер
async def index(_):
    return web.Response(text="ok")


async def verify(_):
    return web.Response(text=VERIFY_CONTENT)


def make_app(bot: Bot):
    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/", index)
    app.router.add_get("/health", index)
    app.router.add_post("/cryptopay", crypto_webhook)
    app.router.add_post("/parity", parity_webhook)
    if VERIFY_PATH:
        app.router.add_get(VERIFY_PATH, verify)
    return app


# ---------------------------------------------------------------- фоновые задачи
async def expirer(bot: Bot):
    while True:
        await asyncio.sleep(60)
        try:
            limit = int(time.time()) - ORDER_TTL
            with db() as conn:
                rows = conn.execute(
                    "SELECT id,user_id FROM orders WHERE status='pending' AND created<?", (limit,)
                ).fetchall()
                for r in rows:
                    conn.execute("UPDATE orders SET status='expired' WHERE id=?", (r["id"],))
            for r in rows:
                try:
                    await bot.send_message(r["user_id"], t(L(r["user_id"]), "expired_msg", oid=r["id"]))
                except Exception:
                    pass
        except Exception:
            log.exception("expirer")


async def main():
    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    runner = web.AppRunner(make_app(bot))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("web server on %s", PORT)

    asyncio.create_task(expirer(bot))
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
