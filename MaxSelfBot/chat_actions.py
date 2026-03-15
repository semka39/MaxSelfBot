"""
chat_actions.py — библиотека для работы с Telegram Web через Playwright.

Основные сущности:
  ChatSession    — контекст открытого чата (отправка, подписка на новые сообщения)
  open_chat()    — открыть чат по имени, вернуть ChatSession
  send_to_chat() — разовая отправка без подписки

Пример использования:

    from playwright.async_api import async_playwright
    from chat_actions import open_chat

    async def on_message(text: str, is_out: bool) -> None:
        if not is_out and text.startswith("."):
            print("Команда:", text)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        page    = await browser.new_page()
        # ... авторизация ...
        session = await open_chat(page, "Мой чат")
        await session.send("Привет!")
        task = session.listen(on_message)
        await task
"""

import asyncio
import base64
from pathlib import Path
from typing import Callable, Awaitable

from playwright.async_api import Page


# ─────────────────────────────────────────────
# Селекторы
# ─────────────────────────────────────────────

def _chat_selector(chat_name: str) -> str:
    return f'xpath=//h3[contains(@class, "title")]//span[contains(normalize-space(), "{chat_name}")]'

INPUT_SELECTOR    = 'div.contenteditable[role="textbox"]'
SEND_BUTTON_SEL   = 'button[aria-label="Отправить сообщение"]'
ATTACH_BUTTON_SEL = 'button[aria-label="Загрузить файл"]'

_JS_GET_MESSAGES = """
() => {
    const items = document.querySelectorAll('.item[data-index]');
    const result = [];
    for (const item of items) {
        const index = parseInt(item.getAttribute('data-index'), 10);
        const wrapper = item.querySelector('[class*="messageWrapper"]');
        if (!wrapper) continue;
        const isOut = wrapper.className.includes('messageWrapper--isOut');
        const bubble = wrapper.querySelector('.bubble');
        if (!bubble) continue;

        // Ищем span.text который НЕ находится внутри .header (там имя отправителя).
        let text = null;
        const spans = bubble.querySelectorAll('span.text');
        for (const span of spans) {
            if (!span.closest('.header')) {
                text = span.innerText.trim();
                break;
            }
        }

        result.push({ index, isOut, text });
    }
    return result;
}
"""

# Callback получает текст сообщения и флаг is_out (True = исходящее).
MessageCallback = Callable[[str, bool], Awaitable[None]]


# ─────────────────────────────────────────────
# ChatSession
# ─────────────────────────────────────────────

class ChatSession:
    """
    Контекст открытого чата. Создаётся через open_chat().

    Методы:
      send(text)                  — отправить текстовое сообщение
      send_image(path, caption)   — отправить картинку
      send_file(path)             — отправить файл
      listen(callback, interval)  — подписаться на новые сообщения
      stop_listening()            — остановить подписку
    """

    def __init__(self, page: Page, chat_name: str):
        self._page       = page
        self._chat_name  = chat_name
        self._listen_task: asyncio.Task | None = None

    # ── Отправка ──────────────────────────────

    async def send(self, text: str) -> None:
        """Отправляет текстовое сообщение."""
        try:
            field = self._page.locator(INPUT_SELECTOR)
            await field.wait_for(state="visible", timeout=5_000)
            await field.click()
            await field.fill(text)
            await self._page.keyboard.press("Enter")
            print(f"[{self._chat_name}] Отправлено: {text!r}")
        except Exception as e:
            print(f"[{self._chat_name}] Ошибка отправки: {e}")

    async def send_image(self, image_path: str | Path, caption: str | None = None) -> None:
        """Отправляет картинку через буфер обмена, с опциональной подписью."""
        path = Path(image_path)
        if not path.exists():
            print(f"[{self._chat_name}] Файл не найден: {path}")
            return

        try:
            image_b64 = base64.b64encode(path.read_bytes()).decode()
            await self._page.evaluate(
                """
                async (b64) => {
                    const response = await fetch(`data:image/png;base64,${b64}`);
                    const blob = await response.blob();
                    await navigator.clipboard.write([
                        new ClipboardItem({ "image/png": blob })
                    ]);
                }
                """,
                image_b64,
            )
            field = self._page.locator(INPUT_SELECTOR)
            await field.click()
            await self._page.keyboard.press("Control+V")
            await asyncio.sleep(1)
            if caption:
                await field.fill(caption)
            await self._page.keyboard.press("Enter")
            print(f"[{self._chat_name}] Отправлена картинка: '{path.name}'")
        except Exception as e:
            print(f"[{self._chat_name}] Ошибка отправки картинки: {e}")

    async def send_file(self, file_path: str | Path) -> None:
        """Отправляет файл через искусственное clipboard-событие paste."""
        path = Path(file_path)
        if not path.exists():
            print(f"[{self._chat_name}] Файл не найден: {path}")
            return

        try:
            file_b64 = base64.b64encode(path.read_bytes()).decode()
            await self._page.evaluate(
                """
                async ([b64, fileName]) => {
                    const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
                    const blob  = new Blob([bytes], { type: "application/octet-stream" });
                    const file  = new File([blob], fileName, { type: "application/octet-stream" });
                    const dt    = new DataTransfer();
                    dt.items.add(file);
                    const input = document.querySelector('div.contenteditable[role="textbox"]');
                    input.dispatchEvent(new ClipboardEvent("paste", {
                        clipboardData: dt, bubbles: true, cancelable: true
                    }));
                }
                """,
                [file_b64, path.name],
            )
            print(f"[{self._chat_name}] Отправлен файл: '{path.name}'")
        except Exception as e:
            print(f"[{self._chat_name}] Ошибка отправки файла: {e}")

    # ── Подписка ──────────────────────────────

    def listen(
        self,
        callback: MessageCallback,
        poll_interval: float = 1.5,
    ) -> asyncio.Task:
        """
        Запускает фоновую задачу опроса новых сообщений.
        Callback вызывается для каждого нового сообщения: await callback(text, is_out).
        При повторном вызове предыдущая задача останавливается.
        Возвращает asyncio.Task — можно отменить через task.cancel().
        """
        self.stop_listening()
        self._listen_task = asyncio.create_task(
            self._listen_loop(callback, poll_interval),
            name=f"listen:{self._chat_name}",
        )
        return self._listen_task

    def stop_listening(self) -> None:
        """Останавливает текущую подписку если она активна."""
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
            self._listen_task = None

    async def _listen_loop(
        self,
        callback: MessageCallback,
        poll_interval: float,
    ) -> None:
        # Запоминаем set индексов которые уже есть в DOM на момент старта.
        # Всё что появится позже — новое. Работает надёжно независимо от того,
        # с какого числа начинаются виртуальные data-index и как они растут.
        initial = await self._page.evaluate(_JS_GET_MESSAGES)
        seen_indices: set[int] = {m["index"] for m in initial}
        print(f"[{self._chat_name}] Слушаем новые сообщения...")

        while True:
            try:
                await asyncio.sleep(poll_interval)
                messages = await self._page.evaluate(_JS_GET_MESSAGES)

                for msg in messages:
                    idx  = msg["index"]
                    text = msg["text"]

                    if idx in seen_indices:
                        continue

                    seen_indices.add(idx)

                    if not text:
                        continue

                    is_out = msg["isOut"]
                    kind   = "Исходящее" if is_out else "Входящее"
                    print(f"[{self._chat_name}] {kind} [idx={idx}]: {text!r}")

                    try:
                        await callback(text, is_out)
                    except Exception as e:
                        print(f"[{self._chat_name}] Ошибка в callback: {e}")

            except asyncio.CancelledError:
                print(f"[{self._chat_name}] Подписка остановлена.")
                raise
            except Exception as e:
                print(f"[{self._chat_name}] Ошибка опроса: {e}")


# ─────────────────────────────────────────────
# Публичные функции
# ─────────────────────────────────────────────

async def chat_exists(page: Page, chat_name: str, timeout: int = 10_000) -> bool:
    """Проверяет, есть ли чат с данным именем в списке."""
    try:
        locator = page.locator(_chat_selector(chat_name)).first
        await locator.wait_for(state="visible", timeout=timeout)
        print(f"[chat_exists] Чат '{chat_name}' найден.")
        return True
    except Exception:
        print(f"[chat_exists] Чат '{chat_name}' не найден.")
        return False


async def open_chat(page: Page, chat_name: str, timeout: int = 10_000) -> "ChatSession | None":
    """
    Открывает чат по имени, ждёт готовности поля ввода и возвращает ChatSession.
    Возвращает None если чат не найден.
    """
    try:
        locator = page.locator(_chat_selector(chat_name)).first
        await locator.wait_for(state="visible", timeout=timeout)
        await locator.click()
        await page.locator(INPUT_SELECTOR).wait_for(state="visible", timeout=timeout)
        print(f"[open_chat] Чат '{chat_name}' открыт.")
        return ChatSession(page, chat_name)
    except Exception as e:
        print(f"[open_chat] Не удалось открыть чат '{chat_name}': {e}")
        return None


async def send_to_chat(
    page: Page,
    chat_name: str,
    message: str | None = None,
    image_path: str | Path | None = None,
) -> bool:
    """
    Разовая отправка: открыть чат → отправить → не подписываться.
    Удобно для одноразовых уведомлений.
    """
    if not await chat_exists(page, chat_name):
        return False
    session = await open_chat(page, chat_name)
    if not session:
        return False
    if image_path:
        await session.send_image(image_path, caption=message)
        return True
    if message:
        await session.send(message)
        return True
    print("[send_to_chat] Не передано ни сообщение, ни картинка.")
    return False


async def send_file_to_chat(page: Page, chat_name: str, file_path: str | Path) -> bool:
    """Разовая отправка файла в чат."""
    if not await chat_exists(page, chat_name):
        return False
    session = await open_chat(page, chat_name)
    if not session:
        return False
    await session.send_file(file_path)
    return True