from __future__ import annotations

import json
import html
import subprocess
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .omniroute import KeychainSecretStore


# Zalo's rendered thumbnails are often only a few hundred pixels wide. Allow
# portrait photos such as 768x1024, while rejecting small chat previews.
_MIN_PHOTO_EDGE = 500
_MIN_PHOTO_PIXELS = 480_000
_MIN_PHOTO_MAX_EDGE = 800
_MAX_PHOTOS = 10
_IMAGE_HEADER_LIMIT = 2 * 1024 * 1024


def _image_dimensions(path):
    """Read JPEG, PNG, or WebP dimensions without loading/decoding the image."""
    with open(path, "rb") as image:
        header = image.read(32)
        if header.startswith(b"\x89PNG\r\n\x1a\n"):
            if len(header) < 24 or header[12:16] != b"IHDR":
                raise ValueError("повреждённый PNG")
            return int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")

        if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
            image.seek(12)
            scanned = 12
            while scanned < _IMAGE_HEADER_LIMIT:
                chunk = image.read(8)
                if len(chunk) != 8:
                    break
                kind, size = chunk[:4], int.from_bytes(chunk[4:], "little")
                scanned += 8
                if kind == b"VP8X":
                    data = image.read(10)
                    if len(data) < 10:
                        break
                    return (int.from_bytes(data[4:7], "little") + 1,
                            int.from_bytes(data[7:10], "little") + 1)
                if kind == b"VP8L":
                    data = image.read(5)
                    if len(data) < 5 or data[0] != 0x2f:
                        break
                    bits = int.from_bytes(data[1:5], "little")
                    return ( (bits & 0x3fff) + 1, ((bits >> 14) & 0x3fff) + 1 )
                if kind == b"VP8 ":
                    data = image.read(10)
                    if len(data) < 10 or data[3:6] != b"\x9d\x01\x2a":
                        break
                    return (int.from_bytes(data[6:8], "little") & 0x3fff,
                            int.from_bytes(data[8:10], "little") & 0x3fff)
                skip = size + (size & 1)
                image.seek(skip, 1)
                scanned += skip
            raise ValueError("не удалось прочитать размеры WebP")

        if header.startswith(b"\xff\xd8"):
            image.seek(2)
            scanned = 2
            sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                           0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
            while scanned < _IMAGE_HEADER_LIMIT:
                byte = image.read(1)
                if not byte:
                    break
                scanned += 1
                if byte != b"\xff":
                    continue
                marker_byte = image.read(1)
                while marker_byte == b"\xff":
                    marker_byte = image.read(1)
                    scanned += 1
                if not marker_byte:
                    break
                scanned += 1
                marker = marker_byte[0]
                if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7 or marker == 0x01:
                    continue
                raw_length = image.read(2)
                if len(raw_length) != 2:
                    break
                length = int.from_bytes(raw_length, "big")
                if length < 2:
                    break
                scanned += length
                if marker in sof_markers:
                    data = image.read(5)
                    if len(data) < 5:
                        break
                    return int.from_bytes(data[3:5], "big"), int.from_bytes(data[1:3], "big")
                image.seek(length - 2, 1)
            raise ValueError("не удалось прочитать размеры JPEG")

    raise ValueError("формат не JPEG, PNG или WebP")


def _validate_photo(path):
    try:
        width, height = _image_dimensions(path)
    except (OSError, ValueError) as error:
        raise ValueError(f"Фото {Path(path).name}: {error}.") from None
    if min(width, height) < _MIN_PHOTO_EDGE or width * height < _MIN_PHOTO_PIXELS or max(width, height) < _MIN_PHOTO_MAX_EDGE:
        raise ValueError(
            f"Фото {Path(path).name} слишком маленькое: {width}×{height}. "
            f"Нужно минимум {_MIN_PHOTO_EDGE} px по короткой стороне, "
            f"{_MIN_PHOTO_PIXELS // 1000} тыс. пикселей и {_MIN_PHOTO_MAX_EDGE} px по длинной стороне; "
            "публикация отменена, загрузите оригинал из Zalo."
        )


class TelegramClient:
    """Validates a Bot API token and a single Telegram channel destination."""

    def __init__(self, store, secrets=None, network_helper=None):
        self.store = store
        self.secrets = secrets or KeychainSecretStore(service="local.botrieltor.telegram")
        default_helper = Path(__file__).resolve().parent.parent / "BotRieltorTelegram"
        if not default_helper.is_file():
            app_helper = Path(__file__).resolve().parent.parent / "dist" / "BotRieltor.app" / "Contents" / "Resources" / "BotRieltorTelegram"
            if app_helper.is_file():
                default_helper = app_helper
        self.network_helper = network_helper or default_helper
        self.connected = False
        self.last_error = ""

    def snapshot(self):
        settings = self.store.get_settings("telegram_channel_id", "telegram_channel_title", "telegram_bot_username", "telegram_message_example")
        has_token = self.connected or bool(settings.get("telegram_channel_id"))
        return {"connected": self.connected or (has_token and not self.last_error),
                "has_token": has_token, "error": self.last_error,
                "channel_id": settings.get("telegram_channel_id", ""),
                "channel_title": settings.get("telegram_channel_title", ""),
                "bot_username": settings.get("telegram_bot_username", ""),
                "message_example": settings.get("telegram_message_example", "")}

    def _call(self, token, method, **parameters):
        if self.network_helper.is_file():
            return self._call_macos(token, method, parameters)
        request = Request(f"https://api.telegram.org/bot{token}/{method}",
                          data=json.dumps(parameters).encode(), method="POST",
                          headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urlopen(request, timeout=12) as response:
                payload = json.load(response)
        except HTTPError as error:
            try:
                description = json.load(error).get("description", "")
            except Exception:
                description = ""
            if error.code in (401, 404):
                message = "Telegram отклонил токен бота."
            elif error.code == 400 and description:
                message = "Telegram: " + description
            else:
                message = f"Telegram вернул HTTP {error.code}."
            raise ValueError(message) from None
        except (URLError, TimeoutError, OSError, json.JSONDecodeError):
            raise ValueError("Не удалось подключиться к Telegram Bot API.") from None
        if not payload.get("ok"):
            self._raise_api_error(payload)
        return payload.get("result")

    @staticmethod
    def _raise_api_error(payload):
        description = str(payload.get("description") or "запрос отклонён")
        if "unauthorized" in description.lower() or "not found" in description.lower():
            raise ValueError("Telegram отклонил токен бота.")
        raise ValueError("Telegram: " + description)

    def _call_macos(self, token, method, parameters, files=None):
        # The helper uses the macOS curl trust chain. The token travels through
        # stdin and never appears in argv or a persistent file.
        envelope = json.dumps({"token": token, "method": method, "parameters": parameters,
                               "files": files or {}}).encode()
        try:
            result = subprocess.run([str(self.network_helper)], input=envelope,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=45, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError("Не удалось подключиться к Telegram Bot API.") from None
        if result.returncode:
            raise ValueError("Не удалось подключиться к Telegram Bot API.")
        try:
            payload = json.loads(result.stdout)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("Telegram вернул некорректный ответ.") from None
        if not payload.get("ok"):
            self._raise_api_error(payload)
        return payload.get("result")

    def publish(self, text, media_paths=None):
        settings = self.store.get_settings("telegram_channel_id")
        channel_id = settings.get("telegram_channel_id", "")
        if not channel_id:
            raise ValueError("Сначала подключите Telegram-канал.")
        text = str(text).strip()
        if not text or len(text) > 4096:
            raise ValueError("Текст Telegram-поста пуст или длиннее 4096 символов.")
        formatted_text = self._format_post_text(text)
        token = self.secrets.get()
        if not token:
            raise ValueError("В настройках Telegram нет токена бота.")
        media = [str(Path(path)) for path in (media_paths or [])]
        if len(media) > _MAX_PHOTOS:
            raise ValueError(f"В объявлении {_MAX_PHOTOS} фото максимум; получено {len(media)}.")
        missing = [path for path in media if not Path(path).is_file()]
        if missing:
            names = ", ".join(Path(path).name for path in missing[:3])
            suffix = "…" if len(missing) > 3 else ""
            raise ValueError(f"Не найдены файлы фото: {names}{suffix}. Публикация отменена.")
        # Validate the entire batch before sending either its caption or images,
        # so a rejected preview cannot leave a partial Telegram post behind.
        for path in media:
            _validate_photo(path)
        message_ids = []
        if media:
            has_caption = len(text) <= 1024
            if not has_caption:
                # Keep the full post in its own message. Telegram photo captions
                # are limited to 1024 characters, so never silently truncate it.
                result = self._call_macos(token, "sendMessage", {
                    "chat_id": channel_id, "text": formatted_text, "parse_mode": "HTML",
                })
                message_ids.append(int(result["message_id"]))
            caption = self._format_post_text(text) if has_caption else ""
            if len(media) == 1:
                parameters = {"chat_id": channel_id}
                if has_caption:
                    parameters.update(caption=caption, parse_mode="HTML")
                result = self._call_macos(token, "sendPhoto", parameters, {"photo": media[0]})
                message_ids.append(int(result["message_id"]))
            else:
                media_spec = []
                files = {}
                for index, path in enumerate(media):
                    name = f"photo{index}"
                    item = {"type": "photo", "media": f"attach://{name}"}
                    if index == 0 and has_caption:
                        item["caption"] = caption
                        item["parse_mode"] = "HTML"
                    media_spec.append(item)
                    files[name] = path
                result = self._call_macos(token, "sendMediaGroup",
                                          {"chat_id": channel_id, "media": media_spec}, files)
                message_ids.extend(int(item["message_id"]) for item in result)
        else:
            result = self._call_macos(token, "sendMessage", {"chat_id": channel_id, "text": formatted_text, "parse_mode": "HTML"})
            message_ids.append(int(result["message_id"]))
        return message_ids

    def edit_post(self, message_id, text, *, has_media=False):
        settings = self.store.get_settings("telegram_channel_id")
        channel_id = settings.get("telegram_channel_id", "")
        if not channel_id:
            raise ValueError("Сначала подключите Telegram-канал.")
        text = str(text).strip()
        limit = 1024 if has_media else 4096
        if not text or len(text) > limit:
            raise ValueError(f"Текст публикации должен быть не длиннее {limit} символов.")
        formatted_text = self._format_post_text(text)
        token = self.secrets.get()
        if not token:
            raise ValueError("В настройках Telegram нет токена бота.")
        method = "editMessageCaption" if has_media else "editMessageText"
        field = "caption" if has_media else "text"
        return self._call_macos(token, method, {"chat_id": channel_id, "message_id": int(message_id), field: formatted_text, "parse_mode": "HTML"})

    @staticmethod
    def _format_post_text(text):
        """Escape plain post text and render the commission line as a Telegram quote."""
        quote = "Мы НЕ берем с вас дополнительную плату и комиссии."
        lines = []
        for line in str(text).splitlines():
            escaped = html.escape(line, quote=False)
            if line.strip() == quote:
                escaped = f"<blockquote>{escaped}</blockquote>"
            lines.append(escaped)
        return "\n".join(lines)

    @staticmethod
    def _channel(value):
        value = str(value).strip()
        if not value:
            raise ValueError("Введите @username или числовой ID Telegram-канала.")
        if value.startswith("@") and len(value) >= 6:
            return value
        if value.startswith("-100") and value[1:].isdigit():
            return value
        raise ValueError("Канал должен быть указан как @username или ID вида -100…")

    @classmethod
    def validate_message_example(cls, value):
        value = str(value or "").strip()
        if len(value) > 20000:
            raise ValueError("Пример сообщения не должен быть длиннее 20 000 символов.")
        return value

    def configure(self, channel_id, token="", message_example=None):
        channel_id = self._channel(channel_id)
        message_example = None if message_example is None else self.validate_message_example(message_example)
        token = str(token).strip()
        if token:
            if ":" not in token or not token.split(":", 1)[0].isdigit():
                raise ValueError("Токен Telegram-бота имеет неверный формат.")
        candidate = token or self.secrets.get()
        if not candidate:
            raise ValueError("Введите токен Telegram-бота.")
        return self.test(channel_id, candidate, persist_token=bool(token), message_example=message_example)

    def test(self, channel_id=None, token=None, persist_token=False, message_example=None):
        settings = self.store.get_settings("telegram_channel_id")
        channel_id = self._channel(channel_id or settings.get("telegram_channel_id", ""))
        token = token or self.secrets.get()
        if not token:
            raise ValueError("Введите токен Telegram-бота.")
        try:
            bot = self._call(token, "getMe")
            chat = self._call(token, "getChat", chat_id=channel_id)
            if chat.get("type") != "channel":
                raise ValueError("Указан не Telegram-канал.")
            member = self._call(token, "getChatMember", chat_id=chat["id"], user_id=bot["id"])
            if member.get("status") not in ("administrator", "creator") or not member.get("can_post_messages", member.get("status") == "creator"):
                raise ValueError("Добавьте бота администратором канала с правом публикации сообщений.")
            canonical_id = str(chat["id"])
            if persist_token:
                self.secrets.set(token)
            self.store.set_settings(telegram_channel_id=canonical_id,
                                    telegram_channel_title=str(chat.get("title", "")),
                                    telegram_bot_username=str(bot.get("username", "")),
                                    **({"telegram_message_example": message_example} if message_example is not None else {}))
            self.connected, self.last_error = True, ""
            self.store.runtime_event(f"Telegram подключён: канал «{chat.get('title', canonical_id)}», бот @{bot.get('username', '')}.")
            return self.snapshot()
        except ValueError as error:
            self.connected, self.last_error = False, str(error)
            raise
