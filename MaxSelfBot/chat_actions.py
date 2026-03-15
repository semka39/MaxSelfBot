"""
chat_actions.py — вспомогательные функции для работы с чатами через Playwright.
"""

import asyncio
import mimetypes
import base64
from pathlib import Path
from typing import Callable, Awaitable
from playwright.async_api import Page


# ─────────────────────────────────────────────
# Внутренние селекторы
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

MessageCallback = Callable[[str, bool], Awaitable[int | None]]


# ─────────────────────────────────────────────
# ChatSession
# ─────────────────────────────────────────────

class ChatSession:
    """
    Контекст открытого чата. Создаётся через open_chat().

    Инкапсулирует отправку сообщений и подписку на новые,
    корректно сбрасывает состояние при каждом открытии чата.
    """

    def __init__(self, page: Page, chat_name: str):
        self._page       = page
        self._chat_name  = chat_name
        self._listen_task: asyncio.Task | None = None

    # ── Отправка ──────────────────────────────

    async def send(self, text: str) -> int | None:
        """
        Отправляет текстовое сообщение.
        Возвращает data-index нового сообщения в DOM (или None при ошибке).
        """
        try:
            before = await _get_max_index(self._page)
            field  = self._page.locator(INPUT_SELECTOR)
            await field.wait_for(state="visible", timeout=5_000)
            await field.click()
            await field.fill(text)
            await self._page.keyboard.press("Enter")
            new_index = await _wait_for_new_index(self._page, before, timeout=5.0)
            print(f"[{self._chat_name}] Отправлено: {text!r} [idx={new_index}]")
            return new_index
        except Exception as e:
            print(f"[{self._chat_name}] Ошибка отправки: {e}")
            return None

    async def send_image(self, image_path: str | Path, text: str | None = None) -> int | None:
        path = Path(image_path)
        if not path.exists():
            return None

        try:
            before = await _get_max_index(self._page)
            image_b64 = base64.b64encode(path.read_bytes()).decode()
            
            # Используем стандартный image/png, он поддерживается всеми браузерами
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
            await asyncio.sleep(1) # Пауза, чтобы Telegram «прожевал» картинку
            
            if text:
                await field.fill(text)
            
            await self._page.keyboard.press("Enter")
            print(f"[{self._chat_name}] Отправлена картинка: '{path.name}'{text} [idx={-1}]")
            return await _wait_for_new_index(self._page, before)
        except Exception as e:
            print(f"[{self._chat_name}] Ошибка картинки: {e}")
            return None

    async def send_file(self, file_path: str | Path) -> int | None:
        path = Path(file_path)
        if not path.exists():
            return None

        try:
            before = await _get_max_index(self._page)
            file_b64 = base64.b64encode(path.read_bytes()).decode()

            # Для файлов используем более простой подход:
            # Если Clipboard API капризничает, мы создаем DataTransfer объект
            await self._page.evaluate(
                """
                async ([b64, fileName]) => {
                    const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
                    const blob = new Blob([bytes], { type: "application/octet-stream" });
                    const file = new File([blob], fileName, { type: "application/octet-stream" });
                    
                    // Хак для обхода ограничений ClipboardItem:
                    // Создаем событие вставки вручную
                    const dataTransfer = new DataTransfer();
                    dataTransfer.items.add(file);
                    
                    const input = document.querySelector('div.contenteditable[role="textbox"]');
                    const event = new ClipboardEvent("paste", {
                        clipboardData: dataTransfer,
                        bubbles: true,
                        cancelable: true
                    });
                    input.dispatchEvent(event);
                }
                """,
                [file_b64, path.name],
            )

            # После dispatchEvent файл в Telegram Web должен подхватиться автоматически
            new_index = await _wait_for_new_index(self._page, before, timeout=10.0)
            print(f"[{self._chat_name}] Отправлен файл: '{path.name}' [idx={new_index}]")
            return new_index
        except Exception as e:
            print(f"[{self._chat_name}] Ошибка файла: {e}")
            return None

    # ── Подписка ──────────────────────────────

    def listen(self, callback: MessageCallback, poll_interval: float = 1.5) -> asyncio.Task:
        """
        Запускает фоновую задачу подписки на новые сообщения.
        При повторном вызове — останавливает предыдущую и стартует новую.
        Возвращает asyncio.Task (можно отменить вручную через task.cancel()).
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

    async def _listen_loop(self, callback: MessageCallback, poll_interval: float) -> None:
        # Ждём стабильного состояния DOM перед началом прослушивания
        last_seen_index = await _get_stable_max_index(self._page)
        print(f"[{self._chat_name}] Слушаем с idx={last_seen_index}...")

        while True:
            try:
                await asyncio.sleep(poll_interval)
                messages = await self._page.evaluate(_JS_GET_MESSAGES)

                for msg in messages:
                    idx  = msg["index"]
                    text = msg["text"]

                    if idx <= last_seen_index:
                        continue
                    if not text:
                        last_seen_index = idx
                        continue

                    last_seen_index = idx
                    is_out = msg["isOut"]
                    kind   = "Исходящее" if is_out else "Входящее"
                    print(f"[{self._chat_name}] {kind} [idx={idx}]: {text!r}")

                    try:
                        result = await callback(text, is_out)
                        if isinstance(result, int) and result > last_seen_index:
                            last_seen_index = result
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
    Открывает чат по имени и возвращает ChatSession.
    Возвращает None если чат не найден.
    """
    try:
        locator = page.locator(_chat_selector(chat_name)).first
        await locator.wait_for(state="visible", timeout=timeout)
        await locator.click()
        await page.locator(INPUT_SELECTOR).wait_for(state="visible", timeout=timeout)

        # Ждём пока Telegram догрузит сообщения в DOM
        await _get_stable_max_index(page)

        print(f"[open_chat] Чат '{chat_name}' открыт.")
        return ChatSession(page, chat_name)
    except Exception as e:
        print(f"[open_chat] Не удалось открыть чат '{chat_name}': {e}")
        return None


# Отдельная функция для быстрого вызова
async def send_file_to_chat(page: Page, chat_name: str, file_path: str | Path) -> bool:
    """
    Быстрая отправка файла: находит чат, открывает его и пуляет файл через Ctrl+V.
    """
    if not await chat_exists(page, chat_name):
        return False
    
    session = await open_chat(page, chat_name)
    if not session:
        return False
        
    result = await session.send_file(file_path)
    return result is not None

async def send_to_chat(
    page: Page,
    chat_name: str,
    message: str | None = None,
    image_path: str | Path | None = None,
) -> bool:
    """
    Быстрая отправка: открыть чат → отправить → не подписываться.
    Удобно для одноразовых уведомлений.
    """
    if not await chat_exists(page, chat_name):
        return False
    session = await open_chat(page, chat_name)
    if not session:
        return False
    if image_path:
        return await session.send_image(image_path, text=message) is not None
    if message:
        return await session.send(message) is not None
    print("[send_to_chat] Не передано ни сообщение, ни картинка.")
    return False


# ─────────────────────────────────────────────
# Внутренние утилиты
# ─────────────────────────────────────────────

async def _get_max_index(page: Page) -> int:
    try:
        result = await page.evaluate("""
            () => {
                const items = document.querySelectorAll('.item[data-index]');
                let max = -1;
                for (const item of items) {
                    const idx = parseInt(item.getAttribute('data-index'), 10);
                    if (idx > max) max = idx;
                }
                return max;
            }
        """)
        return result if result is not None else -1
    except Exception:
        return -1


async def _get_stable_max_index(page: Page, attempts: int = 5, interval: float = 0.4) -> int:
    """
    Делает несколько замеров max_index с паузой между ними.
    Возвращает значение только когда оно два раза подряд одинаковое —
    это означает что Telegram догрузил сообщения в DOM и виртуализация
    завершила первоначальный рендер.
    """
    prev = await _get_max_index(page)
    for _ in range(attempts):
        await asyncio.sleep(interval)
        current = await _get_max_index(page)
        if current == prev:
            return current
        prev = current
    return prev


async def _wait_for_new_index(page: Page, before: int, timeout: float = 5.0) -> int:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        current = await _get_max_index(page)
        if current > before:
            return current
        await asyncio.sleep(0.2)
    return before