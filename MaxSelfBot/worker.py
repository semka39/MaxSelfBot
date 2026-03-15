"""
worker.py — бот для чата «Окно в Европу».

Команды начинаются с точки. После точки допускается случайный пробел — он trimмится.
Сообщения без точки (вне режима сёрфинга) игнорируются.
Исходящие сообщения бота фильтруются через BotSession._sent.
"""

import asyncio
import os
import random
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageDraw, ImageFont
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from chat_actions import ChatSession, open_chat

CHAT_NAME = "Окно в Европу"

# ─────────────────────────────────────────────────────────────────────────────
# Константы
# ─────────────────────────────────────────────────────────────────────────────

WHITELISTS = [
    {"name": "vless_lite.txt",
     "url": "https://raw.githubusercontent.com/zieng2/wl/main/vless_lite.txt"},
    {"name": "whitelist_nowmeow.txt",
     "url": "https://nowmeow.pw/8ybBd3fdCAQ6Ew5H0d66Y1hMbh63GpKUtEXQClIu/whitelist"},
    {"name": "Vless-Reality-White-Lists-Rus-Mobile.txt",
     "url": "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/refs/heads/main/Vless-Reality-White-Lists-Rus-Mobile.txt"},
]

MEME_SUBREDDITS_EN = ["memes", "dankmemes", "wholesomememes", "AdviceAnimals"]
PIKABU_HOT_URL     = "https://pikabu.ru/rss/section/all"
IMAGE_EXTS         = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
BROWSER_VIEWPORT   = {"width": 1280, "height": 720}
CLICK_MAX_DEPTH    = 3

# ─────────────────────────────────────────────────────────────────────────────
# Состояние сёрфинга
# ─────────────────────────────────────────────────────────────────────────────

class SurfState:
    def __init__(self):
        self.playwright  = None
        self.browser: Browser | None                   = None
        self.context: BrowserContext | None            = None
        self.page: Page | None                         = None
        self.active: bool                              = False
        self.click_depth: int                          = 0
        self.click_rect: tuple[int,int,int,int] | None = None

_surf = SurfState()

# ─────────────────────────────────────────────────────────────────────────────
# Утилиты парсинга команд
# ─────────────────────────────────────────────────────────────────────────────

def _parse_cmd(raw: str) -> str | None:
    """
    Извлекает команду из текста сообщения.
    Формат: .<cmd> [args]  — пробел после точки допустим (trimмится).
    Возвращает "cmd args" в нижнем регистре или None если не команда.
    """
    s = raw.strip()
    if not s.startswith("."):
        return None
    return s[1:].strip().lower()


def _parse_arg(raw: str, prefix: str) -> str:
    """
    Возвращает аргумент команды с сохранением оригинального регистра.
    Например: '.dl https://Example.com', prefix='dl' -> 'https://Example.com'
    """
    body = raw.strip()[1:].strip()          # убираем точку + пробел после неё
    return body[len(prefix):].strip()       # убираем имя команды + пробел

# ─────────────────────────────────────────────────────────────────────────────
# Новости BBC
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_bbc_news() -> str:
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get("https://feeds.bbci.co.uk/news/rss.xml")
            resp.raise_for_status()
        entries = re.findall(r"<item>(.*?)</item>", resp.text, re.DOTALL)
        items = []
        for entry in entries[:10]:
            title = re.search(
                r"<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>",
                entry, re.DOTALL,
            )
            link = re.search(r"<link>(.*?)</link>", entry, re.DOTALL)
            t = (title.group(1) or title.group(2) or "—").strip() if title else "—"
            l = link.group(1).strip() if link else ""
            items.append(f"• {t}\n  {l}")
        return "📰 BBC News:\n\n" + "\n\n".join(items) if items else "📰 BBC News: нет данных."
    except Exception as e:
        return f"❌ Ошибка при получении новостей BBC: {e}"

# ─────────────────────────────────────────────────────────────────────────────
# Мемы
# ─────────────────────────────────────────────────────────────────────────────

def _is_image_url(url: str) -> bool:
    return Path(urlparse(url).path).suffix.lower() in IMAGE_EXTS


async def _fetch_meme_en() -> tuple[str | None, str | None]:
    headers   = {"User-Agent": "Mozilla/5.0 (compatible; TelegramBot/1.0)"}
    subreddit = random.choice(MEME_SUBREDDITS_EN)
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers=headers) as client:
            resp = await client.get(f"https://meme-api.com/gimme/{subreddit}")
        if resp.status_code == 200:
            data = resp.json()
            if not data.get("nsfw") and not data.get("spoiler"):
                url = data.get("url", "")
                if _is_image_url(url):
                    return url, data.get("title", "")
    except Exception:
        pass
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers=headers) as client:
            resp = await client.get(f"https://www.reddit.com/r/{subreddit}/hot.json?limit=50")
            resp.raise_for_status()
        posts = resp.json()["data"]["children"]
        random.shuffle(posts)
        for post in posts:
            d = post["data"]
            if d.get("over_18") or d.get("spoiler") or d.get("is_self"):
                continue
            url = d.get("url", "")
            if _is_image_url(url):
                return url, d.get("title", "")
    except Exception as e:
        return None, f"❌ Не удалось получить мем: {e}"
    return None, "❌ Не нашёл мем, попробуй ещё раз."


async def _fetch_meme_ru() -> tuple[str | None, str | None]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; TelegramBot/1.0)"}
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=headers) as client:
            resp = await client.get(PIKABU_HOT_URL)
            resp.raise_for_status()
        items = re.findall(r"<item>(.*?)</item>", resp.text, re.DOTALL)
        candidates = []
        for item in items:
            enc = re.search(r'<enclosure[^>]+url="([^"]+)"', item)
            if not enc:
                continue
            url = enc.group(1)
            if not _is_image_url(url):
                continue
            title_m = re.search(
                r"<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>",
                item, re.DOTALL,
            )
            title = (title_m.group(1) or title_m.group(2) or "").strip() if title_m else ""
            candidates.append((url, title))
        if candidates:
            return random.choice(candidates)
    except Exception as e:
        return None, f"❌ Ошибка Pikabu: {e}"
    return None, "❌ Не нашёл русский мем, попробуй ещё раз."


async def fetch_random_meme(lang: str = "ru") -> tuple[str | None, str | None]:
    return await (_fetch_meme_ru() if lang == "ru" else _fetch_meme_en())

# ─────────────────────────────────────────────────────────────────────────────
# Браузер — запуск / остановка
# ─────────────────────────────────────────────────────────────────────────────

async def browser_start(session: "BotSession") -> None:
    if _surf.browser and _surf.browser.is_connected():
        await session.send("⚠️ Браузер уже запущен.")
        return
    await session.send("🚀 Запускаю браузер...")
    try:
        _surf.playwright = await async_playwright().start()
        _surf.browser    = await _surf.playwright.chromium.launch(headless=True)
        _surf.context    = await _surf.browser.new_context(
            viewport=BROWSER_VIEWPORT,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        _surf.page = await _surf.context.new_page()
        await session.send("✅ Браузер запущен. Войди в режим сёрфинга: .serf")
    except Exception as e:
        await session.send(f"❌ Не удалось запустить браузер: {e}")


async def browser_stop(session: "BotSession") -> None:
    _surf.active      = False
    _surf.click_rect  = None
    _surf.click_depth = 0
    for obj, method in (
        (_surf.page,       "close"),
        (_surf.context,    "close"),
        (_surf.browser,    "close"),
        (_surf.playwright, "stop"),
    ):
        try:
            if obj:
                await getattr(obj, method)()
        except Exception:
            pass
    _surf.page = _surf.context = _surf.browser = _surf.playwright = None
    await session.send("🛑 Браузер остановлен.")

# ─────────────────────────────────────────────────────────────────────────────
# Браузер — скриншот
# ─────────────────────────────────────────────────────────────────────────────

async def _take_screenshot() -> Path | None:
    if not _surf.page:
        return None
    tmp = Path(tempfile.gettempdir()) / "surf_screen.png"
    await _surf.page.screenshot(path=str(tmp), full_page=False)
    return tmp


async def _send_screenshot(session: "BotSession", caption: str | None = None) -> None:
    if caption and len(caption) > 50:
        caption = caption[:47] + "..."
    tmp = await _take_screenshot()
    if not (tmp and tmp.exists()):
        await session.send("❌ Не удалось сделать скриншот.")
        return
    try:
        await asyncio.wait_for(session.send_image(tmp, caption), timeout=15.0)
    except asyncio.TimeoutError:
        await session.send(f"⚠️ Таймаут отправки скриншота.\nURL: {caption or ''}")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

# ─────────────────────────────────────────────────────────────────────────────
# Браузер — сетка клика
# ─────────────────────────────────────────────────────────────────────────────

def _draw_grid(image_path: Path, rect: tuple[int,int,int,int]) -> Path:
    img  = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(img)
    rx, ry, rw, rh = rect
    cw, ch = rw // 3, rh // 3
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
    except Exception:
        font = ImageFont.load_default()
    for row in range(3):
        for col in range(3):
            num = row * 3 + col + 1
            x0, y0 = rx + col * cw, ry + row * ch
            x1, y1 = x0 + cw, y0 + ch
            draw.rectangle([x0, y0, x1, y1], outline="red", width=3)
            tx, ty = x0 + cw // 2 - 10, y0 + ch // 2 - 15
            draw.text((tx + 2, ty + 2), str(num), fill="black", font=font)
            draw.text((tx, ty),         str(num), fill="white", font=font)
    out = Path(tempfile.gettempdir()) / "surf_grid.png"
    img.save(out)
    return out


async def _send_grid(session: "BotSession") -> None:
    tmp_screen = await _take_screenshot()
    if not tmp_screen:
        await session.send("❌ Не удалось сделать скриншот.")
        return
    grid_img = _draw_grid(tmp_screen, _surf.click_rect)
    caption  = (
        f"Выбери зону (1–9) или .cancel для отмены "
        f"(шаг {_surf.click_depth + 1}/{CLICK_MAX_DEPTH})"
    )
    try:
        await asyncio.wait_for(session.send_image(grid_img, caption), timeout=15.0)
    except asyncio.TimeoutError:
        await session.send(caption)
    finally:
        for p in (tmp_screen, grid_img):
            try:
                os.remove(p)
            except OSError:
                pass


def _cell_rect(parent: tuple[int,int,int,int], cell: int) -> tuple[int,int,int,int]:
    rx, ry, rw, rh = parent
    cw, ch = rw // 3, rh // 3
    row, col = divmod(cell - 1, 3)
    return rx + col * cw, ry + row * ch, cw, ch

# ─────────────────────────────────────────────────────────────────────────────
# Обработчик команд сёрфинга
# ─────────────────────────────────────────────────────────────────────────────

async def handle_surf_command(raw_text: str, session: "BotSession") -> None:
    raw = raw_text.strip()

    # ── Режим уточнения клика ─────────────────────────────────────────────
    if _surf.click_rect is not None:
        if _parse_cmd(raw) == "cancel":
            _surf.click_rect  = None
            _surf.click_depth = 0
            await session.send("❌ Клик отменён.")
            return
        if re.fullmatch(r"[1-9]+", raw):
            # Можно ввести сразу несколько цифр: "45" = сначала 4, потом 5
            for ch in raw:
                cell     = int(ch)
                new_rect = _cell_rect(_surf.click_rect, cell)
                _surf.click_depth += 1
                _surf.click_rect   = new_rect
                if _surf.click_depth >= CLICK_MAX_DEPTH:
                    break
            if _surf.click_depth >= CLICK_MAX_DEPTH:
                rx, ry, rw, rh = _surf.click_rect
                cx, cy = rx + rw // 2, ry + rh // 2
                _surf.click_rect  = None
                _surf.click_depth = 0
                await session.send(f"🖱️ Кликаю в ({cx}, {cy})...")
                await _surf.page.mouse.click(cx, cy)
                try:
                    await _surf.page.wait_for_load_state("networkidle", timeout=10_000)
                except Exception:
                    pass
                await _send_screenshot(session, _surf.page.url)
            else:
                await _send_grid(session)
            return
        await session.send("Введи цифры (1–9) или .cancel для отмены.")
        return

    # ── Обычные команды сёрфинга ──────────────────────────────────────────
    cmd = _parse_cmd(raw)
    if cmd is None:
        return

    if cmd == "exit":
        _surf.active = False
        await session.send(
            "🚪 Вышел из режима сёрфинга. Браузер работает в фоне.\n"
            "Войти снова: .serf | Остановить: .stopserf"
        )
        return

    if cmd == "cancel":
        _surf.active = False
        await session.send("🚪 Режим сёрфинга приостановлен.")
        return

    go_m = re.match(r"^go\s+(\S+)$", cmd)
    if go_m:
        url = go_m.group(1)
        if not url.startswith("http"):
            url = "https://" + url
        await session.send(f"🌐 Открываю {url}...")
        try:
            await _surf.page.goto(url, wait_until="networkidle", timeout=20_000)
        except Exception:
            try:
                await _surf.page.goto(url, wait_until="domcontentloaded", timeout=15_000)
            except Exception as e:
                await session.send(f"⚠️ Страница загружена с ошибкой: {e}")
        await _send_screenshot(session, _surf.page.url)
        return

    if cmd == "reload":
        try:
            await _surf.page.reload(wait_until="networkidle", timeout=15_000)
        except Exception:
            pass
        await _send_screenshot(session, _surf.page.url)
        return

    if cmd == "screen":
        await _send_screenshot(session, _surf.page.url)
        return

    scroll_m = re.match(r"^(down|up)(?:\s+(\d+))?$", cmd)
    if scroll_m:
        direction = scroll_m.group(1)
        px = int(scroll_m.group(2)) if scroll_m.group(2) else 600
        dy = px if direction == "down" else -px
        await _surf.page.evaluate(f"window.scrollBy(0, {dy})")
        await asyncio.sleep(0.4)
        await _send_screenshot(session, _surf.page.url)
        return

    click_m = re.match(r"^click(?:\s*([1-9]+))?$", cmd)
    if click_m:
        digits = click_m.group(1) or ""
        rect   = (0, 0, BROWSER_VIEWPORT["width"], BROWSER_VIEWPORT["height"])
        depth  = 0
        for ch in digits:
            if depth >= CLICK_MAX_DEPTH:
                break
            rect  = _cell_rect(rect, int(ch))
            depth += 1
        if depth >= CLICK_MAX_DEPTH:
            rx, ry, rw, rh = rect
            cx, cy = rx + rw // 2, ry + rh // 2
            await session.send(f"🖱️ Кликаю в ({cx}, {cy})...")
            await _surf.page.mouse.click(cx, cy)
            try:
                await _surf.page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass
            await _send_screenshot(session, _surf.page.url)
        else:
            _surf.click_rect  = rect
            _surf.click_depth = depth
            await _send_grid(session)
        return

    type_m = re.match(r"^type\s+", cmd)
    if type_m:
        typed = _parse_arg(raw, "type")
        await _surf.page.keyboard.type(typed, delay=30)
        await asyncio.sleep(0.3)
        await _send_screenshot(session, _surf.page.url)
        return

    if cmd == "enter":
        await _surf.page.keyboard.press("Enter")
        try:
            await _surf.page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        await _send_screenshot(session, _surf.page.url)
        return

    if cmd == "back":
        try:
            await _surf.page.go_back(wait_until="networkidle", timeout=10_000)
        except Exception:
            pass
        await _send_screenshot(session, _surf.page.url)
        return

    if cmd == "fwd":
        try:
            await _surf.page.go_forward(wait_until="networkidle", timeout=10_000)
        except Exception:
            pass
        await _send_screenshot(session, _surf.page.url)
        return

    if cmd == "help":
        await session.send(SURF_HELP_TEXT)
        return

    await session.send(f"❓ Неизвестная команда: {raw}\nВведи .help")

# ─────────────────────────────────────────────────────────────────────────────
# Тексты
# ─────────────────────────────────────────────────────────────────────────────

SURF_HELP_TEXT = """\
🌐 Команды режима сёрфинга:

.go <url>       — открыть страницу (можно без https://)
.reload         — перезагрузить страницу
.screen         — скриншот без действий
.down [px]      — прокрутить вниз (по умолч. 600px)
.up [px]        — прокрутить вверх
.click          — выбрать место клика (сетка 3×3, макс. 3 шага)
.type <текст>   — напечатать текст в активный элемент
.enter          — нажать Enter
.back           — страница назад
.fwd            — страница вперёд
.exit           — выйти из режима (браузер остаётся)\
"""

HELP_TEXT = """\
📋 Список команд:

.start          — приветствие
.help           — этот список

.dl <URL>       — скачать файл по ссылке
.wl             — белые списки для VPN (РФ)
.news           — последние новости BBC
.meme [ru|en]   — случайный мем (по умолч. ru)

.startserf      — запустить браузер для сёрфинга
.serf           — войти в режим сёрфинга
.stopserf       — остановить браузер

Пробел после точки допустим: «. help» = «.help»\
"""

START_TEXT = """\
👋 Привет! Я бот-помощник.

У меня есть полный доступ в интернет, поэтому я могу:

📥 Скачивать файлы по ссылке     → .dl <URL>
📄 Отдавать белые списки для VPN  → .wl
📰 Показывать новости BBC         → .news
😂 Случайный мем (ru/en)          → .meme / .meme en
🌐 Серфить интернет               → .startserf → .serf

Введи .help чтобы увидеть все команды.\
"""

# ─────────────────────────────────────────────────────────────────────────────
# Главный обработчик команд
# ─────────────────────────────────────────────────────────────────────────────

async def handle_command(text: str, session: "BotSession") -> None:
    if _surf.active:
        await handle_surf_command(text, session)
        return

    cmd = _parse_cmd(text)
    if cmd is None:
        return

    if cmd == "start":
        await session.send(START_TEXT)
        return

    if cmd == "help":
        await session.send(HELP_TEXT)
        return

    if cmd == "wl":
        await session.send("⬇️ Скачиваю белые списки...")
        for wl in WHITELISTS:
            await _download_and_send(wl["url"], wl["name"], session)
        return

    if cmd == "news":
        await session.send("⏳ Загружаю новости BBC...")
        news = await fetch_bbc_news()
        await session.send(news)
        return

    meme_m = re.fullmatch(r"meme(?:\s+(ru|en))?", cmd)
    if meme_m:
        lang = (meme_m.group(1) or "ru").lower()
        await session.send(f"🎲 Ищу {'русский' if lang == 'ru' else 'английский'} мем...")
        image_url, title = await fetch_random_meme(lang)
        if image_url:
            tmp = await _download_to_tmp(image_url)
            if tmp:
                try:
                    await asyncio.wait_for(session.send_image(tmp, title), timeout=15.0)
                except asyncio.TimeoutError:
                    await session.send(f"⚠️ Не удалось отправить картинку.\n{image_url}")
                finally:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
            else:
                await session.send(f"❌ Не удалось скачать картинку.\n{image_url}")
        else:
            await session.send(title or "❌ Не удалось получить мем.")
        return

    if cmd == "startserf":
        await browser_start(session)
        return

    if cmd == "serf":
        if not (_surf.browser and _surf.browser.is_connected()):
            await session.send("⚠️ Браузер не запущен. Сначала: .startserf")
            return
        _surf.active = True
        current_url  = _surf.page.url if _surf.page else "about:blank"
        await session.send(
            f"🌐 Режим сёрфинга активен!\n"
            f"Текущая страница: {current_url}\n\n"
            f"{SURF_HELP_TEXT}"
        )
        asyncio.create_task(_send_screenshot(session, current_url))
        return

    if cmd == "stopserf":
        await browser_stop(session)
        return

    dl_m = re.match(r"^dl\s+\S+$", cmd)
    if dl_m:
        url      = _parse_arg(text, "dl")
        filename = _filename_from_url(url)
        await session.send(f"⬇️ Скачиваю: {url}")
        await _download_and_send(url, filename, session)
        return

    await session.send(
        f"❓ Неизвестная команда: {text.strip()}\n"
        f"Введи .help чтобы увидеть список команд."
    )

# ─────────────────────────────────────────────────────────────────────────────
# Утилиты
# ─────────────────────────────────────────────────────────────────────────────

def _filename_from_url(url: str) -> str:
    name = Path(urlparse(url).path).name
    return name if name else "file"


async def _download_to_tmp(url: str) -> Path | None:
    filename = _filename_from_url(url) or "meme.jpg"
    tmp_path = Path(tempfile.gettempdir()) / filename
    headers  = {"User-Agent": "Mozilla/5.0 (compatible; TelegramBot/1.0)"}
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        tmp_path.write_bytes(resp.content)
        return tmp_path
    except Exception:
        return None


async def _download_and_send(url: str, filename: str, session: "BotSession") -> None:
    tmp_path = Path(tempfile.gettempdir()) / filename
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        tmp_path.write_bytes(resp.content)
        await session.send_file(tmp_path)
    except Exception as e:
        await session.send(f"❌ Ошибка при скачивании {filename}:\n{e}")
    finally:
        if tmp_path.exists():
            try:
                os.remove(tmp_path)
            except OSError:
                pass

# ─────────────────────────────────────────────────────────────────────────────
# BotSession — обёртка над ChatSession с фильтрацией собственных сообщений
# ─────────────────────────────────────────────────────────────────────────────

class BotSession:
    """
    Оборачивает ChatSession и запоминает тексты которые бот сам отправил,
    чтобы не реагировать на них как на входящие команды.
    """

    _CACHE_LIMIT = 200

    def __init__(self, session: ChatSession):
        self._s    = session
        self._sent: list[str] = []

    def _remember(self, text: str) -> None:
        key = text.strip()[:60]
        self._sent.append(key)
        if len(self._sent) > self._CACHE_LIMIT:
            self._sent.pop(0)

    def is_bot_message(self, text: str) -> bool:
        return text.strip()[:60] in self._sent

    async def send(self, text: str) -> None:
        self._remember(text)
        await self._s.send(text)

    async def send_image(self, image_path, caption: str | None = None) -> None:
        if caption:
            self._remember(caption)
        await self._s.send_image(image_path, caption)

    async def send_file(self, file_path) -> None:
        await self._s.send_file(file_path)

    def listen(self, callback, poll_interval: float = 1.5):
        return self._s.listen(callback, poll_interval)

    def stop_listening(self) -> None:
        self._s.stop_listening()

# ─────────────────────────────────────────────────────────────────────────────
# Точка входа
# ─────────────────────────────────────────────────────────────────────────────

async def run_worker(page: Page):
    raw_session = await open_chat(page, CHAT_NAME)
    if not raw_session:
        print(f"[run_worker] Не удалось открыть чат '{CHAT_NAME}'.")
        await asyncio.Future()
        return

    session = BotSession(raw_session)
    await session.send("✅ Бот запущен! Введи .help чтобы увидеть команды.")

    async def on_message(text: str, is_out: bool) -> None:
        if session.is_bot_message(text):
            return
        asyncio.create_task(handle_command(text, session))

    task = session.listen(on_message)
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.Future()