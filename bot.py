#!/usr/bin/env python3
"""
Poizon → Telegram Moderation Bot v2.

Поток:
1. Каждые N минут — товар на модерацию в ЛС
2. ✅ Аппрув → пост в канал
3. ❌ Скип → пропуск, берём следующий
4. ⏭ Дальше → показать другой (без аппрува)

Улучшения v2:
- PicklePersistence (бот переживает рестарты)
- Ценовой алерт (уведомление о снижении цены товара в очереди)
- Дайджест в канал (ежедневная статистика)
- Фикс редактирования сообщений (проверка наличия фото)
- История цен хранится в bot_data

Переменные окружения (только Railway):
  POIZON_BOT_TOKEN       — токен бота
  POIZON_CHANNEL_ID      — ID канала для постов
  POIZON_ADMIN_ID        — Telegram ID админа
  POIZON_INTERVAL_MIN    — интервал между постами (мин, по умолч. 10)
"""

import os, sys, json, asyncio, logging, random
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    PicklePersistence,
)
from telegram.constants import ParseMode

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("poizon_bot")

# ─── Конфигурация ────────────────────────────────────────────────

BOT_TOKEN = os.environ.get("POIZON_BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("POIZON_CHANNEL_ID", "")
ADMIN_ID = int(os.environ.get("POIZON_ADMIN_ID", "0"))
INTERVAL_MIN = int(os.environ.get("POIZON_INTERVAL_MIN", "10"))

# Стартовые тестовые товары
TEST_PRODUCTS = [
    {"spuId":"710567678","title":"Nike Air Force 1 '07 White","brand":"Nike","price":599,"currency":"CNY","image":"https://picsum.photos/seed/af1/800/800","url":"https://www.poizon.com/product/710567678"},
    {"spuId":"736929481","title":"Nike Dunk Low Retro White Black","brand":"Nike","price":749,"currency":"CNY","image":"https://picsum.photos/seed/dunk/800/800","url":"https://www.poizon.com/product/736929481"},
    {"spuId":"701204960","title":"Air Jordan 1 Retro High OG Chicago","brand":"Jordan","price":1299,"currency":"CNY","image":"https://picsum.photos/seed/jordan1/800/800","url":"https://www.poizon.com/product/701204960"},
    {"spuId":"740936571","title":"Adidas Samba OG White Green","brand":"Adidas","price":659,"currency":"CNY","image":"https://picsum.photos/seed/samba/800/800","url":"https://www.poizon.com/product/740936571"},
    {"spuId":"712345678","title":"New Balance 990v6 Grey","brand":"New Balance","price":899,"currency":"CNY","image":"https://picsum.photos/seed/nb990/800/800","url":"https://www.poizon.com/product/712345678"},
]

# ─── Вспомогательные функции ──────────────────────────────────────

def make_caption(product: dict, price_alert: str = "") -> str:
    """Форматированный заголовок товара."""
    price_str = f"{product['price']} {product.get('currency', 'CNY')}"
    lines = [
        f"👟 <b>{product['title']}</b>",
        f"💰 <b>{price_str}</b>",
        f"🏷 {product['brand']}",
        f"🔗 <a href=\"{product.get('url', '#')}\">Смотреть на Poizon</a>",
    ]
    if price_alert:
        lines.insert(2, f"🔥 {price_alert}")
    return "\n".join(lines)


def get_moderation_keyboard(spu_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Аппрув", callback_data=f"approve:{spu_id}"),
         InlineKeyboardButton("❌ Скип", callback_data=f"skip:{spu_id}")],
        [InlineKeyboardButton("⏭ Дальше", callback_data=f"next:{spu_id}")],
    ])


def init_queue(bot_data: dict):
    """Инициализировать очередь в bot_data, если её нет."""
    if "queue" not in bot_data:
        bot_data["queue"] = []
    if "products" not in bot_data:
        bot_data["products"] = {}
    if "price_history" not in bot_data:
        bot_data["price_history"] = {}
    if "stats" not in bot_data:
        bot_data["stats"] = {"approved": 0, "skipped": 0, "posted": 0, "total_alerts": 0}


def get_pending_count(bot_data: dict) -> int:
    """Количество товаров в очереди."""
    init_queue(bot_data)
    return len(bot_data["queue"])


def add_product_to_queue(bot_data: dict, product: dict):
    """Добавить товар в очередь, если его там нет."""
    init_queue(bot_data)
    spu = product["spuId"]
    if spu not in bot_data["queue"]:
        bot_data["queue"].append(spu)
        bot_data["products"][spu] = product
        # Сохраняем начальную цену для алерта
        if spu not in bot_data["price_history"]:
            bot_data["price_history"][spu] = [{"price": product["price"], "date": datetime.now(timezone.utc).isoformat()}]
    bot_data.setdefault("_dirty", True)


def remove_from_queue(bot_data: dict, spu_id: str):
    """Удалить товар из очереди."""
    init_queue(bot_data)
    if spu_id in bot_data["queue"]:
        bot_data["queue"].remove(spu_id)


def get_price_alert(bot_data: dict, product: dict) -> str:
    """Проверить, изменилась ли цена. Вернуть строку алерта или ''."""
    init_queue(bot_data)
    spu = product["spuId"]
    history = bot_data["price_history"].get(spu, [])
    if not history:
        return ""
    old_price = history[-1]["price"]
    new_price = product["price"]
    if new_price < old_price:
        pct = ((old_price - new_price) / old_price) * 100
        if pct >= 5:
            bot_data["stats"]["total_alerts"] = bot_data["stats"].get("total_alerts", 0) + 1
            return f"📉 Цена снижена на {pct:.0f}%: ¥{old_price} → ¥{new_price}"
    elif new_price > old_price:
        pct = ((new_price - old_price) / old_price) * 100
        if pct >= 5:
            return f"📈 Цена выросла на {pct:.0f}%: ¥{old_price} → ¥{new_price}"
    return ""


def update_price_history(bot_data: dict, spu_id: str, new_price: float):
    """Обновить историю цен для товара."""
    init_queue(bot_data)
    if spu_id not in bot_data["price_history"]:
        bot_data["price_history"][spu_id] = []
    bot_data["price_history"][spu_id].append({
        "price": new_price,
        "date": datetime.now(timezone.utc).isoformat(),
    })
    # Держим не более 10 записей
    if len(bot_data["price_history"][spu_id]) > 10:
        bot_data["price_history"][spu_id] = bot_data["price_history"][spu_id][-10:]


def find_from_queue(bot_data: dict, spu_id: str) -> dict:
    """Найти товар по spuId в bot_data."""
    init_queue(bot_data)
    return bot_data["products"].get(spu_id, {})


def next_from_queue(bot_data: dict) -> dict:
    """Достать следующий товар из очереди (без удаления). Если очередь пуста — тестовый."""
    init_queue(bot_data)
    if bot_data["queue"]:
        spu = bot_data["queue"][0]
        prod = bot_data["products"].get(spu, {})
        if prod:
            return prod
    # Нет в очереди — случайный тестовый
    prod = random.choice(TEST_PRODUCTS).copy()
    add_product_to_queue(bot_data, prod)
    return prod


# ─── API ──────────────────────────────────────────────────────────

async def send_product_to_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int, product: dict, is_approve_mode: bool = False):
    """Отправить товар в чат с фото (если есть) или текстом."""
    caption = make_caption(product)
    keyboard = get_moderation_keyboard(product["spuId"]) if not is_approve_mode else None

    try:
        msg = await context.bot.send_photo(
            chat_id=chat_id, photo=product["image"],
            caption=caption, reply_markup=keyboard, parse_mode=ParseMode.HTML,
        )
        # Сохраняем что было фото (для корректного редактирования)
        context.chat_data["last_has_photo"] = True
    except Exception as e:
        log.warning(f"Фото не загрузилось ({e}), шлю текст")
        context.chat_data["last_has_photo"] = False
        await context.bot.send_message(
            chat_id=chat_id, text=caption,
            reply_markup=keyboard, parse_mode=ParseMode.HTML,
        )


async def safe_edit_message(query, text: str, reply_markup=None):
    """Безопасное редактирование сообщения — выбирает caption или text."""
    has_photo = query.message.photo or False
    try:
        if has_photo:
            await query.edit_message_caption(caption=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        else:
            await query.edit_message_text(text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except Exception as e:
        log.warning(f"edit_message не удалось: {e}")
        # Пробуем другой вариант на случай ошибки
        try:
            await query.edit_message_text(text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        except:
            pass


# ─── Обработчики команд ───────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_data = context.bot_data
    init_queue(bot_data)
    await update.message.reply_text(
        f"🛒 <b>Poizon бот v2</b>\n"
        f"📢 Канал: {CHANNEL_ID or 'не указан'}\n"
        f"⏱ Интервал: {INTERVAL_MIN} мин\n"
        f"📦 В очереди: {get_pending_count(bot_data)}\n\n"
        f"<b>Команды:</b>\n"
        f"/status — статистика\n"
        f"/next — след. товар\n"
        f"/skip — скип текущего\n"
        f"/postnow — пост сейчас\n"
        f"/digest — дайджест за сегодня",
        parse_mode=ParseMode.HTML
    )


async def show_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    bot_data = context.bot_data
    init_queue(bot_data)
    stats = bot_data.get("stats", {})
    await update.message.reply_text(
        f"📊 <b>Статистика</b>\n"
        f"📦 В очереди: {get_pending_count(bot_data)}\n"
        f"✅ Аппрувнуто: {stats.get('approved', 0)}\n"
        f"❌ Скипнуто: {stats.get('skipped', 0)}\n"
        f"📢 Опубликовано: {stats.get('posted', 0)}\n"
        f"🔥 Ценовых алертов: {stats.get('total_alerts', 0)}\n"
        f"⏱ Интервал: {INTERVAL_MIN} мин",
        parse_mode=ParseMode.HTML
    )


async def show_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await send_next_product(context, update.effective_chat.id)


async def skip_current(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    spu_id = context.user_data.get("current_spu")
    if spu_id:
        bot_data = context.bot_data
        init_queue(bot_data)
        remove_from_queue(bot_data, spu_id)
        bot_data["stats"]["skipped"] = bot_data["stats"].get("skipped", 0) + 1
        await update.message.reply_text("❌ Скипнуто")
        await send_next_product(context, update.effective_chat.id)
    else:
        await update.message.reply_text("Нет текущего товара. Используй /next")


async def post_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("⏩ Запуск...")
    await send_next_product(context, update.effective_chat.id)


async def show_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручной вызов дайджеста."""
    if update.effective_user.id != ADMIN_ID:
        return
    await send_digest(context)


async def send_next_product(context: ContextTypes.DEFAULT_TYPE, chat_id: int = None):
    """Отправить следующий товар на модерацию."""
    if chat_id is None:
        chat_id = ADMIN_ID

    bot_data = context.bot_data
    init_queue(bot_data)

    product = next_from_queue(bot_data)
    context.user_data["current_spu"] = product["spuId"]
    context.user_data["current_product"] = product

    # Ценовой алерт
    price_alert = get_price_alert(bot_data, product)
    if price_alert:
        # Обновляем заголовок с алертом
        product_for_send = dict(product)
        caption = make_caption(product_for_send, price_alert)
        # Отдельно алерт не шлём, он в caption
        pass

    await send_product_to_chat(context, chat_id, product)


async def send_digest(context: ContextTypes.DEFAULT_TYPE):
    """Сформировать и отправить дайджест в канал."""
    if not CHANNEL_ID:
        log.warning("Дайджест: CHANNEL_ID не указан")
        return
    bot_data = context.bot_data
    init_queue(bot_data)
    stats = bot_data.get("stats", {})

    today = datetime.now(timezone.utc).strftime("%d.%m.%Y")
    text = (
        f"📊 <b>Дайджест {today}</b>\n\n"
        f"✅ Аппрувнуто товаров: {stats.get('approved', 0)}\n"
        f"❌ Скипнуто: {stats.get('skipped', 0)}\n"
        f"📢 Опубликовано: {stats.get('posted', 0)}\n"
        f"🔥 Ценовых алертов: {stats.get('total_alerts', 0)}\n"
        f"📦 В очереди: {get_pending_count(bot_data)}\n\n"
        f"<i>Появились вопросы? Пиши @admin</i>"
    )
    try:
        await context.bot.send_message(chat_id=CHANNEL_ID, text=text, parse_mode=ParseMode.HTML)
        log.info("✅ Дайджест отправлен в канал")
    except Exception as e:
        log.error(f"❌ Ошибка дайджеста: {e}")
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"❌ Ошибка дайджеста: {e}")


# ─── Обработчик кнопок ────────────────────────────────────────────

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Не твои кнопки", show_alert=True)
        return

    action, spu_id = query.data.split(":", 1)
    bot_data = context.bot_data
    init_queue(bot_data)

    if action == "approve":
        await approve_product(query, context, spu_id, bot_data)
    elif action == "skip":
        await skip_product(query, spu_id, bot_data)
    elif action == "next":
        await safe_edit_message(query, "⏭ Ищу следующий...")
        await send_next_product(context, query.message.chat_id)


async def approve_product(query, context, spu_id: str, bot_data: dict):
    """Аппрув товара → пост в канал."""
    product = context.user_data.get("current_product", {})
    if product.get("spuId") != spu_id:
        product = find_from_queue(bot_data, spu_id)

    if not product:
        await safe_edit_message(query, "❌ Товар не найден")
        return

    # Обновляем статистику
    bot_data["stats"]["approved"] = bot_data["stats"].get("approved", 0) + 1
    remove_from_queue(bot_data, spu_id)
    bot_data.setdefault("_dirty", True)

    # Обновляем сообщение админу (меняем кнопки на "аппрувнуто")
    success_text = f"✅ <b>Аппрувнуто!</b>\n\n{make_caption(product)}"
    await safe_edit_message(query, success_text)

    # Пост в канал
    if CHANNEL_ID:
        caption_chan = make_caption(product)
        try:
            msg = await context.bot.send_photo(
                chat_id=CHANNEL_ID, photo=product.get("image", ""),
                caption=caption_chan, parse_mode=ParseMode.HTML,
            )
            bot_data["stats"]["posted"] = bot_data["stats"].get("posted", 0) + 1
            log.info(f"✅ Пост в канал: {product['title']} msg_id={msg.message_id}")
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"✅ <b>Опубликовано!</b>\n{product['title']}\n<a href=\"{product.get('url', '')}\">Ссылка</a>",
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            log.error(f"❌ Ошибка поста в канал: {e}")
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"❌ <b>Ошибка поста:</b>\n{product.get('title', '?')}\n{e}",
                parse_mode=ParseMode.HTML,
            )
    else:
        await context.bot.send_message(chat_id=ADMIN_ID, text="⚠️ CHANNEL_ID не указан. Пост не отправлен.")

    await asyncio.sleep(0.5)
    await send_next_product(context, query.message.chat_id)


async def skip_product(query, spu_id: str, bot_data: dict):
    """Скип товара."""
    bot_data["stats"]["skipped"] = bot_data["stats"].get("skipped", 0) + 1
    remove_from_queue(bot_data, spu_id)
    bot_data.setdefault("_dirty", True)
    await safe_edit_message(query, "❌ <b>Скипнуто</b>")


# ─── Плановые задачи ──────────────────────────────────────────────

async def scheduled_post(context: ContextTypes.DEFAULT_TYPE):
    """Плановый показ товара на модерацию."""
    bot_data = context.bot_data
    init_queue(bot_data)
    log.info(f"⏰ Плановый постинг... очередь: {get_pending_count(bot_data)}")
    await send_next_product(context)


async def daily_digest_job(context: ContextTypes.DEFAULT_TYPE):
    """Ежедневный дайджест в канал."""
    log.info("📊 Отправка ежедневного дайджеста")
    await send_digest(context)


def seed_test_products(bot_data: dict):
    """Заполнить тестовыми данными при первом запуске."""
    init_queue(bot_data)
    if bot_data["queue"]:
        return  # Уже есть данные, не дублируем
    for p in TEST_PRODUCTS:
        add_product_to_queue(bot_data, p)
    log.info(f"📦 Загружено {len(TEST_PRODUCTS)} тестовых товаров (первичная инициализация)")


# ─── Запуск ────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        log.critical("❌ POIZON_BOT_TOKEN не указан!")
        sys.exit(1)
    if not CHANNEL_ID:
        log.warning("⚠️ POIZON_CHANNEL_ID не указан — посты только в ЛС")

    # PicklePersistence — бот переживает рестарты
    persistence = PicklePersistence(filepath="poizon_bot_data.pickle", store_data={"bot_data": True, "chat_data": True, "user_data": True})

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .persistence(persistence)
        .build()
    )

    # Инициализация тестовых данных при первом старте
    # Переносим seed в post_init, чтобы bot_data уже был загружен
    async def post_init(application: Application):
        bot_data = application.bot_data
        seed_test_products(bot_data)
        log.info(f"🚀 Бот инициализирован, в очереди: {get_pending_count(bot_data)}")

    app.post_init = post_init

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", show_status))
    app.add_handler(CommandHandler("next", show_next))
    app.add_handler(CommandHandler("skip", skip_current))
    app.add_handler(CommandHandler("postnow", post_now))
    app.add_handler(CommandHandler("digest", show_digest))

    # Кнопки
    app.add_handler(CallbackQueryHandler(button_handler, pattern=r"^(approve|skip|next):"))

    # Плановые задачи
    jq = app.job_queue
    if jq:
        # Модерация: повторяется каждые INTERVAL_MIN минут
        jq.run_repeating(
            scheduled_post,
            interval=INTERVAL_MIN * 60,
            first=30.0,
            chat_id=ADMIN_ID,
        )
        log.info(f"⏰ Плановый постинг: каждые {INTERVAL_MIN} мин")

        # Дайджест: раз в день в 10:00 UTC (≈ 18:00 CST)
        jq.run_daily(
            daily_digest_job,
            time=datetime.time(10, 0, tzinfo=timezone.utc),
            chat_id=ADMIN_ID,
            name="daily_digest",
        )
        log.info("📊 Дайджест: ежедневно в 10:00 UTC")

    log.info("🚀 Poizon бот v2 запущен!")
    app.run_polling()


if __name__ == "__main__":
    main()
