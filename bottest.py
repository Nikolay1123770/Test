#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Metro Shop Telegram Bot — фиксированная версия
- Убраны FastAPI и webhooks.
- Добавлена периодическая проверка CloudTips (polling) через job_queue.
- PUBG ID принимается ТОЛЬКО после нажатия кнопки.
- Подходит для запуска на BotHost (polling).
Настройка CloudTips polling:
- Укажите CLOUDTIPS_POLL_URL — URL для опроса платежей (должен возвращать JSON список платежей с полями payload, status, amount, id)
- Или укажите CLOUDTIPS_API_KEY и CLOUDTIPS_POLL_URL (если требуется авторизация)
- POLL_INTERVAL задаёт интервал опроса в секундах (по умолчанию 30)
"""
import os
import sqlite3
import logging
from datetime import datetime, timedelta
from typing import List, Optional

import requests  # make sure requests is installed in environment
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InputMediaPhoto,
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import BadRequest

# --- Configuration (from env) ---
TG_BOT_TOKEN = os.getenv('TG_BOT_TOKEN', '8593344199:AAHQ2vA7XADGxkjV_xtwMSbOuRDA6ukR4Ik')
OWNER_ID = int(os.getenv('OWNER_ID', '8473513085'))
ADMIN_CHAT_ID = int(os.getenv('ADMIN_CHAT_ID', '-1003448809517'))
NOTIFY_CHAT_IDS = [int(x) for x in os.getenv('NOTIFY_CHAT_IDS', '-1003448809517').split(',') if x.strip()]
DB_PATH = os.getenv('DB_PATH', 'metro_shop.db')

ADMIN_IDS: List[int] = [OWNER_ID]
if os.getenv('ADMIN_IDS'):
    ADMIN_IDS = [int(x) for x in os.getenv('ADMIN_IDS').split(',') if x.strip()]

MAX_WORKERS_PER_ORDER = int(os.getenv('MAX_WORKERS_PER_ORDER', '3'))
WORKER_PERCENT = float(os.getenv('WORKER_PERCENT', '0.7'))

# CloudTips polling settings (user must configure REAL endpoint or script that returns payments)
CLOUDTIPS_POLL_URL = os.getenv('https://server-1-h1gw.onrender.com/payments')  # e.g. https://your-proxy.example.com/cloudtips/payments
CLOUDTIPS_API_KEY = os.getenv('CLOUDTIPS_API_KEY')    # optional auth header
POLL_INTERVAL = int(os.getenv('POLL_INTERVAL', '30'))  # seconds

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- DB helpers ---
def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        tg_id INTEGER UNIQUE,
        username TEXT,
        pubg_id TEXT,
        registered_at TEXT
    )
    ''')
    cur.execute('''
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT,
        price REAL NOT NULL,
        photo TEXT,
        created_at TEXT
    )
    ''')
    cur.execute('''
    CREATE TABLE IF NOT EXISTS product_photos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER,
        file_id TEXT,
        created_at TEXT
    )
    ''')
    cur.execute('''
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        product_id INTEGER,
        price REAL,
        status TEXT,
        created_at TEXT,
        payment_screenshot_file_id TEXT,
        pubg_id TEXT,
        admin_notes TEXT,
        started_at TEXT,
        done_at TEXT
    )
    ''')
    cur.execute('''
    CREATE TABLE IF NOT EXISTS order_workers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER,
        worker_id INTEGER,
        worker_username TEXT,
        taken_at TEXT
    )
    ''')
    cur.execute('''
    CREATE TABLE IF NOT EXISTS reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER,
        buyer_id INTEGER,
        worker_id INTEGER,
        rating INTEGER,
        text TEXT,
        created_at TEXT
    )
    ''')
    cur.execute('''
    CREATE TABLE IF NOT EXISTS worker_payouts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER,
        worker_id INTEGER,
        amount REAL,
        created_at TEXT
    )
    ''')
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

# --- UI / Keyboards ---
MAIN_MENU = ReplyKeyboardMarkup(
    [[KeyboardButton('📦 Каталог'), KeyboardButton('🧾 Мои заказы')],
     [KeyboardButton('🎮 Привязать PUBG ID'), KeyboardButton('📞 Поддержка')]],
    resize_keyboard=True,
)
CANCEL_BUTTON = ReplyKeyboardMarkup([[KeyboardButton('↩️ Назад')]], resize_keyboard=True)
ADMIN_PANEL_KB = ReplyKeyboardMarkup(
    [[KeyboardButton('➕ Добавить товар'), KeyboardButton('✏️ Редактировать товар'), KeyboardButton('🗑️ Удалить товар')],
     [KeyboardButton('📋 Список заказов'), KeyboardButton('↩️ Назад')]],
    resize_keyboard=True,
)

# --- (keep most helper functions unchanged, copied/adapted from original) ---
def format_performers_for_caption(order_id: int) -> str:
    rows = db_execute('SELECT worker_id, worker_username FROM order_workers WHERE order_id=? ORDER BY id', (order_id,), fetch=True)
    if not rows:
        return 'Исполнители: —'
    parts = []
    for worker_id, worker_username in rows:
        if worker_username:
            parts.append(f'@{worker_username}' if not worker_username.startswith('@') else worker_username)
        else:
            parts.append(str(worker_id))
    return 'Исполнители: ' + ', '.join(parts)

def build_admin_keyboard_for_order(order_id: int, order_status: str) -> InlineKeyboardMarkup:
    if order_status == 'pending_verification' or order_status == 'awaiting_screenshot':
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton('✅ Подтвердить оплату', callback_data=f'confirm:{order_id}'),
             InlineKeyboardButton('❌ Отклонить', callback_data=f'reject:{order_id}')],
        ])
    elif order_status in ('paid', 'in_progress', 'delivering'):
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton('🟢 Беру', callback_data=f'take:{order_id}'),
             InlineKeyboardButton('🔴 Сняться', callback_data=f'leave:{order_id}')],
            [InlineKeyboardButton('▶ Начать', callback_data=f'status:{order_id}:in_progress'),
             InlineKeyboardButton('📦 На выдаче', callback_data=f'status:{order_id}:delivering'),
             InlineKeyboardButton('🏁 Выполнено', callback_data=f'status:{order_id}:done')],
        ])
    elif order_status == 'done':
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton('ℹ️ Просмотреть', callback_data=f'detail_order:{order_id}')],
        ])
    else:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton('ℹ️ Просмотреть', callback_data=f'detail_order:{order_id}')],
        ])
    return kb

def build_caption_for_admin_message(order_id: int, buyer_tg: str, pubg_id: Optional[str], product: str, price: float, created_at: str, status: str, started_at: Optional[str] = None, done_at: Optional[str] = None) -> str:
    base_lines = [
        f'📦 Заказ #{order_id}',
        f'Пользователь: {buyer_tg}',
        f'PUBG ID: {pubg_id or "не указан"}',
        f'Товар: {product}',
        f'Сумма: {price}₽',
        f'Статус: {status}',
        f'Время: {created_at}',
    ]
    if started_at:
        base_lines.append(f'Начат: {started_at}')
    if done_at:
        base_lines.append(f'Выполнен: {done_at}')
    base_lines.append(format_performers_for_caption(order_id))
    return '\n'.join(base_lines)

# --- Handlers (kept largely the same, with PUBG ID flow modified) ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None:
        return
    db_execute('INSERT OR IGNORE INTO users (tg_id, username, registered_at) VALUES (?, ?, ?)',
               (user.id, user.username or '', now_iso()))
    text = (
        f"Привет, {user.first_name}!\n"
        "Добро пожаловать в Metro Shop — быстрый способ заказать сопровождение в Metro Royale.\n\n"
        "Привяжите PUBG ID через кнопку в меню ниже."
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=MAIN_MENU)

# --- NOTE: removed heuristic that auto-saved arbitrary text as PUBG ID ---
# New flow: user must press menu -> '🎮 Привязать PUBG ID' -> bot shows inline button "Ввести PUBG ID"
# When user presses that button, bot asks to send PUBG ID text, and only then it is saved.
async def show_pubg_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # triggered when user clicks '🎮 Привязать PUBG ID' from keyboard (text_router will route)
    if update.message is None:
        return
    kb = InlineKeyboardMarkup([[InlineKeyboardButton('Ввести PUBG ID', callback_data='enter_pubg')]])
    await update.message.reply_text('Нажмите кнопку, чтобы ввести PUBG ID (ник или цифры).', reply_markup=kb)

async def enter_pubg_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None:
        return
    await q.answer()
    user = q.from_user
    # set per-user state to expect pubg id in next text message
    context.user_data['awaiting_pubg'] = True
    try:
        await q.message.reply_text('Отправьте ваш PUBG ID (ник или цифры).', reply_markup=CANCEL_BUTTON)
    except Exception:
        pass

# Review flow, product flows and most other handlers copied/kept from original file (omitted here for brevity)
# For clarity I include the routing logic that changed and key functions (payment handling, buy flow, photo_router, admin decision, performer actions, order progress, payouts).
# (The rest of the handlers — add/edit products, reviews, stats etc — should be kept the same as in original file; for brevity they are preserved.)

# For brevity: reuse functions from original file where applicable (products_handler, product_detail_callback, etc.)
# We'll re-import them by copying necessary code pieces from your original file — but in this snippet they should be present unchanged.

# --- Products and buy flow (kept) ---
# (I preserved original products_handler, product_detail_callback, buy_callback — with minimal changes, see below.)

def _get_product_rating_and_count(pid: int):
    rows = db_execute('SELECT r.rating FROM reviews r JOIN orders o ON r.order_id=o.id WHERE o.product_id=?', (pid,), fetch=True)
    if not rows:
        avg = None
    else:
        vals = [r[0] for r in rows if r[0] is not None]
        avg = (sum(vals) / len(vals)) if vals else None
    completed_count_row = db_execute('SELECT COUNT(*) FROM orders WHERE product_id=? AND status=?', (pid, 'done'), fetch=True)
    completed_count = completed_count_row[0][0] if completed_count_row else 0
    return avg, completed_count

# (we assume products_handler/product_detail_callback are copied here unchanged from original file)

# buy_callback (minor note: order created as before)
async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    try:
        await query.answer()
    except BadRequest:
        pass
    data = query.data or ''
    if not data.startswith('buy:'):
        return
    _, pid_str = data.split(':', 1)
    try:
        pid = int(pid_str)
    except ValueError:
        return
    p = db_execute('SELECT id, name, price FROM products WHERE id=?', (pid,), fetch=True)
    if not p:
        try:
            await query.edit_message_text('Товар не найден.')
        except Exception:
            pass
        return
    prod_id, name, price = p[0]
    user = query.from_user
    db_execute('INSERT OR IGNORE INTO users (tg_id, username, registered_at) VALUES (?, ?, ?)', (user.id, user.username or '', now_iso()))
    user_row = db_execute('SELECT id, pubg_id FROM users WHERE tg_id=?', (user.id,), fetch=True)
    user_db_id = user_row[0][0]
    pubg_id = user_row[0][1]
    db_execute('INSERT INTO orders (user_id, product_id, price, status, created_at, pubg_id) VALUES (?, ?, ?, ?, ?, ?)',
               (user_db_id, prod_id, price, 'awaiting_screenshot', now_iso(), pubg_id))
    order_id = db_execute('SELECT id FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 1', (user_db_id,), fetch=True)[0][0]
    try:
        cloudtips_link = f"https://pay.cloudtips.ru/p/2842e969?amount={price}&payload={order_id}"
        await query.message.reply_text(
            f'Вы выбрали: {name} — {price}₽\n\n'
            '💳 *Оплата через CloudTips*\n'
            'Нажмите кнопку ниже, чтобы перейти к оплате.\n\n'
            'После оплаты отправьте *скриншот платежа*.\n'
            'Если вы не указали PUBG ID — привяжите его через меню.',
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Оплатить через CloudTips", url=cloudtips_link)]])
        )
    except Exception as e:
        logger.exception("CloudTips error: %s", e)

# Photo routing & payment_photo_handler (kept from original)
async def photo_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if msg is None:
        return
    user = msg.from_user
    if user is None:
        return
    if is_admin_tg(user.id) and context.user_data.get('product_flow'):
        flow = context.user_data.get('product_flow', {})
        if flow.get('stage') in ('photo', 'extra_photos'):
            await handle_add_product_flow(update, context)
            return
    if is_admin_tg(user.id) and context.user_data.get('edit_flow'):
        flow = context.user_data.get('edit_flow', {})
        if flow.get('stage') == 'editing_photo':
            await handle_edit_product_flow(update, context)
            return
    await payment_photo_handler(update, context)

async def payment_photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat and update.effective_chat.id == ADMIN_CHAT_ID:
        return
    if update.message is None:
        return
    message = update.message
    user = update.effective_user
    if user is None:
        return
    tg_id = user.id
    user_row = db_execute('SELECT id, pubg_id FROM users WHERE tg_id=?', (tg_id,), fetch=True)
    if not user_row:
        await message.reply_text('Сначала выберите товар в каталоге.', reply_markup=MAIN_MENU)
        return
    user_db_id, pubg_id = user_row[0]
    order_row = db_execute('SELECT id, product_id, price, created_at FROM orders WHERE user_id=? AND status=? ORDER BY id DESC LIMIT 1',
                           (user_db_id, 'awaiting_screenshot'), fetch=True)
    if not order_row:
        await message.reply_text('У вас нет активных заказов, ожидающих скриншота.', reply_markup=MAIN_MENU)
        return
    order_id, product_id, price, created_at = order_row[0]
    if not message.photo:
        await message.reply_text('Пожалуйста, отправьте изображение (скриншот оплаты).', reply_markup=MAIN_MENU)
        return
    photo = message.photo[-1]
    file_id = photo.file_id
    db_execute('UPDATE orders SET payment_screenshot_file_id=?, status=? WHERE id=?', (file_id, 'pending_verification', order_id))
    product = db_execute('SELECT name FROM products WHERE id=?', (product_id,), fetch=True)[0][0]
    tg_username = user.username or f'{user.first_name} {user.last_name or ""}'.strip()
    caption = build_caption_for_admin_message(order_id, f'@{tg_username}' if user.username else str(tg_id), pubg_id, product, price, created_at, 'pending_verification')
    kb = build_admin_keyboard_for_order(order_id, 'pending_verification')
    try:
        await context.bot.send_photo(chat_id=ADMIN_CHAT_ID, photo=file_id, caption=caption, reply_markup=kb)
        for nid in NOTIFY_CHAT_IDS:
            try:
                await context.bot.send_message(chat_id=nid, text=f'Новый заказ #{order_id} ожидает проверки. Проверьте в админ-группе.')
            except Exception:
                pass
        await message.reply_text('Скриншот отправлен админам для проверки. Ожидайте подтверждения.', reply_markup=MAIN_MENU)
    except Exception as e:
        logger.exception('Failed to send to admin group: %s', e)
        try:
            await context.bot.send_message(chat_id=OWNER_ID, text=f'Не удалось отправить заказ #{order_id} в админ-группу. Ошибка: {e}')
        except Exception:
            pass
        await message.reply_text('Не удалось отправить заказ в админ-группу. Свяжитесь с поддержкой.', reply_markup=MAIN_MENU)

# Admin confirm/reject, performer actions, order progress, payouts etc. — keep original implementations.
# For brevity in this snippet, assume those functions (admin_decision, performer_action, order_progress_callback,
# calculate_and_record_payouts, leave_review_callback, review_worker_callback, worker_stats_handler, etc.)
# are copied unchanged from your original file. They reference db_execute, build_caption_for_admin_message, etc.

# ---------------- CloudTips polling job ----------------
def poll_cloudtips_once(application) -> None:
    """
    Poll CLOUDTIPS_POLL_URL for payments.
    Expected: endpoint returns JSON list of payments with fields:
      - payload (order_id)
      - status (e.g. 'paid')
      - amount
      - id (payment id)
    If payments with status == 'paid' and payload matches existing order id -> mark order paid.
    NOTE: You must provide a real poll URL that returns payments in this format, or provide a small proxy
    service (e.g. on your server) that converts CloudTips webhook to a GET-able JSON feed.
    """
    if not CLOUDTIPS_POLL_URL:
        return
    headers = {}
    if CLOUDTIPS_API_KEY:
        headers['Authorization'] = f'Bearer {CLOUDTIPS_API_KEY}'
    try:
        resp = requests.get(CLOUDTIPS_POLL_URL, headers=headers, timeout=10)
        if resp.status_code != 200:
            logger.warning('CloudTips poll returned status %s', resp.status_code)
            return
        data = resp.json()
        if not isinstance(data, list):
            logger.warning('CloudTips poll returned unexpected JSON (not list).')
            return
        for payment in data:
            try:
                status = payment.get('status')
                payload = payment.get('payload')  # expected to contain order_id
                if not payload:
                    continue
                # payload may be str or dict; adapt
                if isinstance(payload, dict) and 'order_id' in payload:
                    order_id = int(payload['order_id'])
                else:
                    try:
                        order_id = int(str(payload))
                    except Exception:
                        continue
                if status == 'paid':
                    # check order status
                    row = db_execute('SELECT status, user_id FROM orders WHERE id=?', (order_id,), fetch=True)
                    if not row:
                        continue
                    cur_status, user_id = row[0]
                    if cur_status in ('paid', 'done'):
                        continue
                    # update order
                    db_execute("UPDATE orders SET status=?, admin_notes=? WHERE id=?", ('paid', 'Оплата подтверждена автоматически (CloudTips polling)', order_id))
                    # notify buyer and admins
                    tg_row = db_execute("SELECT tg_id FROM users WHERE id=?", (user_id,), fetch=True)
                    if tg_row:
                        tg_id = tg_row[0][0]
                        try:
                            application.bot.send_message(chat_id=tg_id, text=f"💳 Оплата подтверждена автоматически!\nВаш заказ #{order_id} оплачен.")
                        except Exception:
                            logger.exception('Failed to notify buyer after cloudtips poll')
                    try:
                        application.bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"🔔 Автоматически подтверждена оплата заказа #{order_id} (CloudTips polling).")
                    except Exception:
                        logger.exception('Failed to notify admin after cloudtips poll')
            except Exception:
                logger.exception('Error processing payment record from poll')
    except Exception:
        logger.exception('CloudTips polling request failed')

async def cloudtips_poll_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    # wrapper for job_queue
    app = context.application
    poll_cloudtips_once(app)

# --- Router for plain text messages (modified) ---
async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ignore admin group
    if update.effective_chat and update.effective_chat.id == ADMIN_CHAT_ID:
        return
    if update.message is None or update.message.text is None:
        return
    text = update.message.text.strip()
    user = update.effective_user

    # if user is in review_flow -> handle (kept)
    if context.user_data.get('review_flow'):
        await handle_review_flow(update, context)
        return

    # product add/edit flows for admin (kept)
    if context.user_data.get('product_flow'):
        await handle_add_product_flow(update, context)
        return
    if context.user_data.get('edit_flow'):
        await handle_edit_product_flow(update, context)
        return

    if text == '/admin':
        await admin_menu(update, context)
        return

    if text == '📦 Каталог':
        await products_handler(update, context)
        return
    if text == '🧾 Мои заказы':
        await my_orders(update, context)
        return
    if text == '🎮 Привязать PUBG ID':
        await show_pubg_button(update, context)
        return
    if text == '📞 Поддержка':
        await update.message.reply_text('Свяжитесь с владельцем: @zavik911', reply_markup=MAIN_MENU)
        return
    if text == '↩️ Назад':
        await update.message.reply_text('Вернулись в меню.', reply_markup=MAIN_MENU)
        # clear awaiting_pubg if user backed out
        context.user_data.pop('awaiting_pubg', None)
        return

    # If user is expected to send PUBG ID (after pressing button)
    if context.user_data.pop('awaiting_pubg', False):
        candidate = text.strip()
        # basic validation: no spaces, <= 64 chars
        if not candidate or ' ' in candidate or len(candidate) > 64:
            await update.message.reply_text('Неверный формат PUBG ID. Попробуйте ещё раз или нажмите ↩️ Назад.', reply_markup=CANCEL_BUTTON)
            return
        db_execute('INSERT OR IGNORE INTO users (tg_id, username, registered_at) VALUES (?, ?, ?)',
                   (user.id, user.username or '', now_iso()))
        db_execute('UPDATE users SET pubg_id=? WHERE tg_id=?', (candidate, user.id))
        await update.message.reply_text(f'PUBG ID сохранён: {candidate}', reply_markup=MAIN_MENU)
        return

    # admin panel buttons (kept)
    if text == '➕ Добавить товар' and is_admin_tg(user.id):
        start_product_flow(context.user_data)
        await update.message.reply_text('Добавление товара — шаг 1/4.\nВведите название товара или нажмите /cancel для отмены.', reply_markup=CANCEL_BUTTON)
        return
    # ... rest of admin routing (kept) ...
    if text == '✏️ Редактировать товар' and is_admin_tg(user.id):
        context.user_data['edit_flow'] = {'stage': 'select', 'product_id': None}
        prods = db_execute('SELECT id, name, price FROM products ORDER BY id', fetch=True)
        if not prods:
            await update.message.reply_text('Нет товаров для редактирования.', reply_markup=ADMIN_PANEL_KB)
            context.user_data.pop('edit_flow', None)
            return
        lines = [f'ID {pid}: {name} — {price}₽' for pid, name, price in prods]
        await update.message.reply_text('Выберите ID товара для редактирования:\n\n' + '\n'.join(lines), reply_markup=CANCEL_BUTTON)
        return

    # fallback
    await update.message.reply_text('Неизвестная команда. Выберите действие в меню.', reply_markup=MAIN_MENU)

# --- Build app and start polling ---
def build_app():
    init_db()
    app = ApplicationBuilder().token(TG_BOT_TOKEN).build()

    # routing
    app.add_handler(MessageHandler(filters.Chat(ADMIN_CHAT_ID) & filters.ALL, ignore_admin_group), group=0)
    app.add_handler(CommandHandler('start', start), group=1)
    app.add_handler(CommandHandler('worker', worker_stats_handler), group=1)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router), group=1)
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, photo_router), group=1)

    # callbacks (some callbacks added here; ensure corresponding functions are defined above)
    app.add_handler(CallbackQueryHandler(buy_callback, pattern=r'^buy:'), group=1)
    app.add_handler(CallbackQueryHandler(product_detail_callback, pattern=r'^detail:'), group=1)
    app.add_handler(CallbackQueryHandler(admin_decision, pattern=r'^(confirm:|reject:)'), group=2)
    app.add_handler(CallbackQueryHandler(performer_action, pattern=r'^(take:|leave:)'), group=2)
    app.add_handler(CallbackQueryHandler(order_progress_callback, pattern=r'^status:'), group=2)
    app.add_handler(CallbackQueryHandler(leave_review_callback, pattern=r'^leave_review:'), group=2)
    app.add_handler(CallbackQueryHandler(review_worker_callback, pattern=r'^review_worker:'), group=2)

    # new: PUBG ID entry callback
    app.add_handler(CallbackQueryHandler(enter_pubg_callback, pattern=r'^enter_pubg$'), group=1)

    # error handler
    app.add_error_handler(error_handler)

    # schedule CloudTips polling if configured
    if CLOUDTIPS_POLL_URL:
        logger.info('Scheduling CloudTips polling every %s seconds (url=%s)', POLL_INTERVAL, CLOUDTIPS_POLL_URL)
        app.job_queue.run_repeating(cloudtips_poll_job, interval=POLL_INTERVAL, first=10)

    return app

def main():
    app = build_app()
    logger.info('Starting bot (polling)...')
    app.run_polling(allowed_updates=None)  # polling mode suitable for BotHost

if __name__ == '__main__':
    main()
