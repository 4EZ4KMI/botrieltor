import http.client
import json
import io
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from urllib.error import HTTPError, URLError

from botrieltor.adapters import BoundSource, CollectedBatch
from botrieltor.runtime import Runtime
from botrieltor.server import build_server
from botrieltor.store import Conflict, Store
from botrieltor.omniroute import OmniRouteClient
from botrieltor.interpreter import RawArchiveInterpreter
from botrieltor.listing_builder import ListingBuilder
from botrieltor.telegram import TelegramClient
from botrieltor.publisher import TelegramPublisher
from botrieltor.decision_agent import ClaudeDecisionAgent


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def scalar(self, sql, args=()):
        with self.store.connect() as db:
            return db.execute(sql, args).fetchone()[0]

    def message(self, identifier="1", **extra):
        return {"external_id": identifier, "sender": "agent", "sent_at": "2026-09-16T09:00:00+07:00", "text": "Apartment", **extra}

    def test_replayed_batch_deduplicates_messages_and_media(self):
        source = self.store.add_source("Group", "group")
        message = self.message(media=[{"external_id": "photo-1", "source_ref": "visible-media-reference"}])
        for _ in range(2):
            self.store.commit_batch(source, "collect_archive", [message], {"anchor": "1"})
        self.assertEqual(self.scalar("SELECT count(*) FROM messages"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM media"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM message_versions"), 0)

    def test_raw_zalo_archive_is_durable_before_model_processing(self):
        source = self.store.add_source("Raw Group", "group")
        capture = {
            "external_id": "viewport-1", "captured_at": "2026-09-17T10:00:00+07:00",
            "text": "photo then description",
            "blocks": [{"text": "photo"}, {"text": "description"}],
            "media": [{"external_id": "raw-photo-1", "source_ref": "https://zalo.invalid/photo"}],
        }
        for _ in range(2):
            self.store.commit_raw_batch(source, "collect_archive", [capture], {"scroll_top": 100})
        self.assertEqual(self.scalar("SELECT count(*) FROM raw_captures"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM raw_media"), 1)
        self.assertEqual(self.scalar("SELECT processing_state FROM raw_captures"), "pending")
        self.assertEqual(self.scalar("SELECT count(*) FROM messages"), 0)

    def test_later_raw_capture_upgrades_pending_message_media(self):
        source = self.store.add_source("Raw Group", "group")
        self.store.commit_batch(source, "collect_incremental", [self.message(media=[{
            "external_id": "photo-1", "source_ref": "blob:old", "download_state": "pending",
        }])], None)
        capture = {
            "external_id": "viewport-2", "captured_at": "2026-09-17T10:01:00+07:00", "text": "Apartment",
            "blocks": [], "media": [{"external_id": "photo-1", "source_ref": "blob:old",
                                       "local_path": "/tmp/photo.jpg", "sha256": "abc",
                                       "download_state": "done"}],
        }
        self.store.commit_raw_batch(source, "collect_incremental", [capture], {})
        with self.store.connect() as db:
            row = db.execute("SELECT local_path,sha256,download_state FROM media").fetchone()
        self.assertEqual(tuple(row), ("/tmp/photo.jpg", "abc", "done"))

        # A later pending observation must not erase a successful download.
        capture["captured_at"] = "2026-09-17T10:02:00+07:00"
        capture["media"][0] = {"external_id": "photo-1", "source_ref": "blob:old", "download_state": "pending"}
        self.store.commit_raw_batch(source, "collect_incremental", [capture], {})
        self.assertEqual(self.scalar("SELECT download_state FROM media"), "done")
        self.assertEqual(self.scalar("SELECT download_state FROM raw_media"), "done")

    def test_downloaded_media_wakes_waiting_listing_immediately(self):
        source = self.store.add_source("Raw Group", "group")
        self.store.commit_batch(source, "collect_incremental", [self.message(media=[{
            "external_id": "photo-1", "source_ref": "blob:old", "download_state": "pending",
        }])], None)
        message_id = self.scalar("SELECT id FROM messages")
        listing_id = self.store.add_listing("Apartment")
        with self.store.connect() as db:
            db.execute("INSERT INTO listing_messages(listing_id,message_id) VALUES (?,?)", (listing_id, message_id))
            db.execute("INSERT INTO listing_automation(listing_id,last_error,retry_after,updated_at) VALUES (?,?,9999999999,'now')",
                       (listing_id, "Ожидаются фотографии из Zalo"))
        capture = {"external_id": "viewport", "captured_at": "2026-09-17T10:01:00+07:00", "text": "",
                   "blocks": [], "media": [{"external_id": "photo-1", "source_ref": "blob:old",
                                              "local_path": "/tmp/photo.jpg", "sha256": "abc",
                                              "download_state": "done"}]}
        self.store.commit_raw_batch(source, "collect_incremental", [capture], {})
        with self.store.connect() as db:
            row = db.execute("SELECT last_error,retry_after FROM listing_automation WHERE listing_id=?", (listing_id,)).fetchone()
        self.assertEqual(tuple(row), ("", 0.0))

    def test_reprocessing_same_listing_evidence_does_not_create_duplicate(self):
        source = self.store.add_source("Offers", "group")
        self.store.commit_batch(source, "collect_incremental", [self.message("m1")], None)
        message_id = self.scalar("SELECT id FROM messages")
        candidate = {"title": "Apartment", "description": "Same evidence", "price": "10m",
                     "message_external_ids": ["m1"], "confidence": 1, "needs_review": False}
        self.store.commit_listing_candidates(source, [message_id], [candidate], [])
        self.store.commit_listing_candidates(source, [message_id], [candidate], [])
        self.assertEqual(self.scalar("SELECT count(*) FROM listings"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM listing_automation"), 1)
        self.assertEqual(self.scalar("SELECT listing_id FROM message_analysis WHERE message_id=?", (message_id,)), 1)

    def test_repeated_same_source_text_with_new_message_id_reuses_listing(self):
        source = self.store.add_source("Offers", "group")
        candidate = lambda external_id: {"title": "Apartment", "description": "Same evidence", "price": "10m",
                                         "message_external_ids": [external_id], "confidence": 1,
                                         "needs_review": False}
        self.store.commit_batch(source, "collect_incremental", [self.message("m1", text="Same apartment 10m")], None)
        first = self.scalar("SELECT id FROM messages WHERE external_id='m1'")
        self.store.commit_listing_candidates(source, [first], [candidate("m1")], [])
        self.store.commit_batch(source, "collect_incremental", [self.message("m2", text="  Same   apartment 10m ")], None)
        second = self.scalar("SELECT id FROM messages WHERE external_id='m2'")
        self.store.commit_listing_candidates(source, [second], [candidate("m2")], [])
        self.assertEqual(self.scalar("SELECT count(*) FROM listings"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM listing_messages"), 2)

    def test_photo_text_stay_separate_and_source_ids_are_scoped(self):
        a = self.store.add_source("A", "group")
        b = self.store.add_source("B", "group")
        messages = [self.message("1"), self.message("2", text="", media=[{"external_id": "photo"}])]
        self.store.commit_batch(a, "collect_archive", messages, None)
        self.store.commit_batch(b, "collect_archive", [self.message("1")], None)
        self.assertEqual(self.scalar("SELECT count(*) FROM messages"), 3)
        self.assertEqual(self.scalar("SELECT count(*) FROM listings"), 0)
        self.assertEqual(self.scalar("SELECT count(*) FROM listing_messages"), 0)

    def test_invalid_batch_rolls_back_messages_and_cursor_together(self):
        source = self.store.add_source("Group", "group")
        self.store.commit_batch(source, "collect_archive", [self.message("1")], {"anchor": "1"})
        with self.assertRaises(ValueError):
            self.store.commit_batch(source, "collect_archive", [self.message("2"), self.message("3", media=[{}])], {"anchor": "3"}, complete=True)
        self.assertEqual(self.scalar("SELECT count(*) FROM messages"), 1)
        self.assertEqual(json.loads(self.scalar("SELECT cursor FROM checkpoints")), {"anchor": "1"})
        self.assertEqual(self.scalar("SELECT archive_state FROM sources"), "collecting")

    def test_edits_keep_original_evidence(self):
        source = self.store.add_source("Group", "group")
        self.store.commit_batch(source, "collect_archive", [self.message(text="available")], {"anchor": "1"})
        self.store.commit_batch(source, "collect_incremental", [self.message(text="rented")], {"anchor": "1"})
        self.assertEqual(self.scalar("SELECT text FROM messages"), "rented")
        self.assertEqual(json.loads(self.scalar("SELECT payload FROM message_versions"))["text"], "available")
        self.assertEqual(self.scalar("SELECT count(*) FROM checkpoints"), 2)

    def test_age_never_expires_listing(self):
        listing = self.store.add_listing("Old apartment", status="available")
        with self.store.connect() as db:
            db.execute("UPDATE listings SET created_at='2000-01-01',updated_at='2000-01-01' WHERE id=?", (listing,))
        self.store.recover()
        self.store.enqueue_collection()
        self.assertEqual(self.scalar("SELECT status FROM listings"), "available")
        with self.assertRaises(ValueError):
            self.store.set_listing_status(listing, "rented", 1, origin="expiry")

    def test_status_requires_matching_evidence_and_revision(self):
        a = self.store.add_listing("A", status="available")
        b = self.store.add_listing("B", status="available")
        source = self.store.add_source("Group", "group")
        self.store.commit_batch(source, "collect_incremental", [self.message(text="rented")], None)
        message_id = self.scalar("SELECT id FROM messages")
        with self.store.connect() as db:
            db.execute("INSERT INTO listing_messages VALUES (?,?,1)", (a, message_id))
        with self.assertRaises(ValueError):
            self.store.set_listing_status(b, "rented", 1, origin="confirmed_source", evidence_id=message_id)
        self.store.set_listing_status(a, "rented", 1, origin="confirmed_source", evidence_id=message_id)
        with self.assertRaises(Conflict):
            self.store.set_listing_status(a, "available", 1)
        self.assertEqual(self.scalar("SELECT status FROM listings WHERE id=?", (b,)), "available")

    def test_only_channel_publications_and_no_conversation_outbox(self):
        listing = self.store.add_listing("Apartment", status="available")
        with self.store.connect() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO publications(listing_id,destination_kind,channel_id,message_ids) VALUES (?,'group','123','[1]')", (listing,))
            publication = db.execute("INSERT INTO publications(listing_id,channel_id,message_ids) VALUES (?,'123','[1,2,3]')", (listing,)).lastrowid
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO outbox(publication_id,revision,action,created_at) VALUES (?,1,'send_customer_message','now')", (publication,))
        self.store.set_listing_status(listing, "rented", 1)
        self.assertEqual(self.scalar("SELECT action FROM outbox"), "edit_listing")
        self.assertEqual(self.scalar("SELECT message_ids FROM publications"), "[1,2,3]")

    def test_resume_archive_then_incremental_and_queue_idempotency(self):
        source = self.store.add_source("Group", "group")
        bind_job = self.store.claim_job()
        self.assertEqual(bind_job["kind"], "bind_source")
        self.store.commit_binding(source, "chat-1", "Group", "group")
        self.store.finish_job(bind_job["id"], "done")
        self.store.enqueue_collection()
        self.store.enqueue_collection()
        self.assertEqual(self.scalar("SELECT count(*) FROM jobs WHERE state IN ('pending','running','blocked')"), 1)
        job = self.store.claim_job()
        self.store.recover()
        self.assertEqual(self.scalar("SELECT state FROM jobs WHERE id=?", (job["id"],)), "pending")
        self.store.commit_batch(source, "collect_archive", [], {"oldest": "edge"}, complete=True)
        self.store.finish_job(job["id"], "done")
        self.store.enqueue_collection()
        self.assertEqual(self.store.claim_job()["kind"], "collect_incremental")

    def test_backup_is_readable_and_keeps_seven(self):
        self.store.add_listing("Preserve me")
        for _ in range(8):
            self.store.backup()
        backups = sorted((Path(self.temp.name) / "backups").glob("*.sqlite3"))
        self.assertEqual(len(backups), 7)
        with sqlite3.connect(backups[-1]) as db:
            self.assertEqual(db.execute("SELECT title FROM listings").fetchone()[0], "Preserve me")


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.store.add_source("Group", "group")
        self.runtime = Runtime(self.store)

    def tearDown(self):
        self.runtime.close()
        self.temp.cleanup()

    def await_state(self, state):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with self.store.connect() as db:
                row = db.execute("SELECT state FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
            if row and row[0] == state:
                return
            time.sleep(0.02)
        self.fail(f"Job did not reach {state}")

    def test_missing_adapter_never_claims_success(self):
        self.assertFalse(self.runtime.active)
        self.runtime.start()
        self.await_state("blocked")
        self.assertEqual(self.runtime.summary()["label"], "Ожидание подключения Zalo")
        self.assertEqual(self.store.snapshot()["counts"]["messages"], 0)
        self.runtime.pause()
        self.assertFalse(self.runtime.active)

    def test_new_adapter_resumes_blocked_job(self):
        self.runtime.start()
        self.await_state("blocked")
        self.runtime.pause()
        class Collector:
            def bind(self, source, cancel):
                return BoundSource("chat-1", source["name"], source["kind"])

            def collect(self, source, mode, cursor, cancel):
                return CollectedBatch([], {"edge": "oldest"}, True)
        self.runtime.collector = Collector()
        self.runtime.start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and self.store.snapshot()["sources"][0]["archive_state"] != "complete":
            time.sleep(0.02)
        self.assertEqual(self.store.snapshot()["sources"][0]["binding_state"], "bound")
        self.assertEqual(self.store.snapshot()["sources"][0]["archive_state"], "complete")

    def test_pause_does_not_commit_in_flight_batch(self):
        entered = threading.Event()
        class Collector:
            def bind(self, source, cancel):
                return BoundSource("chat-1", source["name"], source["kind"])

            def collect(self, source, mode, cursor, cancel):
                entered.set()
                cancel.wait(2)
                return CollectedBatch([{"external_id":"1","sent_at":"2026-09-16","text":"test"}], {"edge":"1"}, True)
        self.runtime.collector = Collector()
        self.runtime.start()
        self.assertTrue(entered.wait(2))
        self.runtime.pause()
        self.await_state("pending")
        self.assertEqual(self.store.snapshot()["counts"]["messages"], 0)

    def test_start_retries_only_latest_duplicate_failed_job(self):
        with self.store.connect() as db:
            source_id = db.execute("SELECT id FROM sources").fetchone()[0]
            db.execute("INSERT INTO jobs(source_id,kind,state,detail,updated_at) VALUES (?,'interpret_raw','failed','old','2026-09-17T00:00:00Z')", (source_id,))
            db.execute("INSERT INTO jobs(source_id,kind,state,detail,updated_at) VALUES (?,'interpret_raw','failed','new','2026-09-17T00:01:00Z')", (source_id,))

        class Interpreter:
            def process(self, source, cancel):
                cancel.wait(1)

        self.runtime.interpreter = Interpreter()
        self.runtime.start()
        with self.store.connect() as db:
            active = db.execute("SELECT count(*) FROM jobs WHERE kind='interpret_raw' AND state IN ('pending','running','blocked')").fetchone()[0]
            failed = db.execute("SELECT count(*) FROM jobs WHERE kind='interpret_raw' AND state='failed'").fetchone()[0]
        self.assertEqual(active, 1)
        self.assertEqual(failed, 1)


class RawArchiveInterpreterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.source = self.store.add_source("Archive", "group")

    def tearDown(self):
        self.temp.cleanup()

    def test_agent_parses_durable_capture_and_keeps_media_evidence(self):
        capture = {
            "external_id": "capture-1", "captured_at": "2026-09-17T10:00:00+07:00",
            "text": "Lan 10:00 Căn hộ 12 triệu", "blocks": [{"attrs": {"data-msgid": "z-1"}, "text": "Căn hộ 12 triệu"}],
            "media": [{"external_id": "photo-1", "kind": "image", "source_ref": "https://zalo.invalid/1.jpg",
                       "local_path": "/tmp/1.jpg", "sha256": "abc", "download_state": "done", "ui_index": 0}],
        }
        self.store.commit_raw_batch(self.source, "collect_archive", [capture], {"scroll_top": 0}, complete=True)

        class Omni:
            def chat(self, messages, **_):
                self.messages = messages
                payload = {"messages": [{"capture_external_id": "capture-1", "source_key": "z-1", "sender": "Lan",
                                          "sent_at": "10:00", "text": "Căn hộ 12 triệu", "reply_to": None,
                                          "media_external_ids": ["photo-1"]}]}
                return {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]}

        omni = Omni()
        parsed = RawArchiveInterpreter(self.store, omni).process(
            {"id": self.source, "name": "Archive", "kind": "group"}, threading.Event())
        self.assertEqual(parsed, 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT processing_state FROM raw_captures").fetchone()[0], "parsed")
            self.assertEqual(db.execute("SELECT text FROM messages").fetchone()[0], "Căn hộ 12 triệu")
            media = db.execute("SELECT external_id,local_path,download_state FROM media").fetchone()
            self.assertEqual(tuple(media), ("photo-1", "/tmp/1.jpg", "done"))
        self.assertNotIn("/tmp/1.jpg", omni.messages[1]["content"])

    def test_partial_model_selection_expands_complete_album_and_drops_avatar(self):
        media = [{"external_id": "avatar", "kind": "img", "ui_index": 0, "width": 160, "height": 160,
                  "source_ref": "avatar", "download_state": "done", "local_path": "/tmp/avatar.jpg"}]
        media += [{"external_id": f"photo-{index}", "kind": "img", "ui_index": index,
                   "width": 768, "height": 1024, "source_ref": f"blob:{index}",
                   "download_state": "done", "local_path": f"/tmp/{index}.jpg"} for index in range(1, 9)]
        capture = {"external_id": "album", "captured_at": "2026-09-17T20:11:00+07:00", "text": "album",
                   "blocks": [{"media_indexes": list(range(9)), "text": "wrapper"},
                              {"media_indexes": list(range(1, 9)), "text": "reactions"}], "media": media}
        self.store.commit_raw_batch(self.source, "collect_incremental", [capture], {}, complete=True)

        class Omni:
            def chat(self, *_args, **_kwargs):
                value = {"messages": [{"capture_external_id": "album", "source_key": "photos", "sender": "Lan",
                                        "sent_at": "20:11", "text": "", "reply_to": None,
                                        "media_external_ids": ["avatar", "photo-1", "photo-5"]}]}
                return {"choices": [{"message": {"content": json.dumps(value)}}]}

        RawArchiveInterpreter(self.store, Omni()).process(
            {"id": self.source, "name": "Archive", "kind": "group"}, threading.Event())
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM media").fetchone()[0], 8)
            self.assertFalse(db.execute("SELECT 1 FROM media WHERE external_id='avatar'").fetchone())


class ListingBuilderTests(unittest.TestCase):
    def test_builds_unverified_card_and_real_review(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            source = store.add_source("Offers", "group")
            store.commit_batch(source, "collect_archive", [
                {"external_id": "m1", "sender": "Lan", "sent_at": "10:00", "text": "Gold Coast 2PN 18 triệu"},
                {"external_id": "m2", "sender": "Lan", "sent_at": "10:01", "text": "", "media": [{"external_id": "p1"}]},
            ], None, complete=True)

            class Omni:
                def chat(self, *_args, **_kwargs):
                    value = {"listings": [{"title": "Gold Coast, 2 спальни", "description": "Квартира в Нячанге.",
                                            "price": "18 triệu", "message_external_ids": ["m1", "m2"],
                                            "confidence": 0.7, "needs_review": True,
                                            "review_reason": "Проверить связь фотографии"}], "reviews": []}
                    return {"choices": [{"message": {"content": json.dumps(value, ensure_ascii=False)}}]}

            self.assertEqual(ListingBuilder(store, Omni()).process(
                {"id": source, "name": "Offers", "kind": "group"}, threading.Event()), 2)
            with store.connect() as db:
                self.assertEqual(db.execute("SELECT status FROM listings").fetchone()[0], "unverified")
                self.assertEqual(db.execute("SELECT count(*) FROM listing_messages").fetchone()[0], 2)
                self.assertEqual(db.execute("SELECT count(*) FROM reviews WHERE state='open'").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT count(*) FROM message_analysis").fetchone()[0], 2)
            listing = store.snapshot()["listings"][0]
            self.assertEqual([item["name"] for item in listing["sources"]], ["Offers"])
            self.assertEqual([item["text"] for item in listing["source_messages"]], ["Gold Coast 2PN 18 triệu"])

    def test_prompt_keeps_source_language(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            source = store.add_source("Offers", "group")
            store.commit_batch(source, "collect_archive", [
                {"external_id": "m1", "sender": "Lan", "sent_at": "10:00", "text": "Căn hộ 2PN, 18 triệu"},
            ], None, complete=True)

            class Omni:
                def chat(inner, messages, **_kwargs):
                    inner.prompt = messages[1]["content"]
                    value = {"listings": [{"title": "Căn hộ 2PN", "description": "Căn hộ 2PN, 18 triệu",
                                            "price": "18 triệu", "message_external_ids": ["m1"],
                                            "confidence": 1, "needs_review": False}], "reviews": []}
                    return {"choices": [{"message": {"content": json.dumps(value, ensure_ascii=False)}}]}

            omni = Omni()
            ListingBuilder(store, omni).process({"id": source, "name": "Offers", "kind": "group"}, threading.Event())
            self.assertIn("Do not translate", omni.prompt)
            self.assertNotIn("Write title and description in Russian", omni.prompt)


class TelegramPublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.store.set_settings(telegram_channel_id="-100123", telegram_message_example="🏠 Готовый пример")
        self.listing_id = self.store.add_listing("Студия у моря", "10 млн ₫", "Балкон и кухня")
        photo = Path(self.temp.name) / "listing.jpg"
        photo.write_bytes(b"test-photo")
        source = self.store.add_source("Photo source", "personal")
        self.store.commit_batch(source, "collect_incremental", [{
            "external_id": "publisher-photo-message", "sender": "Lan", "sent_at": "10:00", "text": "",
            "media": [{"external_id": "publisher-photo", "kind": "image", "local_path": str(photo),
                       "download_state": "done", "sha256": "publisher-photo"}],
        }], None)
        with self.store.connect() as db:
            message_id = db.execute("SELECT id FROM messages WHERE external_id='publisher-photo-message'").fetchone()[0]
            db.execute("INSERT INTO listing_messages(listing_id,message_id,confirmed) VALUES (?,?,1)",
                       (self.listing_id, message_id))

        class Omni:
            def chat(inner, messages, **kwargs):
                inner.messages = messages
                inner.kwargs = kwargs
                return {"choices": [{"message": {"content": json.dumps({"text": "🏠 Студия у моря\n💰 10 млн ₫"}, ensure_ascii=False)}}]}

        class Telegram:
            def __init__(inner): inner.calls = []
            def publish(inner, text, media):
                inner.calls.append((text, media))
                return [501]

        self.omni, self.telegram = Omni(), Telegram()
        self.publisher = TelegramPublisher(self.store, self.omni, self.telegram)

    def tearDown(self): self.temp.cleanup()

    def test_confirmation_publishes_once_then_marks_available(self):
        result = self.publisher.publish_available(self.listing_id, 1)
        self.assertEqual(result["message_ids"], [501])
        listing = self.store.get_listing_for_publication(self.listing_id)
        self.assertEqual(listing["status"], "available")
        self.assertEqual(listing["revision"], 2)
        with self.store.connect() as db:
            publication = db.execute("SELECT * FROM publications").fetchone()
            self.assertEqual(publication["state"], "sent")
            self.assertEqual(json.loads(publication["message_ids"]), [501])
        self.assertEqual(len(self.telegram.calls), 1)
        self.assertIn("Готовый пример", self.omni.messages[1]["content"])

    def test_publisher_uses_its_selected_model(self):
        self.store.set_settings(omniroute_publisher_model="custom/telegram-editor")
        self.publisher.publish_available(self.listing_id, 1)
        self.assertEqual(self.omni.kwargs["model"], "custom/telegram-editor")

    def test_failed_send_keeps_listing_unverified_for_retry(self):
        def fail(*_): raise ValueError("Telegram unavailable")
        self.telegram.publish = fail
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.publisher.publish_available(self.listing_id, 1)
        self.assertEqual(self.store.get_listing_for_publication(self.listing_id)["status"], "unverified")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM publications").fetchone()[0], "failed")

    def test_source_contact_is_hidden_and_only_example_contact_is_allowed(self):
        self.store.set_settings(telegram_message_example="Связь: @my_nhatrang\nТелефон: +84 90 123 45 67")
        with self.store.connect() as db:
            db.execute("UPDATE listings SET description='Căn hộ đẹp\\nZalo 0396281320 @diepnguyenhome48' WHERE id=?", (self.listing_id,))
        self.omni.chat = lambda messages, **_: (
            setattr(self.omni, "messages", messages) or
            {"choices": [{"message": {"content": json.dumps({"text": "Квартира\nСвязь: @my_nhatrang\nТелефон: +84 90 123 45 67"}, ensure_ascii=False)}}]}
        )
        self.publisher.publish_available(self.listing_id, 1)
        prompt = self.omni.messages[1]["content"]
        self.assertNotIn("0396281320", prompt)
        self.assertNotIn("@diepnguyenhome48", prompt)
        self.assertIn("@my_nhatrang", self.telegram.calls[0][0])

    def test_unknown_or_missing_publisher_contact_blocks_send(self):
        self.store.set_settings(telegram_message_example="Связь: @my_nhatrang")
        self.omni.chat = lambda *_args, **_kwargs: {"choices": [{"message": {"content": json.dumps({"text": "Связь: @foreign_agent"})}}]}
        with self.assertRaisesRegex(ValueError, "контакт"):
            self.publisher.publish_available(self.listing_id, 1)
        self.assertEqual(self.telegram.calls, [])

    def test_republish_creates_new_post_and_keeps_listing_available(self):
        self.publisher.publish_available(self.listing_id, 1)
        result = self.publisher.republish(self.listing_id)
        self.assertEqual(result["message_ids"], [501])
        self.assertEqual(len(self.telegram.calls), 2)
        self.assertEqual(self.store.get_listing_for_publication(self.listing_id)["status"], "available")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM publications").fetchone()[0], 1)
            self.assertIn("Студия", db.execute("SELECT body FROM publications").fetchone()[0])

    def test_available_listing_without_publication_can_be_published_again(self):
        self.store.set_listing_status(self.listing_id, "available", 1)
        result = self.publisher.republish(self.listing_id)
        self.assertEqual(result["message_ids"], [501])
        with self.store.connect() as db:
            publication = db.execute("SELECT * FROM publications").fetchone()
            self.assertEqual(publication["state"], "sent")
            self.assertEqual(publication["channel_id"], "-100123")

    def test_rented_listing_cannot_be_republished(self):
        self.store.set_listing_status(self.listing_id, "rented", 1)
        with self.assertRaisesRegex(ValueError, "доступный"):
            self.publisher.republish(self.listing_id)
        self.assertEqual(self.telegram.calls, [])

    def test_unverified_listing_can_be_published_manually(self):
        # Listing starts unverified
        self.assertEqual(self.store.get_listing_for_publication(self.listing_id)["status"], "unverified")
        result = self.publisher.republish(self.listing_id)
        self.assertEqual(result["message_ids"], [501])
        self.assertEqual(self.store.get_listing_for_publication(self.listing_id)["status"], "available")


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def test_claude_publish_decision_automatically_publishes_new_listing(self):
        source = self.store.add_source("Offers", "group")
        photo = Path(self.temp.name) / "automation.jpg"
        photo.write_bytes(b"test-photo")
        self.store.commit_batch(source, "collect_incremental", [
            {"external_id": "m1", "sender": "Lan", "sent_at": "10:00", "text": "Căn hộ trống 12 triệu",
             "media": [{"external_id": "p1", "kind": "image", "local_path": str(photo),
                        "download_state": "done", "sha256": "p1"}]},
        ], None)
        with self.store.connect() as db:
            message_id = db.execute("SELECT id FROM messages WHERE external_id='m1'").fetchone()[0]
        self.store.commit_listing_candidates(source, [message_id], [{
            "title": "Căn hộ trống", "description": "Căn hộ trống 12 triệu", "price": "12 triệu",
            "message_external_ids": ["m1"], "confidence": 1, "needs_review": False,
        }], [])

        class Omni:
            last_used_model = "agy/claude-opus-4-6-thinking-medium"
            def claude_decision(self, *_args, **_kwargs):
                return {"model": self.last_used_model, "choices": [{"message": {"content": json.dumps({
                    "decision": "publish", "reason": "Объявление полное, признаков сдачи нет",
                }, ensure_ascii=False)}}]}

        class Publisher:
            def __init__(inner): inner.calls = []
            def publish_available(inner, listing_id, revision):
                inner.calls.append(listing_id)
                self.store.set_listing_status(listing_id, "available", revision, origin="agent")
            def publish_existing_once(inner, listing_id): inner.calls.append(listing_id)

        publisher = Publisher()
        agent = ClaudeDecisionAgent(self.store, Omni(), publisher)
        self.assertTrue(agent.process_next(threading.Event()))
        self.assertTrue(agent.process_next(threading.Event()))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM listings").fetchone()[0], "available")
            self.assertEqual(db.execute("SELECT state FROM listing_automation").fetchone()[0], "done")
        self.assertEqual(len(publisher.calls), 1)

    def test_automation_waits_for_photos_without_calling_claude(self):
        source = self.store.add_source("Фото позже", "group")
        self.store.commit_batch(source, "collect_archive", [{
            "external_id": "m-no-photo", "sender": "Lan", "sent_at": "10:00",
            "text": "Cho thuê căn hộ 12 triệu", "media": [],
        }], None)
        with self.store.connect() as db:
            message_id = db.execute("SELECT id FROM messages WHERE external_id='m-no-photo'").fetchone()[0]
        self.store.commit_listing_candidates(source, [message_id], [{
            "title": "Без фото", "description": "Описание", "price": "12 triệu",
            "message_external_ids": ["m-no-photo"], "confidence": 1,
        }], [])

        class Omni:
            def claude_decision(self, *args, **kwargs):
                raise AssertionError("Claude не должен вызываться до загрузки фотографий")

        class Publisher:
            pass

        agent = ClaudeDecisionAgent(self.store, Omni(), Publisher())
        self.assertTrue(agent.process_next(threading.Event()))
        with self.store.connect() as db:
            row = db.execute("SELECT state,last_error,retry_after FROM listing_automation").fetchone()
            self.assertEqual(row["state"], "pending")
            self.assertIn("фотограф", row["last_error"])
            self.assertGreater(row["retry_after"], 0)

    def test_missing_photos_transitions_to_review_after_max_attempts(self):
        source = self.store.add_source("Фото позже 2", "group")
        self.store.commit_batch(source, "collect_archive", [{
            "external_id": "m-no-photo-2", "sender": "Lan", "sent_at": "10:00",
            "text": "Cho thuê căn hộ 12 triệu", "media": [],
        }], None)
        with self.store.connect() as db:
            message_id = db.execute("SELECT id FROM messages WHERE external_id='m-no-photo-2'").fetchone()[0]
        self.store.commit_listing_candidates(source, [message_id], [{
            "title": "Без фото", "description": "Описание", "price": "12 triệu",
            "message_external_ids": ["m-no-photo-2"], "confidence": 1,
        }], [])

        with self.store.connect() as db:
            db.execute("UPDATE listing_automation SET attempts=5,retry_after=0")

        class Omni:
            pass

        class Publisher:
            pass

        agent = ClaudeDecisionAgent(self.store, Omni(), Publisher())
        self.assertTrue(agent.process_next(threading.Event()))
        with self.store.connect() as db:
            row = db.execute("SELECT state,decision,reason FROM listing_automation").fetchone()
            self.assertEqual(row["state"], "review")
            self.assertEqual(row["decision"], "review")
            self.assertIn("фотограф", row["reason"])

    def test_rented_status_replaces_price_in_channel_and_can_restore_it(self):
        listing_id = self.store.add_listing("Mường Thanh", "17 500 000 VND", status="available")
        original = "📍 Mường Thanh\n💰 17 500 000 донгов/месяц\n@aebussin"
        with self.store.connect() as db:
            db.execute("INSERT INTO publications(listing_id,channel_id,message_ids,body) VALUES (?,'-100123','[77]',?)",
                       (listing_id, original))

        class Telegram:
            def __init__(inner): inner.edits = []
            def edit_post(inner, message_id, text, **kwargs): inner.edits.append((message_id, text, kwargs))

        telegram = Telegram()
        publisher = TelegramPublisher(self.store, object(), telegram)
        self.store.set_listing_status(listing_id, "rented", 1)
        self.assertTrue(publisher.process_outbox())
        self.assertIn("СДАНО / НЕАКТУАЛЬНО", telegram.edits[-1][1])
        self.assertNotIn("17 500 000 донгов/месяц", telegram.edits[-1][1])
        self.store.set_listing_status(listing_id, "available", 2)
        self.assertTrue(publisher.process_outbox())
        self.assertEqual(telegram.edits[-1][1], original)

    def test_clear_booked_notice_marks_only_linked_source_listing_rented(self):
        source = self.store.add_source("Offers", "group")
        self.store.commit_batch(source, "collect_incremental", [
            {"external_id": "offer", "sender": "Lan", "sent_at": "10:00", "text": "Mã A12, 12 triệu"},
            {"external_id": "status", "sender": "Lan", "sent_at": "10:05", "text": "A12 Hết"},
        ], None)
        listing_id = self.store.add_listing("Mã A12", "12 triệu", status="available")
        with self.store.connect() as db:
            rows = {row["external_id"]: row["id"] for row in db.execute("SELECT id,external_id FROM messages")}
            db.execute("INSERT INTO listing_messages(listing_id,message_id) VALUES (?,?)", (listing_id, rows["offer"]))
        self.store.commit_listing_candidates(source, [rows["status"]], [], [{
            "reason": "Сообщение A12 Hết", "message_external_ids": ["status"],
        }])
        review = self.store.claim_review_automation()
        self.store.save_review_decision(review["review_id"], "rented", listing_id,
                                        "Код A12 однозначно совпадает", "agy/claude-opus-4-6-thinking-medium")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM listings WHERE id=?", (listing_id,)).fetchone()[0], "rented")
            self.assertEqual(db.execute("SELECT state FROM reviews").fetchone()[0], "resolved")


class TelegramTests(unittest.TestCase):
    class Secrets:
        def __init__(self): self.value = ""
        def get(self): return self.value
        def set(self, value): self.value = value

    class Response(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *_): self.close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.secrets = self.Secrets()
        self.client = TelegramClient(self.store, self.secrets, network_helper=Path(self.temp.name) / "nonexistent")

    def tearDown(self): self.temp.cleanup()

    def test_connects_only_to_channel_with_posting_rights(self):
        replies = [
            {"ok": True, "result": {"id": 77, "username": "rieltor_bot"}},
            {"ok": True, "result": {"id": -100123, "type": "channel", "title": "Нячанг"}},
            {"ok": True, "result": {"status": "administrator", "can_post_messages": True}},
        ]
        def respond(request, timeout):
            return self.Response(json.dumps(replies.pop(0)).encode())
        example = "🏠 Готовый пост\nЦена: 15 млн ₫\n#нячанг"
        with patch("botrieltor.telegram.urlopen", side_effect=respond):
            result = self.client.configure("@nhatrang", "123:secret", example)
        self.assertTrue(result["connected"])
        self.assertEqual(result["channel_id"], "-100123")
        self.assertEqual(result["channel_title"], "Нячанг")
        self.assertEqual(result["bot_username"], "rieltor_bot")
        self.assertEqual(result["message_example"], example)
        self.assertEqual(self.secrets.value, "123:secret")
        self.assertNotIn(b"123:secret", self.store.path.read_bytes())

    def test_message_example_accepts_free_form_text(self):
        self.assertEqual(self.client.validate_message_example("  #tag {anything}  "), "#tag {anything}")

    def test_polled_snapshot_never_reads_keychain(self):
        self.secrets.get = lambda: (_ for _ in ()).throw(AssertionError("Keychain read"))
        self.assertFalse(self.client.snapshot()["has_token"])
        self.store.set_settings(telegram_channel_id="-100123")
        self.assertTrue(self.client.snapshot()["has_token"])

    def test_edits_album_caption_without_republishing(self):
        self.store.set_settings(telegram_channel_id="-100123")
        self.secrets.set("123:secret")
        with patch.object(self.client, "_call_macos", return_value={"message_id": 8}) as call:
            self.client.edit_post(8, "Исправленный пост", has_media=True)
        self.assertEqual(call.call_args.args[:2], ("123:secret", "editMessageCaption"))
        self.assertEqual(call.call_args.args[2]["message_id"], 8)

    def test_rejects_non_channel_or_missing_post_permission(self):
        common = {"ok": True, "result": {"id": 77, "username": "bot"}}
        group = {"ok": True, "result": {"id": -1, "type": "supergroup", "title": "Group"}}
        with patch("botrieltor.telegram.urlopen", side_effect=[self.Response(json.dumps(common).encode()), self.Response(json.dumps(group).encode())]):
            with self.assertRaisesRegex(ValueError, "не Telegram-канал"):
                self.client.configure("@channel", "123:secret")
        self.assertEqual(self.secrets.value, "")

        channel = {"ok": True, "result": {"id": -1001, "type": "channel", "title": "Channel"}}
        member = {"ok": True, "result": {"status": "administrator", "can_post_messages": False}}
        replies = [common, channel, member]
        with patch("botrieltor.telegram.urlopen", side_effect=lambda *_args, **_kwargs: self.Response(json.dumps(replies.pop(0)).encode())):
            with self.assertRaisesRegex(ValueError, "правом публикации"):
                self.client.configure("@channel", "123:secret")
        self.assertEqual(self.secrets.value, "")


class OmniRouteTests(unittest.TestCase):
    class Secrets:
        def __init__(self): self.value = ""
        def get(self): return self.value
        def set(self, value): self.value = value

    class Response(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *_): self.close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.secrets = self.Secrets()
        self.client = OmniRouteClient(self.store, self.secrets)

    def tearDown(self): self.temp.cleanup()

    def test_configure_uses_gateway_and_keeps_key_out_of_sqlite(self):
        payload = json.dumps({"data": [{"id": "agy/gemini-3.7-flash-tiered"}]}).encode()
        with patch("botrieltor.omniroute.urlopen", return_value=self.Response(payload)) as request:
            result = self.client.configure("http://127.0.0.1:20128/v1", "agy/gemini-3.7-flash-tiered", "secret-test-key")
        self.assertTrue(result["connected"])
        self.assertEqual(result["base_url"], "http://127.0.0.1:20128")
        self.assertIn("Bearer secret-test-key", request.call_args.args[0].headers.values())
        self.assertNotIn(b"secret-test-key", self.store.path.read_bytes())

    def test_polled_snapshot_never_reads_keychain(self):
        self.secrets.get = lambda: (_ for _ in ()).throw(AssertionError("Keychain read"))
        self.assertFalse(self.client.snapshot()["has_key"])
        self.store.set_settings(omniroute_model="agy/gemini-3.7-flash-tiered")
        self.assertTrue(self.client.snapshot()["has_key"])

    def test_rejects_insecure_remote_url_and_unknown_model(self):
        with self.assertRaises(ValueError):
            self.client.configure("http://example.com", "model", "key")
        self.secrets.set("key")
        self.store.set_settings(omniroute_base_url="http://127.0.0.1:20128", omniroute_model="missing")
        payload = json.dumps({"data": [{"id": "available"}]}).encode()
        with patch("botrieltor.omniroute.urlopen", return_value=self.Response(payload)):
            with self.assertRaisesRegex(ValueError, "отсутствует"):
                self.client.test()

    def test_504_retries_then_falls_back_only_to_allowed_gemini_route(self):
        primary = "agy/gemini-3.7-flash-tiered"
        fallback = "antigravity/gemini-3.7-flash-tiered"
        self.secrets.set("key")
        self.store.set_settings(omniroute_base_url="http://127.0.0.1:20128", omniroute_model=primary)
        self.client.available_models = [primary, fallback, "openai/gpt-5", "agy/gemini-image"]
        requested_models = []

        def respond(request, timeout):
            requested_models.append(json.loads(request.data)["model"])
            if len(requested_models) == 1:
                raise HTTPError(request.full_url, 504, "gateway timeout", None, None)
            payload = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
            return self.Response(payload)

        with patch("botrieltor.omniroute.urlopen", side_effect=respond), \
             patch("botrieltor.omniroute.time.sleep"):
            result = self.client.chat([{"role": "user", "content": "test"}])

        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        self.assertEqual(requested_models, [primary, fallback])
        self.assertTrue(all(self.client.allowed_model(model) for model in requested_models))
        self.assertEqual(self.client.last_used_model, fallback)
        self.assertEqual(self.client.fallback_count, 1)

    def test_network_timeout_uses_next_allowed_route(self):
        primary = "antigravity/gemini-3.7-flash-tiered"
        fallback = "agy/gemini-3.7-flash-tiered"
        self.secrets.set("key")
        self.store.set_settings(omniroute_base_url="http://127.0.0.1:20128", omniroute_model=primary)
        self.client.available_models = [primary, fallback]
        requested_models = []

        def respond(request, timeout):
            requested_models.append(json.loads(request.data)["model"])
            if len(requested_models) == 1:
                raise URLError(TimeoutError("timed out"))
            return self.Response(json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode())

        with patch("botrieltor.omniroute.urlopen", side_effect=respond), \
             patch("botrieltor.omniroute.time.sleep"):
            self.client.chat([{"role": "user", "content": "test"}])

        self.assertEqual(requested_models, [primary, fallback])
        self.assertEqual(self.client.last_used_model, fallback)

    def test_arbitrary_route_falls_back_to_same_model_on_another_route(self):
        primary = "provider-one/custom-editor"
        fallback = "provider-two/custom-editor"
        self.secrets.set("key")
        self.store.set_settings(omniroute_base_url="http://127.0.0.1:20128", omniroute_model=primary)
        self.client.available_models = [primary, fallback]
        requested_models = []

        def respond(request, timeout):
            requested_models.append(json.loads(request.data)["model"])
            if len(requested_models) == 1:
                raise HTTPError(request.full_url, 504, "timeout", None, None)
            return self.Response(json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode())

        with patch("botrieltor.omniroute.urlopen", side_effect=respond), \
             patch("botrieltor.omniroute.time.sleep"):
            self.client.chat([{"role": "user", "content": "test"}], model=primary)
        self.assertEqual(requested_models, [primary, fallback])

    def test_invalid_model_output_uses_fallback_before_publishing(self):
        primary = "provider-one/custom-editor"
        fallback = "provider-two/custom-editor"
        self.secrets.set("key")
        self.store.set_settings(omniroute_base_url="http://127.0.0.1:20128", omniroute_model=primary)
        self.client.available_models = [primary, fallback]
        replies = ["bad", "valid"]

        def respond(request, timeout):
            content = replies.pop(0)
            return self.Response(json.dumps({"choices": [{"message": {"content": content}}]}).encode())

        def validate(result):
            if result["choices"][0]["message"]["content"] != "valid":
                raise ValueError("invalid post")

        with patch("botrieltor.omniroute.urlopen", side_effect=respond), \
             patch("botrieltor.omniroute.time.sleep"):
            result = self.client.chat([{"role": "user", "content": "test"}], model=primary, validator=validate)
        self.assertEqual(result["choices"][0]["message"]["content"], "valid")
        self.assertEqual(self.client.last_used_model, fallback)

    def test_chat_allows_user_selected_route(self):
        self.secrets.set("key")
        self.store.set_settings(omniroute_base_url="http://127.0.0.1:20128", omniroute_model="openai/gpt-5")
        self.client.available_models = ["openai/gpt-5"]
        payload = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
        with patch("botrieltor.omniroute.urlopen", return_value=self.Response(payload)) as request, \
             patch("botrieltor.omniroute.time.sleep"):
            self.client.chat([{"role": "user", "content": "test"}])
        self.assertEqual(json.loads(request.call_args.args[0].data)["model"], "openai/gpt-5")

    def test_claude_can_be_selected_for_an_agent(self):
        model = "agy/claude-opus-4-6-thinking-medium"
        self.secrets.set("key")
        self.store.set_settings(omniroute_base_url="http://127.0.0.1:20128", omniroute_model="agy/gemini-3.7-flash-tiered")
        self.client.available_models = ["agy/gemini-3.7-flash-tiered", model]
        payload = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
        with patch("botrieltor.omniroute.urlopen", return_value=self.Response(payload)) as request, \
             patch("botrieltor.omniroute.time.sleep"):
            result = self.client.one_off_claude([{"role": "user", "content": "hard case"}], model=model)
        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        self.assertEqual(request.call_count, 1)
        self.assertEqual(self.client.last_route_reason, "manual")
        with patch("botrieltor.omniroute.urlopen", return_value=self.Response(payload)) as request, \
             patch("botrieltor.omniroute.time.sleep"):
            self.client.chat([{"role": "user", "content": "hard case"}], model=model)
        self.assertEqual(json.loads(request.call_args.args[0].data)["model"], model)

    def test_gweb_is_never_used_even_when_present_in_model_list(self):
        primary = "agy/gemini-3.7-flash-tiered"
        secondary = "antigravity/gemini-3.7-flash-tiered"
        emergency = "gweb/gemini-3.7-flash"
        self.secrets.set("key")
        self.store.set_settings(omniroute_base_url="http://127.0.0.1:20128", omniroute_model=primary)
        self.client.available_models = [primary, secondary, emergency]
        requested_models = []

        def respond(request, timeout):
            model = json.loads(request.data)["model"]
            requested_models.append(model)
            raise HTTPError(request.full_url, 504, "gateway timeout", None, None)

        with patch("botrieltor.omniroute.urlopen", side_effect=respond), \
             patch("botrieltor.omniroute.time.sleep"):
            with self.assertRaises(RuntimeError):
                self.client.chat([{"role": "user", "content": "test"}])

        self.assertEqual(requested_models, [primary, secondary])
        self.assertNotIn(emergency, requested_models)

    def test_gweb_can_be_selected_explicitly(self):
        self.secrets.set("key")
        model = "gweb/gemini-3.7-flash"
        payload = json.dumps({"data": [{"id": model}]}).encode()
        with patch("botrieltor.omniroute.urlopen", return_value=self.Response(payload)):
            result = self.client.configure("http://127.0.0.1:20128", model)
        self.assertEqual(result["model"], model)

    def test_configure_saves_independent_agent_models_without_route_restrictions(self):
        models = {
            "archive": "custom/fast-parser",
            "listing": "openai/gpt-5",
            "decision": "agy/claude-opus-4-6-thinking-medium",
            "publisher": "gweb/gemini-3.7-flash",
        }
        payload = json.dumps({"data": [{"id": value} for value in models.values()]}).encode()
        with patch("botrieltor.omniroute.urlopen", return_value=self.Response(payload)):
            result = self.client.configure("http://127.0.0.1:20128", api_key="key", agent_models=models)
        self.assertEqual(result["agent_models"], models)
        self.assertEqual(result["model"], models["listing"])

    def test_model_requests_are_globally_spaced_and_retry_after_is_honored(self):
        self.client._last_request_at = 100.0
        with patch("botrieltor.omniroute.time.monotonic", side_effect=[105.0, 117.0]), \
             patch("botrieltor.omniroute.time.sleep") as sleep:
            self.client._wait_for_request_slot()
        sleep.assert_called_once_with(7.0)
        self.assertEqual(self.client._last_request_at, 117.0)

        with patch("botrieltor.omniroute.time.monotonic", return_value=200.0):
            self.client._schedule_backoff(0, "45")
        self.assertEqual(self.client._cooldown_until, 245.0)

    def test_local_omniroute_execution_expiration_stays_on_allowed_prefixes(self):
        primary = "agy/gemini-3.7-flash-tiered"
        same_limiter = "antigravity/gemini-3.7-flash-tiered"
        emergency = "gweb/gemini-3.7-flash"
        self.secrets.set("key")
        self.store.set_settings(omniroute_base_url="http://127.0.0.1:20128", omniroute_model=primary)
        self.client.available_models = [primary, same_limiter, "agy/gemini-3.8-flash-tiered", emergency]
        requested_models = []

        def respond(request, timeout):
            model = json.loads(request.data)["model"]
            requested_models.append(model)
            if model == primary:
                body = b"Request exceeded OmniRoute's local rate-limit execution expiration"
                raise HTTPError(request.full_url, 504, "timeout", {}, io.BytesIO(body))
            return self.Response(json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode())

        with patch("botrieltor.omniroute.urlopen", side_effect=respond), \
             patch("botrieltor.omniroute.time.sleep"):
            self.client.chat([{"role": "user", "content": "test"}])

        self.assertEqual(requested_models, [primary, same_limiter])
        self.assertEqual(self.client.last_used_model, same_limiter)

    def test_normal_calls_stay_on_oauth_pool_and_ignore_gweb(self):
        primary = "agy/gemini-3.7-flash-tiered"
        web = "gweb/gemini-3.7-flash"
        self.secrets.set("key")
        self.store.set_settings(omniroute_base_url="http://127.0.0.1:20128", omniroute_model=primary)
        self.client.available_models = [primary, "agy/gemini-3.8-flash-tiered", web]
        requested_models = []

        def respond(request, timeout):
            requested_models.append(json.loads(request.data)["model"])
            return self.Response(json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode())

        with patch("botrieltor.omniroute.urlopen", side_effect=respond), \
             patch("botrieltor.omniroute.time.sleep"):
            for _ in range(4):
                self.client.chat([{"role": "user", "content": "test"}])

        self.assertEqual(requested_models, [primary, primary, primary, primary])
        self.assertEqual(self.client.rotation_count, 0)

    def test_client_timeout_retries_then_uses_allowed_alias(self):
        primary = "agy/gemini-3.7-flash-tiered"
        web = "gweb/gemini-3.7-flash"
        self.secrets.set("key")
        self.store.set_settings(omniroute_base_url="http://127.0.0.1:20128", omniroute_model=primary)
        self.client.available_models = [primary, "antigravity/gemini-3.7-flash-tiered", web]
        requested_models = []

        def respond(request, timeout):
            model = json.loads(request.data)["model"]
            requested_models.append(model)
            if model == primary:
                raise TimeoutError("timed out")
            return self.Response(json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode())

        with patch("botrieltor.omniroute.urlopen", side_effect=respond), \
             patch("botrieltor.omniroute.time.sleep"):
            self.client.chat([{"role": "user", "content": "test"}])

        self.assertEqual(requested_models, [primary, "antigravity/gemini-3.7-flash-tiered"])
        self.assertNotIn(web, requested_models)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.runtime = Runtime(self.store)
        self.server, self.token = build_server(self.store, self.runtime)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.runtime.close()
        self.temp.cleanup()

    def request(self, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        try:
            connection.request("GET" if body is None else "POST", path, body=None if body is None else json.dumps(body), headers=headers or {})
            result = connection.getresponse()
            return result.status, result.read().decode(), dict(result.getheaders())
        finally:
            connection.close()

    def request_bytes(self, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        try:
            connection.request("GET", path, headers=headers or {})
            result = connection.getresponse()
            return result.status, result.read(), dict(result.getheaders())
        finally:
            connection.close()

    def test_loopback_token_and_origin_are_enforced(self):
        self.assertEqual(self.request("/api/snapshot")[0], 403)
        self.assertEqual(self.request("/", headers={"Host":"malicious.example"})[0], 403)
        headers = {"X-App-Token":self.token,"Origin":"https://malicious.example"}
        self.assertEqual(self.request("/api/listings", {"title":"Injected"}, headers)[0], 403)
        self.assertEqual(len(self.store.snapshot()["listings"]), 0)

    def test_native_frontend_assets_and_channel_settings(self):
        status, html, headers = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(self.token, html)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIn("Пример готового сообщения", html)
        self.assertIn("zalo-badge", html)
        self.assertIn("total-published", html)
        self.assertEqual(self.request("/app.js")[0], 200)
        self.assertEqual(self.request("/../../etc/passwd")[0], 404)
        _, data, _ = self.request("/api/snapshot", headers={"X-App-Token":self.token})
        settings = json.loads(data)["settings"]
        self.assertEqual(settings["destination_kind"], "channel")
        self.assertFalse(settings["expiry_enabled"])
        self.assertFalse(settings["outbound_conversations_enabled"])

    def test_manual_status_change_and_stale_revision(self):
        headers = {"X-App-Token":self.token}
        self.assertEqual(self.request("/api/listings", {"title":"Nha Trang","status":"available"}, headers)[0], 200)
        listing = self.store.snapshot()["listings"][0]
        body = {"id":listing["id"],"status":"rented","revision":1}
        self.assertEqual(self.request("/api/listing-status", body, headers)[0], 200)
        self.assertEqual(self.request("/api/listing-status", body, headers)[0], 409)
        self.assertEqual(self.store.snapshot()["listings"][0]["status"], "rented")
        self.assertEqual(self.request("/api/send-message", {"text":"not allowed"}, headers)[0], 404)

    def test_listing_media_is_visible_only_with_token(self):
        source = self.store.add_source("Offers", "group")
        image = Path(self.temp.name) / "raw-media" / "1" / "photo.jpg"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"fake-jpeg")
        self.store.commit_batch(source, "collect_archive", [{"external_id":"m1", "sender":"Lan", "sent_at":"10:00", "text":"Home", "media":[{"external_id":"p1", "local_path":str(image), "download_state":"done", "kind":"image", "sha256":"abc"}]}], None)
        listing_id = self.store.add_listing("Home")
        with self.store.connect() as db:
            message_id = db.execute("SELECT id FROM messages").fetchone()[0]
            db.execute("INSERT INTO listing_messages VALUES (?,?,1)", (listing_id, message_id))
        listing = self.store.snapshot()["listings"][0]
        self.assertEqual(listing["sources"][0]["name"], "Offers")
        media_id = listing["media"][0]["id"]
        self.assertEqual(self.request_bytes(f"/api/media/{media_id}")[0], 403)
        status, body, headers = self.request_bytes(f"/api/media/{media_id}?token={self.token}")
        self.assertEqual((status, body, headers["Content-Type"]), (200, b"fake-jpeg", "image/jpeg"))


if __name__ == "__main__":
    unittest.main()
