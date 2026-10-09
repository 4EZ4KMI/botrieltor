from __future__ import annotations

import hashlib
import base64
import json
import os
import re
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .adapters import BoundSource, RawCaptureBatch


class ZaloAgentCollector:
    """Visible Chrome UI driver that archives source evidence without a model."""

    def __init__(self, data_dir: Path, omniroute=None, node="/usr/local/bin/node", port=49222):
        self.data_dir = Path(data_dir)
        self.profile = self.data_dir / "chrome-profile"
        self.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.node = node
        self.port = port
        self.process = None
        self.lock = threading.Lock()
        self.sequence = 0

    def _start(self):
        if self.process and self.process.poll() is None:
            return
        script = Path(__file__).with_name("zalo_driver.mjs")
        self.process = subprocess.Popen([self.node, str(script), str(self.profile), str(self.port)],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        text=True, bufsize=1,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                        if os.name == "nt" else 0)

    def command(self, action, **values):
        with self.lock:
            self._start()
            self.sequence += 1
            request = {"id": self.sequence, "action": action, **values}
            self.process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError("Драйвер Zalo неожиданно остановился.")
            response = json.loads(line)
            if response.get("error"):
                raise RuntimeError(response["error"])
            return response.get("result", {})

    def open(self):
        return self.command("open")

    def status(self):
        try:
            return self.command("status")
        except Exception as error:
            return {"ready": False, "error": str(error)}

    def bind(self, source, cancel):
        if cancel.is_set():
            raise RuntimeError("Операция остановлена.")
        result = self.command("bind", source={"name": source["name"], "kind": source["kind"]})
        return BoundSource(result["external_ref"], result["observed_name"], result["observed_kind"])

    def _archive(self, source, snapshot):
        """Download every visible attachment and keep the UI structure untouched."""
        media = snapshot.get("media", [])
        downloaded_by_url = {}
        # Pre-populate with data already extracted at snapshot time
        for value in media:
            if value.get("src") and value.get("data"):
                downloaded_by_url[value["src"]] = {"url": value["src"], "data": value["data"], "mime": value.get("mime", "image/jpeg"), "ok": True}

        entries = []
        for value in media:
            reference = str(value.get("src", ""))
            if not reference or reference in downloaded_by_url:
                continue
            candidates = value.get("sources")
            if not isinstance(candidates, list):
                candidates = []
            candidates = list(dict.fromkeys(
                str(candidate) for candidate in [reference, *candidates] if candidate
            ))
            entries.append({"url": reference, "candidates": candidates,
                            "kind": value.get("kind", "image")})
        for start in range(0, len(entries), 8):
            try:
                result = self.command("download_media_batch", items=entries[start:start + 8])
                downloaded_by_url.update({item["url"]: item for item in result.get("items", []) if item.get("ok")})
            except Exception:
                pass
        attachments = []
        for value in media:
            reference = str(value.get("src", ""))
            if not reference:
                continue
            external_id = hashlib.sha256(reference.encode()).hexdigest()
            attachment = {"external_id": external_id, "kind": value.get("kind", "image"),
                          "source_ref": reference, "download_state": "pending",
                          "ui_index": value.get("index"), "alt": value.get("alt", ""),
                          "width": value.get("w", 0), "height": value.get("h", 0)}
            try:
                downloaded = downloaded_by_url.get(reference)
                if downloaded and downloaded.get("data"):
                    raw = base64.b64decode(downloaded["data"], validate=True)
                    digest = hashlib.sha256(raw).hexdigest()
                    extensions = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
                                  "image/gif": ".gif", "video/mp4": ".mp4"}
                    extension = extensions.get(str(downloaded.get("mime", "")).split(";", 1)[0], ".jpg")
                    directory = self.data_dir / "raw-media" / str(source["id"])
                    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                    path = directory / (digest + extension)
                    if not path.exists():
                        temporary = path.with_suffix(path.suffix + ".tmp")
                        temporary.write_bytes(raw)
                        temporary.chmod(0o600)
                        temporary.replace(path)
                    attachment.update(local_path=str(path), sha256=digest, download_state="done")
            except Exception as error:
                attachment["download_error"] = type(error).__name__
            attachments.append(attachment)
        captured_at = datetime.now().astimezone().isoformat(timespec="seconds")
        identity = {
            "active_chat_name": snapshot.get("activeChatName", ""),
            "text": snapshot.get("text", ""),
            "blocks": snapshot.get("blocks", []),
            "media": [item["external_id"] for item in attachments],
            "scroll_top": snapshot.get("scrollTop"),
            "scroll_height": snapshot.get("scrollHeight"),
        }
        external_id = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        ui = {key: snapshot.get(key) for key in
              ("scrollTop", "scrollHeight", "clientHeight", "url", "title")}
        ui["active_chat_name"] = snapshot.get("activeChatName", "")
        return {"external_id": external_id, "captured_at": captured_at,
                "text": snapshot.get("text", ""), "blocks": snapshot.get("blocks", []),
                "media": attachments, "ui": ui}

    @staticmethod
    def _message_key(block, media_by_index):
        attrs = block.get("attrs") or {}
        stable = attrs.get("bubble_id") or next(
            (value for key, value in attrs.items() if key.lower() in ("data-msgid", "data-message-id", "data-id")),
            "",
        )
        if stable:
            return str(stable)
        evidence = {
            "sender": str(block.get("sender", "")).strip(),
            "time": str(block.get("sent_at", "") or attrs.get("sent_at", "") or attrs.get("timestamp_ms", "")).strip(),
            "text": str(block.get("text", "")).strip(),
            "media": [hashlib.sha256(str(media_by_index[index].get("src", "")).encode()).hexdigest()
                      for index in block.get("media_indexes", [])
                      if index in media_by_index],
        }
        if not evidence["text"] and not any(evidence["media"]):
            return ""
        return hashlib.sha256(json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _initial_scan_cursor(cursor):
        value = dict(cursor) if isinstance(cursor, dict) else {}
        # Persist the cutoff once per source. Re-running/retrying a page must not
        # silently move the 24-hour window forward while the scan is in progress.
        if not value.get("initial_scan_cutoff"):
            value["initial_scan_cutoff"] = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
        return value

    @staticmethod
    def _block_time(block):
        attrs = block.get("attrs") or {}
        value = block.get("sent_at") or attrs.get("sent_at") or attrs.get("timestamp_ms")
        # Zalo embeds the send time in stable bubble IDs (for example
        # bb_msg_id_1790682676275_...). Keep this fallback in Python too so
        # scans remain correctly date-bounded if the browser snapshot changes.
        if not value:
            match = re.search(r"(\d{12,14})", str(attrs.get("bubble_id", "")))
            value = match.group(1) if match else None
        if not value:
            return None
        try:
            if str(value).isdigit():
                raw = int(value)
                return datetime.fromtimestamp(raw / (1000 if raw > 10_000_000_000 else 1), timezone.utc)
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None

    @classmethod
    def _limit_initial_scan(cls, snapshot, cutoff):
        """Keep only messages with a timestamp inside the fixed bootstrap window."""
        try:
            boundary = datetime.fromisoformat(str(cutoff).replace("Z", "+00:00"))
            if boundary.tzinfo is None:
                boundary = boundary.replace(tzinfo=timezone.utc)
            boundary = boundary.astimezone(timezone.utc)
        except ValueError:
            return snapshot
        blocks = snapshot.get("blocks", [])
        kept = [block for block in blocks
                if cls._block_time(block) is not None and cls._block_time(block) >= boundary]
        # Preserve raw screen text and geometry while limiting attachments to the
        # retained messages. This still allows photo-first and text-first posts.
        indexes = {index for block in kept for index in block.get("media_indexes", [])}
        result = dict(snapshot)
        result["blocks"] = kept
        result["media"] = [item for item in snapshot.get("media", []) if item.get("index") in indexes]
        result["text"] = "\n".join(str(block.get("text", "")).strip() for block in kept if block.get("text"))
        result["initial_scan"] = {"cutoff": boundary.isoformat(),
                                  "undated_blocks": sum(1 for block in kept if cls._block_time(block) is None)}
        return result

    @classmethod
    def _new_messages_snapshot(cls, snapshot, cursor):
        media = snapshot.get("media", [])
        media_by_index = {item.get("index"): item for item in media if isinstance(item.get("index"), int)}
        blocks = snapshot.get("blocks", [])
        keys = [(block, cls._message_key(block, media_by_index)) for block in blocks]
        keys = [(block, key) for block, key in keys if key]
        seen = set(cursor.get("seen_message_keys", [])) if isinstance(cursor, dict) else set()
        all_seen = list(dict.fromkeys([*(cursor.get("seen_message_keys", []) if isinstance(cursor, dict) else []),
                                      *(key for _, key in keys)]))[-5000:]
        next_cursor = {"seen_message_keys": all_seen}
        if not seen:
            # Establish the live-monitoring watermark without importing the
            # visible backlog that was already in the group before launch.
            return None, next_cursor
        new_blocks = [block for block, key in keys if key not in seen]
        if not new_blocks:
            return None, next_cursor
        new_indexes = {index for block in new_blocks for index in block.get("media_indexes", [])}
        filtered = dict(snapshot)
        filtered["blocks"] = new_blocks
        filtered["media"] = [item for item in media if item.get("index") in new_indexes]
        filtered["text"] = "\n".join(str(block.get("text", "")).strip() for block in new_blocks if block.get("text"))
        return filtered, next_cursor

    def collect(self, source, mode, cursor, cancel):
        if cancel.is_set():
            return RawCaptureBatch([], cursor, False)
        if mode == "collect_initial_scan":
            cursor = self._initial_scan_cursor(cursor)
        result = self.command("collect", source={"name": source["name"], "kind": source["kind"]},
                              mode=mode, cursor=cursor if isinstance(cursor, dict) else {})
        snapshot = result["snapshot"]
        if mode == "collect_initial_scan":
            snapshot = self._limit_initial_scan(snapshot, cursor["initial_scan_cutoff"])
        if mode == "collect_incremental":
            snapshot, seen_cursor = self._new_messages_snapshot(snapshot, cursor)
            if snapshot is None:
                return RawCaptureBatch([], seen_cursor, True)
        capture = self._archive(source, snapshot)
        next_cursor = {"snapshot": hashlib.sha256(snapshot.get("text", "").encode()).hexdigest(),
                       "scroll_top": snapshot.get("scrollTop"), "captured_at": datetime.now().astimezone().isoformat()}
        if mode == "collect_initial_scan":
            next_cursor.update(cursor)
            next_cursor.update(result.get("cursor", {}))
            next_cursor["initial_scan_cutoff"] = cursor["initial_scan_cutoff"]
            # Record durable source-local message keys for de-duplication and
            # diagnostics. Keep overlapping screen captures intact for the raw
            # interpreter to reconstruct albums across viewport boundaries.
            media_by_index = {item.get("index"): item for item in snapshot.get("media", [])
                              if isinstance(item.get("index"), int)}
            existing = list(cursor.get("seen_message_keys", []))
            next_cursor["seen_message_keys"] = list(dict.fromkeys(
                [*existing, *(key for block in snapshot.get("blocks", [])
                              if (key := self._message_key(block, media_by_index)))]
            ))[-5000:]
        if mode == "collect_incremental":
            # A failed transient Zalo media URL must remain eligible for another
            # download attempt. Keep its message key out of the cursor until all
            # media visible in that message has been saved locally.
            media_by_index = {item.get("index"): item for item in snapshot.get("media", [])
                              if isinstance(item.get("index"), int)}
            saved_ids = {item.get("external_id") for item in capture.get("media", [])
                         if item.get("download_state") == "done"}
            retry_keys = set()
            for block in snapshot.get("blocks", []):
                linked = [media_by_index.get(index) for index in block.get("media_indexes", [])]
                linked = [item for item in linked if item and item.get("src")]
                if linked and any(hashlib.sha256(str(item["src"]).encode()).hexdigest() not in saved_ids
                                  for item in linked):
                    key = self._message_key(block, media_by_index)
                    if key:
                        retry_keys.add(key)
            if retry_keys:
                seen_keys = [key for key in seen_cursor.get("seen_message_keys", []) if key not in retry_keys]
                seen_cursor["seen_message_keys"] = seen_keys
            next_cursor.update(seen_cursor)
        return RawCaptureBatch([capture], next_cursor, bool(result.get("complete")))

    def close(self):
        with self.lock:
            if not self.process:
                return
            try:
                self.process.stdin.write(json.dumps({"id": 0, "action": "close"}) + "\n")
                self.process.stdin.flush()
                self.process.wait(timeout=2)
            except Exception:
                self.process.terminate()
            self.process = None
