"""
worker.py — основной бот NetSureVPN для выдачи временного VLESS на 1 час.
"""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict

from playwright.async_api import Page

from chat_actions import (
    ChatSession,
    get_chats_with_unread,
    get_current_chat_id,
    open_chat,
)
from state import BotState
from vpn import create_trial_user, delete_trial_user

# Интервал опроса списка чатов
POLL_INTERVAL = 3.0

# Глобальные хранилища
active_sessions: Dict[str, "BotSession"] = {}
trial_timers: Dict[str, asyncio.Task] = {}   # chat_id → задача удаления


class BotSession:
    """Обёртка над ChatSession с защитой от зацикливания."""

    _CACHE_LIMIT = 200

    def __init__(self, session: ChatSession, chat_id: str | None = None):
        self._s = session
        self.chat_id = chat_id
        self._sent: list[str] = []

    def _remember(self, text: str) -> None:
        key = text.strip()[:100]
        self._sent.append(key)
        if len(self._sent) > self._CACHE_LIMIT:
            self._sent.pop(0)

    def is_bot_message(self, text: str) -> bool:
        return text.strip()[:100] in self._sent

    async def send(self, text: str) -> None:
        self._remember(text)
        await self._s.send(text)

    async def send_image(self, image_path, caption: str | None = None) -> None:
        if caption:
            self._remember(caption)
        await self._s.send_image(image_path, caption)

    def listen(self, callback, **kwargs):
        return self._s.listen(callback, **kwargs)

    def stop_listening(self) -> None:
        self._s.stop_listening()


# =============================================
# Обработчик сообщений
# =============================================

async def handle_message(text: str, bot: BotSession, state: BotState) -> None:
    if not text:
        return

    t = text.strip().lower()

    if t in ("/start", "привет", "здравствуй", "hello", "hi"):
        await bot.send(
            "Привет! 👋\n\n"
            "Я бот **NetSureVPN**.\n"
            "Выдаю временный VPN-доступ на 1 час, чтобы ты смог зайти в Telegram и купить полный тариф.\n\n"
            "Команды:\n"
            "• **настройка** — инструкция по подключению\n"
            "• **получить** — получить пробный доступ (1 раз)\n"
            "• **купить** — купить полный VPN"
        )
        return

    if t.startswith("настройка"):
        await bot.send(
            "🔧 **Как подключить временный VPN**\n\n"
            "1. Скачай приложение:\n"
            "   • Hiddify (рекомендуется) — Google Play / App Store / GitHub\n"
            "   • Happ — Google Play / App Store\n\n"
            "2. Скопируй ссылку на подписку, которую я пришлю.\n"
            "3. В приложении нажми «+» → «Import from clipboard» или «Подписка».\n"
            "4. Вставь ссылку и подключись.\n\n"
            "После подключения ты сможешь открыть Telegram."
        )
        return

    if t.startswith("купить"):
        await bot.send(
            "🛒 **Купить полный доступ**\n\n"
            "Перейди по ссылке к менеджеру:\n"
            "https://t.me/твой_менеджер_или_бот\n\n"
            "Там актуальные тарифы и страны."
        )
        return

    if t.startswith("получить"):
        chat_id = bot.chat_id
        if not chat_id:
            await bot.send("Ошибка определения чата. Попробуй ещё раз.")
            return

        # Проверка, что пробный доступ ещё не выдавался
        if state.get_last_index(f"trial_{chat_id}") == 1:
            await bot.send("Вы уже использовали пробный доступ.\nДля повторного получения — купите полный тариф.")
            return

        try:
            sub_url = await create_trial_user(chat_id)

            await bot.send(
                f"✅ **Пробный VPN на 1 час активирован!**\n\n"
                f"**Ссылка на подписку:**\n"
                f"`{sub_url}`\n\n"
                "Скопируй всю строку и вставь в Hiddify или Happ."
            )

            # Отмечаем, что доступ выдан
            state.update(f"trial_{chat_id}", 1)

            # Таймер на удаление через 1 час
            if chat_id in trial_timers and not trial_timers[chat_id].done():
                trial_timers[chat_id].cancel()

            async def auto_delete():
                await asyncio.sleep(3600)  # 1 час
                await delete_trial_user(chat_id)
                try:
                    await bot.send("⏰ Ваш пробный доступ истёк. Купите полный тариф для продолжения использования.")
                except:
                    pass

            trial_timers[chat_id] = asyncio.create_task(auto_delete())

        except Exception as e:
            await bot.send(f"❌ Ошибка при выдаче пробного доступа: {e}")
        return

    # Неизвестная команда
    await bot.send("Неизвестная команда.\nДоступные: настройка, получить, купить, /start")


def make_on_message(bot: BotSession, state: BotState):
    async def on_message(text: str, is_out: bool) -> None:
        if is_out or bot.is_bot_message(text):
            return
        await handle_message(text, bot, state)
    return on_message


# =============================================
# Главный цикл
# =============================================

async def run_worker(page: Page) -> None:
    active: dict[str, BotSession] = {}
    state = BotState()

    print("[NetSureVPN Bot] Запущен и готов к работе...")

    while True:
        try:
            unread_chats = await get_chats_with_unread(page)

            for entry in unread_chats:
                name = entry["name"]
                if name in active:
                    continue

                print(f"[Bot] Новые сообщения в чате '{name}' — открываю...")

                raw_session = await open_chat(page, name)
                if not raw_session:
                    continue

                chat_id = await get_current_chat_id(page)
                last_idx = state.get_last_index(chat_id) if chat_id else None

                bot = BotSession(raw_session, chat_id=chat_id)

                def make_on_index_seen(cid: str | None):
                    def handler(idx: int):
                        if cid:
                            state.update(cid, idx)
                    return handler

                bot.listen(
                    make_on_message(bot, state),
                    process_existing=True,
                    last_seen_index=last_idx,
                    on_index_seen=make_on_index_seen(chat_id),
                )
                active[name] = bot

            # Очистка завершённых задач
            dead = [n for n, b in active.items() if b._s._listen_task and b._s._listen_task.done()]
            for n in dead:
                del active[n]

        except asyncio.CancelledError:
            print("[Bot] Остановка...")
            for b in active.values():
                b.stop_listening()
            for t in trial_timers.values():
                if not t.done():
                    t.cancel()
            raise
        except Exception as e:
            print(f"[Bot] Ошибка в главном цикле: {e}")

        await asyncio.sleep(POLL_INTERVAL)