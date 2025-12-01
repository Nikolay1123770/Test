#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Metro Shop Telegram Bot (enhanced bot.py)
Added features:
- Performer stats (/worker)
- Order progress statuses: in_progress, delivering, done
- Reviews per worker
- Product preview card with rating & completed count
- Worker payouts calculation & recording (worker_payouts)
- Support for multiple product photos (product_photos)
Requires: python-telegram-bot v20+
"""

import os
import sqlite3
import logging
from datetime import datetime
from typing import List, Optional

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

# --- CloudTips polling config (replaces webhook) ---
import requests
CLOUDTIPS_POLL_URL = os.getenv('CLOUDTIPS_POLL_URL', '')  # e.g. https://your-proxy.example.com/payments
CLOUDTIPS_API_KEY = os.getenv('CLOUDTIPS_API_KEY', '')    # optional
POLL_INTERVAL = int(os.getenv('POLL_INTERVAL', '30'))

def poll_cloudtips_once(application):
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
                payload = payment.get('payload')
                if not payload and isinstance(payment.get('data'), dict):
                    payload = payment.get('data').get('payload')
                if not payload:
                    continue
                try:
                    order_id = int(payload)
                except Exception:
                    continue
                if status == 'paid' and order_id is not None:
                    row = db_execute('SELECT status, user_id FROM orders WHERE id=?', (order_id,), fetch=True)
                    if not row:
                        continue
                    cur_status, user_id = row[0]
                    if cur_status in ('paid','done'):
                        continue
                    db_execute("UPDATE orders SET status=?, admin_notes=? WHERE id=?", ('paid', 'Оплата подтверждена автоматически (CloudTips polling)', order_id))
                    # notify buyer
                    tg_row = db_execute('SELECT tg_id FROM users WHERE id=?', (user_id,), fetch=True)
                    if tg_row:
                        try:
                            application.bot.send_message(chat_id=tg_row[0][0], text=f"💳 Оплата подтверждена автоматически!\nВаш заказ #{order_id} оплачен.")
                        except Exception:
                            logger.exception('Failed to notify buyer after cloudtips poll')
                    try:
                        application.bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"🔔 Автоматически подтверждена оплата заказа #{order_id} (CloudTips polling)." )
                    except Exception:
                        logger.exception('Failed to notify admin after cloudtips poll')
            except Exception:
                logger.exception('Error processing payment record from poll')
    except Exception:
        logger.exception('CloudTips polling request failed')

async def cloudtips_poll_job(context):
    app = context.application
    poll_cloudtips_once(app)

# PUBG ID via explicit button flow
async def enter_pubg_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None:
        return
    await q.answer()
    user = q.from_user
    context.user_data['awaiting_pubg'] = True
    try:
        await q.message.reply_text('Отправьте ваш PUBG ID (ник или цифры).', reply_markup=CANCEL_BUTTON)
    except Exception:
        pass


# --- Configuration ---
TG_BOT_TOKEN = os.getenv('TG_BOT_TOKEN', '8335008959:AAHgeYQ42a48SQT5CcIP5xGAY5ARaIuZesI')
OWNER_ID = int(os.getenv('OWNER_ID', '8473513085'))
ADMIN_CHAT_ID = int(os.getenv('ADMIN_CHAT_ID', '-1003448809517'))
NOTIFY_CHAT_IDS = [int(x) for x in os.getenv('NOTIFY_CHAT_IDS', '-1003448809517').split(',') if x.strip()]
DB_PATH = os.getenv('DB_PATH', 'metro_shop.db')

# bot-level admin ids (owner + optional extra)
ADMIN_IDS: List[int] = [OWNER_ID]
if os.getenv('ADMIN_IDS'):
    ADMIN_IDS = [int(x) for x in os.getenv('ADMIN_IDS').split(',') if x.strip()]

# Maximum number of performers per order
MAX_WORKERS_PER_ORDER = int(os.getenv('MAX_WORKERS_PER_ORDER', '3'))

# Percent to pay to workers (0.0 - 1.0). Will be split equally across workers assigned.
WORKER_PERCENT = float(os.getenv('WORKER_PERCENT', '0.7'))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# --- DB helpers ---
def init_db() -> None:
    """Create tables and new columns. Use safe ALTERs where possible."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Base tables (existing)
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

    # product_photos: optional multiple photos per product (file_id)
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
        admin_notes TEXT
    )
    ''')

    # add columns to orders if not exists: started_at, done_at
    try:
        cur.execute("ALTER TABLE orders ADD COLUMN started_at TEXT")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE orders ADD COLUMN done_at TEXT")
    except Exception:
        pass

    cur.execute('''
    CREATE TABLE IF NOT EXISTS order_workers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER,
        worker_id INTEGER,
        worker_username TEXT,
        taken_at TEXT
    )
    ''')

    # reviews per worker (rating + text)
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

    # worker payouts record
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


# --- Helper functions for order messages & performer list ---
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
    """
    Build inline keyboard for admin-group order message.
    - If status pending_verification -> admin confirm/reject.
    - If paid or in progress -> show performer take/leave + status change buttons.
    - If rejected/done -> only info.
    """
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


# --- Special handler: ignore any messages in admin group (so bot doesn't reply to normal texts there) ---
async def ignore_admin_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    return


# --- Admin product flow helpers (per-user state in context.user_data) ---
def start_product_flow(user_data: dict) -> None:
    user_data['product_flow'] = {'stage': 'name', 'data': {}}


def clear_product_flow(user_data: dict) -> None:
    user_data.pop('product_flow', None)


# --- Handlers ---
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


# --- Review flow handler (text-based parts) ---
async def handle_review_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Flow states stored in context.user_data['review_flow']:
     - stage: 'awaiting_rating' / 'awaiting_text'
     - order_id
     - worker_id
     - temp_rating
     - done_workers -> list of worker_ids already reviewed in this flow
    """
    msg = update.message
    if msg is None:
        return
    user = update.effective_user
    flow = context.user_data.get('review_flow')
    if not flow:
        return

    # cancel
    if msg.text and msg.text.strip().lower() in ['/cancel', '↩️ назад']:
        context.user_data.pop('review_flow', None)
        await msg.reply_text('Оставление отзыва отменено.', reply_markup=MAIN_MENU)
        return

    stage = flow.get('stage')
    if stage == 'awaiting_rating':
        text = (msg.text or '').strip()
        try:
            rating = int(text)
            if rating < 1 or rating > 5:
                raise ValueError()
        except Exception:
            await msg.reply_text('Неверный рейтинг. Отправьте число от 1 до 5.')
            return
        flow['temp_rating'] = rating
        flow['stage'] = 'awaiting_text'
        await msg.reply_text('Опционально: напишите текст отзыва или отправьте "Пропустить".', reply_markup=CANCEL_BUTTON)
        return

    if stage == 'awaiting_text':
        text = (msg.text or '').strip()
        text_value = ''
        if text.lower() not in ('пропустить', 'skip', ''):
            text_value = text
        order_id = flow['order_id']
        worker_id = flow['worker_id']
        buyer_row = db_execute('SELECT id FROM users WHERE tg_id=?', (user.id,), fetch=True)
        buyer_id = buyer_row[0][0] if buyer_row else None
        db_execute('INSERT INTO reviews (order_id, buyer_id, worker_id, rating, text, created_at) VALUES (?, ?, ?, ?, ?, ?)',
                   (order_id, buyer_id, worker_id, flow.get('temp_rating'), text_value, now_iso()))
        # mark done
        done_workers = flow.get('done_workers', [])
        done_workers.append(worker_id)
        flow['done_workers'] = done_workers
        # check if there are remaining workers to review (from order_workers)
        remaining = db_execute('SELECT worker_id, worker_username FROM order_workers WHERE order_id=? AND worker_id NOT IN ({seq})'.format(
            seq=','.join(['?'] * (len(done_workers))) if done_workers else '0'
        ), tuple([order_id] + done_workers), fetch=True) if done_workers else db_execute('SELECT worker_id, worker_username FROM order_workers WHERE order_id=?', (order_id,), fetch=True)

        # The above SQL is a bit awkward; do a simpler approach: fetch all workers, then filter in Python.
        all_ws = db_execute('SELECT worker_id, worker_username FROM order_workers WHERE order_id=? ORDER BY id', (order_id,), fetch=True)
        remaining_workers = [w for w in all_ws if w[0] not in done_workers]

        if remaining_workers:
            # ask for next worker
            next_worker = remaining_workers[0]
            flow['worker_id'] = next_worker[0]
            flow['stage'] = 'awaiting_rating'
            await msg.reply_text(f'Оцените исполнителя @{next_worker[1]} (1-5)', reply_markup=CANCEL_BUTTON)
            return
        else:
            # done with all reviews in this flow
            context.user_data.pop('review_flow', None)
            await msg.reply_text('Спасибо за отзывы! Они помогут другим пользователям и исполнителям.', reply_markup=MAIN_MENU)
            return


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ignore admin group messages
    if update.effective_chat and update.effective_chat.id == ADMIN_CHAT_ID:
        return

    if update.message is None or update.message.text is None:
        return
    text = update.message.text.strip()
    user = update.effective_user

    # If review flow active, handle it first
    if context.user_data.get('review_flow'):
        await handle_review_flow(update, context)
        return

    # If admin is in product add/edit flow, route to handlers for text inputs
    if context.user_data.get('product_flow'):
        await handle_add_product_flow(update, context)
        return
    if context.user_data.get('edit_flow'):
        await handle_edit_product_flow(update, context)
        return

    # admin command
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
        await update.message.reply_text('Отправьте ваш PUBG ID (ник или цифры), или нажмите ↩️ Назад.', reply_markup=CANCEL_BUTTON)
        return
    if text == '📞 Поддержка':
        bot_username = context.bot.username or 'админ'
        await update.message.reply_text('Свяжитесь с владельцем: @zavik911' + bot_username, reply_markup=MAIN_MENU)
        return
    if text == '↩️ Назад':
        await update.message.reply_text('Вернулись в меню.', reply_markup=MAIN_MENU)
        return

    # Admin panel buttons
    if text == '➕ Добавить товар' and is_admin_tg(user.id):
        start_product_flow(context.user_data)
        await update.message.reply_text('Добавление товара — шаг 1/4.\nВведите название товара или нажмите /cancel для отмены.', reply_markup=CANCEL_BUTTON)
        return

    if text == '✏️ Редактировать товар' and is_admin_tg(user.id):
        # start edit flow: ask product id (text flow)
        context.user_data['edit_flow'] = {'stage': 'select', 'product_id': None}
        prods = db_execute('SELECT id, name, price FROM products ORDER BY id', fetch=True)
        if not prods:
            await update.message.reply_text('Нет товаров для редактирования.', reply_markup=ADMIN_PANEL_KB)
            context.user_data.pop('edit_flow', None)
            return
        lines = [f'ID {pid}: {name} — {price}₽' for pid, name, price in prods]
        await update.message.reply_text('Выберите ID товара для редактирования:\n\n' + '\n'.join(lines), reply_markup=CANCEL_BUTTON)
        return

    if text == '🗑️ Удалить товар' and is_admin_tg(user.id):
        prods = db_execute('SELECT id, name, price FROM products ORDER BY id', fetch=True)
        if not prods:
            await update.message.reply_text('Нет товаров для удаления.', reply_markup=ADMIN_PANEL_KB)
            return
        lines = [f'ID {pid}: {name} — {price}₽' for pid, name, price in prods]
        await update.message.reply_text('Отправьте ID товара для удаления:\n\n' + '\n'.join(lines), reply_markup=CANCEL_BUTTON)
        context.user_data['awaiting_delete_id'] = True
        return

    if text == '📋 Список заказов' and is_admin_tg(user.id):
        await list_orders_admin(update, context)
        return

    # PUBG ID entry STRICT: only via button flow
    if context.user_data.pop('awaiting_pubg', False):
        candidate = text.strip()
        if not candidate or ' ' in candidate or len(candidate) > 64:
            await update.message.reply_text('Неверный формат PUBG ID. Попробуйте ещё раз или нажмите ↩️ Назад.', reply_markup=CANCEL_BUTTON)
            return
        db_execute('INSERT OR IGNORE INTO users (tg_id, username, registered_at) VALUES (?, ?, ?)',
                   (user.id, user.username or '', now_iso()))
        db_execute('UPDATE users SET pubg_id=? WHERE tg_id=?', (candidate, user.id))
        await update.message.reply_text(f'PUBG ID сохранён: {candidate}', reply_markup=MAIN_MENU)
        return

    # Admin delete id handling
    if context.user_data.pop('awaiting_delete_id', False) and is_admin_tg(user.id):
        try:
            did = int(text)
        except Exception:
            await update.message.reply_text('Неверный ID.', reply_markup=ADMIN_PANEL_KB)
            return
        row = db_execute('SELECT name FROM products WHERE id=?', (did,), fetch=True)
        if not row:
            await update.message.reply_text('Товар с таким ID не найден.', reply_markup=ADMIN_PANEL_KB)
            return
        db_execute('DELETE FROM products WHERE id=?', (did,))
        await update.message.reply_text(f'Товар #{did} удалён.', reply_markup=ADMIN_PANEL_KB)
        return

    # Admin add-product quick-format (legacy)
    if '|' in text and is_admin_tg(user.id):
        await add_product_text_handler(update, context)
        return

    await update.message.reply_text('Неизвестная команда. Выберите действие в меню.', reply_markup=MAIN_MENU)


# --- Add product interactive flow ---
async def handle_add_product_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles per-step interactive product addition: name -> price -> desc -> photo (and optional extra photos)"""
    msg = update.message
    if msg is None:
        return
    user = update.effective_user
    if not is_admin_tg(user.id):
        clear_product_flow(context.user_data)
        return

    flow = context.user_data.get('product_flow')
    if not flow:
        return

    stage = flow.get('stage')

    # Cancel
    if msg.text and msg.text.strip().lower() in ['/cancel', '↩️ назад']:
        clear_product_flow(context.user_data)
        await msg.reply_text('Добавление товара отменено.', reply_markup=ADMIN_PANEL_KB)
        return

    # Stage handlers
    if stage == 'name':
        name = (msg.text or '').strip()
        if not name:
            await msg.reply_text('Название не может быть пустым. Введите название товара.')
            return
        flow['data']['name'] = name
        flow['stage'] = 'price'
        await msg.reply_text('Шаг 2/5. Введите цену (числом), например: 300', reply_markup=CANCEL_BUTTON)
        return

    if stage == 'price':
        text = (msg.text or '').strip()
        try:
            price = float(text)
            if price < 0:
                raise ValueError()
        except Exception:
            await msg.reply_text('Неверная цена. Введите цену числом, например: 300')
            return
        flow['data']['price'] = price
        flow['stage'] = 'desc'
        await msg.reply_text('Шаг 3/5. Введите описание товара (короткое).', reply_markup=CANCEL_BUTTON)
        return

    if stage == 'desc':
        desc = (msg.text or '').strip()
        flow['data']['description'] = desc
        flow['stage'] = 'photo'
        await msg.reply_text('Шаг 4/5. Отправьте главное фото товара (как фото).', reply_markup=CANCEL_BUTTON)
        return

    if stage == 'photo':
        # This function can be triggered by photo_router when admin sends photo
        if not msg.photo:
            await msg.reply_text('Пожалуйста, отправьте изображение (как фото).')
            return
        photo = msg.photo[-1].file_id
        data = flow['data']
        name = data.get('name')
        price = data.get('price')
        desc = data.get('description')
        created = now_iso()
        db_execute('INSERT INTO products (name, description, price, photo, created_at) VALUES (?, ?, ?, ?, ?)',
                   (name, desc, price, photo, created))
        # get product id
        row = db_execute('SELECT id FROM products WHERE created_at=? ORDER BY id DESC LIMIT 1', (created,), fetch=True)
        pid = row[0][0] if row else None
        flow['data']['product_id'] = pid
        flow['stage'] = 'extra_photos'
        await msg.reply_text('Шаг 5/5 (опционально). Отправьте дополнительные фото по одному или нажмите ↩️ Назад, чтобы завершить.', reply_markup=CANCEL_BUTTON)
        return

    if stage == 'extra_photos':
        # accept photo and add to product_photos
        if msg.photo:
            photo = msg.photo[-1].file_id
            pid = flow['data'].get('product_id')
            if not pid:
                await msg.reply_text('Ошибка: не найден product_id.', reply_markup=ADMIN_PANEL_KB)
                clear_product_flow(context.user_data)
                return
            db_execute('INSERT INTO product_photos (product_id, file_id, created_at) VALUES (?, ?, ?)', (pid, photo, now_iso()))
            await msg.reply_text('Фото добавлено. Отправьте ещё фото или нажмите ↩️ Назад, чтобы завершить.', reply_markup=CANCEL_BUTTON)
            return
        else:
            # treat as finish if user pressed back or text
            clear_product_flow(context.user_data)
            await msg.reply_text(f'Товар добавлен: {flow["data"].get("name")} — {flow["data"].get("price")}₽', reply_markup=ADMIN_PANEL_KB)
            return


# --- Edit product interactive flow ---
async def handle_edit_product_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    edit_flow stages:
      - select : expecting product id (text)
      - choose_field : present inline buttons (handled via callback)
      - editing_name / editing_price / editing_desc / editing_photo : awaiting input
    """
    msg = update.message
    if msg is None:
        return
    user = update.effective_user
    if not is_admin_tg(user.id):
        context.user_data.pop('edit_flow', None)
        return

    flow = context.user_data.get('edit_flow', {})
    stage = flow.get('stage')

    # cancel
    if msg.text and msg.text.strip().lower() in ['/cancel', '↩️ назад']:
        context.user_data.pop('edit_flow', None)
        await msg.reply_text('Редактирование отменено.', reply_markup=ADMIN_PANEL_KB)
        return

    if stage == 'select':
        # expect product id (text path)
        try:
            pid = int((msg.text or '').strip())
        except Exception:
            await msg.reply_text('Неверный ID. Отправьте числовой ID товара, который хотите редактировать.')
            return
        row = db_execute('SELECT id, name, price, description FROM products WHERE id=?', (pid,), fetch=True)
        if not row:
            await msg.reply_text('Товар не найден. Попробуйте другой ID.')
            context.user_data.pop('edit_flow', None)
            return
        context.user_data['edit_flow']['product_id'] = pid
        context.user_data['edit_flow']['stage'] = 'choose_field'
        # show inline buttons for fields
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton('Название', callback_data=f'editfield:name:{pid}'),
             InlineKeyboardButton('Цена', callback_data=f'editfield:price:{pid}')],
            [InlineKeyboardButton('Описание', callback_data=f'editfield:desc:{pid}'),
             InlineKeyboardButton('Фото', callback_data=f'editfield:photo:{pid}')],
            [InlineKeyboardButton('Отмена', callback_data=f'editfield:cancel:{pid}')]
        ])
        await msg.reply_text(f'Выбран товар #{pid}. Выберите поле для редактирования.', reply_markup=kb)
        return

    # handle awaited text/photo for editing fields
    if stage in ('editing_name', 'editing_price', 'editing_desc'):
        pid = flow.get('product_id')
        if pid is None:
            context.user_data.pop('edit_flow', None)
            await msg.reply_text('Ошибка состояния. Попробуйте заново.', reply_markup=ADMIN_PANEL_KB)
            return
        if stage == 'editing_name':
            name = (msg.text or '').strip()
            if not name:
                await msg.reply_text('Название не может быть пустым. Введите название.')
                return
            db_execute('UPDATE products SET name=? WHERE id=?', (name, pid))
            await msg.reply_text(f'Название обновлено для #{pid}.', reply_markup=ADMIN_PANEL_KB)
        elif stage == 'editing_price':
            try:
                price = float((msg.text or '').strip())
                if price < 0:
                    raise ValueError()
            except Exception:
                await msg.reply_text('Неверная цена. Введите число, например: 300')
                return
            db_execute('UPDATE products SET price=? WHERE id=?', (price, pid))
            await msg.reply_text(f'Цена обновлена для #{pid}.', reply_markup=ADMIN_PANEL_KB)
        elif stage == 'editing_desc':
            desc = (msg.text or '').strip()
            db_execute('UPDATE products SET description=? WHERE id=?', (desc, pid))
            await msg.reply_text(f'Описание обновлено для #{pid}.', reply_markup=ADMIN_PANEL_KB)
        context.user_data.pop('edit_flow', None)
        return

    if stage == 'editing_photo':
        # expect photo (this will be routed from photo_router)
        if not msg.photo:
            await msg.reply_text('Пожалуйста, отправьте фото (в виде фото).')
            return
        pid = flow.get('product_id')
        if pid is None:
            context.user_data.pop('edit_flow', None)
            await msg.reply_text('Ошибка состояния. Попробуйте заново.', reply_markup=ADMIN_PANEL_KB)
            return
        file_id = msg.photo[-1].file_id
        db_execute('UPDATE products SET photo=? WHERE id=?', (file_id, pid))
        await msg.reply_text(f'Фото обновлено для #{pid}.', reply_markup=ADMIN_PANEL_KB)
        context.user_data.pop('edit_flow', None)
        return


# Callback to handle which field admin chooses to edit
async def editfield_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None:
        return
    await q.answer()
    data = q.data or ''
    if not data.startswith('editfield:'):
        return
    _, field, pid_str = data.split(':', 2)
    try:
        pid = int(pid_str)
    except ValueError:
        await q.edit_message_text('Неверный product id.')
        return
    user = q.from_user
    if not is_admin_tg(user.id):
        await q.answer(text='Только админы.', show_alert=True)
        return

    if field == 'cancel':
        context.user_data.pop('edit_flow', None)
        try:
            await q.edit_message_text('Редактирование отменено.', reply_markup=None)
        except Exception:
            pass
        return

    # set edit_flow state and ask for value accordingly
    context.user_data['edit_flow'] = {'stage': None, 'product_id': pid}
    if field == 'name':
        context.user_data['edit_flow']['stage'] = 'editing_name'
        try:
            await q.message.reply_text('Введите новое название товара (текст).', reply_markup=CANCEL_BUTTON)
        except Exception:
            pass
    elif field == 'price':
        context.user_data['edit_flow']['stage'] = 'editing_price'
        try:
            await q.message.reply_text('Введите новую цену (число).', reply_markup=CANCEL_BUTTON)
        except Exception:
            pass
    elif field == 'desc':
        context.user_data['edit_flow']['stage'] = 'editing_desc'
        try:
            await q.message.reply_text('Введите новое описание.', reply_markup=CANCEL_BUTTON)
        except Exception:
            pass
    elif field == 'photo':
        context.user_data['edit_flow']['stage'] = 'editing_photo'
        try:
            await q.message.reply_text('Отправьте новое фото (в виде фото).', reply_markup=CANCEL_BUTTON)
        except Exception:
            pass


# Callback to delete product by inline button (optional)
async def delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None:
        return
    await q.answer()
    data = q.data or ''
    if not data.startswith('delete:'):
        return
    _, pid_str = data.split(':', 1)
    try:
        pid = int(pid_str)
    except ValueError:
        await q.edit_message_text('Неверный product id.')
        return
    user = q.from_user
    if not is_admin_tg(user.id):
        await q.answer(text='Только админы.', show_alert=True)
        return
    row = db_execute('SELECT name FROM products WHERE id=?', (pid,), fetch=True)
    if not row:
        await q.edit_message_text('Товар не найден.')
        return
    db_execute('DELETE FROM products WHERE id=?', (pid,))
    try:
        await q.edit_message_text(f'Товар #{pid} удалён.')
    except Exception:
        pass


# New: callback to start edit flow (from product detail)
async def edit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None:
        return
    await q.answer()
    data = q.data or ''
    if not data.startswith('edit:'):
        return
    _, pid_str = data.split(':', 1)
    try:
        pid = int(pid_str)
    except ValueError:
        await q.edit_message_text('Неверный product id.')
        return
    user = q.from_user
    if not is_admin_tg(user.id):
        await q.answer(text='Только админы.', show_alert=True)
        return

    row = db_execute('SELECT id, name, price FROM products WHERE id=?', (pid,), fetch=True)
    if not row:
        try:
            await q.edit_message_text('Товар не найден.')
        except Exception:
            pass
        return

    # set edit_flow and show inline choose-field keyboard
    context.user_data['edit_flow'] = {'stage': 'choose_field', 'product_id': pid}
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton('Название', callback_data=f'editfield:name:{pid}'),
         InlineKeyboardButton('Цена', callback_data=f'editfield:price:{pid}')],
        [InlineKeyboardButton('Описание', callback_data=f'editfield:desc:{pid}'),
         InlineKeyboardButton('Фото', callback_data=f'editfield:photo:{pid}')],
        [InlineKeyboardButton('Отмена', callback_data=f'editfield:cancel:{pid}')]
    ])
    try:
        await q.message.reply_text(f'Редактирование товара #{pid}. Выберите поле для редактирования.', reply_markup=kb)
    except Exception:
        pass


# --- Products display and buy flows ---
def _get_product_rating_and_count(pid: int):
    """Compute average rating across reviews for workers on orders of this product and number of completed orders."""
    # average rating: all reviews for orders where product_id == pid
    rows = db_execute('SELECT r.rating FROM reviews r JOIN orders o ON r.order_id=o.id WHERE o.product_id=?', (pid,), fetch=True)
    if not rows:
        avg = None
    else:
        vals = [r[0] for r in rows if r[0] is not None]
        avg = (sum(vals) / len(vals)) if vals else None
    completed_count_row = db_execute('SELECT COUNT(*) FROM orders WHERE product_id=? AND status=?', (pid, 'done'), fetch=True)
    completed_count = completed_count_row[0][0] if completed_count_row else 0
    return avg, completed_count


async def products_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    products = db_execute('SELECT id, name, description, price, photo FROM products ORDER BY id', fetch=True)
    if not products:
        await update.message.reply_text('Каталог пуст. Админ может добавить товары.', reply_markup=MAIN_MENU)
        return

    for pid, name, desc, price, photo in products:
        avg, completed_count = _get_product_rating_and_count(pid)
        rating_line = f"⭐ {avg:.1f} (отзывы)" if avg is not None else "—"
        caption = f"🛒 *{name}*\n{desc or ''}\n\n💰 Цена: *{price}₽*\n{rating_line} • Выполнено: {completed_count}"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(text=f'Купить — {price}₽', callback_data=f'buy:{pid}'),
             InlineKeyboardButton(text='ℹ️ Подробнее', callback_data=f'detail:{pid}')]
        ])
        try:
            if photo:
                if update.message:
                    await update.message.reply_photo(photo=photo, caption=caption, reply_markup=kb, parse_mode='Markdown')
                else:
                    await context.bot.send_photo(chat_id=update.effective_chat.id, photo=photo, caption=caption, reply_markup=kb, parse_mode='Markdown')
            else:
                if update.message:
                    await update.message.reply_markdown(caption, reply_markup=kb)
                else:
                    await context.bot.send_message(chat_id=update.effective_chat.id, text=caption, reply_markup=kb)
        except Exception:
            try:
                await context.bot.send_message(chat_id=update.effective_chat.id, text=caption, reply_markup=kb)
            except Exception:
                logger.exception("Failed to send product %s", pid)

    if update.message:
        await update.message.reply_text('Выберите товар, чтобы купить, или вернитесь в меню.', reply_markup=MAIN_MENU)


# Product details callback
async def product_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None:
        return
    await q.answer()
    data = q.data or ''
    if not data.startswith('detail:'):
        return
    _, pid_str = data.split(':', 1)
    try:
        pid = int(pid_str)
    except ValueError:
        return
    row = db_execute('SELECT name, description, price, photo FROM products WHERE id=?', (pid,), fetch=True)
    if not row:
        try:
            await q.edit_message_text('Товар не найден.')
        except Exception:
            pass
        return
    name, desc, price, photo = row[0]
    avg, completed_count = _get_product_rating_and_count(pid)
    rating_line = f"⭐ {avg:.1f} (по отзывам)" if avg is not None else "Нет оценок"
    caption = f"*{name}*\n\n{desc or ''}\n\n💰 Цена: *{price}₽*\n{rating_line} • Выполнено: {completed_count}"

    # fetch extra photos
    photos = db_execute('SELECT file_id FROM product_photos WHERE product_id=? ORDER BY id', (pid,), fetch=True) or []
    file_ids = [p[0] for p in photos]
    # include main photo as first if exists
    if photo:
        if not file_ids or file_ids[0] != photo:
            media = [photo] + file_ids
        else:
            media = file_ids
    else:
        media = file_ids

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(text=f'Купить — {price}₽', callback_data=f'buy:{pid}'),
         InlineKeyboardButton(text='Редактировать', callback_data=f'edit:{pid}'),
         InlineKeyboardButton(text='Удалить', callback_data=f'delete:{pid}')]
    ])
    try:
        if media:
            # send media group first (if photo present)
            if len(media) == 1:
                await q.message.reply_photo(photo=media[0], caption=caption, parse_mode='Markdown', reply_markup=kb)
            else:
                # first photo with caption, others as media
                media_group = []
                for i, fid in enumerate(media):
                    if i == 0:
                        media_group.append(InputMediaPhoto(media=fid, caption=caption, parse_mode='Markdown'))
                    else:
                        media_group.append(InputMediaPhoto(media=fid))
                await q.message.reply_media_group(media=media_group)
                # also send inline keyboard as a separate message (since media_group doesn't accept reply_markup for group)
                await q.message.reply_text(' ', reply_markup=kb)
        else:
            await q.message.reply_markdown(caption, reply_markup=kb)
    except Exception:
        try:
            await q.edit_message_text(caption)
        except Exception:
            pass


async def my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None:
        return
    row = db_execute('SELECT id FROM users WHERE tg_id=?', (user.id,), fetch=True)
    if not row:
        await update.message.reply_text('Вы ещё не зарегистрированы.', reply_markup=MAIN_MENU)
        return
    user_db_id = row[0][0]
    rows = db_execute(
        'SELECT o.id, p.name, o.price, o.status FROM orders o JOIN products p ON o.product_id=p.id WHERE o.user_id=? ORDER BY o.id DESC LIMIT 50',
        (user_db_id,), fetch=True)
    if not rows:
        await update.message.reply_text('У вас пока нет заказов.', reply_markup=MAIN_MENU)
        return
    lines = []
    for oid, pname, price, status in rows:
        perf_rows = db_execute('SELECT worker_username FROM order_workers WHERE order_id=? ORDER BY id', (oid,), fetch=True)
        perflist = ', '.join([r[0] or str(r[0]) for r in perf_rows]) if perf_rows else '-'
        lines.append(f'#{oid} {pname} — {price}₽ — {status} — Исполнители: {perflist}')
    await update.message.reply_text('\n'.join(lines), reply_markup=MAIN_MENU)


# User pressed "Купить" inline button
# User pressed "Купить" inline button
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

    # --- FIXED: this block MUST be indented ---
    db_execute(
        'INSERT OR IGNORE INTO users (tg_id, username, registered_at) VALUES (?, ?, ?)',
        (user.id, user.username or '', now_iso())
    )
    user_row = db_execute(
        'SELECT id, pubg_id FROM users WHERE tg_id=?',
        (user.id,), fetch=True
    )
    user_db_id = user_row[0][0]
    pubg_id = user_row[0][1]

    # create new order
    db_execute(
        'INSERT INTO orders (user_id, product_id, price, status, created_at, pubg_id) VALUES (?, ?, ?, ?, ?, ?)',
        (user_db_id, prod_id, price, 'awaiting_screenshot', now_iso(), pubg_id)
    )

    # get inserted order id
    order_id = db_execute(
        'SELECT id FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 1',
        (user_db_id,), fetch=True
    )[0][0]

    # --- CloudTips dynamic payment link ---
    try:
        cloudtips_link = (
            f"https://pay.cloudtips.ru/p/2842e969?"
            f"amount={price}&payload={order_id}"
        )

        await query.message.reply_text(
            f'Вы выбрали: {name} — {price}₽\n\n'
            '💳 *Оплата через CloudTips*\n'
            'Нажмите кнопку ниже, чтобы перейти к оплате.\n\n'
            'После оплаты отправьте *скриншот платежа*.\n'
            'Если вы не указали PUBG ID — добавьте его в сообщении.',
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Оплатить через CloudTips", url=cloudtips_link)]
            ])
        )

    except Exception as e:
        print("CloudTips error:", e)
        pass





# --- Photo routing: either admin product-photo flows OR payment screenshots ---
async def photo_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Route incoming photos:
    - If the user is an admin and is in product_flow or edit_flow expecting a photo -> forward to those handlers.
    - Otherwise treat as payment screenshot.
    """
    msg = update.message
    if msg is None:
        return
    user = msg.from_user
    if user is None:
        return

    # If admin is adding a product and expecting photo -> handle there
    if is_admin_tg(user.id) and context.user_data.get('product_flow'):
        flow = context.user_data.get('product_flow', {})
        if flow.get('stage') in ('photo', 'extra_photos'):
            await handle_add_product_flow(update, context)
            return

    # If admin is editing and expecting photo -> handle edit flow
    if is_admin_tg(user.id) and context.user_data.get('edit_flow'):
        flow = context.user_data.get('edit_flow', {})
        if flow.get('stage') == 'editing_photo':
            await handle_edit_product_flow(update, context)
            return

    # Otherwise treat photo as payment screenshot
    await payment_photo_handler(update, context)


async def payment_photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ignore if in admin chat (prevents users spamming there)
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

    # Build caption and keyboard (confirm/reject)
    caption = build_caption_for_admin_message(order_id, f'@{tg_username}' if user.username else str(tg_id), pubg_id, product, price, created_at, 'pending_verification')
    kb = build_admin_keyboard_for_order(order_id, 'pending_verification')

    # Send to admin group. If bot not in group -> log and notify owner
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


# Admin decision: confirm or reject payment
async def admin_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    try:
        await query.answer()
    except BadRequest:
        pass

    data = query.data or ''
    if not (data.startswith('confirm:') or data.startswith('reject:')):
        return
    action, oid_str = data.split(':', 1)
    try:
        order_id = int(oid_str)
    except ValueError:
        return

    user = query.from_user
    if not is_admin_tg(user.id):
        try:
            await query.answer(text='Только админы могут подтверждать/отклонять оплату.', show_alert=True)
        except Exception:
            pass
        return

    order = db_execute('SELECT user_id, product_id, price, payment_screenshot_file_id, created_at FROM orders WHERE id=?', (order_id,), fetch=True)
    if not order:
        try:
            await query.answer(text='Заказ не найден.', show_alert=True)
        except Exception:
            pass
        return

    user_id, product_id, price, file_id, created_at = order[0]
    buyer_row = db_execute('SELECT tg_id, username, pubg_id FROM users WHERE id=?', (user_id,), fetch=True)
    if not buyer_row:
        buyer_tg = str(user_id)
        pubg_id = None
    else:
        buyer_tg = f"@{buyer_row[0][1]}" if buyer_row[0][1] else str(buyer_row[0][0])
        pubg_id = buyer_row[0][2]

    product_name = db_execute('SELECT name FROM products WHERE id=?', (product_id,), fetch=True)[0][0]

    if action == 'confirm':
        db_execute('UPDATE orders SET status=?, admin_notes=? WHERE id=?', ('paid', f'Оплачен и подтверждён админом {user.id}', order_id))
        caption = build_caption_for_admin_message(order_id, buyer_tg, pubg_id, product_name, price, created_at, 'paid')
        kb = build_admin_keyboard_for_order(order_id, 'paid')
        try:
            await query.edit_message_caption(caption, reply_markup=kb)
        except Exception:
            try:
                await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=caption, reply_markup=kb)
            except Exception:
                logger.exception('Failed to update admin message after confirm')
        try:
            await context.bot.send_message(chat_id=buyer_row[0][0], text=(f'Ваш заказ #{order_id} на \"{product_name}\" оплачен и подтверждён. Ожидайте исполнителей.'))
        except Exception:
            logger.warning('Failed to notify buyer')
        for nid in NOTIFY_CHAT_IDS:
            try:
                await context.bot.send_message(chat_id=nid, text=f'Заказ #{order_id} подтверждён. Ожидаем исполнителей.')
            except Exception:
                pass

    else:  # reject
        db_execute('UPDATE orders SET status=?, admin_notes=? WHERE id=?', ('rejected', f'Отклонён админом {user.id}', order_id))
        caption = build_caption_for_admin_message(order_id, buyer_tg, pubg_id, product_name, price, created_at, 'rejected')
        try:
            await query.edit_message_caption(caption)
        except Exception:
            try:
                await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=caption)
            except Exception:
                pass
        try:
            await context.bot.send_message(chat_id=buyer_row[0][0], text=(f'Ваш заказ #{order_id} был отклонён администратором. Пожалуйста, свяжитесь с поддержкой.'))
        except Exception:
            logger.warning('Failed to notify buyer')


# Performer actions: take or leave
async def performer_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    try:
        await query.answer()
    except BadRequest:
        pass

    data = query.data or ''
    if not (data.startswith('take:') or data.startswith('leave:')):
        return
    action, oid_str = data.split(':', 1)
    try:
        order_id = int(oid_str)
    except ValueError:
        return

    user = query.from_user
    worker_id = user.id
    worker_username = user.username or f'{user.first_name} {user.last_name or ""}'.strip()

    order_row = db_execute('SELECT status, product_id, price, created_at FROM orders WHERE id=?', (order_id,), fetch=True)
    if not order_row:
        try:
            await query.answer(text='Заказ не найден.', show_alert=True)
        except Exception:
            pass
        return
    status, product_id, price, created_at = order_row[0]
    if status != 'paid' and status != 'in_progress' and status != 'delivering':
        try:
            await query.answer(text='Этот функционал доступен только после подтверждения оплаты.', show_alert=True)
        except Exception:
            pass
        return

    current = db_execute('SELECT worker_id FROM order_workers WHERE order_id=?', (order_id,), fetch=True) or []
    current_ids = [r[0] for r in current]

    if action == 'take':
        if worker_id in current_ids:
            try:
                await query.answer(text='Вы уже взяли этот заказ.', show_alert=True)
            except Exception:
                pass
            return
        if len(current_ids) >= MAX_WORKERS_PER_ORDER:
            try:
                await query.answer(text=f'Невозможно взять — максимум {MAX_WORKERS_PER_ORDER} исполнителей уже заняты.', show_alert=True)
            except Exception:
                pass
            return
        db_execute('INSERT INTO order_workers (order_id, worker_id, worker_username, taken_at) VALUES (?, ?, ?, ?)',
                   (order_id, worker_id, worker_username, now_iso()))
        try:
            await query.answer(text='Вы добавлены в исполнители.', show_alert=False)
        except Exception:
            pass

    else:  # leave
        if worker_id not in current_ids:
            try:
                await query.answer(text='Вы не являетесь исполнителем этого заказа.', show_alert=True)
            except Exception:
                pass
            return
        db_execute('DELETE FROM order_workers WHERE order_id=? AND worker_id=?', (order_id, worker_id))
        try:
            await query.answer(text='Вы сняты с выполнения заказа.', show_alert=False)
        except Exception:
            pass

    # update admin message caption
    buyer_row = db_execute('SELECT u.tg_id, u.username, u.pubg_id, p.name FROM orders o JOIN users u ON o.user_id=u.id JOIN products p ON o.product_id=p.id WHERE o.id=?', (order_id,), fetch=True)
    if buyer_row:
        buyer_tg_id, buyer_username, pubg_id, product_name = buyer_row[0]
        buyer_tg = f'@{buyer_username}' if buyer_username else str(buyer_tg_id)
    else:
        buyer_tg = 'неизвестен'
        pubg_id = None
        product_name = db_execute('SELECT name FROM products WHERE id=(SELECT product_id FROM orders WHERE id=?)', (order_id,), fetch=True)[0][0]
    caption = build_caption_for_admin_message(order_id, buyer_tg, pubg_id, product_name, price, created_at, 'paid')
    kb = build_admin_keyboard_for_order(order_id, 'paid')

    try:
        await query.edit_message_caption(caption, reply_markup=kb)
    except Exception:
        try:
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=caption, reply_markup=kb)
        except Exception:
            logger.exception('Failed to update admin message after performer action')


# Order progress callback: in_progress / delivering / done
async def order_progress_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None:
        return
    await q.answer()
    data = q.data or ''
    if not data.startswith('status:'):
        return
    _, oid_str, new_status = data.split(':', 2)
    try:
        order_id = int(oid_str)
    except ValueError:
        return

    user = q.from_user
    worker_id = user.id

    # only workers assigned to order can change its progress
    assigned = db_execute('SELECT worker_id FROM order_workers WHERE order_id=?', (order_id,), fetch=True) or []
    assigned_ids = [r[0] for r in assigned]
    if worker_id not in assigned_ids and not is_admin_tg(user.id):
        try:
            await q.answer(text='Только назначенные исполнители (или админ) могут менять статус.', show_alert=True)
        except Exception:
            pass
        return

    # get order
    row = db_execute('SELECT status, user_id, product_id, price, created_at FROM orders WHERE id=?', (order_id,), fetch=True)
    if not row:
        try:
            await q.answer(text='Заказ не найден.', show_alert=True)
        except Exception:
            pass
        return
    old_status, user_id, product_id, price, created_at = row[0]

    # Update timestamps depending on new_status
    now = now_iso()
    if new_status == 'in_progress':
        db_execute('UPDATE orders SET status=?, started_at=? WHERE id=?', (new_status, now, order_id))
    elif new_status == 'delivering':
        db_execute('UPDATE orders SET status=? WHERE id=?', (new_status, order_id))
    elif new_status == 'done':
        db_execute('UPDATE orders SET status=?, done_at=? WHERE id=?', (new_status, now, order_id))
    else:
        db_execute('UPDATE orders SET status=? WHERE id=?', (new_status, order_id))

    # prepare caption update for admin group
    buyer_row = db_execute('SELECT tg_id, username, pubg_id FROM users WHERE id=?', (user_id,), fetch=True)
    if buyer_row:
        buyer_tg = f"@{buyer_row[0][1]}" if buyer_row[0][1] else str(buyer_row[0][0])
        pubg_id = buyer_row[0][2]
    else:
        buyer_tg = str(user_id)
        pubg_id = None
    product_name = db_execute('SELECT name FROM products WHERE id=?', (product_id,), fetch=True)[0][0]

    # update admin message
    status_row = db_execute('SELECT status, started_at, done_at FROM orders WHERE id=?', (order_id,), fetch=True)[0]
    status_val, started_at, done_at = status_row
    caption = build_caption_for_admin_message(order_id, buyer_tg, pubg_id, product_name, price, created_at, status_val, started_at, done_at)
    kb = build_admin_keyboard_for_order(order_id, status_val)
    try:
        await q.edit_message_caption(caption, reply_markup=kb)
    except Exception:
        try:
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=caption, reply_markup=kb)
        except Exception:
            logger.exception('Failed to update admin message after status change')

    try:
        # notify buyer about status change
        await context.bot.send_message(chat_id=buyer_row[0][0], text=f'Статус вашего заказа #{order_id} изменён: {status_val}')
    except Exception:
        logger.warning('Failed to notify buyer of status change')

    # If done => calculate payouts and trigger review flow
    if new_status == 'done':
        await calculate_and_record_payouts(order_id, context)
        # ask buyer to leave reviews for workers
        # fetch buyer tg_id
        buyer_tg_id = buyer_row[0][0] if buyer_row else None
        if buyer_tg_id:
            # fetch workers
            workers = db_execute('SELECT worker_id, worker_username FROM order_workers WHERE order_id=? ORDER BY id', (order_id,), fetch=True)
            if workers:
                # send a message with a button to start reviews
                kb2 = InlineKeyboardMarkup([[InlineKeyboardButton('Оставить отзыв', callback_data=f'leave_review:{order_id}')]])
                try:
                    await context.bot.send_message(chat_id=buyer_tg_id, text=f'Ваш заказ #{order_id} выполнен. Пожалуйста, оцените исполнителей.', reply_markup=kb2)
                except Exception:
                    logger.warning('Failed to prompt buyer for reviews')


async def calculate_and_record_payouts(order_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Compute payouts for workers assigned to the order, record in worker_payouts,
    and notify admins + workers.
    Split equally between assigned workers.
    """
    order = db_execute('SELECT price FROM orders WHERE id=?', (order_id,), fetch=True)
    if not order:
        return
    price = order[0][0]
    workers = db_execute('SELECT worker_id, worker_username FROM order_workers WHERE order_id=? ORDER BY id', (order_id,), fetch=True) or []
    if not workers:
        # no workers assigned: notify owner
        try:
            await context.bot.send_message(chat_id=OWNER_ID, text=f'Заказ #{order_id} отмечен как выполненный, но исполнителей не найдено для выплаты.')
        except Exception:
            pass
        return
    num = len(workers)
    total_for_workers = round(price * WORKER_PERCENT, 2)
    per_worker = round(total_for_workers / num, 2) if num > 0 else 0.0
    store = []
    for w in workers:
        wid = w[0]
        db_execute('INSERT INTO worker_payouts (order_id, worker_id, amount, created_at) VALUES (?, ?, ?, ?)',
                   (order_id, wid, per_worker, now_iso()))
        store.append((wid, per_worker, w[1] or ''))
    # notify admin(s) about payouts
    summary_lines = [f'Заказ #{order_id} выполнен — общая сумма: {price}₽', f'Всего исполнителей: {num}', f'Доля исполнителей (в сумме): {total_for_workers}₽', 'Выплаты:']
    for wid, amount, wname in store:
        summary_lines.append(f'- @{wname or str(wid)}: {amount}₽')
    summary = '\n'.join(summary_lines)
    try:
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=summary)
    except Exception:
        try:
            await context.bot.send_message(chat_id=OWNER_ID, text=summary)
        except Exception:
            pass

    # notify each worker privately
    for wid, amount, wname in store:
        try:
            await context.bot.send_message(chat_id=wid, text=f'Заказ #{order_id} выполнен. Ваша выплата: {amount}₽ (список выплат доступен админам).')
        except Exception:
            logger.warning('Failed to notify worker %s', wid)


# Callback to open review flow (buttons)
async def leave_review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None:
        return
    await q.answer()
    data = q.data or ''
    if not data.startswith('leave_review:'):
        return
    _, oid_str = data.split(':', 1)
    try:
        order_id = int(oid_str)
    except ValueError:
        return
    # fetch workers for this order
    workers = db_execute('SELECT worker_id, worker_username FROM order_workers WHERE order_id=? ORDER BY id', (order_id,), fetch=True)
    if not workers:
        try:
            await q.message.reply_text('На этот заказ нет назначенных исполнителей.')
        except Exception:
            pass
        return
    # If only one worker -> ask rating directly
    if len(workers) == 1:
        wid, wname = workers[0]
        context.user_data['review_flow'] = {'stage': 'awaiting_rating', 'order_id': order_id, 'worker_id': wid, 'done_workers': []}
        try:
            await q.message.reply_text(f'Оцените исполнителя @{wname} (1-5)', reply_markup=CANCEL_BUTTON)
        except Exception:
            pass
        return
    # multiple workers -> present inline list to choose whom to review (or do all sequentially)
    kb_rows = []
    for wid, wname in workers:
        kb_rows.append([InlineKeyboardButton(text=f'@{wname}', callback_data=f'review_worker:{order_id}:{wid}')])
    try:
        await q.message.reply_text('Выберите исполнителя для отзыва (можно повторять для всех):', reply_markup=InlineKeyboardMarkup(kb_rows))
    except Exception:
        pass


# callback when user selects a worker to review
async def review_worker_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None:
        return
    await q.answer()
    data = q.data or ''
    if not data.startswith('review_worker:'):
        return
    _, oid_str, wid_str = data.split(':', 2)
    try:
        order_id = int(oid_str)
        worker_id = int(wid_str)
    except ValueError:
        return
    # store flow
    context.user_data['review_flow'] = {'stage': 'awaiting_rating', 'order_id': order_id, 'worker_id': worker_id, 'done_workers': []}
    # fetch worker username
    row = db_execute('SELECT worker_username FROM order_workers WHERE order_id=? AND worker_id=?', (order_id, worker_id), fetch=True)
    wname = row[0][0] if row else str(worker_id)
    try:
        await q.message.reply_text(f'Оцените исполнителя @{wname} (1-5)', reply_markup=CANCEL_BUTTON)
    except Exception:
        pass


# Admin panel and small admin helpers
async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin_tg(user.id):
        if update.message:
            await update.message.reply_text('Только админам.')
        return
    if update.message:
        await update.message.reply_text('Панель администратора:', reply_markup=ADMIN_PANEL_KB)


async def add_product_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # legacy 'price|name|desc' format
    if update.message is None:
        return
    user = update.effective_user
    if not is_admin_tg(user.id):
        return
    text = (update.message.text or '').strip()
    if not text or '|' not in text:
        await update.message.reply_text('Использование для админа: <цена>|<название>|<описание>', reply_markup=ADMIN_PANEL_KB)
        return
    try:
        price_str, name, desc = [x.strip() for x in text.split('|', 2)]
        price = float(price_str)
    except Exception:
        await update.message.reply_text('Неверный формат. Пример: 300|Сопровождение|Быстрое сопровождение', reply_markup=ADMIN_PANEL_KB)
        return
    db_execute('INSERT INTO products (name, description, price, created_at) VALUES (?, ?, ?, ?)',
               (name, desc, price, now_iso()))
    await update.message.reply_text(f'Товар добавлен: {name} — {price}₽', reply_markup=MAIN_MENU)


async def list_orders_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin_tg(user.id):
        if update.message:
            await update.message.reply_text('Только админам.')
        return
    rows = db_execute(
        'SELECT o.id, u.tg_id, u.pubg_id, p.name, o.price, o.status, o.created_at FROM orders o JOIN users u ON o.user_id=u.id JOIN products p ON o.product_id=p.id ORDER BY o.id DESC LIMIT 50',
        fetch=True)
    if not rows:
        await update.message.reply_text('Заказов нет.', reply_markup=MAIN_MENU)
        return
    text_lines = []
    for r in rows:
        oid, tg_id, pubg_id, pname, price, status, created = r
        perf_rows = db_execute('SELECT worker_username FROM order_workers WHERE order_id=? ORDER BY id', (oid,), fetch=True)
        perflist = ', '.join([pr[0] or str(pr[0]) for pr in perf_rows]) if perf_rows else '-'
        text_lines.append(f'#{oid} {pname} {price}₽ {status} tg:{tg_id} pubg:{pubg_id or "-"} — Исполнители: {perflist} — {created}')
    big = '\n'.join(text_lines)
    if len(big) <= 4000:
        await update.message.reply_text(big, reply_markup=MAIN_MENU)
    else:
        parts = [big[i:i+3500] for i in range(0, len(big), 3500)]
        for p in parts:
            await update.message.reply_text(p)
        await update.message.reply_text('Конец списка.', reply_markup=MAIN_MENU)


# New admin helper: set photo for product (reply to photo with /setphoto <id>)
async def setphoto_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin_tg(user.id):
        return
    msg = update.message
    if msg is None:
        return
    if not msg.reply_to_message or not msg.reply_to_message.photo:
        await msg.reply_text('Ответьте командой на сообщение с фото товара, например: /setphoto 3')
        return

    args = context.args or []
    if not args:
        await msg.reply_text('Использование: /setphoto <product_id> (в ответ на фото)')
        return
    try:
        pid = int(args[0])
    except ValueError:
        await msg.reply_text('Неверный product_id')
        return

    photo = msg.reply_to_message.photo[-1]
    file_id = photo.file_id

    db_execute('UPDATE products SET photo=? WHERE id=?', (file_id, pid))
    await msg.reply_text(f'Фото установлено для товара {pid}', reply_markup=ADMIN_PANEL_KB)


# Command /add <name> <price> <description> (admin only)
async def add_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin_tg(user.id):
        return
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text('Использование: /add <название> <цена> [описание]')
        return
    name = args[0]
    try:
        price = float(args[1])
    except Exception:
        await update.message.reply_text('Цена должна быть числом')
        return
    desc = ' '.join(args[2:]) if len(args) > 2 else ''
    db_execute('INSERT INTO products (name, description, price, created_at) VALUES (?, ?, ?, ?)', (name, desc, price, now_iso()))
    await update.message.reply_text(f'Товар добавлен: {name} — {price}₽', reply_markup=ADMIN_PANEL_KB)


# Worker stats command (/worker)
async def worker_stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None:
        return
    wid = user.id
    # total taken
    total_taken_row = db_execute('SELECT COUNT(*) FROM order_workers WHERE worker_id=?', (wid,), fetch=True)
    total_taken = total_taken_row[0][0] if total_taken_row else 0
    # total completed (orders where this worker is in order_workers and order status == done)
    total_done_row = db_execute('SELECT COUNT(DISTINCT o.id) FROM orders o JOIN order_workers w ON o.id=w.order_id WHERE w.worker_id=? AND o.status=?', (wid, 'done'), fetch=True)
    total_done = total_done_row[0][0] if total_done_row else 0
    # avg time: for orders where worker took and order done -> average(done_at - taken_at)
    rows = db_execute('SELECT o.created_at, o.started_at, o.done_at, w.taken_at FROM orders o JOIN order_workers w ON o.id=w.order_id WHERE w.worker_id=? AND o.status=?', (wid, 'done'), fetch=True)
    avg_secs = None
    if rows:
        deltas = []
        for created_at, started_at, done_at, taken_at in rows:
            try:
                dt_taken = datetime.fromisoformat(taken_at) if taken_at else None
                dt_done = datetime.fromisoformat(done_at) if done_at else None
                if dt_taken and dt_done:
                    delta = (dt_done - dt_taken).total_seconds()
                    if delta >= 0:
                        deltas.append(delta)
            except Exception:
                pass
        if deltas:
            avg_secs = sum(deltas) / len(deltas)
    avg_time = f"{int(avg_secs//60)} мин" if avg_secs else "—"

    # average rating for this worker
    rating_row = db_execute('SELECT AVG(rating) FROM reviews WHERE worker_id=?', (wid,), fetch=True)
    avg_rating = rating_row[0][0] if rating_row and rating_row[0][0] is not None else None

    text_lines = [
        f'🧾 Статистика исполнителя @{user.username or user.first_name}',
        f'Взято заказов: {total_taken}',
        f'Выполнено: {total_done}',
        f'Среднее время выполнения: {avg_time}',
        f'Средний рейтинг: {avg_rating:.2f}' if avg_rating else 'Средний рейтинг: —',
    ]
    await update.message.reply_text('\n'.join(text_lines), reply_markup=MAIN_MENU)


# Global error handler
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    try:
        app = context.application
        await app.bot.send_message(chat_id=OWNER_ID, text=f'Error: {context.error}')
    except Exception:
        pass
def build_app():
    init_db()
    app = ApplicationBuilder().token(TG_BOT_TOKEN).build()

    # ignore messages in admin group (keeps bot quiet there)
    app.add_handler(MessageHandler(filters.Chat(ADMIN_CHAT_ID) & filters.ALL, ignore_admin_group), group=0)

    # user flows
    app.add_handler(CommandHandler('start', start), group=1)
    app.add_handler(CommandHandler('worker', worker_stats_handler), group=1)  # new
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router), group=1)

    # photo router (routes admin product photos -> product flows, else -> payment handler)
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, photo_router), group=1)

    # callbacks for product browsing / buy / details
    app.add_handler(CallbackQueryHandler(buy_callback, pattern=r'^buy:'), group=1)
    app.add_handler(CallbackQueryHandler(product_detail_callback, pattern=r'^detail:'), group=1)

    # admin / performer callbacks
    app.add_handler(CallbackQueryHandler(admin_decision, pattern=r'^(confirm:|reject:)'), group=2)
    app.add_handler(CallbackQueryHandler(performer_action, pattern=r'^(take:|leave:)'), group=2)
    app.add_handler(CallbackQueryHandler(order_progress_callback, pattern=r'^status:'), group=2)
    app.add_handler(CallbackQueryHandler(leave_review_callback, pattern=r'^leave_review:'), group=2)
    app.add_handler(CallbackQueryHandler(review_worker_callback, pattern=r'^review_worker:'), group=2)

    # product edit/delete callbacks
    app.add_handler(CallbackQueryHandler(editfield_callback, pattern=r'^editfield:'), group=2)
    app.add_handler(CallbackQueryHandler(delete_callback, pattern=r'^delete:'), group=2)
    app.add_handler(CallbackQueryHandler(edit_callback, pattern=r'^edit:'), group=2)  # opens edit flow from detail

    # admin flows / commands
    app.add_handler(CommandHandler('admin', admin_menu), group=1)
    app.add_handler(CommandHandler('add', add_command_handler), group=1)
    app.add_handler(CommandHandler('setphoto', setphoto_handler), group=1)
    # legacy quick-add (kept)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_text_handler), group=1)

    app.add_error_handler(error_handler)
    return app
    
    
    # -------------------- RUN BOTH BOT + WEBHOOK SERVER --------------------
if __name__ == "__main__":
    import threading
    
    # запускаем Telegram бота
    def run_bot():
        app = build_app()
        app.run_polling()

    threading.Thread(target=run_bot).start()

    # запускаем FastAPI для webhook CloudTips
    uvicorn.run(api, host="0.0.0.0", port=8000)
   
