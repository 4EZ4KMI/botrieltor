#!/usr/bin/env python3
"""Live diagnostics without printing credentials or private data."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from botrieltor.omniroute import KeychainSecretStore, OmniRouteClient
from botrieltor.store import Store

data_dir = Path.home() / "Library/Application Support/BotRieltor"
helper = Path(__file__).resolve().parents[1] / "dist/BotRieltor.app/Contents/Resources/BotRieltorKeychain"
store = Store(data_dir)
client = OmniRouteClient(store, KeychainSecretStore(helper))
connection = client.test()
response = client.chat([
    {"role": "system", "content": "Reply with exactly BOTRIELTOR_OK and nothing else."},
    {"role": "user", "content": "Connectivity check."},
], temperature=0)
content = response.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
snapshot = store.snapshot()
print(json.dumps({
    "connected": connection["connected"],
    "model": connection["model"],
    "model_count": len(connection["models"]),
    "agent_reply": content,
    "sources": len(snapshot["sources"]),
    "binding_jobs": sum(job["kind"] == "bind_source" for job in snapshot["jobs"]),
    "messages": snapshot["counts"]["messages"],
}, ensure_ascii=False, indent=2))
