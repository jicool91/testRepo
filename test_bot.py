#!/usr/bin/env python3
"""Quick test — проверка что бот запускается"""
#
# Перед запуском установи переменные окружения:
#   export POIZON_BOT_TOKEN="твой_токен"
#   export POIZON_ADMIN_ID="твой_telegram_id"
#
import os
import sys
sys.path.insert(0, '/data/workspace/poizon-bot')

BOT_TOKEN = os.environ.get("POIZON_BOT_TOKEN")
ADMIN_ID = os.environ.get("POIZON_ADMIN_ID")

if not BOT_TOKEN:
    print("❌ Укажи POIZON_BOT_TOKEN в переменных окружения")
    sys.exit(1)

from telegram import Update
from telegram.ext import Application

async def test():
    app = Application.builder().token(BOT_TOKEN).build()
    me = await app.bot.get_me()
    print(f"✅ Бот @{me.username} (id={me.id}) — работает!")
    print(f"   Имя: {me.first_name}")

import asyncio
asyncio.run(test())
