from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, TypeVar

import structlog
from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.methods import (
    EditMessageCaption,
    EditMessageMedia,
    EditMessageText,
    SendAnimation,
    SendAudio,
    SendDocument,
    SendMediaGroup,
    SendMessage,
    SendPhoto,
    SendVideo,
    SendVoice,
    TelegramMethod,
)
from aiogram.types import InputMedia

logger = structlog.get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
EMOJIS_FILE = PROJECT_ROOT / 'data' / 'emojis.json'


class PremiumEmojiMiddleware(BaseRequestMiddleware):
    def __init__(self) -> None:
        super().__init__()
        self._emojis_map: dict[str, str] = {}
        self._last_loaded: float = 0.0
        self._load_emojis()

    def _load_emojis(self) -> None:
        """Загружает маппинг эмодзи из файла emojis.json с проверкой времени изменения."""
        if not EMOJIS_FILE.exists():
            self._emojis_map = {}
            return

        try:
            mtime = EMOJIS_FILE.stat().st_mtime
            if mtime > self._last_loaded:
                with EMOJIS_FILE.open('r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        # Фильтруем пустые значения и некорректные ключи
                        self._emojis_map = {str(k): str(v) for k, v in data.items() if k and v}
                        self._last_loaded = mtime
                        logger.info(
                            'Loaded premium emojis mapping',
                            count=len(self._emojis_map),
                            file=str(EMOJIS_FILE),
                        )
        except Exception as e:
            logger.error('Failed to load emojis.json', error=e)

    def _replace_html(self, text: str) -> str:
        """Заменяет обычные эмодзи на премиумные во фрагментах вне HTML тегов."""
        if not text or not self._emojis_map:
            return text

        # Разделяем по тегам HTML, чтобы не затронуть эмодзи в атрибутах (например, в href)
        parts = re.split(r'(<[^>]+>)', text)
        for i in range(len(parts)):
            # Четные индексы — это текст вне HTML тегов
            if i % 2 == 0:
                for emoji, emoji_id in self._emojis_map.items():
                    parts[i] = parts[i].replace(emoji, f'<tg-emoji emoji-id="{emoji_id}">{emoji}</tg-emoji>')
        return ''.join(parts)

    def _replace_markdown_v2(self, text: str) -> str:
        """Заменяет обычные эмодзи на премиумные в формате MarkdownV2.

        Формат премиум эмодзи в MarkdownV2: [emoji](tg://emoji?id=emoji_id)
        """
        if not text or not self._emojis_map:
            return text

        # Разделяем по markdown-ссылкам [текст](ссылка), чтобы не испортить их
        parts = re.split(r'(\[[^\]]+\]\([^)]+\))', text)
        for i in range(len(parts)):
            if i % 2 == 0:
                for emoji, emoji_id in self._emojis_map.items():
                    parts[i] = parts[i].replace(emoji, f'[{emoji}](tg://emoji?id={emoji_id})')
        return ''.join(parts)

    def _process_text(self, text: str | None, parse_mode: str | None) -> str | None:
        """Обрабатывает текст в зависимости от режима разметки (HTML / MarkdownV2)."""
        if not text:
            return text

        self._load_emojis()

        # Умолчания aiogram 3 используют Default object
        if parse_mode is not None and not isinstance(parse_mode, str):
            parse_mode = str(getattr(parse_mode, 'value', 'html')) if hasattr(parse_mode, 'value') else 'html'
            
        mode = (parse_mode or 'html').lower()
        if 'html' in mode:
            return self._replace_html(text)
        if 'markdown' in mode:
            # Для MarkdownV2 используем специальный формат
            return self._replace_markdown_v2(text)

        return text

    def _process_media(self, media: InputMedia, parse_mode: str | None) -> InputMedia:
        """Обрабатывает InputMedia объект (фото, видео в медиагруппах и т.д.)."""
        if not hasattr(media, 'caption') or not media.caption:
            return media

        media_parse_mode = getattr(media, 'parse_mode', None) or parse_mode
        new_caption = self._process_text(media.caption, media_parse_mode)
        if new_caption != media.caption:
            # Объекты Telegram API (aiogram.types) заморожены (frozen) в Pydantic v2.
            # Мы не можем менять их поля напрямую. Используем model_copy для обновления.
            if hasattr(media, 'model_copy'):
                return media.model_copy(update={'caption': new_caption})
            elif hasattr(media, 'copy'):
                return media.copy(update={'caption': new_caption})
        return media

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[Any],
        bot: Bot,
        method: TelegramMethod[Any],
    ) -> Any:
        # Проверяем тип запроса и обрабатываем соответствующие поля
        if isinstance(method, (SendMessage, EditMessageText)):
            method.text = self._process_text(method.text, method.parse_mode)

        elif isinstance(method, (SendPhoto, SendVideo, SendAnimation, SendAudio, SendDocument, SendVoice)):
            method.caption = self._process_text(method.caption, method.parse_mode)

        elif isinstance(method, EditMessageCaption):
            method.caption = self._process_text(method.caption, method.parse_mode)

        elif isinstance(method, EditMessageMedia):
            if method.media:
                method.media = self._process_media(method.media, getattr(method, 'parse_mode', None))

        elif isinstance(method, SendMediaGroup):
            if method.media:
                method.media = [
                    self._process_media(m, getattr(method, 'parse_mode', None))
                    for m in method.media
                ]

        return await make_request(bot, method)
