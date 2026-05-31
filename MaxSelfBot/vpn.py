"""
vpn.py — обновлённые заглушки (можно заменить на реальный API).
"""

import asyncio
import uuid
from datetime import datetime


# ─────────────────────────────────────────────
# Настройки (ЗАМЕНИ НА СВОИ!)
# ─────────────────────────────────────────────

PANEL_BASE_URL = "https://your-panel.example.com"  # например 3x-ui / Marzban
PANEL_API_TOKEN = "YOUR_REAL_TOKEN_HERE"

SUBSCRIPTION_URL_TEMPLATE = f"{PANEL_BASE_URL}/sub/{{user_uuid}}"


async def create_trial_user(chat_id: str) -> str:
    """Создаёт пользователя на 1 час и возвращает ссылку на подписку."""
    await asyncio.sleep(0.2)  # имитация

    # Детерминированный UUID для теста
    user_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"trial-{chat_id}"))
    sub_url = SUBSCRIPTION_URL_TEMPLATE.format(user_uuid=user_uuid)

    print(f"[vpn] Создан trial для {chat_id} → {sub_url}")
    # TODO: Реальный запрос к панели
    return sub_url


async def delete_trial_user(chat_id: str) -> None:
    """Удаляет пользователя."""
    await asyncio.sleep(0.2)
    user_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"trial-{chat_id}"))
    print(f"[vpn] Удалён trial для {chat_id} (uuid={user_uuid})")
    # TODO: Реальный DELETE запрос к панели


# Для будущего расширения
async def get_user_status(chat_id: str) -> dict:
    return {"active": True, "expires": datetime.now() + timedelta(hours=1)}