#!/usr/bin/env python3
"""
Poizon → Telegram Moderation Bot.

Поток:
1. Каждые N минут — тестовый/реальный товар на модерацию в ЛС
2. ✅ Аппрув → пост в канал + уведомление
3. ❌ Скип → пропуск, берём следующий
4. ⏭ Дальше → показать другой (без аппрува)

Переменные окружения:
  POIZON_BOT_TOKEN       — токен бота
  POIZON_CHANNEL_ID      — @username или -100... канала
  POIZON_ADMIN_ID        — твой Telegram ID (задаётся в Railway)
  POIZON_INTERVAL_MIN    — интервал между постами (по умолч. 10)
"""

import os, sys, json, asyncio, logging, random, sqlite3
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.constants import ParseMode

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("poizon_bot")

BOT_TOKEN = os.environ.get("POIZON_BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("POIZON_CHANNEL_ID", "")
ADMIN_ID = int(os.environ.get("POIZON_ADMIN_ID", "0"))
INTERVAL_MIN = int(os.environ.get("POIZON_INTERVAL_MIN", "10"))

TEST_PRODUCTS = [
    {"spuId":"710567678","title":"Nike Air Force 1 \'07 White","brand":"Nike","price":599,"currency":"CNY","image":"https://picsum.photos/seed/af1/800/800","url":"https://www.poizon.com/product/710567678"},
    {"spuId":"736929481","title":"Nike Dunk Low Retro White Black","brand":"Nike","price":749,"currency":"CNY","image":"https://picsum.photos/seed/dunk/800/800","url":"https://www.poizon.com/product/736929481"},
    {"spuId":"701204960","title":"Air Jordan 1 Retro High OG Chicago","brand":"Jordan","price":1299,"currency":"CNY","image":"https://picsum.photos/seed/jordan1/800/800","url":"https://www.poizon.com/product/701204960"},
    {"spuId":"740936571","title":"Adidas Samba OG White Green","brand":"Adidas","price":659,"currency":"CNY","image":"https://picsum.photos/seed/samba/800/800","url":"https://www.poizon.com/product/740936571"},
    {"spuId":"712345678","title":"New Balance 990v6 Grey","brand":"New Balance","price":899,"currency":"CNY","image":"https://picsum.photos/seed/nb990/800/800","url":"https://www.poizon.com/product/712345678"},
]

DB_PATH = os.path.join(os.path.dirname(__file__), "poizon_bot.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS products (
            spu_id TEXT PRIMARY KEY, title TEXT NOT NULL, brand TEXT,
            price REAL, currency TEXT DEFAULT 'CNY', image_url TEXT,
            product_url TEXT, source TEXT DEFAULT 'poizon',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            spu_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','approved','skipped')),
            posted_to_channel INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS queue (
            spu_id TEXT PRIMARY KEY, added_at TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    return conn

def get_pending_count() -> int:
    conn = init_db()
    cnt = conn.execute("SELECT COUNT(*) FROM approvals WHERE status='pending'").fetchone()[0]
    conn.close()
    return cnt

def mark_approved(spu_id: str):
    conn = init_db()
    conn.execute("UPDATE approvals SET status='approved' WHERE spu_id=?", (spu_id,))
    conn.execute("DELETE FROM queue WHERE spu_id=?", (spu_id,))
    conn.commit(); conn.close()

def mark_skipped(spu_id: str):
    conn = init_db()
    conn.execute("UPDATE approvals SET status='skipped' WHERE spu_id=?", (spu_id,))
    conn.execute("DELETE FROM queue WHERE spu_id=?", (spu_id,))
    conn.commit(); conn.close()

def mark_posted(spu_id: str):
    conn = init_db()
    conn.execute("UPDATE approvals SET posted_to_channel=1 WHERE spu_id=?", (spu_id,))
    conn.commit(); conn.close()

def add_product_to_queue(spu_id: str):
    conn = init_db()
    conn.execute("INSERT OR IGNORE INTO queue (spu_id) VALUES (?)", (spu_id,))
    conn.commit(); conn.close()

def make_product_caption(product: dict, show_buttons_hint: bool = False) -> str:
    price_str = f"{product['price']} {product.get('currency', 'CNY')}"
    lines = [
        f"👟 <b>{product['title']}</b>",
        f"🏷 <b>{price_str}</b>",
        f"👟 {product['brand']}",
        f"🔗 <a href=\"{product.get('url', '#')}\">Смотреть на Poizon</a>",
    ]
    if show_buttons_hint:
        lines.append("\n👇 <i>Кнопки внизу</i>")
    return "\n".join(lines)

def get_moderation_keyboard(spu_id: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Аппрув", callback_data=f"approve:{spu_id}"),
         InlineKeyboardButton("❌ Скип", callback_data=f"skip:{spu_id}")],
        [InlineKeyboardButton("⏭ Дальше", callback_data=f"next:{spu_id}")],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🛒 <b>Poizon бот жив!</b>\nКанал: {CHANNEL_ID or 'не указан'}\nИнтервал: {INTERVAL_MIN} мин\nОчередь: {get_pending_count()}\n\n/status — статистика\n/next — след. товар\n/skip — скип\n/postnow — сейчас",
        parse_mode=ParseMode.HTML)

async def show_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    conn = init_db()
    pending = conn.execute("SELECT COUNT(*) FROM approvals WHERE status='pending'").fetchone()[0]
    approved = conn.execute("SELECT COUNT(*) FROM approvals WHERE status='approved'").fetchone()[0]
    skipped = conn.execute("SELECT COUNT(*) FROM approvals WHERE status='skipped'").fetchone()[0]
    posted = conn.execute("SELECT COUNT(*) FROM approvals WHERE posted_to_channel=1").fetchone()[0]
    conn.close()
    await update.message.reply_text(
        f"📊 <b>Статус</b>\n⏳ В ожидании: {pending}\n✅ Аппрувнуто: {approved}\n❌ Скипнуто: {skipped}\n📢 Опубликовано: {posted}\n⏱ Интервал: {INTERVAL_MIN} мин",
        parse_mode=ParseMode.HTML)

async def show_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    await send_next_product(context, update.effective_chat.id)

async def send_next_product(context: ContextTypes.DEFAULT_TYPE, chat_id: int = None):
    if chat_id is None:
        chat_id = ADMIN_ID
    conn = init_db()
    row = conn.execute("SELECT q.spu_id FROM queue q JOIN approvals a ON q.spu_id = a.spu_id WHERE a.status='pending' ORDER BY q.added_at LIMIT 1").fetchone()
    conn.close()
    if row:
        conn = init_db()
        prod = conn.execute("SELECT spu_id, title, brand, price, currency, image_url, product_url FROM products WHERE spu_id=?", (row[0],)).fetchone()
        conn.close()
        if prod:
            product = {"spuId": prod[0], "title": prod[1], "brand": prod[2], "price": prod[3], "currency": prod[4] or "CNY", "image": prod[5], "url": prod[6] or f"https://www.poizon.com/product/{prod[0]}"}
        else:
            product = random.choice(TEST_PRODUCTS).copy()
            product["spuId"] = row[0]
    else:
        product = random.choice(TEST_PRODUCTS).copy()
        add_product_to_queue(product["spuId"])

    caption = make_product_caption(product, show_buttons_hint=True)
    keyboard = get_moderation_keyboard(product["spuId"])

    try:
        context.user_data["current_spu"] = product["spuId"]
        context.user_data["current_product"] = product
        context.user_data["last_has_photo"] = True
    except: pass

    try:
        msg = await context.bot.send_photo(chat_id=chat_id, photo=product["image"], caption=caption, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    except Exception as e:
        log.warning(f"Фото не загрузилось, шлю текст: {e}")
        try: context.user_data["last_has_photo"] = False
        except: pass
        await context.bot.send_message(chat_id=chat_id, text=caption, reply_markup=keyboard, parse_mode=ParseMode.HTML)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Не твои кнопки", show_alert=True)
        return
    action, spu_id = query.data.split(":", 1)
    if action == "approve":
        await approve_product(query, context, spu_id)
    elif action == "skip":
        await skip_product(query, spu_id)
    elif action == "next":
        await query.edit_message_text("⏭ Ищу следующий...")
        await send_next_product(context, query.message.chat_id)

async def approve_product(query, context, spu_id: str):
    mark_approved(spu_id)
    product = {}
    try: product = context.user_data.get("current_product", {})
    except: pass
    if product.get("spuId") != spu_id:
        conn = init_db()
        prod = conn.execute("SELECT title, brand, price, currency, image_url, product_url FROM products WHERE spu_id=?", (spu_id,)).fetchone()
        conn.close()
        if prod:
            product = {"spuId": spu_id, "title": prod[0], "brand": prod[1], "price": prod[2], "currency": prod[3] or "CNY", "image": prod[4], "url": prod[5] or f"https://www.poizon.com/product/{spu_id}"}
        else:
            for p in TEST_PRODUCTS:
                if p["spuId"] == spu_id: product = p; break

    has_photo = True
    try: has_photo = context.user_data.get("last_has_photo", True)
    except: pass

    success_text = f"✅ <b>Аппрувнуто!</b>\n\n{make_product_caption(product)}"
    try:
        if has_photo:
            await query.edit_message_caption(caption=success_text, parse_mode=ParseMode.HTML, reply_markup=None)
        else:
            await query.edit_message_text(text=success_text, parse_mode=ParseMode.HTML, reply_markup=None)
    except: pass

    if CHANNEL_ID:
        caption_chan = make_product_caption(product)
        try:
            msg = await context.bot.send_photo(chat_id=CHANNEL_ID, photo=product.get("image", ""), caption=caption_chan, parse_mode=ParseMode.HTML)
            mark_posted(spu_id)
            log.info(f"✅ Пост в канал: {product['title']} msg_id={msg.message_id}")
            await context.bot.send_message(chat_id=ADMIN_ID, text=f"✅ <b>Опубликовано!</b>\n{product['title']}\n<a href=\"{product.get('url', '')}\">Ссылка</a>", parse_mode=ParseMode.HTML)
        except Exception as e:
            log.error(f"❌ Ошибка поста в канал: {e}")
            await context.bot.send_message(chat_id=ADMIN_ID, text=f"❌ <b>Ошибка поста:</b>\n{product.get('title', '?')}\n{e}", parse_mode=ParseMode.HTML)
    else:
        await context.bot.send_message(chat_id=ADMIN_ID, text="⚠️ CHANNEL_ID не указан. Пост не отправлен.")
    await asyncio.sleep(1)
    await send_next_product(context, query.message.chat_id)

async def skip_product(query, spu_id: str):
    mark_skipped(spu_id)
    try:
        await query.edit_message_caption(caption="❌ <b>Скипнуто</b>", parse_mode=ParseMode.HTML, reply_markup=None)
    except:
        try:
            await query.edit_message_text(text="❌ <b>Скипнуто</b>", parse_mode=ParseMode.HTML, reply_markup=None)
        except: pass

async def skip_current(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    spu_id = None
    try: spu_id = context.user_data.get("current_spu")
    except: pass
    if spu_id:
        mark_skipped(spu_id)
        await update.message.reply_text("❌ Скипнуто")
        await send_next_product(context, update.effective_chat.id)
    else:
        await update.message.reply_text("Нет текущего. Используй /next")

async def post_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text("⏩ Запуск...")
    await send_next_product(context, update.effective_chat.id)

async def scheduled_post(context: ContextTypes.DEFAULT_TYPE):
    log.info(f"⏰ Плановый постинг... очередь: {get_pending_count()}")
    await send_next_product(context)

def seed_test_data():
    conn = init_db()
    for p in TEST_PRODUCTS:
        conn.execute("INSERT OR IGNORE INTO products (spu_id, title, brand, price, currency, image_url, product_url) VALUES (?,?,?,?,?,?,?)",
            (p["spuId"], p["title"], p["brand"], p["price"], p.get("currency","CNY"), p["image"], p["url"]))
        conn.execute("INSERT OR IGNORE INTO approvals (spu_id, status) VALUES (?, 'pending')", (p["spuId"],))
        conn.execute("INSERT OR IGNORE INTO queue (spu_id) VALUES (?)", (p["spuId"],))
    conn.commit()
    log.info(f"📦 Загружено {len(TEST_PRODUCTS)} тестовых товаров")
    conn.close()

def main():
    if not BOT_TOKEN:
        log.error("❌ POIZON_BOT_TOKEN не указан!"); sys.exit(1)
    if not CHANNEL_ID:
        log.warning("⚠️ POIZON_CHANNEL_ID не указан — посты только в ЛС")
    init_db(); seed_test_data()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", show_status))
    app.add_handler(CommandHandler("next", show_next))
    app.add_handler(CommandHandler("skip", skip_current))
    app.add_handler(CommandHandler("postnow", post_now))
    app.add_handler(CallbackQueryHandler(button_handler, pattern=r"^(approve|skip|next):"))
    jq = app.job_queue
    if jq:
        jq.run_repeating(scheduled_post, interval=INTERVAL_MIN*60, first=30.0)
        log.info(f"⏰ Плановый постинг: каждые {INTERVAL_MIN} мин")
    log.info("🚀 Poizon бот запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()
