#!/usr/bin/env python3
"""Process one durable raw batch without printing private chat content."""
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from botrieltor.interpreter import RawArchiveInterpreter
from botrieltor.omniroute import KeychainSecretStore, OmniRouteClient
from botrieltor.store import Store

root = Path(__file__).resolve().parents[1]
data_dir = Path.home() / "Library/Application Support/BotRieltor"
helper = root / "dist/BotRieltor.app/Contents/Resources/BotRieltorKeychain"
store = Store(data_dir)
client = OmniRouteClient(store, KeychainSecretStore(helper))
client.test()
with store.connect() as db:
    source = db.execute(
        "SELECT s.* FROM sources s WHERE EXISTS (SELECT 1 FROM raw_captures r WHERE r.source_id=s.id AND r.processing_state='pending') ORDER BY s.id LIMIT 1"
    ).fetchone()
    before_messages = db.execute("SELECT count(*) FROM messages").fetchone()[0]
    before_pending = db.execute("SELECT count(*) FROM raw_captures WHERE processing_state='pending'").fetchone()[0]
if source is None:
    raise SystemExit("No pending raw capture")
processed = RawArchiveInterpreter(store, client).process(dict(source), threading.Event())
with store.connect() as db:
    after_messages = db.execute("SELECT count(*) FROM messages").fetchone()[0]
    after_pending = db.execute("SELECT count(*) FROM raw_captures WHERE processing_state='pending'").fetchone()[0]
print(json.dumps({"processed_captures": processed, "messages_added": after_messages - before_messages,
                  "pending_change": after_pending - before_pending, "model": client.last_used_model}, ensure_ascii=False))
