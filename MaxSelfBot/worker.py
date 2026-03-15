"""
worker.py — переключение между чатами через asyncio.Event.
"""

import asyncio
from playwright.async_api import Page

from chat_actions import ChatSession, open_chat

CHAT_A = "Окно в Европу"
CHAT_B = "Окно в Европу 2"


def make_echo_callback(session: ChatSession):
    async def on_message(text: str) -> int | None:
        return await session.send(f"Эхо: {text}")
    return on_message


def make_switching_callback(session_a: ChatSession, switch_done: asyncio.Event):
    """
    Колбэк для чата A.
    При '123' — сигнализирует о переключении и останавливает подписку.
    Не открывает чат B сам: этим занимается run_worker.
    """
    async def on_message(text: str) -> int | None:
        if text.strip() == "123":
            print(f"[{CHAT_A}] Получено '123' — сигнал переключения...")
            switch_done.set()          # сигнал главному циклу
            session_a.stop_listening() # отменяем task_a изнутри
            return None

        return await session_a.send(f"Эхо: {text}")

    return on_message


async def run_worker(page: Page):
    session_a = await open_chat(page, CHAT_A)
    if not session_a:
        print("Не удалось открыть чат.")
        await asyncio.Future()
        return

    await session_a.send("Сервис запущен!")
    await session_a.send_file(r"D:\VisualStudio\MaxSelfBot\MaxSelfBot\chat_actions.py")
    await session_a.send_image(r"D:\semen\ava7.png", "test")

    switch_done = asyncio.Event()
    task_a = session_a.listen(make_switching_callback(session_a, switch_done))

    # Ждём либо завершения task_a, либо сигнала switch_done
    # (они придут почти одновременно, но switch_done надёжнее для синхронизации)
    await asyncio.gather(task_a, return_exceptions=True)

    if switch_done.is_set():
        print(f"[run_worker] Переключаемся в '{CHAT_B}'...")

        # Только теперь открываем чат B — task_a уже мертва, DOM свободен
        session_b = await open_chat(page, CHAT_B)
        if not session_b:
            print(f"Не удалось открыть '{CHAT_B}'.")
            await asyncio.Future()
            return

        await session_b.send("Сессия переведена сюда")
        task_b = session_b.listen(make_echo_callback(session_b))

        # Держим task_b живой
        await asyncio.gather(task_b, return_exceptions=True)

    # Держим браузер открытым
    await asyncio.Future()