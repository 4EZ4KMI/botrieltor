from __future__ import annotations

import json
import re


class TelegramPublisher:
    """Creates a deterministic Telegram post and records its Telegram IDs."""

    def __init__(self, store, omniroute, telegram):
        self.store = store
        self.omniroute = omniroute
        self.telegram = telegram

    def _require_posting_enabled(self):
        if not self.store.telegram_posting_enabled():
            raise ValueError("Публикации в Telegram выключены. Включите их переключателем в приложении.")

    @staticmethod
    def _text(response):
        content = response.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0]
        try:
            value = json.loads(content)
        except json.JSONDecodeError:
            raise ValueError("Агент не смог подготовить Telegram-пост. Повторите позже.") from None
        text = str(value.get("text", "")).strip() if isinstance(value, dict) else ""
        if not text or len(text) > 4096:
            raise ValueError("Агент вернул пустой или слишком длинный Telegram-пост.")
        return text

    @staticmethod
    def _contacts(text):
        text = str(text or "")
        handles = {value.lower() for value in re.findall(r"(?<![\w@])@[A-Za-z0-9_]{5,}", text)}
        phones = set()
        for value in re.findall(r"(?<!\d)(?:\+?\d[\s().-]*){9,15}(?!\d)", text):
            digits = re.sub(r"\D", "", value)
            if 9 <= len(digits) <= 12 and (digits.startswith("0") or digits.startswith("84") or digits.startswith("7")):
                phones.add(digits)
        return handles, phones

    @classmethod
    def _strip_source_contacts(cls, text):
        contact_words = re.compile(r"(?i)\b(contact|hotline|phone|zalo|whats?app|telegram|li[eê]n\s*h[eệ]|телефон|контакт)\b")
        clean = []
        for line in str(text or "").splitlines():
            handles, phones = cls._contacts(line)
            if handles or phones or contact_words.search(line):
                continue
            clean.append(line)
        return "\n".join(clean).strip()

    @classmethod
    def _validate_contacts(cls, text, example):
        actual_handles, actual_phones = cls._contacts(text)
        allowed_handles, allowed_phones = cls._contacts(example)
        unknown = sorted((actual_handles - allowed_handles) | (actual_phones - allowed_phones))
        if unknown:
            raise ValueError("Агент попытался добавить контакт, которого нет в вашем примере. Публикация остановлена.")
        missing = sorted((allowed_handles - actual_handles) | (allowed_phones - actual_phones))
        if missing:
            raise ValueError("Агент не добавил ваш контакт из примера. Публикация остановлена.")

    @staticmethod
    def _contact_footer(_example=""):
        return "По всем вопросам: @aebussin"

    @staticmethod
    def _validate_format(text, contact_footer):
        """Reject any generated copy that departs from the agreed minimal template."""
        lines = str(text).strip().splitlines()
        expected_length = 12
        valid = (
            len(lines) == expected_length
            and re.fullmatch(r"НЯЧАНГ(?: \| МОЖНО С ЖИВОТНЫМИ)?", lines[0])
            and lines[1] == ""
            and re.fullmatch(r"Локация: .+", lines[2])
            and lines[3] == ""
            and re.fullmatch(r"• .+ VND / месяц", lines[4])
            and re.fullmatch(r"• Депозит: .+", lines[5])
            and re.fullmatch(r"• Оплата: .+", lines[6])
            and re.fullmatch(r"• Контракт: .+", lines[7])
            and lines[8] == ""
            and lines[9] == "Мы НЕ берем с вас дополнительную плату и комиссии."
            and lines[10] == ""
            and lines[11] == contact_footer
        )
        if not valid:
            raise ValueError("Публикация остановлена: пост не соответствует согласованному короткому формату.")

    @staticmethod
    def _source_text(listing):
        parts = [listing.get("title", ""), listing.get("description", ""), listing.get("price", "")]
        parts.extend(item.get("text", "") for item in listing.get("source_messages", []))
        return "\n".join(str(part) for part in parts if part)

    @classmethod
    def _location_fact(cls, text, explicit=""):
        explicit = cls._strip_source_contacts(re.sub(r"\s+", " ", str(explicit or "")).strip())
        if explicit:
            return explicit[:200]
        patterns = [
            r"^\s*(?:[📍📌•-]\s*)?(?:địa\s*chỉ|dia\s*chi|địa\s*điểm|dia\s*diem|vị\s*trí|vi\s*tri|khu\s*vực|khu\s*vuc|address|location|district|ward|локация|адрес|район)\s*[:：-]\s*(.+)$",
        ]
        for line in str(text).splitlines():
            location = cls._fact(line, patterns, fallback="")
            # If contact details share a line with the address, skip it rather
            # than risk copying a source contact into the public post.
            location = cls._strip_source_contacts(location)
            if location:
                return location
        return "не указана"

    @staticmethod
    def _fact(text, patterns, fallback="не указан"):
        for line in str(text).splitlines():
            for pattern in patterns:
                match = re.search(pattern, line, re.IGNORECASE)
                if match:
                    value = re.sub(r"\s+", " ", match.group(1)).strip(" •:-,;.")
                    if value:
                        return value
        return fallback

    @staticmethod
    def _month_word(amount):
        try:
            value = float(str(amount).replace(",", "."))
        except ValueError:
            return "месяцев"
        if not value.is_integer():
            return "месяца"
        number = int(value)
        if number % 10 == 1 and number % 100 != 11:
            return "месяц"
        if number % 10 in (2, 3, 4) and number % 100 not in (12, 13, 14):
            return "месяца"
        return "месяцев"

    @classmethod
    def _russianize_value(cls, value, *, contract=False):
        value = re.sub(r"\s+", " ", str(value or "")).strip()
        value = re.sub(r"(?i)(\d+(?:[.,]\d+)?)\s*tháng\b",
                       lambda match: f"{match.group(1)} {cls._month_word(match.group(1))}", value)
        # Zalo listings often omit spaces and Vietnamese users may mistype
        # triệu as triẹu or trieu. Accept these forms before the Russian post
        # is assembled so the source spelling cannot leak into Telegram.
        value = re.sub(r"(?i)(\d+(?:[.,]\d+)?)\s*(?:tri(?:ệ|ẹ|e)u|tr)\b", r"\1 млн", value)
        value = re.sub(r"(?i)^trước\s+", "", value)
        value = re.sub(r"(?i)\blinh\s*hoạt\b", "гибкие условия" if contract else "гибкий", value)
        value = re.sub(r"(?i)\bdài\s*hạn\b", "долгосрочный", value)
        value = re.sub(r"(?i)\bngắn\s*hạn\b", "краткосрочный", value)
        value = re.sub(r"(?i)\bkhông\s+c[oó]\b", "не предусмотрено", value)
        return value

    @classmethod
    def _russianize_rental_term(cls, value):
        value = cls._russianize_value(value)
        # In the deposit/payment fields, a bare small number is the common
        # shorthand for months (for example, "cọc 1, thanh toán 1").
        if re.fullmatch(r"\d+(?:[.,]\d+)?", value):
            try:
                amount = float(value.replace(",", "."))
            except ValueError:
                amount = 4
            if 0 < amount <= 3:
                return f"{value} {cls._month_word(value)}"
        return value

    @classmethod
    def _payment_fact(cls, text):
        amount = re.compile(
            r"(?:thanh\s*to[aá]n|payment)\s*(?:trước\s*)?[:：-]?\s*"
            r"([\d.,]+\s*(?:tháng|months?|month|triệu|tr|million|млн|vnd|vnđ|đồng|đ|₫)?)",
            re.IGNORECASE,
        )
        for line in str(text or "").splitlines():
            matches = list(amount.finditer(line))
            if matches:
                return cls._russianize_value(matches[-1].group(1))
        return cls._russianize_value(cls._fact(text, [
            r"(?:оплата|период\s+оплаты|payment)\s*[:：-]?\s*(.+)$",
        ]))

    @classmethod
    def _compose(cls, listing, _example=""):
        source = cls._source_text(listing)
        price = str(listing.get("price", "")).strip()
        if not price or price.lower() in {"не указан", "unknown", "n/a"}:
            price = cls._fact(source, [r"(?:цена|стоимость|price|gi[aá])\s*[:：-]?\s*([\d.,]+(?:\s*(?:млн|million|triệu|tr|k))?)"])
        price = re.sub(r"(?i)\s*(?:đồng|dong|vnd|vnđ|đ|₫)\s*", " ", price).strip(" •:-,;.")
        price = re.sub(r"(?i)\s*(?:/\s*(?:month|tháng|месяц)|per\s+month)\s*$", "", price).strip()
        price = cls._russianize_value(price) or "не указан"

        deposit = cls._fact(source, [
            r"(?:депозит|залог|deposit|cọc)\s*[:：-]?\s*(.+)$",
        ])
        deposit = re.split(r"(?i)\s*(?:[-–—,+;]\s*)?(?=thanh\s*to[aá]n|payment|оплата\b)", deposit, maxsplit=1)[0].strip()
        deposit = cls._russianize_rental_term(deposit)
        payment = cls._russianize_rental_term(cls._payment_fact(source))
        contract = cls._fact(source, [
            r"(?:контракт|срок\s+(?:аренды|контракта)|contract|hợp\s*đồng|thời\s*hạn\s*thuê)\s*[:：-]?\s*(.+)$",
        ])
        contract = cls._russianize_value(contract, contract=True)
        pets_allowed = bool(re.search(
            r"(?:можно\s+с\s+животными|разрешены?\s+животные|pets?\s+(?:allowed|ok)|cho\s+ph[eé]p\s+th[uú]\s*cưng|nhận\s+(?:pet|th[uú]\s*cưng))",
            source, re.IGNORECASE))
        headline = "НЯЧАНГ | МОЖНО С ЖИВОТНЫМИ" if pets_allowed else "НЯЧАНГ"
        text = "\n".join([
            headline, "", f"Локация: {cls._location_fact(source, listing.get('location'))}", "",
            f"• {price} VND / месяц", f"• Депозит: {deposit}",
            f"• Оплата: {payment}", f"• Контракт: {contract}", "",
            "Мы НЕ берем с вас дополнительную плату и комиссии.", "",
            "По всем вопросам: @aebussin",
        ])
        cls._validate_format(text, "По всем вопросам: @aebussin")
        return text

    @staticmethod
    def _require_photos(listing):
        if not listing.get("media_paths"):
            raise ValueError("Объект нельзя публиковать без фотографий. Сначала дождитесь их загрузки из Zalo.")
        if not listing.get("description", "").strip():
            raise ValueError("Объект нельзя публиковать без текста описания.")

    def repair_publication(self, listing_id):
        self._require_posting_enabled()
        example = ""
        publication = self.store.get_publication(listing_id)
        listing = self.store.get_listing_for_publication(listing_id)
        text = self._compose(listing, example)
        message_ids = publication["message_ids"]
        if not message_ids:
            raise ValueError("У публикации нет Telegram message ID.")
        self.telegram.edit_post(
            message_ids[0], text,
            has_media=bool(listing["media_paths"]) and len(text) <= 1024,
        )
        self.store.update_publication_body(publication["id"], listing_id, text)
        return {"message_ids": message_ids, "text": text}

    def republish(self, listing_id):
        self._require_posting_enabled()
        settings = self.store.get_settings("telegram_channel_id")
        channel_id = str(settings.get("telegram_channel_id", "")).strip()
        example = ""
        if not channel_id:
            raise ValueError("Сначала подключите Telegram-канал.")
        listing = self.store.get_listing_for_publication(listing_id)
        if listing["status"] == "rented":
            raise ValueError("Повторно публиковать можно только доступный объект.")
        if listing["status"] != "available":
            self.store.set_listing_status(listing_id, "available", listing["revision"], origin="manual")
            listing = self.store.get_listing_for_publication(listing_id)
        self._require_photos(listing)
        text = self._compose(listing, example)
        message_ids = self.telegram.publish(text, listing["media_paths"])
        self.store.replace_publication(listing_id, channel_id, message_ids, text)
        self.store.finish_listing_automation(listing_id)
        return {"message_ids": message_ids, "text": text}

    def publish_existing_once(self, listing_id):
        """Publish a previously confirmed available card only when it has no sent post."""
        try:
            publication = self.store.get_publication(listing_id)
            return {"message_ids": publication["message_ids"], "text": publication["body"], "already_sent": True}
        except ValueError:
            pass
        return self.republish(listing_id)

    @staticmethod
    def _rented_text(body):
        lines = str(body or "").splitlines()
        price = re.compile(r"(?i)(💰|💵|стоим|цена|price|gi[aá]|\b(?:vnd|vnđ|đ|₫)\b|донг)")
        replacement = "⛔️ СДАНО / НЕАКТУАЛЬНО"
        for index, line in enumerate(lines):
            if price.search(line):
                lines[index] = replacement
                break
        else:
            lines.insert(1 if lines else 0, replacement)
        return "\n".join(lines).strip()

    def process_outbox(self):
        if not self.store.telegram_posting_enabled():
            return False
        task = self.store.claim_outbox()
        if not task:
            return False
        try:
            if not task["message_ids"]:
                self.store.finish_outbox(task["id"])
                return True
            text = task["body"]
            if task["status"] == "rented":
                text = self._rented_text(text)
            # Long posts are sent as a standalone text message before photos;
            # short posts use the first photo caption.
            has_caption = bool(task["media_count"]) and len(str(task["body"])) <= 1024
            self.telegram.edit_post(task["message_ids"][0], text, has_media=has_caption)
            self.store.finish_outbox(task["id"])
        except Exception as error:
            self.store.fail_outbox(task["id"], error)
        return True

    def publish_available(self, listing_id, expected_revision):
        self._require_posting_enabled()
        settings = self.store.get_settings("telegram_channel_id")
        channel_id = str(settings.get("telegram_channel_id", "")).strip()
        example = ""
        if not channel_id:
            raise ValueError("Сначала подключите Telegram-канал.")
        listing = self.store.get_listing_for_publication(listing_id)
        self._require_photos(listing)
        reservation = self.store.reserve_publication(listing_id, channel_id, expected_revision)
        if reservation["already_sent"]:
            raise ValueError("Этот объект уже опубликован в канале.")
        publication_id = reservation["id"]
        try:
            text = self._compose(listing, example)
            message_ids = self.telegram.publish(text, listing["media_paths"])
            self.store.complete_publication(publication_id, listing_id, expected_revision, message_ids, text)
            return {"message_ids": message_ids, "text": text}
        except Exception as error:
            self.store.fail_publication(publication_id, error)
            raise
