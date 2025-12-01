#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Metro Shop Telegram Bot (BotHost-compatible)

Функции:
- CloudTips polling (оплата без webhook)
- PUBG ID только по кнопке
- Отзывы пользователей + отзывы в карточках товара
- Статистика администратора
- Каталог, заказы, CRUD товаров
- Полная поддержка python-telegram-bot v20+
"""

import os
import asyncio
import logging
import sqlite3
from datetime import datetime
from typing import List, Optional

import httpx
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)


# -------------------- CONFIG --------------------

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "YOUR_TOKEN_HERE")

OWNER_ID = int(os.getenv("OWNER_ID", "0"))
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))

NOTIFY_CHAT_IDS = [
    int(x) for x in os.getenv("NOTIFY_CHAT_IDS", "").split(",") if x.strip()
]

DB_PATH = os.getenv("DB_PATH", "metro_shop.db")

# CloudTips
CLOUDTIPS_BASE = os.getenv("CLOUDTIPS_BASE", "https://pay.cloudtips.ru/p/2842e969")
CLOUDTIPS_STATUS_API = os.getenv(
    "CLOUDTIPS_STATUS_API",
    "https://pay.cloudtips.ru/api/payment/{payment_id}/status"
)

ADMIN_IDS: List[int] = [OWNER_ID]
if os.getenv("ADMIN_IDS"):
    ADMIN_IDS = [
        int(x) for x in os.getenv("ADMIN_IDS").split(",") if x.strip()
    ]


# -------------------- LOGGING --------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)
# -------------------- DB HELPERS --------------------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER UNIQUE,
            username TEXT,
            pubg_id TEXT,
            registered_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            price REAL NOT NULL,
            photo TEXT,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS product_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER,
            file_id TEXT,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            product_id INTEGER,
            price REAL,
            status TEXT,
            created_at TEXT,
            payment_payload TEXT,
            payment_checked INTEGER DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER,
            buyer_id INTEGER,
            worker_id INTEGER,
            rating INTEGER,
            text TEXT,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS worker_payouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER,
            worker_id INTEGER,
            amount REAL,
            created_at TEXT
        )
    """)

    conn.commit()
    conn.close()


def db_execute(query: str, params: tuple = (), fetch: bool = False):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute(query, params)

    data = None
    if fetch:
        data = cur.fetchall()
    else:
        conn.commit()

    conn.close()
    return data


def now_iso() -> str:
    return datetime.utcnow().isoformat()


def is_admin_tg(tg_id: int) -> bool:
    return tg_id in ADMIN_IDS


# -------------------- KEYBOARDS --------------------

MAIN_MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📦 Каталог"), KeyboardButton("🧾 Мои заказы")],
        [KeyboardButton("🎮 Привязать PUBG ID"), KeyboardButton("⭐ Отзывы")],
        [KeyboardButton("📞 Поддержка")],
    ],
    resize_keyboard=True
)

CANCEL_KB = ReplyKeyboardMarkup(
    [[KeyboardButton("↩️ Назад")]],
    resize_keyboard=True
)

ADMIN_PANEL_KB = ReplyKeyboardMarkup(
    [
        [
            KeyboardButton("➕ Добавить товар"),
            KeyboardButton("✏️ Редактировать товар"),
            KeyboardButton("🗑️ Удалить товар")
        ],
        [
            KeyboardButton("📋 Список заказов"),
            KeyboardButton("📊 Статистика бота")
        ]
    ],
    resize_keyboard=True
)
# -------------------- UTIL / RATING --------------------

def get_product_rating_and_done_count(pid: int):
    rows = db_execute(
        'SELECT r.rating FROM reviews r JOIN orders o ON r.order_id=o.id WHERE o.product_id=?',
        (pid,), fetch=True
    )
    if not rows:
        avg = None
    else:
        vals = [r[0] for r in rows if r[0] is not None]
        avg = (sum(vals) / len(vals)) if vals else None

    done_count_row = db_execute(
        'SELECT COUNT(*) FROM orders WHERE product_id=? AND status="done"',
        (pid,), fetch=True
    )
    done_count = done_count_row[0][0] if done_count_row else 0
    return avg, done_count


# -------------------- CLOUDTIPS POLLING --------------------

async def check_cloudtips_payment_api(payment_payload: str) -> Optional[str]:
    """
    One-shot check to CloudTips status endpoint. Returns status string or None.
    NOTE: If CloudTips API differs, set CLOUDTIPS_STATUS_API env accordingly.
    """
    url = CLOUDTIPS_STATUS_API.format(payment_id=payment_payload)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url)
            if r.status_code != 200:
                return None
            data = r.json()
            return data.get("status")
    except Exception as e:
        logger.warning("CloudTips API check error: %s", e)
        return None


async def poll_payment_and_finalize(order_id: int, context: ContextTypes.DEFAULT_TYPE, max_attempts: int = 60, interval: int = 5):
    """
    Background poller for order status. Marks order as paid if found.
    Not blocking — should be scheduled with asyncio.create_task(...)
    """
    row = db_execute("SELECT user_id, price, payment_payload FROM orders WHERE id=?", (order_id,), fetch=True)
    if not row:
        logger.warning("Order %s not found for polling", order_id)
        return

    user_id, price, payload = row[0]
    user_row = db_execute("SELECT tg_id FROM users WHERE id=?", (user_id,), fetch=True)
    tg_id = user_row[0][0] if user_row else None

    attempt = 0
    paid = False
    while attempt < max_attempts:
        attempt += 1
        status = await check_cloudtips_payment_api(str(payload))
        logger.info("Order %s poll attempt %s status=%s", order_id, attempt, status)
        if status == "paid":
            paid = True
            break
        if status == "failed":
            paid = False
            break
        await asyncio.sleep(interval)

    if paid:
        db_execute("UPDATE orders SET status=?, payment_checked=1 WHERE id=?", ("paid", order_id))
        logger.info("Order %s marked as paid", order_id)
        if tg_id:
            try:
                await context.bot.send_message(chat_id=tg_id, text=f"✅ Оплата подтверждена. Ваш заказ #{order_id} принят.")
            except Exception:
                logger.exception("Notify buyer failed for order %s", order_id)
        try:
            if ADMIN_CHAT_ID:
                await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"Заказ #{order_id} оплачен. Сумма: {price}₽")
            for nid in NOTIFY_CHAT_IDS:
                try:
                    await context.bot.send_message(chat_id=nid, text=f"Заказ #{order_id} оплачен.")
                except Exception:
                    pass
        except Exception:
            logger.exception("Notify admin failed for order %s", order_id)
    else:
        db_execute("UPDATE orders SET payment_checked=1 WHERE id=?", (order_id,))
        if tg_id:
            try:
                await context.bot.send_message(chat_id=tg_id, text=f"❌ Оплата для заказа #{order_id} не подтверждена.")
            except Exception:
                logger.exception("Notify buyer failed for failed order %s", order_id)
        try:
            if ADMIN_CHAT_ID:
                await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"Оплата для заказа #{order_id} не подтверждена (timeout).")
        except Exception:
            logger.exception("Notify admin failed for timeout order %s", order_id)


# -------------------- HANDLERS: START / TEXT ROUTER --------------------

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    db_execute("INSERT OR IGNORE INTO users (tg_id, username, registered_at) VALUES (?, ?, ?)",
               (user.id, user.username or "", now_iso()))
    await update.message.reply_text(f"Привет, {user.first_name}! Добро пожаловать в магазин.", reply_markup=MAIN_MENU)


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Main text router. Handles:
     - PUBG ID awaiting state (only set by button)
     - Menu commands
     - Admin flows (add/edit/delete product)
    """
    # ignore messages from admin group if desired
    if update.effective_chat and update.effective_chat.id == ADMIN_CHAT_ID:
        # optionally process admin group messages elsewhere
        pass

    msg = update.message
    if not msg or not msg.text:
        return

    text = msg.text.strip()
    user = update.effective_user

    # PUBG ID awaiting (only after user pressed 'Привязать PUBG ID')
    if context.user_data.get("awaiting_pubg"):
        pubg = text
        db_execute("INSERT OR IGNORE INTO users (tg_id, username, registered_at) VALUES (?, ?, ?)",
                   (user.id, user.username or "", now_iso()))
        db_execute("UPDATE users SET pubg_id=? WHERE tg_id=?", (pubg, user.id))
        context.user_data.pop("awaiting_pubg", None)
        await msg.reply_text(f"✅ PUBG ID сохранён: {pubg}", reply_markup=MAIN_MENU)
        return

    # Admin product add flow
    if context.user_data.get("product_flow"):
        await handle_add_product_flow(update, context)
        return

    # Main menu
    if text == "📦 Каталог":
        await products_handler(update, context)
        return
    if text == "🧾 Мои заказы":
        await my_orders(update, context)
        return
    if text == "🎮 Привязать PUBG ID":
        context.user_data["awaiting_pubg"] = True
        await msg.reply_text("Отправьте ваш PUBG ID (ник или цифры).", reply_markup=CANCEL_KB)
        return
    if text == "⭐ Отзывы":
        await reviews_handler(update, context)
        return
    if text == "📞 Поддержка":
        await msg.reply_text("Напишите владельцу: @zavik911", reply_markup=MAIN_MENU)
        return
    if text == "↩️ Назад":
        context.user_data.clear()
        await msg.reply_text("Отменено.", reply_markup=MAIN_MENU)
        return

    # Admin shortcuts
    if text == "/admin" and is_admin_tg(user.id):
        await admin_menu(update, context)
        return
    if text == "➕ Добавить товар" and is_admin_tg(user.id):
        start_product_flow(context.user_data)
        await msg.reply_text("Добавление товара — введите название:", reply_markup=CANCEL_KB)
        return
    if text == "✏️ Редактировать товар" and is_admin_tg(user.id):
        await start_edit_flow(update, context)
        return
    if text == "🗑️ Удалить товар" and is_admin_tg(user.id):
        await start_delete_flow(update, context)
        return
    if text == "📋 Список заказов" and is_admin_tg(user.id):
        await list_orders_admin(update, context)
        return
    if text == "📊 Статистика бота" and is_admin_tg(user.id):
        await bot_stats_handler(update, context)
        return

    # fallback
    await msg.reply_text("Неизвестная команда. Выберите действие из меню.", reply_markup=MAIN_MENU)
# -------------------- PRODUCTS: callbacks & buy flow --------------------

async def product_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()
    data = q.data or ""
    if not data.startswith("detail:"):
        return
    _, pid = data.split(":", 1)
    try:
        pid = int(pid)
    except:
        return
    row = db_execute("SELECT name, description, price, photo FROM products WHERE id=?", (pid,), fetch=True)
    if not row:
        await q.message.reply_text("Товар не найден.")
        return
    name, desc, price, photo = row[0]
    avg, done_ct = get_product_rating_and_done_count(pid)
    rating_line = f"⭐ {avg:.1f}" if avg else "Нет оценок"
    caption = f"*{name}*\n\n{desc or ''}\n\n💰 Цена: *{price}₽*\n{rating_line} • Выполнено: {done_ct}"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Купить — {price}₽", callback_data=f"buy:{pid}"),
         InlineKeyboardButton("Отзывы", callback_data=f"show_reviews:{pid}")],
    ])
    try:
        await q.message.reply_markdown(caption, reply_markup=kb)
    except Exception:
        await q.message.reply_text(caption, reply_markup=kb)


async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()
    data = q.data or ""
    if not data.startswith("buy:"):
        return
    _, pid_str = data.split(":", 1)
    try:
        pid = int(pid_str)
    except:
        return
    prod = db_execute("SELECT name, price FROM products WHERE id=?", (pid,), fetch=True)
    if not prod:
        await q.message.reply_text("Товар не найден.")
        return
    name, price = prod[0]
    user = q.from_user
    # ensure user in DB
    db_execute("INSERT OR IGNORE INTO users (tg_id, username, registered_at) VALUES (?, ?, ?)",
               (user.id, user.username or "", now_iso()))
    user_row = db_execute("SELECT id, pubg_id FROM users WHERE tg_id=?", (user.id,), fetch=True)
    user_db_id = user_row[0][0]
    # create order
    db_execute("INSERT INTO orders (user_id, product_id, price, status, created_at) VALUES (?, ?, ?, ?, ?)",
               (user_db_id, pid, price, "awaiting_payment", now_iso()))
    order_id = db_execute("SELECT id FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_db_id,), fetch=True)[0][0]
    # set payload and send link
    db_execute("UPDATE orders SET payment_payload=? WHERE id=?", (str(order_id), order_id))
    payment_url = f"{CLOUDTIPS_BASE}?amount={int(price)}&payload={order_id}"
    try:
        await q.message.reply_text(
            f"Вы выбрали: {name} — {price}₽\n\n"
            f"Перейдите по ссылке для оплаты:\n{payment_url}\n\n"
            "После оплаты бот автоматически проверит платёж и подтвердит заказ.",
            reply_markup=MAIN_MENU
        )
    except Exception:
        pass
    # start background polling (non-blocking)
    asyncio.create_task(poll_payment_and_finalize(order_id, context))


# -------------------- REVIEWS --------------------

async def show_reviews_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()
    data = q.data or ""
    if not data.startswith("show_reviews:"):
        return
    _, pid_str = data.split(":", 1)
    try:
        pid = int(pid_str)
    except:
        return
    rows = db_execute("""
        SELECT r.rating, r.text, r.created_at, u.username
        FROM reviews r
        JOIN orders o ON r.order_id=o.id
        LEFT JOIN users u ON r.buyer_id=u.id
        WHERE o.product_id=?
        ORDER BY r.id DESC LIMIT 20
    """, (pid,), fetch=True)
    if not rows:
        await q.message.reply_text("По этому товару ещё нет отзывов.", reply_markup=MAIN_MENU)
        return
    text = f"⭐ Отзывы по товару #{pid}:\n\n"
    for rating, txt, created, username in rows:
        user_label = f"@{username}" if username else "Пользователь"
        text += f"{user_label} — ⭐{rating}\n"
        if txt:
            text += f"«{txt}»\n"
        text += f"{created}\n\n"
    parts = [text[i:i+3900] for i in range(0, len(text), 3900)]
    for p in parts:
        await q.message.reply_text(p, reply_markup=MAIN_MENU)


async def reviews_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db_execute("""
        SELECT r.rating, r.text, r.created_at, u.username
        FROM reviews r
        LEFT JOIN users u ON r.buyer_id=u.id
        ORDER BY r.id DESC LIMIT 30
    """, (), fetch=True)
    if not rows:
        await update.message.reply_text("Отзывов пока нет.", reply_markup=MAIN_MENU)
        return
    text = "⭐ Последние отзывы:\n\n"
    for rating, txt, created, username in rows:
        user_label = f"@{username}" if username else "Пользователь"
        text += f"{user_label} — ⭐{rating}\n"
        if txt:
            text += f"«{txt}»\n"
        text += f"{created}\n\n"
    parts = [text[i:i+3900] for i in range(0, len(text), 3900)]
    for p in parts:
        await update.message.reply_text(p, reply_markup=MAIN_MENU)


# -------------------- ORDERS: user view --------------------

async def my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    row = db_execute("SELECT id FROM users WHERE tg_id=?", (user.id,), fetch=True)
    if not row:
        await update.message.reply_text("Вы ещё не зарегистрированы.", reply_markup=MAIN_MENU)
        return
    user_db_id = row[0][0]
    rows = db_execute("SELECT o.id, p.name, o.price, o.status FROM orders o JOIN products p ON o.product_id=p.id WHERE o.user_id=? ORDER BY o.id DESC LIMIT 50", (user_db_id,), fetch=True)
    if not rows:
        await update.message.reply_text("У вас пока нет заказов.", reply_markup=MAIN_MENU)
        return
    text = ""
    for oid, pname, price, status in rows:
        text += f"#{oid} {pname} — {price}₽ — {status}\n"
    await update.message.reply_text(text, reply_markup=MAIN_MENU)
# -------------------- ADMIN PANEL --------------------

async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin_tg(user.id):
        if update.message:
            await update.message.reply_text("Только админам.")
        return
    await update.message.reply_text("Панель администратора:", reply_markup=ADMIN_PANEL_KB)


async def list_orders_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_tg(update.effective_user.id):
        await update.message.reply_text("Только админам.")
        return

    rows = db_execute("""
        SELECT o.id, u.tg_id, p.name, o.price, o.status, o.created_at 
        FROM orders o 
        LEFT JOIN users u ON o.user_id=u.id 
        LEFT JOIN products p ON o.product_id=p.id 
        ORDER BY o.id DESC LIMIT 100
    """, fetch=True)

    if not rows:
        await update.message.reply_text("Заказов нет.", reply_markup=ADMIN_PANEL_KB)
        return

    text = ""
    for oid, tg_id, pname, price, status, created in rows:
        text += f"#{oid} {pname} — {price}₽ — {status} — tg:{tg_id} — {created}\n"

    parts = [text[i:i+3900] for i in range(0, len(text), 3900)]
    for p in parts:
        await update.message.reply_text(p, reply_markup=ADMIN_PANEL_KB)


# -------------------- ADMIN: Statistics --------------------

async def bot_stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_tg(update.effective_user.id):
        await update.message.reply_text("Только админам.")
        return

    total_users = db_execute("SELECT COUNT(*) FROM users", fetch=True)[0][0]
    total_products = db_execute("SELECT COUNT(*) FROM products", fetch=True)[0][0]
    total_orders = db_execute("SELECT COUNT(*) FROM orders", fetch=True)[0][0]
    paid_orders = db_execute("SELECT COUNT(*) FROM orders WHERE status='paid'", fetch=True)[0][0]
    done_orders = db_execute("SELECT COUNT(*) FROM orders WHERE status='done'", fetch=True)[0][0]

    revenue_row = db_execute("SELECT SUM(price) FROM orders WHERE status IN ('paid','done')", fetch=True)
    total_revenue = revenue_row[0][0] or 0

    avg_check = round(total_revenue / total_orders, 2) if total_orders else 0

    text = (
        "📊 *Статистика бота*\n\n"
        f"👥 Пользователи: *{total_users}*\n"
        f"🛒 Товары: *{total_products}*\n"
        f"📦 Заказы: *{total_orders}*\n"
        f"💳 Оплачено: *{paid_orders}*\n"
        f"🏁 Выполнено: *{done_orders}*\n"
        f"💰 Оборот: *{total_revenue}₽*\n"
        f"📎 Средний чек: *{avg_check}₽*"
    )

    try:
        await update.message.reply_markdown(text, reply_markup=ADMIN_PANEL_KB)
    except:
        await update.message.reply_text(text, reply_markup=ADMIN_PANEL_KB)


# -------------------- ADMIN: ADD PRODUCT FLOW --------------------

def start_product_flow(user_data: dict):
    user_data["product_flow"] = {
        "stage": "name",
        "data": {}
    }

def clear_product_flow(user_data: dict):
    user_data.pop("product_flow", None)


async def handle_add_product_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg is None:
        return

    if not is_admin_tg(update.effective_user.id):
        clear_product_flow(context.user_data)
        return

    flow = context.user_data.get("product_flow", {})
    stage = flow.get("stage")

    # Cancel
    if msg.text and msg.text.strip().lower() in ("/cancel", "↩️ назад"):
        clear_product_flow(context.user_data)
        await msg.reply_text("Добавление товара отменено.", reply_markup=ADMIN_PANEL_KB)
        return

    # Stage 1 — name
    if stage == "name":
        name = msg.text.strip()
        if not name:
            await msg.reply_text("Название не может быть пустым.")
            return
        flow["data"]["name"] = name
        flow["stage"] = "price"
        await msg.reply_text("Введите цену (числом):", reply_markup=CANCEL_KB)
        return

    # Stage 2 — price
    if stage == "price":
        try:
            price = float(msg.text.strip())
        except:
            await msg.reply_text("Неверная цена. Введите число.")
            return
        flow["data"]["price"] = price
        flow["stage"] = "desc"
        await msg.reply_text("Введите описание товара:", reply_markup=CANCEL_KB)
        return

    # Stage 3 — description
    if stage == "desc":
        desc = msg.text.strip()
        flow["data"]["description"] = desc

        db_execute(
            "INSERT INTO products (name, description, price, created_at) VALUES (?, ?, ?, ?)",
            (flow["data"]["name"], flow["data"]["description"], flow["data"]["price"], now_iso())
        )

        clear_product_flow(context.user_data)
        await msg.reply_text("Товар добавлен!", reply_markup=ADMIN_PANEL_KB)
        return


# -------------------- ADMIN: EDIT PRODUCT --------------------

async def start_edit_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db_execute("SELECT id, name, price FROM products ORDER BY id", fetch=True)
    if not rows:
        await update.message.reply_text("Нет товаров для редактирования.", reply_markup=ADMIN_PANEL_KB)
        return

    lines = "\n".join([f"{pid}: {name} — {price}₽" for pid, name, price in rows])

    context.user_data["edit_flow"] = True
    await update.message.reply_text(
        "Отправьте ID товара для редактирования:\n\n" + lines,
        reply_markup=CANCEL_KB
    )


async def start_delete_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db_execute("SELECT id, name, price FROM products ORDER BY id", fetch=True)
    if not rows:
        await update.message.reply_text("Нет товаров для удаления.", reply_markup=ADMIN_PANEL_KB)
        return

    lines = "\n".join([f"{pid}: {name} — {price}₽" for pid, name, price in rows])

    context.user_data["delete_flow"] = True
    await update.message.reply_text(
        "Отправьте ID товара для удаления:\n\n" + lines,
        reply_markup=CANCEL_KB
    )


# Flow router for edit/delete
async def message_router_for_admin_flows(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    user = update.effective_user

    if not is_admin_tg(user.id):
        return

    # Cancel
    if text in ("/cancel", "↩️ Назад"):
        context.user_data.pop("edit_flow", None)
        context.user_data.pop("delete_flow", None)
        await update.message.reply_text("Отменено.", reply_markup=ADMIN_PANEL_KB)
        return

    # EDIT FLOW
    if context.user_data.get("edit_flow"):
        try:
            pid = int(text)
        except:
            await update.message.reply_text("Неверный ID товара.")
            return

        row = db_execute("SELECT name, price, description FROM products WHERE id=?", (pid,), fetch=True)
        if not row:
            await update.message.reply_text("Товар не найден.")
            return

        name, price, desc = row[0]
        context.user_data["edit_flow"] = {"pid": pid, "stage": "field"}

        await update.message.reply_text(
            f"Редактирование товара #{pid}:\n"
            f"Название: {name}\nЦена: {price}₽\nОписание: {desc}\n\n"
            "Введите новое значение (или оставьте пустым, чтобы не менять). "
            "Формат: name=..., price=..., desc=...",
            reply_markup=CANCEL_KB
        )
        return

    # DELETE FLOW
    if context.user_data.get("delete_flow"):
        try:
            pid = int(text)
        except:
            await update.message.reply_text("Неверный ID.")
            return

        db_execute("DELETE FROM products WHERE id=?", (pid,))
        db_execute("DELETE FROM product_photos WHERE product_id=?", (pid,))
        context.user_data.pop("delete_flow", None)

        await update.message.reply_text("Товар удалён.", reply_markup=ADMIN_PANEL_KB)
        return
# -------------------- AD HOC: finish edit flow (apply changes) --------------------

async def editing_product_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    После того как админ выбрал ID товара и прислал строку в формате:
      name=Новое название, price=123, desc=Новое описание
    или просто часть этих полей — обновляем запись.
    """
    if not update.message or not update.message.text:
        return
    user = update.effective_user
    if not is_admin_tg(user.id):
        return

    ef = context.user_data.get("edit_flow")
    # ef can be dict with pid & stage 'field' OR earlier boolean flag
    if not ef or not isinstance(ef, dict) or ef.get("stage") != "field":
        # nothing to do here for editing_product_text
        return

    pid = ef.get("pid")
    text = update.message.text.strip()
    if text in ("/cancel", "↩️ Назад"):
        context.user_data.pop("edit_flow", None)
        await update.message.reply_text("Редактирование отменено.", reply_markup=ADMIN_PANEL_KB)
        return

    # parse simple key=value pairs separated by commas
    parts = [p.strip() for p in text.split(",") if p.strip()]
    fields = {}
    for p in parts:
        if "=" not in p:
            continue
        k, v = p.split("=", 1)
        k = k.strip().lower()
        v = v.strip()
        if k in ("name", "price", "desc", "description"):
            fields[k] = v

    if not fields:
        await update.message.reply_text("Не найдено полей для обновления. Используйте формат: name=..., price=..., desc=...", reply_markup=CANCEL_KB)
        return

    # apply updates
    if "name" in fields:
        db_execute("UPDATE products SET name=? WHERE id=?", (fields["name"], pid))
    if "price" in fields:
        try:
            price_val = float(fields["price"])
            db_execute("UPDATE products SET price=? WHERE id=?", (price_val, pid))
        except:
            await update.message.reply_text("Неверное значение для price. Обновление цены пропущено.")
    if "desc" in fields or "description" in fields:
        desc_val = fields.get("desc", fields.get("description", ""))
        db_execute("UPDATE products SET description=? WHERE id=?", (desc_val, pid))

    context.user_data.pop("edit_flow", None)
    await update.message.reply_text("Товар обновлён.", reply_markup=ADMIN_PANEL_KB)


# -------------------- REGISTER HANDLERS --------------------

def register_handlers(app):
    # core
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    # products callbacks
    app.add_handler(CallbackQueryHandler(product_detail_callback, pattern=r'^detail:'))
    app.add_handler(CallbackQueryHandler(buy_callback, pattern=r'^buy:'))
    app.add_handler(CallbackQueryHandler(show_reviews_callback, pattern=r'^show_reviews:'))

    # reviews command
    app.add_handler(CommandHandler("reviews", reviews_handler))

    # admin UI and flows
    app.add_handler(CommandHandler("admin", admin_menu))
    # adding product flow (text)
    app.add_handler(MessageHandler(filters.Regex('^➕ Добавить товар$'), handle_add_product_flow))
    # message router for admin flows (edit/delete selection)
    app.add_handler(MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_IDS), message_router_for_admin_flows))
    # editing apply
    app.add_handler(MessageHandler(filters.TEXT & filters.User(user_id=ADMIN_IDS), editing_product_text))

    # list orders / stats accessible via buttons or commands
    app.add_handler(CommandHandler("stats", bot_stats_handler))
    app.add_handler(MessageHandler(filters.Regex('^📋 Список заказов$') & filters.User(user_id=ADMIN_IDS), list_orders_admin))
    app.add_handler(MessageHandler(filters.Regex('^📊 Статистика бота$') & filters.User(user_id=ADMIN_IDS), bot_stats_handler))


# -------------------- STARTUP / MAIN --------------------

def main():
    init_db()
    app = ApplicationBuilder().token(TG_BOT_TOKEN).build()

    register_handlers(app)

    logger.info("Bot starting (polling)...")
    # run polling (blocking)
    app.run_polling()

if __name__ == "__main__":
    main()
