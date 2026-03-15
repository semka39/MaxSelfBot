import asyncio
import os
from playwright.async_api import async_playwright
from auth import run_auth
from worker import run_worker

# Путь к данным сессии
USER_DATA_DIR = "./session_data"

async def main():
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=True,
            viewport={"width": 1280, "height": 720},
            # Разрешаем запись в буфер обмена — нужно для отправки картинок через Ctrl+V
            permissions=["clipboard-read", "clipboard-write"],
        )

        page = context.pages[0] if context.pages else await context.new_page()

        print("Проверка сессии...")
        await page.goto("https://web.max.ru", wait_until="networkidle")

        chats_sel = '#aside-header-title'
        try:
            await page.wait_for_selector(chats_sel, timeout=2000)
            print("Сессия активна.")
        except:
            print("Сессия не найдена или устарела. Переход к авторизации.")
            await run_auth(page)
            print(f"Сессия макса получена и сохранена в: {USER_DATA_DIR}")

        try:
            await run_worker(page)
        except asyncio.CancelledError:
            print("\nСкрипт остановлен пользователем.")
        finally:
            await context.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass