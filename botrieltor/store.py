from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


WAITING_PHOTOS_TTL_SECONDS = 60


class Conflict(ValueError):
    pass


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('group','personal')),
    enabled INTEGER NOT NULL DEFAULT 1, archive_state TEXT NOT NULL DEFAULT 'not_started',
    binding_state TEXT NOT NULL DEFAULT 'unbound', external_ref TEXT,
    observed_name TEXT NOT NULL DEFAULT '', binding_detail TEXT NOT NULL DEFAULT '',
    bootstrap_state TEXT NOT NULL DEFAULT 'reconciled',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(id),
    external_id TEXT NOT NULL, sender TEXT NOT NULL, sent_at TEXT NOT NULL,
    text TEXT NOT NULL, reply_to TEXT, payload TEXT NOT NULL, fingerprint TEXT NOT NULL,
    captured_at TEXT NOT NULL, UNIQUE(source_id, external_id)
);
CREATE TABLE IF NOT EXISTS message_versions (
    id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL REFERENCES messages(id),
    payload TEXT NOT NULL, captured_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS message_analysis (
    message_id INTEGER PRIMARY KEY REFERENCES messages(id),
    state TEXT NOT NULL, listing_id INTEGER REFERENCES listings(id), analyzed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL REFERENCES messages(id),
    external_id TEXT NOT NULL, kind TEXT NOT NULL, source_ref TEXT,
    local_path TEXT, sha256 TEXT, download_state TEXT NOT NULL DEFAULT 'pending',
    UNIQUE(message_id, external_id)
);
CREATE TABLE IF NOT EXISTS raw_captures (
    id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(id),
    external_id TEXT NOT NULL, captured_at TEXT NOT NULL, source_text TEXT NOT NULL,
    payload TEXT NOT NULL, fingerprint TEXT NOT NULL,
    processing_state TEXT NOT NULL DEFAULT 'pending',
    UNIQUE(source_id, external_id)
);
CREATE TABLE IF NOT EXISTS raw_media (
    id INTEGER PRIMARY KEY, capture_id INTEGER NOT NULL REFERENCES raw_captures(id),
    external_id TEXT NOT NULL, kind TEXT NOT NULL, source_ref TEXT,
    local_path TEXT, sha256 TEXT, download_state TEXT NOT NULL DEFAULT 'pending',
    UNIQUE(capture_id, external_id)
);
CREATE TABLE IF NOT EXISTS checkpoints (
    source_id INTEGER NOT NULL REFERENCES sources(id), mode TEXT NOT NULL,
    cursor TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(source_id, mode)
);
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY, title TEXT NOT NULL, city TEXT NOT NULL DEFAULT 'Нячанг',
    location TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '', price TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK(status IN ('unverified','available','rented')),
    status_reason TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS listing_messages (
    listing_id INTEGER NOT NULL REFERENCES listings(id), message_id INTEGER NOT NULL REFERENCES messages(id),
    confirmed INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(listing_id, message_id)
);
CREATE TABLE IF NOT EXISTS offers (
    id INTEGER PRIMARY KEY, listing_id INTEGER NOT NULL REFERENCES listings(id),
    source_id INTEGER NOT NULL REFERENCES sources(id), author TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unverified', evidence_message_id INTEGER REFERENCES messages(id)
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY, listing_id INTEGER REFERENCES listings(id),
    reason TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publications (
    id INTEGER PRIMARY KEY, listing_id INTEGER NOT NULL REFERENCES listings(id),
    destination_kind TEXT NOT NULL DEFAULT 'channel' CHECK(destination_kind = 'channel'),
    channel_id TEXT NOT NULL, message_ids TEXT NOT NULL,
    applied_revision INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'sent' CHECK(state IN ('publishing','sent','failed')),
    body TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT '',
    UNIQUE(listing_id, channel_id)
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY, source_id INTEGER REFERENCES sources(id),
    kind TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
    detail TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_collection
    ON jobs(source_id, kind) WHERE state IN ('pending','running','blocked');
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY, publication_id INTEGER NOT NULL REFERENCES publications(id),
    revision INTEGER NOT NULL, action TEXT NOT NULL CHECK(action IN ('publish_listing','edit_listing')),
    state TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
    UNIQUE(publication_id, revision, action)
);
CREATE TABLE IF NOT EXISTS listing_automation (
    listing_id INTEGER PRIMARY KEY REFERENCES listings(id),
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','running','decided','done','review')),
    decision TEXT NOT NULL DEFAULT '' CHECK(decision IN ('','publish','rented','review')),
    reason TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '', retry_after REAL NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_automation (
    review_id INTEGER PRIMARY KEY REFERENCES reviews(id),
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','running','done','review')),
    decision TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
    retry_after REAL NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL
);
"""


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.data_dir / "workspace.sqlite3"
        with self.connect() as db:
            db.executescript(SCHEMA)
            posting_default_migration = db.execute(
                "SELECT 1 FROM settings WHERE key='telegram_posting_default_off_v1'").fetchone()
            if not posting_default_migration:
                db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('telegram_posting_enabled','false')")
                db.execute("INSERT INTO settings(key,value) VALUES ('telegram_posting_default_off_v1','true')")
            defaults = {"city": "Нячанг", "destination_kind": "channel",
                        "expiry_enabled": False, "outbound_conversations_enabled": False,
                        "history_depth": "all_available", "poll_minutes": 15}
            for key, value in defaults.items():
                db.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (key, json.dumps(value)))
            incremental_bootstrap = db.execute(
                "SELECT 1 FROM settings WHERE key='zalo_incremental_only_bootstrap_v1'"
            ).fetchone()
            if not incremental_bootstrap:
                # Existing chats are baselined on launch; do not backfill their
                # entire Zalo history or reprocess evidence already on disk.
                baseline_time = now()
                baseline_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('zalo_new_messages_baseline',?)",
                           (json.dumps(baseline_time),))
                db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('zalo_baseline_epoch_ms',?)",
                           (json.dumps(baseline_ms),))
                db.execute("UPDATE sources SET archive_state='complete' WHERE enabled=1")
                db.execute("UPDATE raw_captures SET processing_state='parsed' WHERE processing_state='pending'")
                db.execute(
                    "INSERT OR IGNORE INTO message_analysis(message_id,state,listing_id,analyzed_at) "
                    "SELECT id,'reviewed',NULL,? FROM messages WHERE id NOT IN (SELECT message_id FROM message_analysis)",
                    (baseline_time,),
                )
                db.execute(
                    "UPDATE jobs SET state='done',detail='История до обновления пропущена',updated_at=? "
                    "WHERE kind IN ('collect_archive','interpret_raw','build_listings') "
                    "AND state IN ('pending','running','blocked')",
                    (baseline_time,),
                )
                db.execute("INSERT INTO settings(key,value) VALUES ('zalo_incremental_only_bootstrap_v1','true')")
            incremental_bootstrap_v2 = db.execute(
                "SELECT 1 FROM settings WHERE key='zalo_incremental_only_bootstrap_v2'"
            ).fetchone()
            if not incremental_bootstrap_v2:
                # Drop a stale UI indicator left by the old photo-only history scan.
                db.execute("DELETE FROM settings WHERE key='waiting_photos'")
                db.execute("UPDATE sources SET archive_state='complete' WHERE enabled=1")
                db.execute(
                    "UPDATE jobs SET state='done',detail='Архивная история пропущена',updated_at=? "
                    "WHERE kind='collect_archive' AND state IN ('pending','running','blocked')",
                    (now(),),
                )
                db.execute("INSERT INTO settings(key,value) VALUES ('zalo_incremental_only_bootstrap_v2','true')")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(sources)")}
            migrations = {
                "binding_state": "ALTER TABLE sources ADD COLUMN binding_state TEXT NOT NULL DEFAULT 'unbound'",
                "external_ref": "ALTER TABLE sources ADD COLUMN external_ref TEXT",
                "observed_name": "ALTER TABLE sources ADD COLUMN observed_name TEXT NOT NULL DEFAULT ''",
                "binding_detail": "ALTER TABLE sources ADD COLUMN binding_detail TEXT NOT NULL DEFAULT ''",
            }
            for column, statement in migrations.items():
                if column not in columns:
                    db.execute(statement)
            added_bootstrap_column = "bootstrap_state" not in columns
            if added_bootstrap_column:
                db.execute("ALTER TABLE sources ADD COLUMN bootstrap_state TEXT NOT NULL DEFAULT 'reconciled'")
            source_bootstrap_migration = db.execute(
                "SELECT 1 FROM settings WHERE key='zalo_source_24h_bootstrap_v1'").fetchone()
            if not source_bootstrap_migration:
                # Before this migration no source could have been explicitly
                # reconciled against BotRieltor's publication history.
                if added_bootstrap_column:
                    db.execute("UPDATE sources SET archive_state='not_started',bootstrap_state='scanning' WHERE enabled=1")
                else:
                    db.execute(
                        """UPDATE sources SET archive_state='not_started',bootstrap_state='scanning'
                           WHERE enabled=1 AND archive_state='not_started'
                             AND NOT EXISTS (SELECT 1 FROM raw_captures r WHERE r.source_id=sources.id)
                             AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.source_id=sources.id)
                             AND NOT EXISTS (SELECT 1 FROM checkpoints c WHERE c.source_id=sources.id)""")
                db.execute("INSERT INTO settings(key,value) VALUES ('zalo_source_24h_bootstrap_v1','true')")
            # The first five-group import reached the top of each viewport but
            # the browser driver failed to extract message timestamps. If an
            # enabled source was therefore marked complete without saving any
            # evidence, let the app itself retry it with the corrected driver.
            # Disabled sources (including GINSENG) are never touched here.
            bootstrap_retry = db.execute(
                "SELECT 1 FROM settings WHERE key='zalo_24h_bootstrap_retry_v1'").fetchone()
            if not bootstrap_retry:
                empty_sources = [row[0] for row in db.execute(
                    """SELECT s.id FROM sources s
                       WHERE s.enabled=1 AND s.archive_state='complete' AND s.bootstrap_state='processing'
                         AND NOT EXISTS (SELECT 1 FROM raw_captures r WHERE r.source_id=s.id)
                         AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.source_id=s.id)""")]
                for source_id in empty_sources:
                    db.execute("UPDATE sources SET archive_state='not_started',bootstrap_state='scanning' WHERE id=?",
                               (source_id,))
                    db.execute("DELETE FROM checkpoints WHERE source_id=? AND mode IN ('collect_initial_scan','collect_archive','collect_incremental')",
                               (source_id,))
                    latest_scan = db.execute(
                        "SELECT max(id) FROM jobs WHERE source_id=? AND kind IN ('collect_initial_scan','collect_archive')",
                        (source_id,)).fetchone()[0]
                    if latest_scan:
                        db.execute("UPDATE jobs SET state='pending',detail='Повтор первичного импорта после исправления дат' WHERE id=?",
                                   (latest_scan,))
                db.execute("INSERT INTO settings(key,value) VALUES ('zalo_24h_bootstrap_retry_v1','true')")
            bootstrap_resume = db.execute(
                "SELECT 1 FROM settings WHERE key='zalo_24h_bootstrap_resume_v1'").fetchone()
            if not bootstrap_resume:
                # Recover only incomplete first-scan jobs interrupted by the old
                # timestamp gate. The normal runtime will continue these jobs.
                db.execute(
                    """UPDATE jobs SET state='pending',detail='Повтор первичного импорта с надёжной границей истории'
                       WHERE id IN (
                           SELECT j.id FROM jobs j JOIN sources s ON s.id=j.source_id
                           WHERE s.enabled=1 AND s.bootstrap_state='scanning'
                             AND j.kind IN ('collect_initial_scan','collect_archive')
                             AND j.state IN ('blocked','failed')
                             AND (j.detail LIKE '%границы 24 часов%' OR j.detail LIKE '%драйвер Zalo неожиданно остановился%')
                       )""")
                db.execute("INSERT INTO settings(key,value) VALUES ('zalo_24h_bootstrap_resume_v1','true')")
            trusted_scroll_scan = db.execute(
                "SELECT 1 FROM settings WHERE key='zalo_24h_trusted_scroll_scan_v1'"
            ).fetchone()
            if not trusted_scroll_scan:
                # Earlier scans could claim they reached chat history top even
                # when the selected scroll pane was not the pane snapshotted.
                # A top-only completion without any dated bubble is not evidence
                # that the 24-hour window was covered; retry enabled groups once.
                retry_sources = []
                for source in db.execute(
                    "SELECT id FROM sources WHERE enabled=1 AND kind='group' AND archive_state='complete'"
                ).fetchall():
                    checkpoint = db.execute(
                        "SELECT cursor FROM checkpoints WHERE source_id=? AND mode='collect_initial_scan'",
                        (source['id'],),
                    ).fetchone()
                    if not checkpoint:
                        continue
                    try:
                        cursor = json.loads(checkpoint['cursor'])
                    except (TypeError, ValueError):
                        continue
                    untrusted_top = (bool(cursor.get('history_top_reached'))
                                     and not bool(cursor.get('cutoff_reached'))
                                     and cursor.get('initial_scan_oldest_timestamp_ms') is None)
                    if untrusted_top:
                        retry_sources.append(int(source['id']))
                for source_id in retry_sources:
                    db.execute(
                        "UPDATE sources SET archive_state='not_started',bootstrap_state='scanning' WHERE id=?",
                        (source_id,),
                    )
                    db.execute(
                        "DELETE FROM checkpoints WHERE source_id=? AND mode IN ('collect_initial_scan','collect_archive','collect_incremental')",
                        (source_id,),
                    )
                    db.execute(
                        "UPDATE jobs SET state='done',detail='Перезапущено полное сканирование 24 часов',updated_at=? "
                        "WHERE source_id=? AND kind IN ('collect_initial_scan','collect_archive','collect_incremental') "
                        "AND state IN ('pending','running','blocked')",
                        (now(), source_id),
                    )
                    latest_scan = db.execute(
                        "SELECT max(id) FROM jobs WHERE source_id=? AND kind='collect_initial_scan'",
                        (source_id,),
                    ).fetchone()[0]
                    if latest_scan:
                        db.execute(
                            "UPDATE jobs SET state='pending',detail='Повтор первичного сканирования с подтверждённой прокруткой',updated_at=? WHERE id=?",
                            (now(), latest_scan),
                        )
                    else:
                        db.execute(
                            "INSERT INTO jobs(source_id,kind,state,detail,updated_at) VALUES (?,'collect_initial_scan','pending',?,?)",
                            (source_id, 'Повтор первичного сканирования с подтверждённой прокруткой', now()),
                        )
                if retry_sources:
                    self.event(db, "maintenance", f"Повторно сканирую последние 24 часа в {len(retry_sources)} группах: прежняя отметка «начало истории» не подтверждалась датами сообщений.")
                db.execute("INSERT INTO settings(key,value) VALUES ('zalo_24h_trusted_scroll_scan_v1','true')")
            publication_columns = {row["name"] for row in db.execute("PRAGMA table_info(publications)")}
            publication_migrations = {
                "state": "ALTER TABLE publications ADD COLUMN state TEXT NOT NULL DEFAULT 'sent'",
                "body": "ALTER TABLE publications ADD COLUMN body TEXT NOT NULL DEFAULT ''",
                "last_error": "ALTER TABLE publications ADD COLUMN last_error TEXT NOT NULL DEFAULT ''",
            }
            for column, statement in publication_migrations.items():
                if column not in publication_columns:
                    db.execute(statement)
            listing_columns = {row["name"] for row in db.execute("PRAGMA table_info(listings)")}
            if "location" not in listing_columns:
                db.execute("ALTER TABLE listings ADD COLUMN location TEXT NOT NULL DEFAULT ''")
            outbox_columns = {row["name"] for row in db.execute("PRAGMA table_info(outbox)")}
            if "last_error" not in outbox_columns:
                db.execute("ALTER TABLE outbox ADD COLUMN last_error TEXT NOT NULL DEFAULT ''")
            if "retry_after" not in outbox_columns:
                db.execute("ALTER TABLE outbox ADD COLUMN retry_after REAL NOT NULL DEFAULT 0")
            db.execute("UPDATE jobs SET state='done',detail=detail || ' · Позже успешно завершено' WHERE state='failed' AND EXISTS (SELECT 1 FROM jobs newer WHERE newer.source_id=jobs.source_id AND newer.kind=jobs.kind AND newer.state='done' AND newer.id>jobs.id)")
            db.execute("UPDATE jobs SET state='done',detail=detail || ' · Уже поставлен повтор' WHERE state='failed' AND EXISTS (SELECT 1 FROM jobs active WHERE active.source_id=jobs.source_id AND active.kind=jobs.kind AND active.state IN ('pending','running','blocked') AND active.id<>jobs.id)")
            raw_migration = db.execute("SELECT 1 FROM settings WHERE key='raw_archive_migrated_v1'").fetchone()
            if not raw_migration:
                db.execute("UPDATE sources SET archive_state='not_started' WHERE enabled=1")
                db.execute("UPDATE jobs SET state='done',detail='Заменено сырой загрузкой без модели',updated_at=? WHERE kind IN ('collect_archive','collect_incremental') AND state IN ('pending','running','blocked')", (now(),))
                db.execute("INSERT INTO settings(key,value) VALUES ('raw_archive_migrated_v1','true')")
            source_language_migration = db.execute("SELECT 1 FROM settings WHERE key='source_language_cards_v1'").fetchone()
            if not source_language_migration:
                changed = 0
                for listing in db.execute("SELECT id FROM listings ORDER BY id").fetchall():
                    texts = [row[0].strip() for row in db.execute(
                        """SELECT m.text FROM messages m JOIN listing_messages lm ON lm.message_id=m.id
                           WHERE lm.listing_id=? AND trim(m.text)<>'' ORDER BY m.sent_at,m.id""",
                        (listing["id"],)).fetchall() if row[0].strip()]
                    if not texts:
                        continue
                    description = "\n\n".join(texts)[:20000]
                    first_line = next((line.strip() for line in description.splitlines() if line.strip()), "Объект без названия")
                    title = first_line[:200]
                    db.execute("UPDATE listings SET title=?,description=?,revision=revision+1,updated_at=? WHERE id=?",
                               (title, description, now(), listing["id"]))
                    changed += 1
                db.execute("INSERT INTO settings(key,value) VALUES ('source_language_cards_v1','true')")
                if changed:
                    self.event(db, "listing", f"Восстановлен исходный язык карточек из сообщений Zalo: {changed}.")
            automation_migration = db.execute("SELECT 1 FROM settings WHERE key='listing_automation_v1'").fetchone()
            if not automation_migration:
                # Historical unverified cards remain manual so enabling this
                # feature cannot consume Claude quota on the whole archive.
                db.execute(
                    """INSERT OR IGNORE INTO listing_automation(listing_id,state,decision,reason,updated_at)
                       SELECT l.id,CASE WHEN EXISTS(SELECT 1 FROM publications p WHERE p.listing_id=l.id AND p.state='sent')
                                           THEN 'done' ELSE 'pending' END,
                              CASE WHEN EXISTS(SELECT 1 FROM publications p WHERE p.listing_id=l.id AND p.state='sent')
                                   THEN 'publish' ELSE '' END,
                              CASE WHEN EXISTS(SELECT 1 FROM publications p WHERE p.listing_id=l.id AND p.state='sent')
                                   THEN 'Уже опубликовано' ELSE 'Ожидает решения Claude' END,?
                       FROM listings l WHERE l.status='available'""", (now(),))
                db.execute("INSERT INTO settings(key,value) VALUES ('listing_automation_v1','true')")
            recent_automation_migration = db.execute("SELECT 1 FROM settings WHERE key='listing_automation_recent_v2'").fetchone()
            if not recent_automation_migration:
                # Catch cards/reviews created by the old process immediately
                # before this upgrade, without importing the historical archive.
                db.execute(
                    """INSERT OR IGNORE INTO listing_automation(listing_id,state,decision,reason,updated_at)
                       SELECT id,'pending','','Свежий объект, созданный перед обновлением',?
                       FROM listings WHERE status='unverified' AND datetime(created_at)>=datetime('now','-1 hour')""",
                    (now(),))
                db.execute(
                    """INSERT OR IGNORE INTO review_automation(review_id,updated_at)
                       SELECT id,? FROM reviews WHERE state='open' AND datetime(created_at)>=datetime('now','-1 hour')""",
                    (now(),))
                db.execute("INSERT INTO settings(key,value) VALUES ('listing_automation_recent_v2','true')")
            photo_rebuild = db.execute(
                "SELECT 1 FROM settings WHERE key='zalo_photo_attachment_rebuild_v2'"
            ).fetchone()
            if not photo_rebuild:
                # Rebuild only the enabled group sources from the last 24 hours.
                # Keep Zalo evidence and downloaded files intact; remove only
                # unposted derived cards so the normal application pipeline can
                # recreate them with the corrected photo association logic.
                cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
                cutoff_text = cutoff.isoformat(timespec="seconds")
                cutoff_ms = int(cutoff.timestamp() * 1000)
                source_ids = [row[0] for row in db.execute(
                    "SELECT id FROM sources WHERE enabled=1 AND kind='group'")]
                if source_ids:
                    source_marks = ",".join("?" for _ in source_ids)
                    listing_ids = [row[0] for row in db.execute(
                        f"""SELECT DISTINCT lm.listing_id FROM listing_messages lm
                            JOIN messages m ON m.id=lm.message_id
                            WHERE m.source_id IN ({source_marks})
                              AND NOT EXISTS (SELECT 1 FROM publications p
                                              WHERE p.listing_id=lm.listing_id AND p.state='sent')""",
                        source_ids)]
                    if listing_ids:
                        marks = ",".join("?" for _ in listing_ids)
                        review_ids = [row[0] for row in db.execute(
                            f"SELECT id FROM reviews WHERE listing_id IN ({marks})", listing_ids)]
                        if review_ids:
                            review_marks = ",".join("?" for _ in review_ids)
                            db.execute(f"DELETE FROM review_automation WHERE review_id IN ({review_marks})", review_ids)
                            db.execute(f"DELETE FROM reviews WHERE id IN ({review_marks})", review_ids)
                        db.execute(f"DELETE FROM listing_automation WHERE listing_id IN ({marks})", listing_ids)
                        db.execute(f"DELETE FROM offers WHERE listing_id IN ({marks})", listing_ids)
                        db.execute(f"UPDATE message_analysis SET listing_id=NULL WHERE listing_id IN ({marks})", listing_ids)
                        db.execute(f"DELETE FROM listing_messages WHERE listing_id IN ({marks})", listing_ids)
                        db.execute(f"DELETE FROM listings WHERE id IN ({marks})", listing_ids)
                    # Reset recent analysis; keep older records out of this import.
                    db.execute(
                        f"""DELETE FROM message_analysis WHERE message_id IN (
                            SELECT id FROM messages WHERE source_id IN ({source_marks})
                              AND datetime(captured_at)>=datetime(?)
                        )""", (*source_ids, cutoff_text))
                    db.execute(
                        f"""INSERT OR REPLACE INTO message_analysis(message_id,state,listing_id,analyzed_at)
                            SELECT id,'reviewed',NULL,? FROM messages
                            WHERE source_id IN ({source_marks}) AND datetime(captured_at)<datetime(?)""",
                        (now(), *source_ids, cutoff_text))
                    db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('zalo_new_messages_baseline',?)",
                               (json.dumps(cutoff_text),))
                    db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('zalo_baseline_epoch_ms',?)",
                               (json.dumps(cutoff_ms),))
                    db.execute("DELETE FROM settings WHERE key='waiting_photos'")
                    self.event(db, "maintenance", "Повторная сборка объявлений за 24 часа из пяти активных групп Zalo; фото и исходная история сохранены.")
                db.execute("INSERT INTO settings(key,value) VALUES ('zalo_photo_attachment_rebuild_v2','true')")

            # The active workflow is now one source: GINSENG.  Apply this after
            # legacy archive/seed migrations so those five-group routines cannot
            # re-enable historic backfills or hold the runtime on a five-source
            # bootstrap.  Keep all existing evidence and cards, but make the
            # cutover point explicit so only messages captured from now on enter
            # the automatic publication pipeline.
            single_source_migration = db.execute(
                "SELECT 1 FROM settings WHERE key='zalo_ginseng_only_workflow_v1'"
            ).fetchone()
            if not single_source_migration:
                def source_key(value):
                    return "".join(
                        char.casefold() for char in unicodedata.normalize("NFKC", str(value))
                        if char.isalnum()
                    )

                candidates = [row for row in db.execute(
                    "SELECT * FROM sources WHERE kind='group'"
                ).fetchall() if source_key(row["name"]) == "ginseng"]
                if candidates:
                    chosen = sorted(candidates, key=lambda row: (
                        not bool(row["external_ref"]), not bool(row["enabled"]), row["id"]
                    ))[0]
                    source_id = int(chosen["id"])
                else:
                    source_id = db.execute(
                        """INSERT INTO sources(name,kind,enabled,archive_state,bootstrap_state,created_at)
                           VALUES ('GINSENG','group',1,'complete','reconciled',?)""",
                        (now(),),
                    ).lastrowid

                db.execute("UPDATE sources SET enabled=CASE WHEN id=? THEN 1 ELSE 0 END", (source_id,))
                stamp = now()
                epoch_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

                # Cancel interrupted work from the old multi-group setup. A
                # fresh bind is queued below if GINSENG has no saved chat ID.
                db.execute(
                    """UPDATE jobs SET state='done',detail='Источник отключён при переходе на GINSENG',updated_at=?
                       WHERE (source_id IS NULL OR source_id<>?)
                         AND state IN ('pending','running','blocked','failed')""",
                    (stamp, source_id),
                )
                db.execute(
                    """UPDATE jobs SET state='done',detail='Старый архив закрыт при переходе на новые объявления GINSENG',updated_at=?
                       WHERE source_id=? AND state IN ('pending','running','blocked','failed')""",
                    (stamp, source_id),
                )
                db.execute("UPDATE sources SET archive_state='complete',bootstrap_state='reconciled' WHERE id=?",
                           (source_id,))
                db.execute("UPDATE raw_captures SET processing_state='parsed' WHERE source_id=? AND processing_state='pending'",
                           (source_id,))
                db.execute(
                    """INSERT OR IGNORE INTO message_analysis(message_id,state,listing_id,analyzed_at)
                       SELECT id,'reviewed',NULL,? FROM messages WHERE source_id=?""",
                    (stamp, source_id),
                )

                # Do not let previously queued cards from any of the five old
                # groups publish just because automatic posting is now enabled.
                db.execute(
                    """UPDATE listing_automation
                       SET state='review',decision='review',reason='Создано до перехода на новые объявления GINSENG',
                           retry_after=0,updated_at=?
                       WHERE state IN ('pending','running','decided')""",
                    (stamp,),
                )
                db.execute(
                    "UPDATE outbox SET state='done',last_error='Старая очередь публикаций закрыта при переходе на GINSENG' WHERE action='publish_listing' AND state='pending'"
                )

                # Retire the old five-group seed flow and start the new-message
                # baseline without deleting listings, publications, or media.
                db.execute("DELETE FROM settings WHERE key IN ('zalo_seed_only_mode','zalo_latest_group_seed_scan_v1','zalo_latest_group_seed_stage_v1','zalo_latest_group_seed_scan_error_v1','waiting_photos')")
                for key, value in (
                    ('zalo_latest_group_baseline_v1', 'true'),
                    ('zalo_latest_group_reseed_after_clear_v1', 'true'),
                    ('zalo_latest_group_source_split_v1', 'true'),
                    ('zalo_new_messages_baseline', json.dumps(stamp)),
                    ('zalo_baseline_epoch_ms', json.dumps(epoch_ms)),
                    ('telegram_posting_enabled', 'true'),
                    ('zalo_ginseng_only_workflow_v1', 'true'),
                ):
                    db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", (key, value))
                self.event(db, "maintenance", "Переключено на новые объявления только из группы GINSENG; автопубликация в Telegram включена.")
            db.execute("INSERT OR IGNORE INTO jobs(source_id,kind,detail,updated_at) SELECT id,'bind_source','Агент найдёт чат по названию',? FROM sources WHERE enabled=1 AND external_ref IS NULL", (now(),))
        self.path.chmod(0o600)
        if self._latest_group_reseed_is_needed():
            # Preserve the old archive before clearing evidence that cannot be
            # trusted to belong to its recorded Zalo source.
            self.backup()
        self._prepare_latest_group_baseline()
        self._resume_latest_group_seed_scan()
        self._split_latest_group_seed_cards_by_source()

    def _latest_group_reseed_is_needed(self):
        with self.connect() as db:
            baseline_done = db.execute(
                "SELECT 1 FROM settings WHERE key='zalo_latest_group_baseline_v1'"
            ).fetchone()
            reseed_done = db.execute(
                "SELECT 1 FROM settings WHERE key='zalo_latest_group_reseed_after_clear_v1'"
            ).fetchone()
            prior_seed_row = db.execute(
                "SELECT value FROM settings WHERE key='zalo_latest_group_seed'"
            ).fetchone()
            try:
                prior_seed = json.loads(prior_seed_row[0]) if prior_seed_row else {}
            except (TypeError, ValueError):
                prior_seed = {}
            return bool(
                baseline_done and not reseed_done and prior_seed.get("completed_at")
                and db.execute("SELECT count(*) FROM listings").fetchone()[0] == 0
            )

    def _resume_latest_group_seed_scan(self):
        """Allow one corrected retry for sources failed by the previous chat clicker."""
        with self.connect() as db:
            if not db.execute(
                "SELECT 1 FROM settings WHERE key='zalo_latest_group_seed_scan_v1'"
            ).fetchone():
                return
            marker = "zalo_latest_group_chat_binding_retry_v17"
            if db.execute("SELECT 1 FROM settings WHERE key=?", (marker,)).fetchone():
                return
            retried = False
            for source in db.execute(
                "SELECT id,external_ref,archive_state FROM sources WHERE enabled=1 AND kind='group' ORDER BY id"
            ):
                if source["archive_state"] == "complete":
                    continue
                failed = db.execute(
                    "SELECT 1 FROM jobs WHERE source_id=? AND kind IN ('collect_initial_scan','collect_archive') AND state='failed' LIMIT 1",
                    (source["id"],),
                ).fetchone()
                if not failed:
                    continue
                db.execute(
                    "UPDATE jobs SET state='done',detail='Повтор запущен после исправления выбора чата',updated_at=? WHERE source_id=? AND kind IN ('collect_initial_scan','collect_archive') AND state IN ('pending','failed','blocked')",
                    (now(), source["id"]),
                )
                db.execute(
                    "DELETE FROM checkpoints WHERE source_id=? AND mode IN ('collect_initial_scan','collect_archive','collect_incremental')",
                    (source["id"],),
                )
                db.execute(
                    "UPDATE sources SET archive_state='not_started',bootstrap_state='scanning' WHERE id=?",
                    (source["id"],),
                )
                kind = "bind_source" if not source["external_ref"] else "collect_initial_scan"
                db.execute("INSERT OR IGNORE INTO jobs(source_id,kind,detail,updated_at) VALUES (?,?,?,?)",
                           (source["id"], kind, "Повтор после исправления выбора точного чата", now()))
                self.event(db, "maintenance", f"Повторно выбираю чат Zalo для источника #{source['id']}; старые не подтверждённые снимки не учитываются.")
                retried = True
            if retried:
                db.execute("INSERT INTO settings(key,value) VALUES (?,'true')", (marker,))

    def _split_latest_group_seed_cards_by_source(self, db=None):
        """Keep each selected group's baseline card independent from exact-text copies."""
        if db is None:
            with self.connect() as connection:
                return self._split_latest_group_seed_cards_by_source(connection)

        marker = "zalo_latest_group_source_split_v1"
        if db.execute("SELECT 1 FROM settings WHERE key=?", (marker,)).fetchone():
            return False
        seed_row = db.execute("SELECT value FROM settings WHERE key='zalo_latest_group_seed'").fetchone()
        seed = json.loads(seed_row[0]) if seed_row else {}
        sources = {
            int(source_id): [int(value) for value in item.get("evidence_message_ids", [])]
            for source_id, item in seed.get("sources", {}).items()
            if item.get("evidence_message_ids")
        }
        if not seed.get("completed_at") or len(sources) < 2:
            return False

        source_listings = {}
        for source_id, message_ids in sources.items():
            marks = ",".join("?" for _ in message_ids)
            source_listings[source_id] = {
                int(row[0]) for row in db.execute(
                    f"""SELECT DISTINCT lm.listing_id FROM listing_messages lm
                        JOIN messages m ON m.id=lm.message_id
                        WHERE m.source_id=? AND m.id IN ({marks})""",
                    (source_id, *message_ids),
                )
            }

        support = {}
        for source_id, listing_ids in source_listings.items():
            for listing_id in listing_ids:
                support.setdefault(listing_id, set()).add(source_id)
        shared = {listing_id: source_ids for listing_id, source_ids in support.items()
                  if len(source_ids) > 1}
        if not shared:
            # If a prior attempt or future import already separated every group,
            # remember that this migration has nothing left to change.
            assigned = [next(iter(ids)) for ids in source_listings.values() if len(ids) == 1]
            if len(assigned) == len(sources) and len(set(assigned)) == len(sources):
                db.execute("INSERT INTO settings(key,value) VALUES (?, 'true')", (marker,))
                return False
            return False

        base_listing_id = max(shared, key=lambda listing_id: (len(shared[listing_id]), -listing_id))
        base = db.execute("SELECT * FROM listings WHERE id=?", (base_listing_id,)).fetchone()
        if base is None:
            return False
        base = dict(base)
        base_automation_row = db.execute(
            "SELECT * FROM listing_automation WHERE listing_id=?", (base_listing_id,)
        ).fetchone()
        base_automation = dict(base_automation_row) if base_automation_row else None
        base_reviews = [dict(row) for row in db.execute(
            "SELECT * FROM reviews WHERE listing_id=? ORDER BY id", (base_listing_id,)
        )]
        base_review_automation = {}
        for review in base_reviews:
            row = db.execute("SELECT * FROM review_automation WHERE review_id=?", (review["id"],)).fetchone()
            if row:
                base_review_automation[review["id"]] = dict(row)

        owners = sorted(shared[base_listing_id])
        owner_source = owners[0]
        listing_columns = [row[1] for row in db.execute("PRAGMA table_info(listings)") if row[1] != "id"]
        automation_columns = [row[1] for row in db.execute("PRAGMA table_info(listing_automation)")
                              if row[1] != "listing_id"]
        review_automation_columns = [row[1] for row in db.execute("PRAGMA table_info(review_automation)")
                                     if row[1] != "review_id"]
        now_value = now()
        split_count = 0

        for source_id in owners:
            if source_id == owner_source:
                destination_listing_id = base_listing_id
            else:
                copied_listing = {column: base[column] for column in listing_columns}
                copied_listing["updated_at"] = now_value
                placeholders = ",".join("?" for _ in listing_columns)
                destination_listing_id = db.execute(
                    f"INSERT INTO listings({','.join(listing_columns)}) VALUES ({placeholders})",
                    [copied_listing[column] for column in listing_columns],
                ).lastrowid
                if base_automation:
                    copied_automation = {column: base_automation[column] for column in automation_columns}
                    placeholders = ",".join("?" for _ in range(len(automation_columns) + 1))
                    columns = ["listing_id", *automation_columns]
                    db.execute(
                        f"INSERT INTO listing_automation({','.join(columns)}) VALUES ({placeholders})",
                        [destination_listing_id, *[copied_automation[column] for column in automation_columns]],
                    )

            message_ids = sources[source_id]
            for message_id in message_ids:
                confirmed_row = db.execute(
                    "SELECT confirmed FROM listing_messages WHERE listing_id=? AND message_id=?",
                    (base_listing_id, message_id),
                ).fetchone()
                confirmed = int(confirmed_row[0]) if confirmed_row else 0
                db.execute(
                    "INSERT OR IGNORE INTO listing_messages(listing_id,message_id,confirmed) VALUES (?,?,?)",
                    (destination_listing_id, message_id, confirmed),
                )
                if destination_listing_id != base_listing_id:
                    db.execute("DELETE FROM listing_messages WHERE listing_id=? AND message_id=?",
                               (base_listing_id, message_id))
                db.execute("UPDATE message_analysis SET listing_id=? WHERE message_id=?",
                           (destination_listing_id, message_id))

            selected_ids = set(message_ids)
            for review in base_reviews:
                try:
                    review_ids = [int(value) for value in json.loads(review.get("evidence", "[]"))]
                except (TypeError, ValueError):
                    review_ids = []
                filtered = [value for value in review_ids if value in selected_ids]
                if not filtered:
                    continue
                if destination_listing_id == base_listing_id:
                    db.execute("UPDATE reviews SET evidence=? WHERE id=?",
                               (json.dumps(filtered), review["id"]))
                    continue
                new_review_id = db.execute(
                    "INSERT INTO reviews(listing_id,reason,evidence,state,created_at) VALUES (?,?,?,?,?)",
                    (destination_listing_id, review["reason"], json.dumps(filtered),
                     review["state"], review["created_at"]),
                ).lastrowid
                source_automation = base_review_automation.get(review["id"])
                if source_automation:
                    values = [source_automation[column] for column in review_automation_columns]
                    placeholders = ",".join("?" for _ in range(len(review_automation_columns) + 1))
                    columns = ["review_id", *review_automation_columns]
                    db.execute(
                        f"INSERT INTO review_automation({','.join(columns)}) VALUES ({placeholders})",
                        [new_review_id, *values],
                    )
            if destination_listing_id != base_listing_id:
                split_count += 1

        db.execute("INSERT INTO settings(key,value) VALUES (?, 'true')", (marker,))
        self.event(db, "maintenance",
                   f"Базовые карточки разделены по группам Zalo: создано отдельных карточек {split_count + 1}.")
        return bool(split_count)

    @staticmethod
    def _message_timestamp(value):
        try:
            parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).timestamp()
        except (TypeError, ValueError):
            return float("inf")

    @classmethod
    def _looks_like_rental_ad(cls, text):
        value = " ".join(str(text or "").lower().split())
        if len(value) < 40:
            return False
        subject = any(term in value for term in (
            "cho thuê", "cho thue", "căn hộ", "căn studio", "studio", "nhà nguyên căn",
            "apartment", "for rent", "renting",
        ))
        price = bool(re.search(r"\d[\d.,]*\s*(?:triệu|tr|vnd|vnđ|đ|dong|million)", value))
        monthly = "tháng" in value or "/month" in value or "per month" in value
        unavailable_only = any(term in value for term in ("@all hết", "đã thuê", "da thue", "hết rồi")) and not subject
        return subject and (price or monthly) and not unavailable_only

    @classmethod
    def _photo_first_evidence(cls, messages, ad_message):
        ordered = sorted(messages, key=lambda row: (cls._message_timestamp(row["sent_at"]), row["id"]))
        index = next((i for i, row in enumerate(ordered) if row["id"] == ad_message["id"]), None)
        if index is None:
            return [ad_message["id"]]
        evidence = [ad_message["id"]]
        ad_time = cls._message_timestamp(ad_message["sent_at"])
        cursor = index - 1
        while cursor >= 0:
            previous = ordered[cursor]
            previous_text = str(previous["text"] or "").strip()
            if previous_text:
                # Emoji-only reactions may sit between the album and caption;
                # any descriptive text marks the previous listing boundary.
                if len(previous_text) <= 8 and all(
                    unicodedata.category(char)[0] in ("S", "P", "Z")
                    for char in previous_text
                ):
                    cursor -= 1
                    continue
                break
            delta = ad_time - cls._message_timestamp(previous["sent_at"])
            if delta < 0 or delta > 10 * 60:
                break
            if previous.get("has_media"):
                evidence.append(previous["id"])
            elif not previous.get("has_media"):
                break
            cursor -= 1
        return list(reversed(evidence))

    def _prepare_latest_group_baseline(self):
        """Queue one latest photo-backed ad per enabled Zalo group as a one-off baseline."""
        with self.connect() as db:
            if db.execute(
                "SELECT 1 FROM settings WHERE key='zalo_latest_group_seed_scan_v1'"
            ).fetchone():
                return
            baseline_done = db.execute(
                "SELECT 1 FROM settings WHERE key='zalo_latest_group_baseline_v1'"
            ).fetchone()
            reseed_done = db.execute(
                "SELECT 1 FROM settings WHERE key='zalo_latest_group_reseed_after_clear_v1'"
            ).fetchone()
            prior_seed_row = db.execute(
                "SELECT value FROM settings WHERE key='zalo_latest_group_seed'"
            ).fetchone()
            prior_seed = json.loads(prior_seed_row[0]) if prior_seed_row else {}
            is_cleared_completed_baseline = bool(
                baseline_done and not reseed_done and prior_seed.get("completed_at")
                and db.execute("SELECT count(*) FROM listings").fetchone()[0] == 0
            )
            if baseline_done and not is_cleared_completed_baseline:
                return
            if is_cleared_completed_baseline:
                # The remaining history has no verified active-chat identity.
                # Re-read each enabled group instead of reusing possibly
                # misattributed messages from the prior collector.
                sources = db.execute(
                    "SELECT id FROM sources WHERE enabled=1 AND kind='group' ORDER BY id"
                ).fetchall()
                source_ids = [int(row[0]) for row in sources]
                if not source_ids:
                    self.event(db, "maintenance", "Не найдены активные группы Zalo для новой точки отсчёта.")
                    return
                marks = ",".join("?" for _ in source_ids)
                message_ids = [row[0] for row in db.execute(
                    f"SELECT id FROM messages WHERE source_id IN ({marks})", source_ids
                )]
                if message_ids:
                    message_marks = ",".join("?" for _ in message_ids)
                    db.execute(f"DELETE FROM media WHERE message_id IN ({message_marks})", message_ids)
                    db.execute(f"DELETE FROM message_versions WHERE message_id IN ({message_marks})", message_ids)
                    db.execute(f"DELETE FROM message_analysis WHERE message_id IN ({message_marks})", message_ids)
                    db.execute(f"DELETE FROM messages WHERE id IN ({message_marks})", message_ids)
                raw_ids = [row[0] for row in db.execute(
                    f"SELECT id FROM raw_captures WHERE source_id IN ({marks})", source_ids
                )]
                if raw_ids:
                    raw_marks = ",".join("?" for _ in raw_ids)
                    db.execute(f"DELETE FROM raw_media WHERE capture_id IN ({raw_marks})", raw_ids)
                    db.execute(f"DELETE FROM raw_captures WHERE id IN ({raw_marks})", raw_ids)
                db.execute(f"DELETE FROM checkpoints WHERE source_id IN ({marks})", source_ids)
                db.execute(f"DELETE FROM jobs WHERE source_id IN ({marks})", source_ids)
                db.execute(
                    f"""UPDATE jobs SET state='done',detail='Пропущено: первичная точка строится только по пяти активным группам',updated_at=?
                        WHERE source_id NOT IN ({marks})
                          AND kind IN ('bind_source','collect_archive','collect_initial_scan','collect_incremental','interpret_raw','build_listings')
                          AND state IN ('pending','blocked')""",
                    (now(), *source_ids),
                )
                db.execute(
                    f"UPDATE sources SET archive_state='not_started',bootstrap_state='scanning' WHERE id IN ({marks})",
                    source_ids,
                )
                db.execute("DELETE FROM settings WHERE key IN ('zalo_latest_group_baseline_v1','zalo_latest_group_seed','zalo_seed_only_mode','zalo_new_messages_baseline','zalo_baseline_epoch_ms')")
                db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('zalo_latest_group_reseed_after_clear_v1','true')")
                db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('zalo_latest_group_seed_scan_v1','true')")
                self.event(db, "maintenance", f"Старая неподтверждённая история сброшена; заново сканирую последние 24 часа только в {len(source_ids)} активных группах.")
                return
            sources = db.execute(
                "SELECT id,name FROM sources WHERE enabled=1 AND kind='group' ORDER BY id").fetchall()
            selected = {}
            current_time = now()
            for source in sources:
                rows = [dict(row) for row in db.execute(
                    """SELECT m.*,
                              EXISTS(SELECT 1 FROM media md WHERE md.message_id=m.id) AS has_media
                       FROM messages m WHERE m.source_id=? ORDER BY m.id""", (source["id"],))]
                ads = [row for row in rows if self._looks_like_rental_ad(row["text"])]
                if not ads:
                    continue
                latest = max(ads, key=lambda row: (self._message_timestamp(row["sent_at"]), row["id"]))
                evidence = self._photo_first_evidence(rows, latest)
                if not any(row["id"] in evidence and row["has_media"] for row in rows):
                    continue
                selected[int(source["id"])] = {
                    "name": source["name"], "ad_message_id": int(latest["id"]),
                    "evidence_message_ids": [int(value) for value in evidence],
                    "external_id": latest["external_id"], "sent_at": latest["sent_at"],
                }

            if not selected:
                # Preserve a clear diagnostic and let a later app launch retry.
                self.event(db, "maintenance", "Не удалось выбрать фотообъявления для точки отсчёта.")
                return

            source_ids = list(selected)
            marks = ",".join("?" for _ in source_ids)
            # Clear only unposted cards derived from these active groups. Keep
            # message history, image files, and any publication records intact.
            listing_ids = [row[0] for row in db.execute(
                f"""SELECT DISTINCT lm.listing_id FROM listing_messages lm
                    JOIN messages m ON m.id=lm.message_id
                    WHERE m.source_id IN ({marks})
                      AND NOT EXISTS (SELECT 1 FROM publications p
                                      WHERE p.listing_id=lm.listing_id AND p.state='sent')""",
                source_ids)]
            if listing_ids:
                listing_marks = ",".join("?" for _ in listing_ids)
                review_ids = [row[0] for row in db.execute(
                    f"SELECT id FROM reviews WHERE listing_id IN ({listing_marks})", listing_ids)]
                if review_ids:
                    review_marks = ",".join("?" for _ in review_ids)
                    db.execute(f"DELETE FROM review_automation WHERE review_id IN ({review_marks})", review_ids)
                    db.execute(f"DELETE FROM reviews WHERE id IN ({review_marks})", review_ids)
                publication_ids = [row[0] for row in db.execute(
                    f"SELECT id FROM publications WHERE listing_id IN ({listing_marks})", listing_ids)]
                if publication_ids:
                    publication_marks = ",".join("?" for _ in publication_ids)
                    db.execute(f"DELETE FROM outbox WHERE publication_id IN ({publication_marks})", publication_ids)
                    db.execute(f"DELETE FROM publications WHERE id IN ({publication_marks})", publication_ids)
                db.execute(f"UPDATE message_analysis SET listing_id=NULL WHERE listing_id IN ({listing_marks})", listing_ids)
                for table in ("listing_automation", "offers", "listing_messages"):
                    db.execute(f"DELETE FROM {table} WHERE listing_id IN ({listing_marks})", listing_ids)
                db.execute(f"DELETE FROM listings WHERE id IN ({listing_marks})", listing_ids)

            db.execute(
                f"""INSERT OR REPLACE INTO message_analysis(message_id,state,listing_id,analyzed_at)
                    SELECT id,'reviewed',NULL,? FROM messages WHERE source_id IN ({marks})""",
                (current_time, *source_ids))
            evidence_ids = [message_id for item in selected.values()
                            for message_id in item["evidence_message_ids"]]
            if evidence_ids:
                evidence_marks = ",".join("?" for _ in evidence_ids)
                db.execute(f"DELETE FROM message_analysis WHERE message_id IN ({evidence_marks})", evidence_ids)

            # Do not parse any wider history in this one-off run. Incremental
            # Zalo cursors stay saved and will resume after the user relaunches.
            db.execute(f"UPDATE raw_captures SET processing_state='parsed' WHERE source_id IN ({marks})", source_ids)
            db.execute(
                f"""DELETE FROM jobs WHERE source_id IN ({marks}) AND kind IN
                    ('collect_archive','collect_initial_scan','collect_incremental','interpret_raw','build_listings')""",
                source_ids)
            for source_id in selected:
                db.execute("UPDATE sources SET archive_state='complete',bootstrap_state='reconciled' WHERE id=?", (source_id,))
                db.execute("INSERT OR IGNORE INTO jobs(source_id,kind,detail,updated_at) VALUES (?,'build_listings','Последнее объявление для точки отсчёта',?)",
                           (source_id, current_time))
            db.execute("DELETE FROM settings WHERE key IN ('zalo_new_messages_baseline','zalo_baseline_epoch_ms')")
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('zalo_latest_group_seed',?)",
                       (json.dumps(selected, ensure_ascii=False),))
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('zalo_seed_only_mode','true')")
            db.execute("INSERT INTO settings(key,value) VALUES ('zalo_latest_group_baseline_v1','true')")
            self.event(db, "maintenance", f"Подготовлена точка отсчёта: выбрано {len(selected)} последних фотообъявлений из пяти активных групп.")

    @contextmanager
    def connect(self):
        # Several worker and UI threads share this local database. Give SQLite
        # time to wait out another writer instead of failing the caller after
        # a short burst of concurrent writes.
        db = sqlite3.connect(self.path, timeout=60)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def event(db, kind, text):
        db.execute("INSERT INTO events(at,kind,text) VALUES (?,?,?)", (now(), kind, text))

    def recover(self):
        with self.connect() as db:
            count = db.execute("UPDATE jobs SET state='pending',detail='Возобновлено после закрытия',updated_at=? WHERE state='running'", (now(),)).rowcount
            db.execute("UPDATE listing_automation SET state=CASE WHEN decision='' THEN 'pending' ELSE 'decided' END,updated_at=? WHERE state='running'", (now(),))
            db.execute("UPDATE review_automation SET state='pending',updated_at=? WHERE state='running'", (now(),))
            db.execute("UPDATE outbox SET state='pending' WHERE state='running'")
            self.event(db, "startup", "Приложение запущено. Автоматический сбор активен." + (f" Восстановлено заданий: {count}." if count else ""))

    def backup(self):
        directory = self.data_dir / "backups"
        directory.mkdir(exist_ok=True, mode=0o700)
        destination = directory / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".sqlite3")
        with self.connect() as source:
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()
        destination.chmod(0o600)
        for old in sorted(directory.glob("*.sqlite3"))[:-7]:
            old.unlink()

    def snapshot(self):
        with self.connect() as db:
            rows = lambda sql: [dict(row) for row in db.execute(sql)]
            listings = rows("SELECT * FROM listings ORDER BY updated_at DESC,id DESC")
            for listing in listings:
                automation = db.execute("SELECT state,decision,reason,model,last_error FROM listing_automation WHERE listing_id=?",
                                        (listing["id"],)).fetchone()
                listing["automation"] = dict(automation) if automation else None
                publication = db.execute(
                    "SELECT state FROM publications WHERE listing_id=? ORDER BY id DESC LIMIT 1",
                    (listing["id"],)).fetchone()
                listing["publication_state"] = publication["state"] if publication else ""
                listing["media"] = [dict(row) for row in db.execute(
                    """SELECT md.id,md.kind FROM media md
                       JOIN listing_messages lm ON lm.message_id=md.message_id
                       WHERE lm.listing_id=? AND md.download_state='done' AND md.local_path IS NOT NULL
                       ORDER BY md.message_id,md.id LIMIT 12""", (listing["id"],))]
                listing["sources"] = [dict(row) for row in db.execute(
                    """SELECT DISTINCT s.id,s.name,s.kind FROM sources s
                       JOIN messages m ON m.source_id=s.id
                       JOIN listing_messages lm ON lm.message_id=m.id
                       WHERE lm.listing_id=? ORDER BY s.name""", (listing["id"],))]
                listing["source_messages"] = [dict(row) for row in db.execute(
                    """SELECT m.id,m.sender,m.sent_at,m.text,s.name AS source_name
                       FROM messages m
                       JOIN sources s ON s.id=m.source_id
                       JOIN listing_messages lm ON lm.message_id=m.id
                       WHERE lm.listing_id=? AND trim(m.text)<>''
                       ORDER BY m.sent_at,m.id LIMIT 12""", (listing["id"],))]
            return {
                "settings": {r["key"]: json.loads(r["value"]) for r in db.execute("SELECT * FROM settings")},
                "listings": listings,
                "sources": rows("SELECT s.*, (SELECT count(*) FROM messages m WHERE m.source_id=s.id) AS messages, (SELECT count(*) FROM raw_captures r WHERE r.source_id=s.id) AS raw_captures FROM sources s ORDER BY id"),
                "reviews": rows("SELECT * FROM reviews WHERE state='open' ORDER BY id DESC"),
                "jobs": rows("SELECT j.*,s.name AS source_name FROM jobs j LEFT JOIN sources s ON s.id=j.source_id ORDER BY j.id DESC LIMIT 100"),
                "events": rows("SELECT * FROM events ORDER BY id DESC LIMIT 80"),
                "counts": {"messages": db.execute("SELECT count(*) FROM messages").fetchone()[0],
                           "media": db.execute("SELECT count(*) FROM media").fetchone()[0],
                           "raw_captures": db.execute("SELECT count(*) FROM raw_captures").fetchone()[0],
                           "raw_media": db.execute("SELECT count(*) FROM raw_media").fetchone()[0],
                           "publications": db.execute("SELECT count(*) FROM publications").fetchone()[0]},
            }

    def get_source(self, source_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
            return dict(row) if row else None

    def get_media_path(self, media_id):
        with self.connect() as db:
            row = db.execute("SELECT local_path,kind FROM media WHERE id=? AND download_state='done'", (media_id,)).fetchone()
            return dict(row) if row else None

    def get_listing_for_publication(self, listing_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
            if row is None:
                raise ValueError("Объект не найден.")
            listing = dict(row)
            listing["sources"] = [item[0] for item in db.execute(
                """SELECT DISTINCT s.name FROM sources s JOIN messages m ON m.source_id=s.id
                   JOIN listing_messages lm ON lm.message_id=m.id WHERE lm.listing_id=? ORDER BY s.name""",
                (listing_id,))]
            listing["media_paths"] = [item[0] for item in db.execute(
                """SELECT md.local_path FROM media md JOIN listing_messages lm ON lm.message_id=md.message_id
                   WHERE lm.listing_id=? AND md.kind<>'video' AND md.download_state='done'
                   AND md.local_path IS NOT NULL
                   ORDER BY md.message_id, md.id LIMIT 10""", (listing_id,))]
            listing["source_messages"] = [dict(item) for item in db.execute(
                """SELECT m.sender,m.sent_at,m.text FROM messages m
                   JOIN listing_messages lm ON lm.message_id=m.id
                   WHERE lm.listing_id=? AND trim(m.text)<>'' ORDER BY m.sent_at,m.id""", (listing_id,))]
            return listing

    def get_publication(self, listing_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM publications WHERE listing_id=? AND state='sent' ORDER BY id DESC LIMIT 1", (listing_id,)).fetchone()
            if row is None:
                raise ValueError("У объекта нет опубликованного Telegram-поста.")
            result = dict(row)
            result["message_ids"] = json.loads(result["message_ids"])
            return result

    def update_publication_body(self, publication_id, listing_id, body):
        with self.connect() as db:
            revision = db.execute("SELECT revision FROM listings WHERE id=?", (listing_id,)).fetchone()
            if revision is None:
                raise ValueError("Объект не найден.")
            db.execute("UPDATE publications SET body=?,last_error='',applied_revision=? WHERE id=? AND listing_id=?",
                       (body, revision[0], publication_id, listing_id))
            self.event(db, "telegram", f"NT-{listing_id:05d}: опубликованный пост обновлён.")

    def replace_publication(self, listing_id, channel_id, message_ids, body):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            listing = db.execute("SELECT revision,status FROM listings WHERE id=?", (listing_id,)).fetchone()
            if listing is None:
                raise ValueError("Объект не найден.")
            if listing["status"] != "available":
                raise ValueError("Повторно публиковать можно только доступный объект.")
            publication = db.execute(
                "SELECT id FROM publications WHERE listing_id=? AND channel_id=? ORDER BY id DESC LIMIT 1",
                (listing_id, channel_id)).fetchone()
            if publication is None:
                db.execute(
                    """INSERT INTO publications(listing_id,channel_id,message_ids,applied_revision,state,body,last_error)
                       VALUES (?,?,?,?, 'sent',?,'')""",
                    (listing_id, channel_id, json.dumps(message_ids), listing["revision"], body))
            else:
                db.execute("UPDATE publications SET message_ids=?,body=?,applied_revision=?,state='sent',last_error='' WHERE id=?",
                           (json.dumps(message_ids), body, listing["revision"], publication["id"]))
            self.event(db, "telegram", f"NT-{listing_id:05d}: объект повторно опубликован в Telegram.")

    def reserve_publication(self, listing_id, channel_id, expected_revision):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            listing = db.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
            if listing is None:
                raise ValueError("Объект не найден.")
            if listing["revision"] != expected_revision:
                raise Conflict("Объект уже изменён. Обновите список и повторите действие.")
            if listing["status"] != "unverified":
                raise ValueError("Публикация доступна при первом подтверждении объекта.")
            publication = db.execute("SELECT * FROM publications WHERE listing_id=? AND channel_id=?", (listing_id, channel_id)).fetchone()
            if publication and publication["state"] == "sent":
                return {"id": publication["id"], "already_sent": True}
            if publication and publication["state"] == "publishing":
                raise Conflict("Объект уже публикуется.")
            if publication:
                publication_id = publication["id"]
                db.execute("UPDATE publications SET state='publishing',last_error='' WHERE id=?", (publication_id,))
            else:
                publication_id = db.execute(
                    "INSERT INTO publications(listing_id,channel_id,message_ids,state) VALUES (?,?,'[]','publishing')",
                    (listing_id, channel_id)).lastrowid
            self.event(db, "telegram", f"NT-{listing_id:05d}: начата подготовка публикации.")
            return {"id": publication_id, "already_sent": False}

    def fail_publication(self, publication_id, error):
        with self.connect() as db:
            db.execute("UPDATE publications SET state='failed',last_error=? WHERE id=? AND state='publishing'",
                       (str(error)[:1000], publication_id))
            self.event(db, "telegram", f"Публикация #{publication_id} не завершена: {str(error)[:300]}")

    def complete_publication(self, publication_id, listing_id, expected_revision, message_ids, body):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            listing = db.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
            if listing is None or listing["revision"] != expected_revision:
                db.execute("UPDATE publications SET state='sent',message_ids=?,body=?,last_error='Объект изменился во время отправки' WHERE id=?",
                           (json.dumps(message_ids), body, publication_id))
                raise Conflict("Пост отправлен, но объект успел измениться. Проверьте его в канале.")
            revision = expected_revision + 1
            db.execute("UPDATE listings SET status='available',status_reason='Опубликовано после ручного подтверждения',revision=?,updated_at=? WHERE id=?",
                       (revision, now(), listing_id))
            db.execute("UPDATE publications SET state='sent',message_ids=?,body=?,last_error='',applied_revision=? WHERE id=?",
                       (json.dumps(message_ids), body, revision, publication_id))
            self.event(db, "telegram", f"NT-{listing_id:05d} опубликован в Telegram и подтверждён как доступный.")

    def get_settings(self, *keys):
        with self.connect() as db:
            result = {}
            for row in db.execute("SELECT key,value FROM settings WHERE key IN (%s)" % ",".join("?" for _ in keys), keys):
                result[row["key"]] = json.loads(row["value"])
            return result

    def set_settings(self, **values):
        with self.connect() as db:
            for key, value in values.items():
                db.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    def telegram_posting_enabled(self):
        return bool(self.get_settings("telegram_posting_enabled").get("telegram_posting_enabled", False))

    def set_telegram_posting_enabled(self, enabled):
        if not isinstance(enabled, bool):
            raise ValueError("Укажите, включить или выключить публикации в Telegram.")
        self.set_settings(telegram_posting_enabled=enabled)
        with self.connect() as db:
            self.event(db, "telegram", "Публикации в Telegram включены." if enabled else
                       "Публикации в Telegram выключены; исходящая очередь приостановлена.")

    def add_source(self, name, kind):
        name = str(name).strip()
        if not name or len(name) > 200 or kind not in ("group", "personal"):
            raise ValueError("Укажите название источника и его тип.")
        with self.connect() as db:
            source_id = db.execute("INSERT INTO sources(name,kind,archive_state,bootstrap_state,created_at) VALUES (?,?, 'not_started','scanning',?)", (name, kind, now())).lastrowid
            db.execute("INSERT INTO jobs(source_id,kind,detail,updated_at) VALUES (?,'bind_source','Агент найдёт чат по названию',?)", (source_id, now()))
            self.event(db, "source", f"Добавлен источник «{name}». Агенту поставлена задача найти и привязать чат в Zalo.")
        return source_id

    def delete_sources(self, source_ids):
        """Delete multiple sources and their related evidence, jobs, and on-disk files."""
        ids = []
        for value in source_ids:
            try:
                sid = int(value)
                if sid > 0 and sid not in ids:
                    ids.append(sid)
            except (TypeError, ValueError):
                pass
        if not ids:
            raise ValueError("Выберите источники для удаления.")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in ids)
            existing = [row["id"] for row in db.execute(f"SELECT id, name FROM sources WHERE id IN ({placeholders})", ids)]
            if not existing:
                raise ValueError("Выбранные источники уже удалены.")
            marks = ",".join("?" for _ in existing)

            # Messages and their attachments
            msg_ids = [row[0] for row in db.execute(f"SELECT id FROM messages WHERE source_id IN ({marks})", existing)]
            if msg_ids:
                msg_marks = ",".join("?" for _ in msg_ids)
                db.execute(f"DELETE FROM listing_messages WHERE message_id IN ({msg_marks})", msg_ids)
                db.execute(f"DELETE FROM message_analysis WHERE message_id IN ({msg_marks})", msg_ids)
                db.execute(f"DELETE FROM media WHERE message_id IN ({msg_marks})", msg_ids)
                db.execute(f"DELETE FROM message_versions WHERE message_id IN ({msg_marks})", msg_ids)
                db.execute(f"DELETE FROM messages WHERE id IN ({msg_marks})", msg_ids)

            # Raw captures and their media
            cap_ids = [row[0] for row in db.execute(f"SELECT id FROM raw_captures WHERE source_id IN ({marks})", existing)]
            if cap_ids:
                cap_marks = ",".join("?" for _ in cap_ids)
                db.execute(f"DELETE FROM raw_media WHERE capture_id IN ({cap_marks})", cap_ids)
                db.execute(f"DELETE FROM raw_captures WHERE id IN ({cap_marks})", cap_ids)

            db.execute(f"DELETE FROM offers WHERE source_id IN ({marks})", existing)
            db.execute(f"DELETE FROM checkpoints WHERE source_id IN ({marks})", existing)
            db.execute(f"DELETE FROM jobs WHERE source_id IN ({marks})", existing)
            db.execute(f"DELETE FROM sources WHERE id IN ({marks})", existing)

            self.event(db, "source", f"Удалено источников Zalo: {len(existing)}.")

        for sid in existing:
            source_dir = self.data_dir / "raw-media" / str(sid)
            if source_dir.exists():
                shutil.rmtree(source_dir, ignore_errors=True)
        return len(existing)

    def commit_binding(self, source_id, external_ref, observed_name, observed_kind):
        external_ref, observed_name = str(external_ref).strip(), str(observed_name).strip()
        if not external_ref or not observed_name or observed_kind not in ("group", "personal"):
            raise ValueError("Агент не вернул устойчивую идентификацию чата.")
        with self.connect() as db:
            source = db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
            if source is None:
                raise ValueError("Источник не найден.")
            if source["kind"] != observed_kind:
                raise ValueError("Найден чат другого типа. Нужна проверка.")
            if db.execute("SELECT 1 FROM sources WHERE external_ref=? AND id<>?", (external_ref, source_id)).fetchone():
                raise ValueError("Этот чат уже привязан к другому источнику.")
            db.execute("UPDATE sources SET binding_state='bound',external_ref=?,observed_name=?,binding_detail='' WHERE id=?",
                       (external_ref, observed_name, source_id))
            self.event(db, "connection", f"Источник «{source['name']}» привязан агентом к чату Zalo «{observed_name}».")

    def add_listing(self, title, price="", description="", status="unverified"):
        title, price, description = str(title).strip(), str(price).strip(), str(description).strip()
        if not title or len(title) > 200 or len(price) > 150 or len(description) > 20000:
            raise ValueError("Проверьте название и длину полей объекта.")
        if status not in ("unverified", "available"):
            raise ValueError("Новый объект должен ожидать проверки или быть доступным.")
        with self.connect() as db:
            listing_id = db.execute("INSERT INTO listings(title,price,description,status,status_reason,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                                    (title, price, description, status, "Добавлено вручную", now(), now())).lastrowid
            if status == "available":
                db.execute("INSERT INTO listing_automation(listing_id,state,decision,reason,updated_at) VALUES (?,'decided','publish','Доступность подтверждена пользователем',?)",
                           (listing_id, now()))
            self.event(db, "listing", f"Объект NT-{listing_id:05d} добавлен вручную. В Telegram не опубликован.")
        return listing_id

    def delete_listings(self, listing_ids, *, clear_reviews=False):
        """Delete local property cards without deleting their Zalo evidence."""
        ids = []
        for value in listing_ids:
            try:
                listing_id = int(value)
            except (TypeError, ValueError):
                raise ValueError("Передан неверный идентификатор объекта.") from None
            if listing_id > 0 and listing_id not in ids:
                ids.append(listing_id)
        if not ids or len(ids) > 1000:
            raise ValueError("Выберите от 1 до 1000 объектов.")
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = [row[0] for row in db.execute(
                f"SELECT id FROM listings WHERE id IN ({placeholders})", ids)]
            if not existing:
                raise ValueError("Выбранные объекты уже удалены.")
            marks = ",".join("?" for _ in existing)
            message_ids = [row[0] for row in db.execute(
                f"SELECT message_id FROM listing_messages WHERE listing_id IN ({marks})", existing)]
            if message_ids:
                message_marks = ",".join("?" for _ in message_ids)
                db.execute(
                    f"UPDATE message_analysis SET state='reviewed',listing_id=NULL,analyzed_at=? WHERE message_id IN ({message_marks})",
                    (now(), *message_ids),
                )
            publication_ids = [row[0] for row in db.execute(
                f"SELECT id FROM publications WHERE listing_id IN ({marks})", existing)]
            if publication_ids:
                publication_marks = ",".join("?" for _ in publication_ids)
                db.execute(f"DELETE FROM outbox WHERE publication_id IN ({publication_marks})", publication_ids)
            review_ids = [row[0] for row in db.execute(
                f"SELECT id FROM reviews WHERE listing_id IN ({marks})", existing)]
            if clear_reviews:
                review_ids = [row[0] for row in db.execute("SELECT id FROM reviews")]
            if review_ids:
                review_marks = ",".join("?" for _ in review_ids)
                db.execute(f"DELETE FROM review_automation WHERE review_id IN ({review_marks})", review_ids)
                db.execute(f"DELETE FROM reviews WHERE id IN ({review_marks})", review_ids)
            for table in ("publications", "listing_automation", "offers", "listing_messages"):
                db.execute(f"DELETE FROM {table} WHERE listing_id IN ({marks})", existing)
            db.execute(f"DELETE FROM listings WHERE id IN ({marks})", existing)
            self.event(db, "listing", f"Удалено объектов: {len(existing)}. История Zalo и фотографии сохранены.")
        return len(existing)

    def clear_all_listings_and_reset_baseline(self):
        """Wipes all property cards, publications, outbox, reviews, and resets baseline so only new messages are processed."""
        current_time = now()
        current_epoch_ms = int(time.time() * 1000)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE message_analysis SET listing_id = NULL")
            for table in (
                "outbox", "publications", "listing_automation", "review_automation",
                "reviews", "offers", "listing_messages", "listings"
            ):
                db.execute(f"DELETE FROM {table}")
            db.execute("INSERT OR REPLACE INTO message_analysis(message_id, state, listing_id, analyzed_at) SELECT id, 'reviewed', NULL, ? FROM messages", (current_time,))
            db.execute("UPDATE raw_captures SET processing_state='parsed'")
            db.execute("DELETE FROM jobs WHERE kind IN ('interpret_raw', 'build_listings')")
            db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('zalo_new_messages_baseline', ?)", (json.dumps(current_time),))
            db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('zalo_baseline_epoch_ms', ?)", (json.dumps(current_epoch_ms),))
            db.execute("DELETE FROM settings WHERE key='waiting_photos'")
            self.event(db, "maintenance", "Все объявления удалены. Базовый уровень зафиксирован — бот будет обрабатывать только сообщения, отправленные с этого момента.")
        for sub in ("media", "raw-media"):
            target_dir = self.data_dir / sub
            if target_dir.exists():
                shutil.rmtree(target_dir, ignore_errors=True)
                target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    def clear_all_listings_and_media(self):
        """Wipes all property cards, messages, raw captures, reviews, publications, jobs, and on-disk photos."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for table in (
                "outbox", "publications", "listing_automation", "review_automation",
                "reviews", "offers", "listing_messages", "message_analysis", "listings",
                "media", "message_versions", "messages",
                "raw_media", "raw_captures", "checkpoints", "jobs", "events"
            ):
                db.execute(f"DELETE FROM {table}")
            self.event(db, "maintenance", "База объектов, сырой архив и фотографии полностью очищены.")
        for sub in ("media", "raw-media"):
            target_dir = self.data_dir / sub
            if target_dir.exists():
                shutil.rmtree(target_dir, ignore_errors=True)
                target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    def clear_raw_archive(self):
        """Wipes raw captures, raw media, and on-disk raw-media directory."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for table in ("raw_media", "raw_captures"):
                db.execute(f"DELETE FROM {table}")
            db.execute("DELETE FROM checkpoints WHERE mode IN ('collect_archive', 'collect_initial_scan', 'collect_incremental')")
            db.execute("DELETE FROM jobs WHERE kind IN ('collect_archive', 'collect_initial_scan', 'collect_incremental', 'interpret_raw')")
            db.execute("UPDATE sources SET archive_state='not_started',bootstrap_state='scanning'")
            self.event(db, "maintenance", "Сырой архив Zalo и сохранённые медиафайлы очищены.")
        target_dir = self.data_dir / "raw-media"
        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)
            target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    def set_listing_status(self, listing_id, status, expected_revision, *, origin="manual", evidence_id=None):
        if status not in ("unverified", "available", "rented"):
            raise ValueError("Неизвестный статус. Автоматического статуса по давности нет.")
        if origin not in ("manual", "confirmed_source", "agent"):
            raise ValueError("Статус меняется только вручную или по подтверждённому сообщению.")
        with self.connect() as db:
            # Lock before the read so concurrent status decisions cannot overwrite each other.
            db.execute("BEGIN IMMEDIATE")
            listing = db.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
            if listing is None:
                raise ValueError("Объект не найден.")
            if listing["revision"] != expected_revision:
                raise Conflict("Объект уже изменён. Обновите список и повторите действие.")
            if origin == "confirmed_source":
                evidence = db.execute("SELECT 1 FROM listing_messages WHERE listing_id=? AND message_id=? AND confirmed=1", (listing_id, evidence_id)).fetchone()
                if not evidence:
                    raise ValueError("Нет подтверждённой связи сообщения с этим объектом.")
            if listing["status"] == status:
                return
            reason = ("Отмечено пользователем" if origin == "manual" else
                      f"Подтверждённое сообщение #{evidence_id}" if origin == "confirmed_source" else
                      "Решение агента Claude по связанным сообщениям")
            revision = listing["revision"] + 1
            db.execute("UPDATE listings SET status=?,status_reason=?,revision=?,updated_at=? WHERE id=?", (status, reason, revision, now(), listing_id))
            for publication in db.execute("SELECT id FROM publications WHERE listing_id=?", (listing_id,)):
                db.execute("INSERT OR IGNORE INTO outbox(publication_id,revision,action,created_at) VALUES (?,?,'edit_listing',?)", (publication["id"], revision, now()))
            self.event(db, "status", f"NT-{listing_id:05d}: {listing['status']} → {status}. {reason}. Изменение сохранено локально.")

    def enqueue_collection(self):
        with self.connect() as db:
            if self.get_settings("zalo_seed_only_mode").get("zalo_seed_only_mode"):
                return
            reseed_scan = db.execute(
                "SELECT 1 FROM settings WHERE key='zalo_latest_group_seed_scan_v1'"
            ).fetchone()
            if reseed_scan:
                for source in db.execute(
                    "SELECT * FROM sources WHERE enabled=1 AND kind='group' ORDER BY id"
                ):
                    if source["archive_state"] == "complete":
                        continue
                    failed = db.execute(
                        "SELECT 1 FROM jobs WHERE source_id=? AND kind IN ('collect_initial_scan','collect_archive') AND state='failed' LIMIT 1",
                        (source["id"],),
                    ).fetchone()
                    if failed:
                        continue
                    kind = "bind_source" if not source["external_ref"] else "collect_initial_scan"
                    db.execute("INSERT OR IGNORE INTO jobs(source_id,kind,updated_at) VALUES (?,?,?)",
                               (source["id"], kind, now()))
                return
            for source in db.execute("SELECT * FROM sources WHERE enabled=1"):
                kind = "bind_source" if not source["external_ref"] else ("collect_incremental" if source["archive_state"] == "complete" else "collect_initial_scan")
                db.execute("INSERT OR IGNORE INTO jobs(source_id,kind,updated_at) VALUES (?,?,?)", (source["id"], kind, now()))

    def complete_latest_group_seed_if_ready(self):
        with self.connect() as db:
            reseed_scan = db.execute(
                "SELECT 1 FROM settings WHERE key='zalo_latest_group_seed_scan_v1'"
            ).fetchone()
            if reseed_scan:
                stage_row = db.execute(
                    "SELECT value FROM settings WHERE key='zalo_latest_group_seed_stage_v1'"
                ).fetchone()
                if not stage_row:
                    sources = [dict(row) for row in db.execute(
                        "SELECT id,name,archive_state FROM sources WHERE enabled=1 AND kind='group' ORDER BY id"
                    )]
                    if len(sources) != 5 or any(row["archive_state"] != "complete" for row in sources):
                        return False
                    source_ids = [row["id"] for row in sources]
                    marks = ",".join("?" for _ in source_ids)
                    unfinished_raw = db.execute(
                        f"SELECT 1 FROM raw_captures WHERE source_id IN ({marks}) AND processing_state<>'parsed' LIMIT 1",
                        source_ids,
                    ).fetchone()
                    active_parse = db.execute(
                        f"SELECT 1 FROM jobs WHERE source_id IN ({marks}) AND kind='interpret_raw' AND state IN ('pending','running','blocked') LIMIT 1",
                        source_ids,
                    ).fetchone()
                    if unfinished_raw or active_parse:
                        return False

                    selected = {}
                    missing = []
                    for source in sources:
                        rows = [dict(row) for row in db.execute(
                            """SELECT m.*,
                                      EXISTS(SELECT 1 FROM media md WHERE md.message_id=m.id
                                             AND md.download_state='done' AND md.local_path IS NOT NULL) AS has_media
                               FROM messages m WHERE m.source_id=? ORDER BY m.id""", (source["id"],)
                        )]
                        ads = sorted(
                            (row for row in rows if self._looks_like_rental_ad(row["text"])),
                            key=lambda row: (self._message_timestamp(row["sent_at"]), row["id"]),
                            reverse=True,
                        )
                        chosen = None
                        for ad in ads:
                            evidence = self._photo_first_evidence(rows, ad)
                            if any(row["id"] in evidence and row["has_media"] for row in rows):
                                chosen = (ad, evidence)
                                break
                        if not chosen:
                            missing.append(source["name"])
                            continue
                        ad, evidence = chosen
                        selected[int(source["id"])] = {
                            "name": source["name"], "ad_message_id": int(ad["id"]),
                            "evidence_message_ids": [int(value) for value in evidence],
                            "external_id": ad["external_id"], "sent_at": ad["sent_at"],
                        }

                    if missing:
                        diagnostic = "Не нашёл последнее фотообъявление в активных группах: " + ", ".join(missing)
                        prior = db.execute(
                            "SELECT value FROM settings WHERE key='zalo_latest_group_seed_scan_error_v1'"
                        ).fetchone()
                        if not prior or json.loads(prior[0]) != diagnostic:
                            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('zalo_latest_group_seed_scan_error_v1',?)",
                                       (json.dumps(diagnostic, ensure_ascii=False),))
                            self.event(db, "maintenance", diagnostic)
                        return False

                    current_time = now()
                    db.execute(
                        f"""INSERT OR REPLACE INTO message_analysis(message_id,state,listing_id,analyzed_at)
                            SELECT id,'reviewed',NULL,? FROM messages WHERE source_id IN ({marks})""",
                        (current_time, *source_ids),
                    )
                    for source_id, item in selected.items():
                        evidence_ids = item["evidence_message_ids"]
                        evidence_marks = ",".join("?" for _ in evidence_ids)
                        db.execute(f"DELETE FROM message_analysis WHERE message_id IN ({evidence_marks})", evidence_ids)
                        db.execute("INSERT OR IGNORE INTO jobs(source_id,kind,detail,updated_at) VALUES (?,'build_listings','Последнее фотообъявление для точки отсчёта',?)",
                                   (source_id, current_time))
                    db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('zalo_latest_group_seed',?)",
                               (json.dumps(selected, ensure_ascii=False),))
                    db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('zalo_latest_group_seed_stage_v1','prepared')")
                    db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('zalo_seed_only_mode','true')")
                    db.execute("DELETE FROM settings WHERE key='zalo_latest_group_seed_scan_error_v1'")
                    db.execute("DELETE FROM settings WHERE key='waiting_photos'")
                    self.event(db, "maintenance", "Из 24-часового скана выбраны только последние фотообъявления пяти активных групп; готовлю пять отдельных карточек.")
                    return False

            value = db.execute("SELECT value FROM settings WHERE key='zalo_seed_only_mode'").fetchone()
            if not value or not json.loads(value[0]):
                return False
            seed_row = db.execute("SELECT value FROM settings WHERE key='zalo_latest_group_seed'").fetchone()
            seed = json.loads(seed_row[0]) if seed_row else {}
            if not seed:
                return False
            for source_id, item in seed.items():
                message_ids = item.get("evidence_message_ids", [])
                if not message_ids:
                    return False
                marks = ",".join("?" for _ in message_ids)
                analyzed = db.execute(
                    f"SELECT count(*) FROM message_analysis WHERE message_id IN ({marks})", message_ids).fetchone()[0]
                if analyzed != len(set(message_ids)):
                    return False
                if db.execute(
                    """SELECT 1 FROM jobs WHERE source_id=? AND kind='build_listings'
                       AND state IN ('pending','running','blocked') LIMIT 1""", (int(source_id),)).fetchone():
                    return False
                card_rows = db.execute(
                    """SELECT 1 FROM listing_messages ad
                       JOIN listing_messages photo ON photo.listing_id=ad.listing_id
                       JOIN media md ON md.message_id=photo.message_id
                       WHERE ad.message_id=? AND md.download_state='done' AND md.local_path IS NOT NULL LIMIT 1""",
                    (int(item["ad_message_id"]),)).fetchone()
                if not card_rows:
                    return False
            if reseed_scan:
                card_ids = []
                for item in seed.values():
                    rows = db.execute(
                        "SELECT DISTINCT listing_id FROM listing_messages WHERE message_id=?",
                        (int(item["ad_message_id"]),),
                    ).fetchall()
                    if len(rows) != 1:
                        return False
                    card_ids.append(int(rows[0][0]))
                if len(card_ids) != 5 or len(set(card_ids)) != 5:
                    return False
            stamp = now()
            epoch_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            db.execute("DELETE FROM settings WHERE key='zalo_seed_only_mode'")
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('zalo_new_messages_baseline',?)",
                       (json.dumps(stamp),))
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('zalo_baseline_epoch_ms',?)",
                       (json.dumps(epoch_ms),))
            completed = {"completed_at": stamp, "sources": seed}
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('zalo_latest_group_seed',?)",
                       (json.dumps(completed, ensure_ascii=False),))
            self._split_latest_group_seed_cards_by_source(db)
            if reseed_scan:
                db.execute("DELETE FROM settings WHERE key IN ('zalo_latest_group_seed_scan_v1','zalo_latest_group_seed_stage_v1','zalo_latest_group_seed_scan_error_v1')")
            self.event(db, "maintenance", "Последние объявления пяти активных групп сохранены; курсоры зафиксированы, сбор приостановлен до следующего запуска.")
            return True

    def enqueue_interpretation(self):
        with self.connect() as db:
            scan = db.execute("SELECT 1 FROM settings WHERE key='zalo_latest_group_seed_scan_v1'").fetchone()
            query = "SELECT * FROM sources WHERE enabled=1 AND kind='group'" if scan else "SELECT * FROM sources WHERE enabled=1"
            for source in db.execute(query):
                pending = db.execute("SELECT 1 FROM raw_captures WHERE source_id=? AND processing_state='pending' LIMIT 1", (source["id"],)).fetchone()
                if pending:
                    db.execute("INSERT OR IGNORE INTO jobs(source_id,kind,detail,updated_at) VALUES (?,'interpret_raw','Разбор локального сырого архива',?)", (source["id"], now()))

    def enqueue_listing_analysis(self):
        with self.connect() as db:
            reseed_scan = db.execute(
                "SELECT 1 FROM settings WHERE key='zalo_latest_group_seed_scan_v1'"
            ).fetchone()
            if reseed_scan and not db.execute(
                "SELECT 1 FROM settings WHERE key='zalo_latest_group_seed_stage_v1' AND value='prepared'"
            ).fetchone():
                return
            baseline = self.get_settings("zalo_new_messages_baseline").get("zalo_new_messages_baseline")
            baseline_filter = "AND m.captured_at > ?" if baseline else ""
            if reseed_scan:
                seed_row = db.execute("SELECT value FROM settings WHERE key='zalo_latest_group_seed'").fetchone()
                seed = json.loads(seed_row[0]) if seed_row else {}
                source_ids = [int(value) for value in seed]
                if not source_ids:
                    return
                marks = ",".join("?" for _ in source_ids)
                source_rows = db.execute(f"SELECT * FROM sources WHERE enabled=1 AND kind='group' AND id IN ({marks})", source_ids)
            else:
                source_rows = db.execute("SELECT * FROM sources WHERE enabled=1")
            for source in source_rows:
                args = (source["id"], baseline) if baseline else (source["id"],)
                pending = db.execute(f"SELECT 1 FROM messages m LEFT JOIN message_analysis a ON a.message_id=m.id WHERE m.source_id=? AND a.message_id IS NULL {baseline_filter} LIMIT 1", args).fetchone()
                if pending:
                    db.execute("INSERT OR IGNORE INTO jobs(source_id,kind,detail,updated_at) VALUES (?,'build_listings','Сборка карточек объектов',?)", (source["id"], now()))

    def claim_job(self, *, only_kind=None, only_kinds=None, exclude_kind=None, photo_settle_seconds=0):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            where = ["state='pending'"]
            args = []
            if only_kind is not None:
                where.append("kind=?")
                args.append(only_kind)
            if only_kinds:
                where.append("kind IN (%s)" % ",".join("?" for _ in only_kinds))
                args.extend(only_kinds)
            if exclude_kind is not None:
                where.append("kind<>?")
                args.append(exclude_kind)
            if photo_settle_seconds > 0:
                # Zalo may split one multi-photo send into several messages and
                # expose the tail only on a later scan. Let an adjacent
                # text/photo pair settle before the listing model can publish a
                # partial album. Photo-only batches without a description still
                # reach the builder immediately and remain in the waiting queue.
                where.append("""(
                    kind<>'build_listings' OR NOT EXISTS (
                        SELECT 1 FROM messages text_msg
                        LEFT JOIN message_analysis text_analysis ON text_analysis.message_id=text_msg.id
                        WHERE text_msg.source_id=jobs.source_id AND trim(text_msg.text)<>''
                          AND (text_analysis.message_id IS NULL OR
                              (text_analysis.state='waiting_photos' AND text_analysis.listing_id IS NULL))
                          AND EXISTS (
                              SELECT 1 FROM messages photo_msg
                              LEFT JOIN message_analysis photo_analysis ON photo_analysis.message_id=photo_msg.id
                              WHERE photo_msg.source_id=jobs.source_id AND trim(photo_msg.text)=''
                                AND (photo_analysis.message_id IS NULL OR
                                     (photo_analysis.state='waiting_photos' AND photo_analysis.listing_id IS NULL))
                                AND EXISTS (SELECT 1 FROM media md WHERE md.message_id=photo_msg.id)
                                AND abs(julianday(COALESCE(NULLIF(text_msg.sent_at,''),text_msg.captured_at)) -
                                        julianday(COALESCE(NULLIF(photo_msg.sent_at,''),photo_msg.captured_at))) <= (10.0/1440.0)
                          )
                          AND (
                              EXISTS (SELECT 1 FROM raw_captures r
                                      WHERE r.source_id=jobs.source_id AND r.processing_state='pending')
                              OR EXISTS (SELECT 1 FROM raw_captures r
                                         WHERE r.source_id=jobs.source_id
                                           AND (julianday('now')-julianday(r.captured_at))*86400 < ?)
                              OR EXISTS (SELECT 1 FROM messages recent
                                         WHERE recent.source_id=jobs.source_id
                                           AND (julianday('now')-julianday(recent.captured_at))*86400 < ?)
                          )
                    )
                )""")
                args.extend((float(photo_settle_seconds), float(photo_settle_seconds)))
                # Do not process the text-only half of a new post while a
                # nearby batch of downloaded photos is still being normalized.
                where.append("""(
                    kind<>'build_listings' OR NOT EXISTS (
                        SELECT 1 FROM raw_captures r
                        WHERE r.source_id=jobs.source_id
                          AND EXISTS (SELECT 1 FROM raw_media rm WHERE rm.capture_id=r.id)
                          AND (r.processing_state='pending' OR
                               (julianday('now')-julianday(r.captured_at))*86400 < ?)
                    )
                )""")
                args.append(float(photo_settle_seconds))
            job = db.execute(f"SELECT * FROM jobs WHERE {' AND '.join(where)} ORDER BY id LIMIT 1", args).fetchone()
            if not job:
                return None
            db.execute("UPDATE jobs SET state='running',updated_at=? WHERE id=?", (now(), job["id"]))
            return dict(job)

    def finish_job(self, job_id, state, detail=""):
        if state not in ("pending", "blocked", "done", "failed"):
            raise ValueError("Некорректное состояние задания.")
        with self.connect() as db:
            db.execute("UPDATE jobs SET state=?,detail=?,updated_at=? WHERE id=?", (state, detail, now(), job_id))
            if state == "failed":
                db.execute("UPDATE sources SET binding_state='failed',binding_detail=? WHERE id=(SELECT source_id FROM jobs WHERE id=? AND kind='bind_source')", (detail, job_id))
            if state in ("failed", "blocked"):
                self.event(db, "connection", detail)

    def job_context(self, job):
        with self.connect() as db:
            source = db.execute("SELECT * FROM sources WHERE id=?", (job["source_id"],)).fetchone()
            checkpoint = db.execute("SELECT cursor FROM checkpoints WHERE source_id=? AND mode=?", (job["source_id"], job["kind"])).fetchone()
            cursor = json.loads(checkpoint[0]) if checkpoint else None
            if job["kind"] in ("collect_archive", "collect_initial_scan") and not cursor:
                # Each newly bound source gets one bounded bootstrap window from
                # the newest messages, independent of the unread position.
                cursor = {"initial_scan_cutoff": (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds"),
                          "initial_scan_started": False, "initial_scan_position": 0,
                          "bootstrap_pages": 0}
                db.execute("INSERT INTO checkpoints VALUES (?,?,?,?) ON CONFLICT(source_id,mode) DO UPDATE SET cursor=excluded.cursor,updated_at=excluded.updated_at",
                           (job["source_id"], job["kind"], json.dumps(cursor), now()))
                db.execute("UPDATE sources SET bootstrap_state='scanning' WHERE id=?", (job["source_id"],))
            return dict(source), cursor

    def reconcile_source_bootstrap(self, source_id):
        """Release listings imported during the initial 24-hour source scan."""
        with self.connect() as db:
            changed = db.execute("UPDATE sources SET bootstrap_state='reconciled' WHERE id=? AND bootstrap_state='reconcile_pending'", (source_id,)).rowcount
            if not changed:
                raise ValueError("Для источника нет завершённой первичной сверки.")
            self.event(db, "source", f"Первичная история источника #{source_id} сверена; автоматизация новых карточек разрешена.")

    def finalize_bootstrap_if_ready(self, source_id):
        """Advance to reconciliation only after raw parsing and listing analysis drain."""
        with self.connect() as db:
            source = db.execute("SELECT bootstrap_state FROM sources WHERE id=?", (source_id,)).fetchone()
            if not source or source[0] != "processing":
                return False
            pending_raw = db.execute("SELECT 1 FROM raw_captures WHERE source_id=? AND processing_state='pending' LIMIT 1", (source_id,)).fetchone()
            pending_messages = db.execute(
                """SELECT 1 FROM messages m LEFT JOIN message_analysis a ON a.message_id=m.id
                   WHERE m.source_id=? AND a.message_id IS NULL LIMIT 1""", (source_id,)).fetchone()
            pending_jobs = db.execute(
                """SELECT 1 FROM jobs WHERE source_id=? AND kind IN ('interpret_raw','build_listings')
                   AND state IN ('pending','running','blocked') LIMIT 1""", (source_id,)).fetchone()
            if pending_raw or pending_messages or pending_jobs:
                return False
            db.execute("UPDATE sources SET bootstrap_state='reconciled' WHERE id=? AND bootstrap_state='processing'", (source_id,))
            self.event(db, "source", f"Первичный сбор источника #{source_id} обработан и сверён со всеми отправленными постами BotRieltor; неоднозначные карточки оставлены на проверку.")
            return True

    @staticmethod
    def _raw_block_keys(capture):
        blocks = capture.get("blocks") or []
        media = capture.get("media") or []
        by_index = {item.get("ui_index"): item for item in media if isinstance(item.get("ui_index"), int)}
        keys = []
        for block in blocks:
            attrs = block.get("attrs") or {}
            stable = attrs.get("bubble_id") or next((value for key, value in attrs.items()
                if key.lower() in ("data-msgid", "data-message-id", "data-id")), "")
            if stable:
                keys.append(str(stable)); continue
            evidence = {"text": str(block.get("text", "")).strip(),
                        "media": [by_index[index].get("source_ref", "") for index in block.get("media_indexes", [])
                                  if index in by_index]}
            if evidence["text"] or any(evidence["media"]):
                keys.append(hashlib.sha256(json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode()).hexdigest())
        return keys

    def commit_batch(self, source_id, mode, messages, cursor, *, complete=False):
        """Save messages, revisions, media references and cursor atomically.

        A collector must use stable source-local IDs. Photo and text messages stay
        separate: timestamp proximity alone must never create listing links.
        An archive cursor must not be used as the live catch-up cursor.
        """
        if mode not in ("collect_archive", "collect_initial_scan", "collect_incremental"):
            raise ValueError("Неизвестный режим сбора.")
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM sources WHERE id=?", (source_id,)).fetchone():
                raise ValueError("Источник не найден.")
            self._upsert_messages(db, source_id, messages)
            db.execute("INSERT INTO checkpoints VALUES (?,?,?,?) ON CONFLICT(source_id,mode) DO UPDATE SET cursor=excluded.cursor,updated_at=excluded.updated_at", (source_id, mode, json.dumps(cursor), now()))
            if mode in ("collect_archive", "collect_initial_scan"):
                db.execute("UPDATE sources SET archive_state=?,bootstrap_state=? WHERE id=?",
                           ("complete" if complete else "collecting", "processing" if complete else "scanning", source_id))

    def _upsert_messages(self, db, source_id, messages):
        for message in messages:
            external_id = str(message.get("external_id", "")).strip()
            if not external_id or not message.get("sent_at"):
                raise ValueError("Нужны устойчивый ID сообщения и время из источника.")
            payload = json.dumps(message, ensure_ascii=False, sort_keys=True)
            fingerprint = hashlib.sha256(payload.encode()).hexdigest()
            previous = db.execute("SELECT * FROM messages WHERE source_id=? AND external_id=?", (source_id, external_id)).fetchone()
            if previous and previous["fingerprint"] == fingerprint:
                continue
            values = (str(message.get("sender", "")), str(message["sent_at"]), str(message.get("text", "")), message.get("reply_to"), payload, fingerprint, str(message.get("captured_at") or now()))
            if previous:
                message_id = previous["id"]
                db.execute("INSERT INTO message_versions(message_id,payload,captured_at) VALUES (?,?,?)", (message_id, previous["payload"], previous["captured_at"]))
                db.execute("UPDATE messages SET sender=?,sent_at=?,text=?,reply_to=?,payload=?,fingerprint=?,captured_at=? WHERE id=?", (*values, message_id))
            else:
                message_id = db.execute("INSERT INTO messages(source_id,external_id,sender,sent_at,text,reply_to,payload,fingerprint,captured_at) VALUES (?,?,?,?,?,?,?,?,?)", (source_id, external_id, *values)).lastrowid
            for media in message.get("media", []):
                if not media.get("external_id"):
                    raise ValueError("У вложения отсутствует устойчивый ID.")
                analysis = db.execute(
                    "SELECT state FROM message_analysis WHERE message_id=?", (message_id,)
                ).fetchone()
                is_expired = bool(analysis and analysis["state"] == "expired")
                db.execute("INSERT INTO media(message_id,external_id,kind,source_ref,local_path,sha256,download_state) VALUES (?,?,?,?,?,?,?) ON CONFLICT(message_id,external_id) DO UPDATE SET source_ref=excluded.source_ref,local_path=CASE WHEN excluded.download_state='expired' THEN NULL ELSE COALESCE(excluded.local_path,media.local_path) END,sha256=CASE WHEN excluded.download_state='expired' THEN NULL ELSE COALESCE(excluded.sha256,media.sha256) END,download_state=CASE WHEN excluded.download_state='expired' THEN 'expired' WHEN excluded.download_state='done' THEN 'done' ELSE media.download_state END",
                           (message_id, str(media["external_id"]), media.get("kind", "image"), media.get("source_ref"),
                            None if is_expired else media.get("local_path"),
                            None if is_expired else media.get("sha256"),
                            "expired" if is_expired else media.get("download_state", "pending")))

    def pending_raw_batch(self, source_id, *, limit=8, max_chars=120000):
        with self.connect() as db:
            result, used = [], 0
            for row in db.execute("SELECT * FROM raw_captures WHERE source_id=? AND processing_state='pending' ORDER BY id LIMIT 20", (source_id,)):
                capture = json.loads(row["payload"])
                text = str(capture.get("text", ""))[-20000:]
                blocks = []
                for block in capture.get("blocks", [])[-160:]:
                    compact = {key: block.get(key) for key in
                               ("index", "attrs", "className", "sender", "sent_at", "text", "media_indexes", "rect")}
                    compact["className"] = str(compact.get("className") or "")[:500]
                    compact["text"] = str(compact.get("text", ""))[:2000]
                    blocks.append(compact)
                media = [{key: item.get(key) for key in ("external_id", "kind", "ui_index", "alt", "width", "height", "source_ref")}
                         for item in capture.get("media", [])]
                compact = {"db_id": row["id"], "external_id": row["external_id"], "captured_at": row["captured_at"],
                           "text": text, "blocks": blocks, "media": media}
                size = len(json.dumps(compact, ensure_ascii=False))
                if result and (len(result) >= limit or used + size > max_chars):
                    break
                result.append(compact)
                used += size
            return result

    def commit_interpretation(self, source_id, capture_ids, messages):
        capture_ids = [int(value) for value in capture_ids]
        if not capture_ids:
            raise ValueError("Нет сырых пакетов для подтверждения разбора.")
        with self.connect() as db:
            placeholders = ",".join("?" for _ in capture_ids)
            capture_rows = db.execute(
                f"SELECT id,external_id,captured_at FROM raw_captures WHERE source_id=? AND id IN ({placeholders})",
                (source_id, *capture_ids),
            ).fetchall()
            found = len(capture_rows)
            if found != len(set(capture_ids)):
                raise ValueError("Сырой пакет не принадлежит выбранному источнику.")
            capture_times = {row["external_id"]: row["captured_at"] for row in capture_rows}
            raw_media = {}
            for row in db.execute(f"SELECT * FROM raw_media WHERE capture_id IN ({placeholders})", capture_ids):
                raw_media.setdefault(row["external_id"], dict(row))
            for message in messages:
                message.setdefault("captured_at", capture_times.get(message.get("evidence_capture")))
                enriched = []
                for media in message.get("media", []):
                    evidence = raw_media.get(str(media.get("external_id", "")), {})
                    enriched.append({**media, "source_ref": evidence.get("source_ref"),
                                     "local_path": evidence.get("local_path"), "sha256": evidence.get("sha256"),
                                     "download_state": evidence.get("download_state", "pending")})
                message["media"] = enriched
            self._upsert_messages(db, source_id, messages)
            db.execute(f"UPDATE raw_captures SET processing_state='parsed' WHERE source_id=? AND id IN ({placeholders})", (source_id, *capture_ids))

    def has_pending_raw(self, source_id):
        with self.connect() as db:
            return bool(db.execute("SELECT 1 FROM raw_captures WHERE source_id=? AND processing_state='pending' LIMIT 1", (source_id,)).fetchone())

    def pending_message_batch(self, source_id, *, limit=500):
        with self.connect() as db:
            seed_only = self.get_settings("zalo_seed_only_mode").get("zalo_seed_only_mode")
            seed_ids = None
            if seed_only:
                seed_row = db.execute("SELECT value FROM settings WHERE key='zalo_latest_group_seed'").fetchone()
                seed = json.loads(seed_row[0]) if seed_row else {}
                sources = seed.get("sources", seed)
                item = sources.get(str(source_id), {}) if isinstance(sources, dict) else {}
                seed_ids = [int(value) for value in item.get("evidence_message_ids", [])]
                if not seed_ids:
                    return []
            baseline = self.get_settings("zalo_new_messages_baseline").get("zalo_new_messages_baseline")
            baseline_ms = self.get_settings("zalo_baseline_epoch_ms").get("zalo_baseline_epoch_ms") or 0
            if seed_ids is None:
                rows = db.execute(
                    """SELECT m.* FROM messages m
                       LEFT JOIN message_analysis a ON a.message_id=m.id
                       WHERE m.source_id=? AND a.message_id IS NULL
                       ORDER BY m.id LIMIT ?""",
                    (source_id, limit),
                ).fetchall()
            else:
                marks = ",".join("?" for _ in seed_ids)
                rows = db.execute(
                    f"""SELECT m.* FROM messages m
                        LEFT JOIN message_analysis a ON a.message_id=m.id
                        WHERE m.source_id=? AND a.message_id IS NULL AND m.id IN ({marks})
                        ORDER BY m.id LIMIT ?""",
                    (source_id, *seed_ids, limit),
                ).fetchall()

            import re
            pending_rows = []
            for r in rows:
                item = dict(r)
                is_old = False
                if baseline and item.get("captured_at") and item["captured_at"] <= baseline:
                    is_old = True
                if baseline_ms:
                    m_ts = re.search(r'\b(178\d{10,11})\b', str(item.get("external_id", "")) + " " + str(item.get("sent_at", "")))
                    if m_ts and int(m_ts.group(1)) <= baseline_ms:
                        is_old = True

                if is_old:
                    db.execute("INSERT OR REPLACE INTO message_analysis(message_id, state, listing_id, analyzed_at) VALUES (?, 'reviewed', NULL, ?)", (item["id"], now()))
                else:
                    pending_rows.append(item)

            if not pending_rows:
                return []

            first_pending_id = pending_rows[0]["id"]
            text_anchors = [item for item in pending_rows if str(item.get("text", "")).strip()]
            first_text = min(
                text_anchors or pending_rows,
                key=lambda item: (self._message_timestamp(item.get("sent_at") or item.get("captured_at")),
                                  int(item["id"])),
            )
            anchor_time = self._message_timestamp(first_text.get("sent_at") or first_text.get("captured_at"))
            if anchor_time == float("inf"):
                anchor_time = self._message_timestamp(first_text.get("captured_at"))
            cutoff_time = anchor_time - 10 * 60
            if text_anchors and anchor_time != float("inf"):
                previous_text_rows = db.execute(
                    "SELECT id,sent_at,captured_at FROM messages WHERE source_id=? AND trim(text)<>''",
                    (source_id,),
                ).fetchall()
                previous_text_times = [
                    self._message_timestamp(row["sent_at"] or row["captured_at"])
                    for row in previous_text_rows
                    if (self._message_timestamp(row["sent_at"] or row["captured_at"]), int(row["id"]))
                    < (anchor_time, int(first_text["id"]))
                ]
                if previous_text_times:
                    cutoff_time = max(cutoff_time, max(previous_text_times))
            cutoff_at = datetime.fromtimestamp(cutoff_time, timezone.utc).isoformat(timespec="microseconds")
            anchor_at = datetime.fromtimestamp(anchor_time, timezone.utc).isoformat(timespec="microseconds")
            preceding_rows = [] if seed_ids is not None else db.execute(
                """SELECT m.* FROM messages m
                   JOIN message_analysis a ON a.message_id=m.id
                   WHERE m.source_id=?
                     AND a.state='waiting_photos' AND a.listing_id IS NULL
                     AND trim(m.text) = ''
                     AND EXISTS (SELECT 1 FROM media md WHERE md.message_id=m.id)
                     AND (julianday(?) <= julianday(a.analyzed_at) + (? / 86400.0)
                          OR julianday('now') <= julianday(a.analyzed_at) + (? / 86400.0))
                     AND julianday(COALESCE(NULLIF(m.sent_at,''),m.captured_at)) > julianday(?)
                     AND julianday(COALESCE(NULLIF(m.sent_at,''),m.captured_at)) <= julianday(?)
                   ORDER BY julianday(COALESCE(NULLIF(m.sent_at,''),m.captured_at)) DESC,m.id DESC LIMIT 50""",
                (source_id, first_text.get("sent_at") or first_text.get("captured_at"),
                 WAITING_PHOTOS_TTL_SECONDS, WAITING_PHOTOS_TTL_SECONDS, cutoff_at, anchor_at),
            ).fetchall()

            context_rows = []
            # Also handle the reverse order: a description may arrive before
            # its photos. Zalo can omit sender labels, so link only when there
            # is one unambiguous recent rental description in this source.
            photo_only = next((item for item in pending_rows
                               if not str(item.get("text", "")).strip()
                               and db.execute("SELECT 1 FROM media WHERE message_id=? LIMIT 1", (item["id"],)).fetchone()), None) if seed_ids is None else None
            if photo_only:
                photo_time = self._message_timestamp(photo_only.get("sent_at") or photo_only.get("captured_at"))
                if photo_time != float("inf"):
                    photo_at = datetime.fromtimestamp(photo_time, timezone.utc).isoformat(timespec="microseconds")
                    candidates = db.execute(
                        """SELECT m.* FROM messages m
                           WHERE m.source_id=? AND trim(m.text)<>''
                             AND julianday(COALESCE(NULLIF(m.sent_at,''),m.captured_at)) < julianday(?)
                             AND julianday(COALESCE(NULLIF(m.sent_at,''),m.captured_at)) >= julianday(?,'-10 minutes')
                             AND NOT EXISTS (SELECT 1 FROM media own WHERE own.message_id=m.id)
                             AND NOT EXISTS (
                                 SELECT 1 FROM listing_messages lm
                                 JOIN listing_messages lm2 ON lm2.listing_id=lm.listing_id
                                 JOIN media attached ON attached.message_id=lm2.message_id
                                 WHERE lm.message_id=m.id)
                           ORDER BY julianday(COALESCE(NULLIF(m.sent_at,''),m.captured_at)) DESC,m.id DESC""",
                        (source_id, photo_at, photo_at),
                    ).fetchall()
                    eligible = []
                    for candidate in candidates:
                        candidate = dict(candidate)
                        if not self._looks_like_rental_ad(candidate.get("text", "")):
                            continue
                        text_sender = str(candidate.get("sender", "")).strip()
                        photo_sender = str(photo_only.get("sender", "")).strip()
                        if text_sender and photo_sender and text_sender.casefold() != photo_sender.casefold():
                            continue
                        eligible.append(candidate)
                    # The same description may be posted twice while photos
                    # upload. Treat exact repeats as one candidate and use the
                    # latest preceding copy; distinct descriptions remain
                    # ambiguous and are deliberately not linked automatically.
                    if len(eligible) > 1:
                        normalized = {
                            " ".join(str(candidate.get("text", "")).casefold().split())
                            for candidate in eligible
                        }
                        if len(normalized) == 1:
                            eligible = eligible[:1]  # query order is newest first
                    if len(eligible) == 1:
                        context_rows.append(eligible[0])

            rows = list(reversed(preceding_rows)) + context_rows + list(pending_rows)
            rows = list({row["id"]: row for row in rows}.values())
            rows.sort(key=lambda item: item["id"])
            result = []
            for row in rows:
                item = dict(row)
                item["media"] = [dict(media) for media in db.execute("SELECT external_id,kind,local_path,download_state FROM media WHERE message_id=?", (row["id"],))]
                result.append(item)
            return result

    def has_pending_messages(self, source_id):
        with self.connect() as db:
            baseline = self.get_settings("zalo_new_messages_baseline").get("zalo_new_messages_baseline")
            baseline_filter = "AND m.captured_at > ?" if baseline else ""
            args = (source_id, baseline) if baseline else (source_id,)
            return bool(db.execute(f"SELECT 1 FROM messages m LEFT JOIN message_analysis a ON a.message_id=m.id WHERE m.source_id=? AND a.message_id IS NULL {baseline_filter} LIMIT 1", args).fetchone())

    def defer_photo_only_messages(self, source_id, message_ids):
        """Retain photo-first messages as context until their following text arrives."""
        ids = list(dict.fromkeys(int(value) for value in message_ids))
        if not ids:
            return
        marks = ",".join("?" for _ in ids)
        stamp = now()
        with self.connect() as db:
            rows = db.execute(
                f"""SELECT m.id FROM messages m
                    WHERE m.source_id=? AND m.id IN ({marks}) AND trim(m.text)=''
                      AND EXISTS (SELECT 1 FROM media md WHERE md.message_id=m.id)""",
                (source_id, *ids),
            ).fetchall()
            if len(rows) != len(ids):
                raise ValueError("Отложить можно только фото-сообщения выбранной группы.")
            db.executemany(
                """INSERT INTO message_analysis(message_id,state,listing_id,analyzed_at)
                   VALUES (?,'waiting_photos',NULL,?)
                   ON CONFLICT(message_id) DO UPDATE SET
                     state='waiting_photos',
                     analyzed_at=CASE WHEN message_analysis.state='waiting_photos'
                                      THEN message_analysis.analyzed_at ELSE excluded.analyzed_at END
                   WHERE message_analysis.listing_id IS NULL""",
                ((int(row["id"]), stamp) for row in rows),
            )
            waiting = db.execute(
                """SELECT count(md.id) AS count,min(a.analyzed_at) AS since
                   FROM messages m JOIN message_analysis a ON a.message_id=m.id
                   JOIN media md ON md.message_id=m.id
                   WHERE m.source_id=? AND a.state='waiting_photos' AND a.listing_id IS NULL
                     AND trim(m.text)=''
                     AND NOT EXISTS (SELECT 1 FROM listing_messages lm WHERE lm.message_id=m.id)""",
                (source_id,),
            ).fetchone()
            return {"count": int(waiting["count"] or 0), "since": waiting["since"]}

    def expire_waiting_photos(self, *, max_age_seconds=WAITING_PHOTOS_TTL_SECONDS):
        """Expire unattached photo messages after their waiting window elapses."""
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max(0, max_age_seconds))).isoformat(timespec="seconds")
        expired_ids = []
        expired_external_ids = set()
        candidate_paths = set()
        waiting_text_by_source = {}
        pending_raw_by_source = {}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            candidates = db.execute(
                """SELECT m.id,m.source_id,m.sent_at,m.captured_at,a.analyzed_at
                   FROM messages m JOIN message_analysis a ON a.message_id=m.id
                   JOIN sources s ON s.id=m.source_id
                   WHERE a.state='waiting_photos' AND a.listing_id IS NULL AND trim(m.text)=''
                     AND s.enabled=1 AND s.kind='group'
                     AND a.analyzed_at<=?
                     AND EXISTS (SELECT 1 FROM media md WHERE md.message_id=m.id)
                     AND NOT EXISTS (SELECT 1 FROM listing_messages lm WHERE lm.message_id=m.id)
                   ORDER BY a.analyzed_at,m.id""",
                (cutoff,),
            ).fetchall()
            for message in candidates:
                source_id = int(message["source_id"])
                photo_time = self._message_timestamp(message["sent_at"] or message["captured_at"])
                if photo_time == float("inf"):
                    photo_time = self._message_timestamp(message["captured_at"])
                if photo_time != float("inf"):
                    waiting_since = self._message_timestamp(message["analyzed_at"])
                    deadline = waiting_since + max(0, max_age_seconds)
                    # A caption captured before the deadline keeps the batch
                    # alive while the normal 30-second album settle finishes.
                    if source_id not in waiting_text_by_source:
                        waiting_text_by_source[source_id] = db.execute(
                            """SELECT sent_at,captured_at FROM messages
                               WHERE source_id=? AND trim(text)<>'' AND id NOT IN
                                 (SELECT message_id FROM message_analysis)""",
                            (source_id,),
                        ).fetchall()
                    has_on_time_text = False
                    for row in waiting_text_by_source[source_id]:
                        text_time = self._message_timestamp(row["sent_at"])
                        if text_time == float("inf"):
                            text_time = self._message_timestamp(row["captured_at"])
                        if text_time <= deadline and abs(text_time - photo_time) <= 600:
                            has_on_time_text = True
                            break
                    if has_on_time_text:
                        continue

                    # Text may still be in the raw Zalo queue, before message
                    # rows exist. Protect it only when it reached the app by
                    # the same deadline.
                    if source_id not in pending_raw_by_source:
                        pending_raw_by_source[source_id] = db.execute(
                            """SELECT captured_at,source_text FROM raw_captures
                               WHERE source_id=? AND processing_state='pending' AND trim(source_text)<>''""",
                            (source_id,),
                        ).fetchall()
                    if any(
                        self._message_timestamp(row["captured_at"]) <= deadline
                        and abs(self._message_timestamp(row["captured_at"]) - photo_time) <= 600
                        for row in pending_raw_by_source[source_id]
                    ):
                        continue

                expired_ids.append(int(message["id"]))
                expired_external_ids.update(
                    row["external_id"] for row in db.execute(
                        "SELECT external_id FROM media WHERE message_id=?", (message["id"],)
                    )
                )
                candidate_paths.update(
                    row["local_path"] for row in db.execute(
                        "SELECT local_path FROM media WHERE message_id=? AND local_path IS NOT NULL",
                        (message["id"],),
                    )
                )

            if expired_ids:
                marks = ",".join("?" for _ in expired_ids)
                db.execute(
                    f"UPDATE message_analysis SET state='expired',listing_id=NULL,analyzed_at=? WHERE message_id IN ({marks})",
                    (now(), *expired_ids),
                )
                db.execute(
                    f"UPDATE media SET local_path=NULL,sha256=NULL,download_state='expired' WHERE message_id IN ({marks})",
                    expired_ids,
                )
                # Keep raw evidence as history, but clear an attachment cache
                # when no still-live message uses that same media ID.
                for external_id in expired_external_ids:
                    active_ref = db.execute(
                        """SELECT 1 FROM media md JOIN messages m ON m.id=md.message_id
                           LEFT JOIN message_analysis a ON a.message_id=m.id
                           WHERE m.source_id IN (SELECT source_id FROM messages WHERE id IN (%s))
                             AND md.external_id=? AND COALESCE(a.state,'')<>'expired' LIMIT 1"""
                        % marks,
                        (*expired_ids, external_id),
                    ).fetchone()
                    pending_ref = db.execute(
                        """SELECT 1 FROM raw_media rm JOIN raw_captures rc ON rc.id=rm.capture_id
                           WHERE rc.source_id IN (SELECT source_id FROM messages WHERE id IN (%s))
                             AND rc.processing_state='pending' AND rm.external_id=? LIMIT 1""" % marks,
                        (*expired_ids, external_id),
                    ).fetchone()
                    if not active_ref and not pending_ref:
                        raw_paths = db.execute(
                            """SELECT rm.local_path FROM raw_media rm JOIN raw_captures rc ON rc.id=rm.capture_id
                               WHERE rc.source_id IN (SELECT source_id FROM messages WHERE id IN (%s))
                                 AND rm.external_id=? AND rm.local_path IS NOT NULL""" % marks,
                            (*expired_ids, external_id),
                        ).fetchall()
                        candidate_paths.update(row["local_path"] for row in raw_paths)
                        db.execute(
                            """UPDATE raw_media SET local_path=NULL,sha256=NULL,download_state='expired'
                               WHERE external_id=? AND capture_id IN (
                                   SELECT id FROM raw_captures WHERE source_id IN
                                     (SELECT source_id FROM messages WHERE id IN (%s)))""" % marks,
                            (external_id, *expired_ids),
                        )

            waiting = db.execute(
                """SELECT count(md.id) AS count,min(a.analyzed_at) AS since
                   FROM messages m JOIN message_analysis a ON a.message_id=m.id
                   JOIN media md ON md.message_id=m.id
                   JOIN sources s ON s.id=m.source_id
                   WHERE a.state='waiting_photos' AND a.listing_id IS NULL AND trim(m.text)=''
                     AND s.enabled=1 AND s.kind='group'
                     AND NOT EXISTS (SELECT 1 FROM listing_messages lm WHERE lm.message_id=m.id)"""
            ).fetchone()
            waiting_value = ({"count": int(waiting["count"]), "since": waiting["since"]}
                             if waiting["count"] else None)
            current_waiting = db.execute("SELECT value FROM settings WHERE key='waiting_photos'").fetchone()
            current_value = json.loads(current_waiting["value"]) if current_waiting else None
            if current_value != waiting_value:
                db.execute(
                    """INSERT INTO settings(key,value) VALUES ('waiting_photos',?)
                       ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                    (json.dumps(waiting_value, ensure_ascii=False),),
                )
            if expired_ids:
                self.event(db, "runtime", f"Ожидание фото истекло: {len(expired_ids)} сообщений удалено из очереди без описания.")

        removed_files = 0
        raw_root = (self.data_dir / "raw-media").resolve()
        for value in candidate_paths:
            try:
                path = Path(value).resolve()
                path.relative_to(raw_root)
                with self.connect() as db:
                    still_referenced = db.execute(
                        """SELECT 1 FROM media WHERE local_path=? AND download_state='done'
                           UNION ALL
                           SELECT 1 FROM raw_media WHERE local_path=? AND download_state='done'
                           LIMIT 1""",
                        (str(path), str(path)),
                    ).fetchone()
                if not still_referenced and path.is_file():
                    path.unlink()
                    removed_files += 1
            except (OSError, ValueError):
                continue
        return {"messages": len(expired_ids), "files": removed_files}

    def claim_listing_automation(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            posting_setting = db.execute("SELECT value FROM settings WHERE key='telegram_posting_enabled'").fetchone()
            posting_enabled = bool(json.loads(posting_setting[0])) if posting_setting else False
            row = db.execute(
                """SELECT a.*,l.status,l.revision FROM listing_automation a
                   JOIN listings l ON l.id=a.listing_id
                   WHERE a.state IN ('pending','decided') AND a.retry_after<=?
                     AND NOT (a.state='decided' AND a.decision='publish' AND ?=0)
                     AND NOT EXISTS (
                         SELECT 1 FROM listing_messages lm
                         JOIN messages m ON m.id=lm.message_id
                         JOIN sources s ON s.id=m.source_id
                         WHERE lm.listing_id=l.id AND s.bootstrap_state IN ('scanning','processing','reconcile_pending'))
                   ORDER BY CASE a.state WHEN 'decided' THEN 0 ELSE 1 END,a.listing_id LIMIT 1""",
                (time.time(), int(posting_enabled))).fetchone()
            if row is None:
                return None
            db.execute("UPDATE listing_automation SET state='running',attempts=attempts+1,updated_at=? WHERE listing_id=?",
                       (now(), row["listing_id"]))
            return dict(row)

    def listing_automation_context(self, listing_id):
        listing = self.get_listing_for_publication(listing_id)
        with self.connect() as db:
            row = db.execute("SELECT * FROM listing_automation WHERE listing_id=?", (listing_id,)).fetchone()
            listing["automation"] = dict(row) if row else {}
        return listing

    def save_listing_decision(self, listing_id, decision, reason, model):
        if decision not in ("publish", "rented", "review"):
            raise ValueError("Claude вернул неизвестное решение по объекту.")
        reason = str(reason).strip()[:1000] or "Решение Claude без пояснения"
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            listing = db.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
            automation = db.execute("SELECT * FROM listing_automation WHERE listing_id=?", (listing_id,)).fetchone()
            if listing is None or automation is None:
                raise ValueError("Задание автоматизации объекта не найдено.")
            if decision == "publish":
                db.execute("UPDATE listing_messages SET confirmed=1 WHERE listing_id=?", (listing_id,))
                db.execute("UPDATE listing_automation SET state='decided',decision='publish',reason=?,model=?,last_error='',retry_after=0,updated_at=? WHERE listing_id=?",
                           (reason, model, now(), listing_id))
                self.event(db, "agent", f"Claude разрешил автоматическую публикацию NT-{listing_id:05d}: {reason[:240]}")
                return
            if decision == "rented":
                if listing["status"] != "rented":
                    revision = listing["revision"] + 1
                    db.execute("UPDATE listings SET status='rented',status_reason=?,revision=?,updated_at=? WHERE id=?",
                               (f"Claude: {reason}", revision, now(), listing_id))
                    for publication in db.execute("SELECT id FROM publications WHERE listing_id=?", (listing_id,)):
                        db.execute("INSERT OR IGNORE INTO outbox(publication_id,revision,action,created_at) VALUES (?,?,'edit_listing',?)",
                                   (publication["id"], revision, now()))
                db.execute("UPDATE listing_messages SET confirmed=1 WHERE listing_id=?", (listing_id,))
                db.execute("UPDATE listing_automation SET state='done',decision='rented',reason=?,model=?,last_error='',retry_after=0,updated_at=? WHERE listing_id=?",
                           (reason, model, now(), listing_id))
                self.event(db, "status", f"Claude отметил NT-{listing_id:05d} как сданный: {reason[:240]}")
                return
            if not db.execute("SELECT 1 FROM reviews WHERE listing_id=? AND state='open'", (listing_id,)).fetchone():
                evidence = [row[0] for row in db.execute("SELECT message_id FROM listing_messages WHERE listing_id=?", (listing_id,))]
                db.execute("INSERT INTO reviews(listing_id,reason,evidence,created_at) VALUES (?,?,?,?)",
                           (listing_id, reason, json.dumps(evidence), now()))
            db.execute("UPDATE listing_automation SET state='review',decision='review',reason=?,model=?,last_error='',retry_after=0,updated_at=? WHERE listing_id=?",
                       (reason, model, now(), listing_id))
            self.event(db, "review", f"Claude оставил NT-{listing_id:05d} на проверку: {reason[:240]}")

    def finish_listing_automation(self, listing_id):
        with self.connect() as db:
            db.execute("UPDATE listing_automation SET state='done',last_error='',retry_after=0,updated_at=? WHERE listing_id=?",
                       (now(), listing_id))

    def fail_listing_automation(self, listing_id, error, delay=120):
        with self.connect() as db:
            row = db.execute("SELECT decision FROM listing_automation WHERE listing_id=?", (listing_id,)).fetchone()
            state = "decided" if row and row["decision"] == "publish" else "pending"
            db.execute("UPDATE listing_automation SET state=?,last_error=?,retry_after=?,updated_at=? WHERE listing_id=?",
                       (state, str(error)[:1000], time.time() + delay, now(), listing_id))
            err_name = type(error).__name__ if isinstance(error, Exception) else str(error)[:40]
            self.event(db, "agent", f"Автоматизация NT-{listing_id:05d} будет повторена позже: {err_name}.")

    def claim_review_automation(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT a.* FROM review_automation a JOIN reviews r ON r.id=a.review_id
                   WHERE a.state='pending' AND r.state='open' AND a.retry_after<=?
                   ORDER BY a.review_id LIMIT 1""", (time.time(),)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE review_automation SET state='running',attempts=attempts+1,updated_at=? WHERE review_id=?",
                       (now(), row["review_id"]))
            return dict(row)

    def review_automation_context(self, review_id):
        with self.connect() as db:
            review = db.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
            if review is None:
                raise ValueError("Спорный случай не найден.")
            evidence_ids = json.loads(review["evidence"])
            if not evidence_ids:
                raise ValueError("У спорного случая нет сообщений-доказательств.")
            placeholders = ",".join("?" for _ in evidence_ids)
            messages = [dict(row) for row in db.execute(
                f"""SELECT m.id,m.source_id,m.sender,m.sent_at,m.text,s.name AS source_name
                    FROM messages m JOIN sources s ON s.id=m.source_id
                    WHERE m.id IN ({placeholders}) ORDER BY m.sent_at,m.id""", evidence_ids)]
            source_ids = sorted({row["source_id"] for row in messages})
            candidates = []
            if source_ids:
                source_placeholders = ",".join("?" for _ in source_ids)
                candidates = [dict(row) for row in db.execute(
                    f"""SELECT DISTINCT l.id,l.title,l.description,l.price,l.status,l.updated_at
                        FROM listings l JOIN listing_messages lm ON lm.listing_id=l.id
                        JOIN messages m ON m.id=lm.message_id
                        WHERE m.source_id IN ({source_placeholders})
                        ORDER BY l.updated_at DESC,l.id DESC LIMIT 60""", source_ids)]
            return {"review": dict(review), "messages": messages, "candidates": candidates}

    def save_review_decision(self, review_id, decision, listing_id, reason, model):
        if decision not in ("rented", "review", "dismiss"):
            raise ValueError("Claude вернул неизвестное решение по спорному случаю.")
        reason = str(reason).strip()[:1000] or "Решение Claude без пояснения"
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            review = db.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
            if review is None:
                raise ValueError("Спорный случай не найден.")
            if decision == "rented":
                target = db.execute("SELECT * FROM listings WHERE id=?", (int(listing_id),)).fetchone()
                evidence_ids = json.loads(review["evidence"])
                placeholders = ",".join("?" for _ in evidence_ids)
                shares_source = target is not None and db.execute(
                    f"""SELECT 1 FROM listing_messages lm JOIN messages existing ON existing.id=lm.message_id
                        JOIN messages evidence ON evidence.source_id=existing.source_id
                        WHERE lm.listing_id=? AND evidence.id IN ({placeholders}) LIMIT 1""",
                    (int(listing_id), *evidence_ids)).fetchone()
                if not shares_source:
                    raise ValueError("Claude выбрал объект из другого источника.")
                if target["status"] != "rented":
                    revision = target["revision"] + 1
                    db.execute("UPDATE listings SET status='rented',status_reason=?,revision=?,updated_at=? WHERE id=?",
                               (f"Claude по сообщению о сдаче: {reason}", revision, now(), int(listing_id)))
                    for publication in db.execute("SELECT id FROM publications WHERE listing_id=?", (int(listing_id),)):
                        db.execute("INSERT OR IGNORE INTO outbox(publication_id,revision,action,created_at) VALUES (?,?,'edit_listing',?)",
                                   (publication["id"], revision, now()))
                db.execute("UPDATE reviews SET state='resolved',listing_id=?,reason=? WHERE id=?",
                           (int(listing_id), reason, review_id))
                state = "done"
                self.event(db, "status", f"Claude связал уведомление со NT-{int(listing_id):05d} и отметил объект как сданный.")
            elif decision == "dismiss":
                db.execute("UPDATE reviews SET state='resolved',reason=? WHERE id=?", (reason, review_id))
                state = "done"
            else:
                db.execute("UPDATE reviews SET reason=? WHERE id=?", (reason, review_id))
                state = "review"
            db.execute("UPDATE review_automation SET state=?,decision=?,reason=?,model=?,last_error='',retry_after=0,updated_at=? WHERE review_id=?",
                       (state, decision, reason, model, now(), review_id))

    def fail_review_automation(self, review_id, error, delay=120):
        with self.connect() as db:
            db.execute("UPDATE review_automation SET state='pending',last_error=?,retry_after=?,updated_at=? WHERE review_id=?",
                       (str(error)[:1000], time.time() + delay, now(), review_id))

    def claim_outbox(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            posting = db.execute("SELECT value FROM settings WHERE key='telegram_posting_enabled'").fetchone()
            if not posting or not json.loads(posting[0]):
                return None
            row = db.execute(
                """SELECT o.*,p.listing_id,p.message_ids,p.body,l.status,
                          (SELECT count(*) FROM media md JOIN listing_messages lm ON lm.message_id=md.message_id
                           WHERE lm.listing_id=l.id AND md.kind<>'video' AND md.download_state='done' AND md.local_path IS NOT NULL) AS media_count
                   FROM outbox o JOIN publications p ON p.id=o.publication_id JOIN listings l ON l.id=p.listing_id
                   WHERE o.state='pending' AND o.retry_after<=?
                     AND NOT EXISTS (
                         SELECT 1 FROM listing_messages lm JOIN messages m ON m.id=lm.message_id
                         JOIN sources s ON s.id=m.source_id
                         WHERE lm.listing_id=l.id AND s.bootstrap_state IN ('scanning','processing','reconcile_pending'))
                   ORDER BY o.id LIMIT 1""", (time.time(),)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE outbox SET state='running' WHERE id=?", (row["id"],))
            result = dict(row)
            result["message_ids"] = json.loads(result["message_ids"])
            return result

    def finish_outbox(self, outbox_id):
        with self.connect() as db:
            db.execute("UPDATE outbox SET state='done',last_error='',retry_after=0 WHERE id=?", (outbox_id,))

    def fail_outbox(self, outbox_id, error, delay=120):
        with self.connect() as db:
            db.execute("UPDATE outbox SET state='pending',last_error=?,retry_after=? WHERE id=?",
                       (str(error)[:1000], time.time() + delay, outbox_id))

    def commit_listing_candidates(self, source_id, message_ids, candidates, loose_reviews):
        message_ids = [int(value) for value in message_ids]
        if not message_ids:
            return
        with self.connect() as db:
            placeholders = ",".join("?" for _ in message_ids)
            rows = db.execute(f"SELECT id,external_id FROM messages WHERE source_id=? AND id IN ({placeholders})", (source_id, *message_ids)).fetchall()
            if len(rows) != len(set(message_ids)):
                raise ValueError("Сообщения не принадлежат выбранному источнику.")
            expired = {int(row[0]) for row in db.execute(
                f"SELECT message_id FROM message_analysis WHERE state='expired' AND message_id IN ({placeholders})",
                message_ids,
            )}
            by_external = {row["external_id"]: row["id"] for row in rows if int(row["id"]) not in expired}
            linked = set()
            linked_to = {}
            source_bootstrap = db.execute("SELECT bootstrap_state FROM sources WHERE id=?", (source_id,)).fetchone()
            bootstrap_hold = bool(source_bootstrap and source_bootstrap[0] in ("scanning", "processing", "reconcile_pending"))
            for candidate in candidates:
                evidence = [by_external[value] for value in candidate.get("message_external_ids", []) if value in by_external]
                if not evidence:
                    continue
                title = str(candidate.get("title", "")).strip()[:200] or "Объект без названия"
                description = str(candidate.get("description", "")).strip()[:20000]
                price = str(candidate.get("price", "")).strip()[:150]
                location = str(candidate.get("location", "")).strip()[:200]

                existing = {row[0] for row in db.execute(
                    f"SELECT DISTINCT listing_id FROM listing_messages WHERE message_id IN ({','.join('?' for _ in evidence)})",
                    evidence,
                )}
                if len(existing) == 1:
                    listing_id = existing.pop()
                    db.execute(
                        """UPDATE listings SET title=?,description=?,price=?,
                           location=CASE WHEN ?<>'' THEN ? ELSE location END,updated_at=?
                           WHERE id=? AND status='unverified'""",
                        (title, description, price, location, location, now(), listing_id),
                    )
                elif existing:
                    listing_id = sorted(existing)[0]
                else:
                    listing_id = db.execute(
                        """INSERT INTO listings(title,description,price,location,status,status_reason,created_at,updated_at)
                           VALUES (?,?,?,?,'unverified','Создано агентом из архива Zalo',?,?)""",
                        (title, description, price, location, now(), now()),
                    ).lastrowid
                    automation_state = "review" if bootstrap_hold else "pending"
                    automation_decision = "review" if automation_state == "review" else ""
                    automation_reason = "Первичная история: нужна сверка с отправленными постами BotRieltor" if bootstrap_hold else ""
                    db.execute("INSERT INTO listing_automation(listing_id,state,decision,reason,updated_at) VALUES (?,?,?,?,?)",
                               (listing_id, automation_state, automation_decision, automation_reason, now()))
                for message_id in evidence:
                    db.execute("INSERT OR IGNORE INTO listing_messages(listing_id,message_id,confirmed) VALUES (?,?,0)", (listing_id, message_id))
                    linked.add(message_id)
                    linked_to[message_id] = listing_id
                if bootstrap_hold:
                    db.execute("UPDATE listing_automation SET state='review',decision='review',reason='Первичная сверка с историей BotRieltor',retry_after=0,updated_at=? WHERE listing_id=? AND state IN ('pending','decided')",
                               (now(), listing_id))
                confidence = float(candidate.get("confidence", 0) or 0)
                if candidate.get("needs_review") or confidence < 0.8 or bootstrap_hold:
                    reason = ("Первичная история: нужно сверить с отправленными постами BotRieltor" if bootstrap_hold else
                              str(candidate.get("review_reason", "").strip() or "Нужно проверить объединение сообщений в объект"))
                    db.execute("INSERT INTO reviews(listing_id,reason,evidence,created_at) VALUES (?,?,?,?)", (listing_id, reason, json.dumps(evidence), now()))
            for review in loose_reviews:
                evidence = [by_external[value] for value in review.get("message_external_ids", []) if value in by_external]
                reason = str(review.get("reason", "")).strip()
                if reason and evidence:
                    review_id = db.execute("INSERT INTO reviews(reason,evidence,created_at) VALUES (?,?,?)", (reason, json.dumps(evidence), now())).lastrowid
                    db.execute("INSERT INTO review_automation(review_id,updated_at) VALUES (?,?)", (review_id, now()))
            for message_id in message_ids:
                if message_id in expired:
                    continue
                prior = db.execute(
                    "SELECT a.state,a.analyzed_at,trim(m.text) AS text FROM message_analysis a "
                    "JOIN messages m ON m.id=a.message_id WHERE a.message_id=?",
                    (message_id,),
                ).fetchone()
                has_unattached_photo = bool(
                    prior and not prior["text"]
                    and db.execute("SELECT 1 FROM media WHERE message_id=? LIMIT 1", (message_id,)).fetchone()
                    and message_id not in linked
                )
                state = "linked" if message_id in linked else "waiting_photos" if has_unattached_photo else "reviewed"
                analyzed_at = now()
                if has_unattached_photo and prior["state"] == "waiting_photos" and prior["analyzed_at"]:
                    analyzed_at = prior["analyzed_at"]
                db.execute("INSERT OR REPLACE INTO message_analysis(message_id,state,listing_id,analyzed_at) VALUES (?,?,?,?)",
                           (message_id, state, linked_to.get(message_id), analyzed_at))

    def commit_raw_batch(self, source_id, mode, captures, cursor, *, complete=False):
        """Persist source evidence and its cursor before any model processing."""
        if mode not in ("collect_archive", "collect_initial_scan", "collect_incremental"):
            raise ValueError("Неизвестный режим сбора.")
        with self.connect() as db:
            media_ready = set()
            source = db.execute("SELECT name,observed_name FROM sources WHERE id=?", (source_id,)).fetchone()
            if not source:
                raise ValueError("Источник не найден.")
            for capture in captures:
                active_chat = str((capture.get("ui") or {}).get("active_chat_name") or "")
                canonical = lambda value: "".join(
                    char.casefold() for char in unicodedata.normalize("NFC", value) if char.isalnum()
                )
                expected = canonical(source["name"])
                observed = canonical(active_chat)
                if not observed or observed != expected:
                    raise ValueError(
                        f"Zalo не подтвердил группу «{source['name']}» (в снимке «{active_chat or 'название отсутствует'}»); данные не сохранены."
                    )
                if active_chat and active_chat != source["observed_name"]:
                    db.execute("UPDATE sources SET observed_name=? WHERE id=?", (active_chat, source_id))
                external_id = str(capture.get("external_id", "")).strip()
                captured_at = str(capture.get("captured_at", "")).strip()
                if not external_id or not captured_at:
                    raise ValueError("Сырому снимку нужны устойчивый ID и время захвата.")
                payload = json.dumps(capture, ensure_ascii=False, sort_keys=True)
                evidence = {key: value for key, value in capture.items() if key != "captured_at"}
                fingerprint = hashlib.sha256(json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                previous = db.execute(
                    "SELECT id,fingerprint FROM raw_captures WHERE source_id=? AND external_id=?",
                    (source_id, external_id),
                ).fetchone()
                if previous and previous["fingerprint"] == fingerprint:
                    capture_id = previous["id"]
                elif previous:
                    capture_id = previous["id"]
                    db.execute(
                        "UPDATE raw_captures SET captured_at=?,source_text=?,payload=?,fingerprint=? WHERE id=?",
                        (captured_at, str(capture.get("text", "")), payload, fingerprint, capture_id),
                    )
                else:
                    capture_id = db.execute(
                        "INSERT INTO raw_captures(source_id,external_id,captured_at,source_text,payload,fingerprint) VALUES (?,?,?,?,?,?)",
                        (source_id, external_id, captured_at, str(capture.get("text", "")), payload, fingerprint),
                    ).lastrowid
                for media in capture.get("media", []):
                    media_id = str(media.get("external_id", "")).strip()
                    if not media_id:
                        raise ValueError("У сырого вложения отсутствует устойчивый ID.")
                    db.execute(
                        "INSERT INTO raw_media(capture_id,external_id,kind,source_ref,local_path,sha256,download_state) VALUES (?,?,?,?,?,?,?) "
                        "ON CONFLICT(capture_id,external_id) DO UPDATE SET source_ref=excluded.source_ref,local_path=COALESCE(excluded.local_path,raw_media.local_path),sha256=COALESCE(excluded.sha256,raw_media.sha256),download_state=CASE WHEN excluded.download_state='done' THEN 'done' ELSE raw_media.download_state END WHERE raw_media.download_state<>'expired'",
                        (capture_id, media_id, media.get("kind", "image"), media.get("source_ref"),
                         media.get("local_path"), media.get("sha256"), media.get("download_state", "pending")),
                    )
                    if media.get("download_state") == "done" and media.get("local_path"):
                        # A later Zalo snapshot may finally make an earlier blob image
                        # downloadable. Upgrade every matching parsed message from this
                        # source without spending another model request.
                        db.execute(
                            """UPDATE media SET source_ref=?,local_path=?,sha256=?,download_state='done'
                               WHERE external_id=? AND message_id IN
                                   (SELECT id FROM messages WHERE source_id=?)
                                 AND NOT EXISTS (SELECT 1 FROM message_analysis a
                                                 WHERE a.message_id=media.message_id AND a.state='expired')""",
                            (media.get("source_ref"), media.get("local_path"), media.get("sha256"),
                             media_id, source_id),
                        )
                        media_ready.update(row[0] for row in db.execute(
                            """SELECT DISTINCT lm.listing_id FROM listing_messages lm
                               JOIN messages m ON m.id=lm.message_id
                               JOIN media md ON md.message_id=m.id
                               WHERE m.source_id=? AND md.external_id=?""",
                            (source_id, media_id),
                        ))
            if media_ready:
                placeholders = ",".join("?" for _ in media_ready)
                db.execute(
                    f"""UPDATE listing_automation SET retry_after=0,last_error='',updated_at=?
                        WHERE listing_id IN ({placeholders}) AND state IN ('pending','decided')""",
                    (now(), *sorted(media_ready)),
                )
            db.execute(
                "INSERT INTO checkpoints VALUES (?,?,?,?) ON CONFLICT(source_id,mode) DO UPDATE SET cursor=excluded.cursor,updated_at=excluded.updated_at",
                (source_id, mode, json.dumps(cursor), now()),
            )
            if mode in ("collect_archive", "collect_initial_scan"):
                db.execute("UPDATE sources SET archive_state=?,bootstrap_state=? WHERE id=?",
                           ("complete" if complete else "collecting", "processing" if complete else "scanning", source_id))
                if complete:
                    keys = list((cursor or {}).get("seen_message_keys", []))
                    if not keys:
                        for row in db.execute("SELECT payload FROM raw_captures WHERE source_id=? ORDER BY id DESC LIMIT 1000", (source_id,)):
                            try:
                                keys.extend(self._raw_block_keys(json.loads(row[0])))
                            except (TypeError, ValueError):
                                continue
                    # The collector uses the last 5000 source-local keys as its
                    # overlap watermark. Seed it from the imported bootstrap so
                    # messages arriving during the scan remain discoverable.
                    seeded = {"seen_message_keys": list(dict.fromkeys(keys))[-5000:]}
                    db.execute("INSERT INTO checkpoints VALUES (?,?,?,?) ON CONFLICT(source_id,mode) DO UPDATE SET cursor=excluded.cursor,updated_at=excluded.updated_at",
                               (source_id, "collect_incremental", json.dumps(seeded), now()))

    def runtime_event(self, text):
        with self.connect() as db:
            self.event(db, "runtime", text)
