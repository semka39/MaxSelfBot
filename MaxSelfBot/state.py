"""
state.py — персистентное хранилище состояния бота.

Сохраняет на диск последний обработанный data-index для каждого чата,
чтобы после перезапуска бот не отвечал повторно на старые сообщения.

Формат файла (bot_state.json):
    {
        "<chat_id>": <last_seen_index>,
        ...
    }

Использование:
    state = BotState()                        # загружает файл если есть
    idx   = state.get_last_index("56366434")  # None если чат новый
    state.update("56366434", 142)             # сохраняет сразу на диск
"""

import json
from pathlib import Path

DEFAULT_PATH = Path("bot_state.json")


class BotState:
    def __init__(self, path: Path = DEFAULT_PATH):
        self._path: Path = path
        self._data: dict[str, int] = {}
        self._load()

    # ── Чтение ────────────────────────────────

    def get_last_index(self, chat_id: str) -> int | None:
        """
        Возвращает последний сохранённый index для чата,
        или None если чат встречается впервые.
        """
        return self._data.get(chat_id)

    # ── Запись ────────────────────────────────

    def update(self, chat_id: str, index: int) -> None:
        """
        Обновляет last_seen_index для чата и сразу сбрасывает файл.
        Сохраняет только если index больше текущего (защита от гонки).
        """
        current = self._data.get(chat_id)
        if current is not None and index <= current:
            return
        self._data[chat_id] = index
        self._save()

    # ── Внутренние методы ─────────────────────

    def _load(self) -> None:
        if not self._path.exists():
            print(f"[BotState] Файл '{self._path}' не найден — начинаем с чистого состояния.")
            return
        try:
            raw = self._path.read_text(encoding="utf-8")
            self._data = json.loads(raw)
            print(f"[BotState] Загружено состояние: {len(self._data)} чат(ов).")
        except Exception as e:
            print(f"[BotState] Не удалось прочитать '{self._path}': {e} — начинаем с чистого.")
            self._data = {}

    def _save(self) -> None:
        try:
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._path)  # атомарная замена
        except Exception as e:
            print(f"[BotState] Ошибка сохранения состояния: {e}")
