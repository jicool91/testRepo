#!/usr/bin/env python3
"""
Poizon → Telegram Moderation Bot v3.

Поток:
1. Каждые N минут — товар на модерацию в ЛС
2. ✅ Аппрув → пост в канал
3. ❌ Скип → пропуск, берём следующий
4. ⏭ Дальше → показать другой (без аппрува)

Улучшения v3:
- Мониторинг цен уже опубликованных товаров (проверка каждые 6 ч)
  - >10% вниз → редактируем пост с 🔴
  - >5% вверх → редактируем пост с 🟢
  - Канал уведомлений в ЛС с ссылкой на пост
- PicklePersistence (бот переживает рестарты)
- Ценовой алерт в очереди
- Дайджест в канал
- Фикс редактирования сообщений (caption vs text)

Переменные окружения (только Railway):
  POIZON_BOT_TOKEN         — токен бота
  POIZON_CHANNEL_ID        — ID канала для постов
  POIZON_ADMIN_ID          — Telegram ID админа
  POIZON_INTERVAL_MIN      — интервал модерации (мин, по умолч. 10)
  POIZON_CLIENT_SECRET     — Poizon API secret_key (опц.)
  POIZON_CHECK_INTERVAL_H  — интервал проверки цен (ч, по умолч. 6)
  POIZON_PRICE_DROP_PCT    — % снижения для алерта (по умолч. 10)
  POIZON_PRICE_RISE_PCT    — % роста для алерта (по умолч. 5)
"""

import os, sys, json, asyncio, logging, logging.handlers, random, html, traceback, time as time_module
from datetime import datetime, time, timezone
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    PicklePersistence,
    PersistenceInput,
)
from telegram.constants import ParseMode

# ─── Настройка логирования ────────────────────────────────────────

LOG_DIR = "/data/logs"

# Формат сообщений
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

logger = logging.getLogger()
logger.setLevel(logging.INFO)
logger.handlers.clear()  # убираем дефолтный handler

# 1) Stdout — краткие ошибки и ключевые события
# Фильтр: в stdout идёт ERROR+ от всех + INFO только от poizon_bot
class StdoutFilter(logging.Filter):
    def filter(self, record):
        # В stdout: все ERROR+, но INFO/WARNING только от poizon_bot
        if record.levelno >= logging.ERROR:
            return True
        if record.name == "poizon_bot":
            return True
        return False

stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setLevel(logging.INFO)
stdout_handler.addFilter(StdoutFilter())
stdout_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
stdout_handler.setFormatter(stdout_fmt)
logger.addHandler(stdout_handler)

# 2) Файловый лог с ротацией на midnight (на volume /data/logs)
# 2) Файловый лог с ротацией по размеру (на volume /data/logs)
LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
LOG_BACKUP_COUNT = 20             # максимум 200 MB логов

os.makedirs(LOG_DIR, exist_ok=True)
file_handler = logging.handlers.RotatingFileHandler(
    filename=os.path.join(LOG_DIR, "poizon-bot.log"),
    maxBytes=LOG_MAX_BYTES,
    backupCount=LOG_BACKUP_COUNT,
    encoding="utf-8",
)
file_handler.setLevel(logging.DEBUG)
file_fmt = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
file_handler.setFormatter(file_fmt)
logger.addHandler(file_handler)

# 3) Отдельный файл для ошибок с полным трейсом и контекстом
error_handler = logging.handlers.RotatingFileHandler(
    filename=os.path.join(LOG_DIR, "poizon-bot.error.log"),
    maxBytes=LOG_MAX_BYTES,
    backupCount=60,  # максимум 600 MB для ошибок
    encoding="utf-8",
)
error_handler.setLevel(logging.ERROR)
error_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s:%(lineno)d (%(funcName)s): %(message)s", datefmt=LOG_DATE_FORMAT)
error_handler.setFormatter(error_fmt)
logger.addHandler(error_handler)

# Логгер для нашего бота
log = logging.getLogger("poizon_bot")

# Тихий режим для шумных либ — их спам нам не нужен
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("telegram.vendor.ptb_urllib3").setLevel(logging.WARNING)
logging.getLogger("telegram._bot").setLevel(logging.WARNING)  # без токена

log.info(f"📁 Логи: stdout (ошибки+события) + {LOG_DIR}/poizon-bot.log (всё, 10MB ротация) + .error.log (ошибки, 10MB ротация)")

# ─── Слежение за диском ────────────────────────────────────────────
DISK_CLEANUP_THRESHOLD_PCT = 85  # при 85% заполнения начинаем чистить
DISK_CRITICAL_PCT = 95

async def check_disk_usage(context: ContextTypes.DEFAULT_TYPE):
    """Проверка места на volume. Если подбираемся к лимиту — чистим старые логи."""
    try:
        stat = os.statvfs(LOG_DIR)
        total = stat.f_frsize * stat.f_blocks
        free = stat.f_frsize * stat.f_bfree
        used_pct = (1 - free / total) * 100 if total else 0
        
        if used_pct >= DISK_CRITICAL_PCT:
            log.warning(f"🚨 ДИСК КРИТИЧЕСКИЙ: {used_pct:.0f}% — экстренная чистка!")
            clean_old_logs(keep_last=5)  # оставляем только 5 последних
        elif used_pct >= DISK_CLEANUP_THRESHOLD_PCT:
            log.info(f"⚠️ Диск заполнен на {used_pct:.0f}% — чищу старые логи")
            clean_old_logs(keep_last=10)
        else:
            log.debug(f"💾 Диск: {used_pct:.0f}% занято ({total // (1024**3)} GB total)")
    except Exception as e:
        log.error(f"Ошибка проверки диска: {e}")


def clean_old_logs(keep_last: int = 10):
    """Удаляет старые ротированные логи, оставляя keep_last штук."""
    import glob
    for pattern in ["poizon-bot.log.*", "poizon-bot.error.log.*"]:
        files = sorted(glob.glob(os.path.join(LOG_DIR, pattern)))
        for f in files[:-keep_last]:
            try:
                os.remove(f)
                log.info(f"🗑 Удалён старый лог: {os.path.basename(f)}")
            except OSError as e:
                log.error(f"Не удалось удалить {f}: {e}")

# ─── Конфигурация (только из окружения) ───────────────────────────

BOT_TOKEN = os.environ.get("POIZON_BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("POIZON_CHANNEL_ID", "")
ADMIN_ID = int(os.environ.get("POIZON_ADMIN_ID", "0"))
INTERVAL_MIN = int(os.environ.get("POIZON_INTERVAL_MIN", "10"))
CHECK_INTERVAL_H = int(os.environ.get("POIZON_CHECK_INTERVAL_H", "6"))
PRICE_DROP_PCT = int(os.environ.get("POIZON_PRICE_DROP_PCT", "10"))
PRICE_RISE_PCT = int(os.environ.get("POIZON_PRICE_RISE_PCT", "5"))

# Тестовые данные (никаких секретов — просто демо-товары)
TEST_PRODUCTS = [
    {"spuId":"710567678","title":"Nike Air Force 1 '07 White","brand":"Nike","price":599,"currency":"CNY","image":"https://picsum.photos/seed/af1/800/800","url":"https://www.poizon.com/product/710567678"},
    {"spuId":"736929481","title":"Nike Dunk Low Retro White Black","brand":"Nike","price":749,"currency":"CNY","image":"https://picsum.photos/seed/dunk/800/800","url":"https://www.poizon.com/product/736929481"},
    {"spuId":"701204960","title":"Air Jordan 1 Retro High OG Chicago","brand":"Jordan","price":1299,"currency":"CNY","image":"https://picsum.photos/seed/jordan1/800/800","url":"https://www.poizon.com/product/701204960"},
    {"spuId":"740936571","title":"Adidas Samba OG White Green","brand":"Adidas","price":659,"currency":"CNY","image":"https://picsum.photos/seed/samba/800/800","url":"https://www.poizon.com/product/740936571"},
    {"spuId":"712345678","title":"New Balance 990v6 Grey","brand":"New Balance","price":899,"currency":"CNY","image":"https://picsum.photos/seed/nb990/800/800","url":"https://www.poizon.com/product/712345678"},
    {"spuId":"750000001","title":"Nike Revolution 7 White","brand":"Nike","price":260,"currency":"CNY","image":"https://picsum.photos/seed/rev7/800/800","url":"https://www.poizon.com/product/750000001"},
]

# ─── Инициализация bot_data ───────────────────────────────────────

def init_bot_data(bot_data: dict):
    """Гарантировать, что bot_data содержит все ключи."""
    defaults = {
        "queue": [],
        "products": {},
        "price_history": {},
        "stats": {"approved": 0, "skipped": 0, "posted": 0, "total_alerts": 0, "price_changes": 0},
        "posted_posts": {},  # {spu_id: {message_id, channel_id, price, title, image_url, url, brand, currency}}
    }
    for k, v in defaults.items():
        if k not in bot_data:
            bot_data[k] = v


# ─── Форматирование ───────────────────────────────────────────────

def make_caption(product: dict, price_info: str = "") -> str:
    """Форматированный заголовок товара с опциональным ценовым уведомлением."""
    price_str = f"{product['price']} {product.get('currency', 'CNY')}"
    lines = [
        f"👟 <b>{product['title']}</b>",
        f"💰 <b>{price_str}</b>",
        f"🏷 {product['brand']}",
        f"🔗 <a href=\"{product.get('url', '#')}\">Смотреть на Poizon</a>",
    ]
    if price_info:
        lines.insert(2, price_info)
    return "\n".join(lines)


def price_change_cell(old_price: float, new_price: float, currency: str = "CNY") -> Optional[str]:
    """Сформировать строку с цветной эмодзи об изменении цены.
    Возвращает None, если изменение меньше порогов."""
    if old_price == new_price:
        return None

    pct = ((new_price - old_price) / old_price) * 100

    if new_price < old_price:
        drop = abs(pct)
        return f"🔴 <b>Цена ↓ на {drop:.0f}%</b> (¥{old_price} → ¥{new_price})"
    else:
        return f"🟢 <b>Цена ↑ на {pct:.0f}%</b> (¥{old_price} → ¥{new_price})"


def get_moderation_keyboard(spu_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Аппрув", callback_data=f"approve:{spu_id}"),
         InlineKeyboardButton("❌ Скип", callback_data=f"skip:{spu_id}")],
        [InlineKeyboardButton("⏭ Дальше", callback_data=f"next:{spu_id}")],
    ])


# ─── Работа с очередью ────────────────────────────────────────────

def get_pending_count(bot_data: dict) -> int:
    init_bot_data(bot_data)
    return len(bot_data["queue"])


def add_product_to_queue(bot_data: dict, product: dict):
    init_bot_data(bot_data)
    spu = product["spuId"]
    if spu not in bot_data["queue"]:
        bot_data["queue"].append(spu)
        bot_data["products"][spu] = product
        if spu not in bot_data["price_history"]:
            bot_data["price_history"][spu] = [{"price": product["price"], "date": datetime.now(timezone.utc).isoformat()}]


def remove_from_queue(bot_data: dict, spu_id: str):
    init_bot_data(bot_data)
    if spu_id in bot_data["queue"]:
        bot_data["queue"].remove(spu_id)


def find_product(bot_data: dict, spu_id: str) -> dict:
    init_bot_data(bot_data)
    return bot_data["products"].get(spu_id, {})


def next_from_queue(bot_data: dict) -> dict:
    init_bot_data(bot_data)
    if bot_data["queue"]:
        spu = bot_data["queue"][0]
        prod = bot_data["products"].get(spu, {})
        if prod:
            return prod
    prod = random.choice(TEST_PRODUCTS).copy()
    add_product_to_queue(bot_data, prod)
    return prod


# ─── Ценовые алерты в очереди ─────────────────────────────────────

def get_price_alert(bot_data: dict, product: dict) -> str:
    """Проверить, изменилась ли цена товара в очереди."""
    init_bot_data(bot_data)
    spu = product["spuId"]
    history = bot_data["price_history"].get(spu, [])
    if not history:
        return ""
    old_price = history[-1]["price"]
    new_price = product["price"]
    if new_price < old_price:
        pct = ((old_price - new_price) / old_price) * 100
        if pct >= PRICE_DROP_PCT:
            bot_data["stats"]["total_alerts"] = bot_data["stats"].get("total_alerts", 0) + 1
            return f"🔥 📉 Цена упала на {pct:.0f}%: ¥{old_price} → ¥{new_price}"
    elif new_price > old_price:
        pct = ((new_price - old_price) / old_price) * 100
        if pct >= PRICE_RISE_PCT:
            return f"🔥 📈 Цена выросла на {pct:.0f}%: ¥{old_price} → ¥{new_price}"
    return ""


def update_price_history(bot_data: dict, spu_id: str, new_price: float):
    init_bot_data(bot_data)
    if spu_id not in bot_data["price_history"]:
        bot_data["price_history"][spu_id] = []
    bot_data["price_history"][spu_id].append({
        "price": new_price,
        "date": datetime.now(timezone.utc).isoformat(),
    })
    if len(bot_data["price_history"][spu_id]) > 10:
        bot_data["price_history"][spu_id] = bot_data["price_history"][spu_id][-10:]


# ─── Мониторинг опубликованных цен ────────────────────────────────

async def check_posted_prices(context: ContextTypes.DEFAULT_TYPE):
    """Проверить цены всех опубликованных товаров. Редактировать посты при изменениях."""
    bot_data = context.bot_data
    init_bot_data(bot_data)
    posted = bot_data.get("posted_posts", {})
    if not posted:
        log.info("📊 Нет опубликованных товаров для проверки цен")
        return

    log.info(f"🔍 Проверка цен {len(posted)} опубликованных товаров...")
    changes = 0

    for spu_id, post_info in posted.items():
        old_price = post_info.get("price", 0)
        # Пробуем получить актуальную цену через get_spu_detail
        new_price = await fetch_current_price(spu_id, context)
        if new_price is None or new_price == old_price:
            continue

        pct = ((new_price - old_price) / old_price) * 100

        # Определяем, превышает ли изменение порог
        is_drop = new_price < old_price
        threshold = PRICE_DROP_PCT if is_drop else PRICE_RISE_PCT
        if abs(pct) < threshold:
            continue

        # Обновляем запись
        post_info["price"] = new_price
        post_info["last_check"] = datetime.now(timezone.utc).isoformat()
        bot_data["stats"]["price_changes"] = bot_data["stats"].get("price_changes", 0) + 1
        changes += 1

        # Формулируем строку изменения
        cell = price_change_cell(old_price, new_price)
        if not cell:
            continue

        # Редактируем пост в канале — обновляем caption с price_info
        channel_id = post_info.get("channel_id", CHANNEL_ID)
        message_id = post_info.get("message_id")
        if not message_id:
            continue

        product = {
            "spuId": spu_id,
            "title": post_info.get("title", ""),
            "price": new_price,
            "currency": post_info.get("currency", "CNY"),
            "brand": post_info.get("brand", ""),
            "url": post_info.get("url", f"https://www.poizon.com/product/{spu_id}"),
        }

        new_caption = make_caption(product, price_info=cell)

        try:
            await context.bot.edit_message_caption(
                chat_id=channel_id,
                message_id=message_id,
                caption=new_caption,
                parse_mode=ParseMode.HTML,
            )
            log.info(f"✏️ Цена обновлена: {post_info.get('title')} msg_id={message_id}: {cell}")
        except Exception as e:
            log.warning(f"Не удалось редактировать пост msg_id={message_id}: {e}")
            continue

        # Уведомление в ЛС со ссылкой на пост
        msg_link = f"https://t.me/c/{str(channel_id).replace('-100', '')}/{message_id}"
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"📊 <b>Изменение цены</b>\n"
                f"{cell}\n\n"
                f"👟 {post_info.get('title')}\n"
                f"<a href=\"{msg_link}\">🔗 Пост в канале</a>"
            ),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

    if changes == 0:
        log.info("✅ Цены не изменились")
    else:
        log.info(f"✅ Обновлено {changes} постов")
        bot_data.setdefault("_dirty", True)


async def fetch_current_price(spu_id: str, context: ContextTypes.DEFAULT_TYPE) -> Optional[float]:
    """Получить актуальную цену товара. Сначала Poizon API, потом заглушка.

    Для тестирования без реального API возвращаем случайное изменение ±15%.
    Для продакшена — раскомментировать вызов get_spu_detail с secret_key.
    """
    from poizon_client import get_spu_detail

    secret_key = os.environ.get("POIZON_CLIENT_SECRET", "")
    if secret_key:
        try:
            # Синхронный API в asyncio.to_thread чтобы не блокировать event loop
            data = await asyncio.to_thread(get_spu_detail, spu_id, 15, secret_key)
            if data and "result" in data:
                result = data["result"]
                price = result.get("price") or result.get("salePrice") or result.get("minPrice")
                if price:
                    return float(price)
        except Exception as e:
            log.warning(f"API ошибка для {spu_id}: {e}")

    # Фоллбэк для тестов: случайное изменение
    bot_data = context.bot_data
    init_bot_data(bot_data)
    posted = bot_data.get("posted_posts", {})
    old_price = posted.get(spu_id, {}).get("price", 0)
    if old_price:
        # Тестовая заглушка: 10% шанс изменения цены
        if random.random() < 0.1:
            change = random.choice([-1, -1, 1])  # чаще вниз для теста
            return round(old_price * (1 + change * random.uniform(0.05, 0.20)), 0)
    return None


# ─── Отправка сообщений ───────────────────────────────────────────

async def send_product_to_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int, product: dict, is_approve_mode: bool = False):
    """Отправить товар с фото (если возможно) или текстом."""
    caption = make_caption(product)
    keyboard = get_moderation_keyboard(product["spuId"]) if not is_approve_mode else None

    try:
        await context.bot.send_photo(
            chat_id=chat_id, photo=product["image"],
            caption=caption, reply_markup=keyboard, parse_mode=ParseMode.HTML,
        )
        context.chat_data["last_has_photo"] = True
    except Exception as e:
        log.warning(f"Фото не загрузилось ({e}), шлю текст")
        context.chat_data["last_has_photo"] = False
        await context.bot.send_message(
            chat_id=chat_id, text=caption,
            reply_markup=keyboard, parse_mode=ParseMode.HTML,
        )


async def safe_edit_message(query, text: str, reply_markup=None):
    """Безопасное редактирование — выбирает caption или text."""
    has_photo = bool(query.message.photo)
    try:
        if has_photo:
            await query.edit_message_caption(caption=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        else:
            await query.edit_message_text(text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except Exception as e:
        log.warning(f"edit_message не удалось: {e}")
        try:
            await query.edit_message_text(text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        except:
            pass


# ─── Обработчики команд ───────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_data = context.bot_data
    init_bot_data(bot_data)
    await update.message.reply_text(
        f"🛒 <b>Poizon бот v3</b>\n"
        f"📢 Канал: {CHANNEL_ID or 'не указан'}\n"
        f"⏱ Модерация: каждые {INTERVAL_MIN} мин\n"
        f"🔍 Проверка цен: каждые {CHECK_INTERVAL_H} ч\n"
        f"📦 В очереди: {get_pending_count(bot_data)}\n\n"
        f"<b>Команды:</b>\n"
        f"/status — статистика\n"
        f"/next — след. товар\n"
        f"/skip — скип текущего\n"
        f"/postnow — пост сейчас\n"
        f"/digest — дайджест\n"
        f"/check — проверка цен опубликованных",
        parse_mode=ParseMode.HTML
    )


async def show_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    bot_data = context.bot_data
    init_bot_data(bot_data)
    stats = bot_data.get("stats", {})
    posted = bot_data.get("posted_posts", {})
    posted_cnt = len(posted)
    price_checks = stats.get("price_changes", 0)
    
    # Uptime
    started_at = bot_data.get("started_at")
    uptime_str = "?"
    if started_at:
        elapsed = time_module.time() - started_at
        h, rem = divmod(int(elapsed), 3600)
        m = rem // 60
        uptime_str = f"{h}ч {m}мин"
    
    # Ошибки
    errors = stats.get("errors", 0)
    last_error = stats.get("last_error", "")
    error_line = f"\n🔥 Ошибок: {errors}" if errors else ""
    
    await update.message.reply_text(
        f"📊 <b>Статистика</b>\n"
        f"📦 В очереди: {get_pending_count(bot_data)}\n"
        f"📢 Опубликовано: {posted_cnt}\n"
        f"✅ Аппрувнуто: {stats.get('approved', 0)}\n"
        f"❌ Скипнуто: {stats.get('skipped', 0)}\n"
        f"🔥 Ценовых алертов: {stats.get('total_alerts', 0)}\n"
        f"📊 Изменений цен: {price_checks}\n"
        f"⏱ Модерация: {INTERVAL_MIN} мин\n"
        f"🔍 Мониторинг: {CHECK_INTERVAL_H} ч\n"
        f"⏳ Uptime: {uptime_str}{error_line}",
        parse_mode=ParseMode.HTML
    )


async def show_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await send_next_product(context, update.effective_chat.id)


async def skip_current(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    spu_id = context.user_data.get("current_spu") if context.user_data else context.bot_data.get("current_spu")
    if spu_id:
        bot_data = context.bot_data
        init_bot_data(bot_data)
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
    if update.effective_user.id != ADMIN_ID:
        return
    await send_digest(context)


async def manual_price_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручная проверка цен опубликованного."""
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("🔍 Проверяю цены опубликованных товаров...")
    await check_posted_prices(context)
    await update.message.reply_text("✅ Проверка завершена")


async def send_next_product(context: ContextTypes.DEFAULT_TYPE, chat_id: int = None):
    if chat_id is None:
        chat_id = ADMIN_ID

    bot_data = context.bot_data
    init_bot_data(bot_data)

    product = next_from_queue(bot_data)
    # Сохраняем текущий товар в bot_data (доступно из джобов без user_data)
    bot_data["current_spu"] = product["spuId"]
    bot_data["current_product"] = product
    # Если есть user_data (вызов из команды, а не из джоба) — сохраняем и туда
    if context.user_data is not None:
        context.user_data["current_spu"] = product["spuId"]
        context.user_data["current_product"] = product

    await send_product_to_chat(context, chat_id, product)


async def send_digest(context: ContextTypes.DEFAULT_TYPE):
    """Ежедневный дайджест в канал."""
    if not CHANNEL_ID:
        log.warning("Дайджест: CHANNEL_ID не указан")
        return
    bot_data = context.bot_data
    init_bot_data(bot_data)
    stats = bot_data.get("stats", {})
    posted = bot_data.get("posted_posts", {})

    today = datetime.now(timezone.utc).strftime("%d.%m.%Y")
    text = (
        f"📊 <b>Дайджест {today}</b>\n\n"
        f"✅ Аппрувнуто: {stats.get('approved', 0)}\n"
        f"❌ Скипнуто: {stats.get('skipped', 0)}\n"
        f"📢 Опубликовано в канале: {len(posted)}\n"
        f"🔄 Изменений цен: {stats.get('price_changes', 0)}\n"
        f"🔥 Алертов очереди: {stats.get('total_alerts', 0)}\n"
        f"📦 В ожидании: {get_pending_count(bot_data)}"
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
    init_bot_data(bot_data)

    if action == "approve":
        await approve_product(query, context, spu_id, bot_data)
    elif action == "skip":
        await skip_product(query, spu_id, bot_data)
    elif action == "next":
        await safe_edit_message(query, "⏭ Ищу следующий...")
        await send_next_product(context, query.message.chat_id)


async def approve_product(query, context, spu_id: str, bot_data: dict):
    """Аппрув товара → пост в канал + сохраняем для мониторинга цен."""
    product = context.user_data.get("current_product", {}) if context.user_data else context.bot_data.get("current_product", {})
    if product.get("spuId") != spu_id:
        product = find_product(bot_data, spu_id)

    if not product:
        await safe_edit_message(query, "❌ Товар не найден")
        return

    bot_data["stats"]["approved"] = bot_data["stats"].get("approved", 0) + 1
    remove_from_queue(bot_data, spu_id)

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

            # ✨ Сохраняем пост для мониторинга цен
            bot_data["posted_posts"][spu_id] = {
                "message_id": msg.message_id,
                "channel_id": CHANNEL_ID,
                "price": product["price"],
                "currency": product.get("currency", "CNY"),
                "title": product["title"],
                "brand": product["brand"],
                "image_url": product.get("image", ""),
                "url": product.get("url", f"https://www.poizon.com/product/{spu_id}"),
                "post_date": datetime.now(timezone.utc).isoformat(),
                "last_check": datetime.now(timezone.utc).isoformat(),
            }

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
    bot_data["stats"]["skipped"] = bot_data["stats"].get("skipped", 0) + 1
    remove_from_queue(bot_data, spu_id)
    await safe_edit_message(query, "❌ <b>Скипнуто</b>")


# ─── Плановые задачи ──────────────────────────────────────────────

async def scheduled_post(context: ContextTypes.DEFAULT_TYPE):
    """Плановый показ товара на модерацию."""
    try:
        bot_data = context.bot_data
        init_bot_data(bot_data)
        log.info(f"⏰ Плановый постинг... очередь: {get_pending_count(bot_data)}")
        await send_next_product(context)
    except Exception as e:
        log.error(f"scheduled_post ошибка: {e}", exc_info=True)


async def daily_digest_job(context: ContextTypes.DEFAULT_TYPE):
    """Ежедневный дайджест в канал."""
    try:
        log.info("📊 Отправка ежедневного дайджеста")
        await send_digest(context)
    except Exception as e:
        log.error(f"daily_digest_job ошибка: {e}", exc_info=True)


async def price_check_job(context: ContextTypes.DEFAULT_TYPE):
    """Плановая проверка цен опубликованных товаров."""
    try:
        log.info(f"🔍 Плановая проверка цен (каждые {CHECK_INTERVAL_H} ч)")
        await check_posted_prices(context)
    except Exception as e:
        log.error(f"price_check_job ошибка: {e}", exc_info=True)


def seed_test_products(bot_data: dict):
    init_bot_data(bot_data)
    if bot_data["queue"]:
        return
    for p in TEST_PRODUCTS:
        add_product_to_queue(bot_data, p)
    log.info(f"📦 Загружено {len(TEST_PRODUCTS)} тестовых товаров")


# ─── Запуск ────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        log.critical("❌ POIZON_BOT_TOKEN не указан!")
        sys.exit(1)
    if not CHANNEL_ID:
        log.warning("⚠️ POIZON_CHANNEL_ID не указан — посты только в ЛС")

    persistence = PicklePersistence(
        filepath="poizon_bot_data.pickle",
        store_data=PersistenceInput(bot_data=True, chat_data=True, user_data=True, callback_data=False),
    )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .persistence(persistence)
        .build()
    )

    async def post_init(application: Application):
        bot_data = application.bot_data
        seed_test_products(bot_data)
        bot_data["started_at"] = time_module.time()
        log.info(f"🚀 Бот инициализирован, в очереди: {get_pending_count(bot_data)}")

    app.post_init = post_init

    # ─── Глобальный обработчик ошибок ────────────────────────────────
    async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
        """Ловит все необработанные ошибки из хендлеров и джобов."""
        log.error("💥 Глобальная ошибка:", exc_info=context.error)

        # Считаем ошибки в статистику
        bot_data = context.application.bot_data
        init_bot_data(bot_data)
        stats = bot_data["stats"]
        stats["errors"] = stats.get("errors", 0) + 1
        stats["last_error"] = str(context.error)[:200]
        stats["last_error_time"] = datetime.now(timezone.utc).isoformat()

        # Шлём админу краткое уведомление
        tb = "".join(traceback.format_exception(None, context.error, context.error.__traceback__))
        err_msg = str(context.error)[:500]
        text = (
            f"💥 <b>Ошибка</b>\n"
            f"<code>{html.escape(err_msg)}</code>\n\n"
            f"<pre>{html.escape(tb[:1500])}</pre>"
        )
        if len(text) > 4000:
            text = text[:3997] + "..."
        try:
            await context.bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode=ParseMode.HTML)
        except Exception as e:
            log.error(f"Не удалось отправить уведомление об ошибке: {e}")

    app.add_error_handler(global_error_handler)

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", show_status))
    app.add_handler(CommandHandler("next", show_next))
    app.add_handler(CommandHandler("skip", skip_current))
    app.add_handler(CommandHandler("postnow", post_now))
    app.add_handler(CommandHandler("digest", show_digest))
    app.add_handler(CommandHandler("check", manual_price_check))

    # Кнопки
    app.add_handler(CallbackQueryHandler(button_handler, pattern=r"^(approve|skip|next):"))

    # Плановые задачи
    jq = app.job_queue
    if jq:
        # Модерация
        jq.run_repeating(
            scheduled_post,
            interval=INTERVAL_MIN * 60,
            first=30.0,
            chat_id=ADMIN_ID,
        )
        log.info(f"⏰ Модерация: каждые {INTERVAL_MIN} мин")

        # Дайджест: 10:00 UTC ≈ 18:00 CST
        jq.run_daily(
            daily_digest_job,
            time=time(10, 0, tzinfo=timezone.utc),
            chat_id=ADMIN_ID,
            name="daily_digest",
        )
        log.info("📊 Дайджест: ежедневно в 10:00 UTC")

        # 🔍 Проверка цен опубликованных товаров
        jq.run_repeating(
            price_check_job,
            interval=CHECK_INTERVAL_H * 3600,
            first=120.0,  # Первая проверка через 2 минуты после старта (для теста)
            chat_id=ADMIN_ID,
            name="price_monitor",
        )
        log.info(f"🔍 Мониторинг цен: каждые {CHECK_INTERVAL_H} ч")

        # 💾 Проверка диска: каждый час
        jq.run_repeating(
            check_disk_usage,
            interval=3600,
            first=300.0,  # через 5 минут после старта
            name="disk_monitor",
        )
        log.info(f"💾 Мониторинг диска: каждый час")

    log.info("🚀 Poizon бот v3 запущен!")
    try:
        app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=True)
    except KeyboardInterrupt:
        log.info("👋 Остановка бота...")
    except Exception as e:
        log.critical(f"💥 Фатальная ошибка: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    main()
