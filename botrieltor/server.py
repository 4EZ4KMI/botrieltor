from __future__ import annotations

import argparse
import errno
import json
import mimetypes
import os
import secrets
import signal
import shutil
import sys
import threading
from threading import current_thread, main_thread
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from .runtime import Runtime
from .store import Conflict, Store
from .omniroute import OmniRouteClient
from .interpreter import RawArchiveInterpreter
from .listing_builder import ListingBuilder
from .zalo import ZaloAgentCollector
from .telegram import TelegramClient
from .publisher import TelegramPublisher
from .decision_agent import ClaudeDecisionAgent
from .paths import default_data_dir

STATIC = Path(__file__).parent / "static"


def build_server(store, runtime, port=0, omniroute=None, telegram=None, publisher=None):
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        server_version = "BotRieltor"

        def log_message(self, *_):
            pass

        def send(self, status, value, content_type="application/json; charset=utf-8"):
            data = json.dumps(value, ensure_ascii=False).encode() if isinstance(value, (dict, list)) else value
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def allowed_host(self):
            return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

        def authorized(self):
            supplied = self.headers.get("X-App-Token", "")
            if not secrets.compare_digest(supplied, token):
                return False
            origin = self.headers.get("Origin")
            return origin is None or origin in {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}

        def do_GET(self):
            if not self.allowed_host():
                return self.send(403, {"error": "Недопустимый адрес приложения."})
            parsed = urlsplit(self.path)
            path = parsed.path
            if path == "/api/health":
                return self.send(200, {"service": "botrieltor", "version": "0.1.0"})
            if path == "/api/snapshot":
                if not self.authorized():
                    return self.send(403, {"error": "Откройте приложение заново."})
                return self.send(200, {**store.snapshot(), "runtime": runtime.summary(),
                                       "omniroute": omniroute.snapshot() if omniroute else {"connected": False, "has_key": False, "models": []},
                                       "telegram": telegram.snapshot() if telegram else {"connected": False, "has_token": False}})
            if path.startswith("/api/media/"):
                supplied = parse_qs(parsed.query).get("token", [""])[0]
                if not secrets.compare_digest(supplied, token):
                    return self.send(403, {"error": "Откройте приложение заново."})
                try:
                    media_id = int(path.removeprefix("/api/media/"))
                except ValueError:
                    return self.send(404, {"error": "Фотография не найдена."})
                media = store.get_media_path(media_id)
                if not media or not media.get("local_path"):
                    return self.send(404, {"error": "Фотография не найдена."})
                media_path = Path(media["local_path"]).resolve()
                data_root = store.data_dir.resolve()
                if not media_path.is_relative_to(data_root) or not media_path.is_file():
                    return self.send(404, {"error": "Фотография не найдена."})
                content_type = mimetypes.guess_type(media_path.name)[0] or "application/octet-stream"
                if not content_type.startswith(("image/", "video/")):
                    return self.send(415, {"error": "Неподдерживаемый формат файла."})
                return self.send(200, media_path.read_bytes(), content_type)
            assets = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}
            if path not in assets:
                return self.send(404, {"error": "Не найдено."})
            filename, content_type = assets[path]
            content = (STATIC / filename).read_bytes()
            if filename == "index.html":
                content = content.replace(b"__APP_TOKEN__", token.encode())
            self.send(200, content, content_type)

        def do_POST(self):
            if not self.allowed_host() or not self.authorized():
                return self.send(403, {"error": "Запрос отклонён. Откройте приложение заново."})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 100000:
                    return self.send(413, {"error": "Некорректный размер запроса."})
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("Ожидался объект JSON.")
                path = urlsplit(self.path).path
                if path == "/api/runtime":
                    if body.get("action") == "start":
                        runtime.start()
                    elif body.get("action") == "pause":
                        runtime.pause()
                    else:
                        raise ValueError("Неизвестное действие.")
                elif path == "/api/sources":
                    store.add_source(body.get("name", ""), body.get("kind", "group"))
                    if runtime.active:
                        store.enqueue_collection()
                elif path == "/api/omniroute":
                    if omniroute is None:
                        raise ValueError("Модуль OmniRoute недоступен.")
                    omniroute.configure(body.get("base_url", ""), body.get("model", ""), body.get("api_key", ""), {
                        "archive": body.get("archive_model", ""),
                        "listing": body.get("listing_model", ""),
                        "decision": body.get("decision_model", ""),
                        "publisher": body.get("publisher_model", ""),
                    })
                elif path == "/api/omniroute-test":
                    if omniroute is None:
                        raise ValueError("Модуль OmniRoute недоступен.")
                    omniroute.test()
                elif path == "/api/telegram":
                    if telegram is None:
                        raise ValueError("Модуль Telegram недоступен.")
                    telegram.configure(body.get("channel_id", ""), body.get("token", ""), body.get("message_example"))
                elif path == "/api/telegram-posting":
                    store.set_telegram_posting_enabled(body.get("enabled"))
                elif path == "/api/zalo":
                    if runtime.collector is None:
                        raise ValueError("Драйвер Zalo недоступен.")
                    if body.get("action") == "open":
                        runtime.collector.open()
                    elif body.get("action") == "open_source":
                        source = store.get_source(int(body["source_id"]))
                        if source is None:
                            raise ValueError("Источник не найден.")
                        runtime.collector.bind(source, threading.Event())
                    else:
                        raise ValueError("Неизвестное действие Zalo.")
                elif path == "/api/listings":
                    store.add_listing(body.get("title", ""), body.get("price", ""), body.get("description", ""), body.get("status", "unverified"))
                elif path == "/api/listing-status":
                    listing_id, status, revision = int(body["id"]), body["status"], int(body["revision"])
                    current = store.get_listing_for_publication(listing_id)
                    if status == "available" and current["status"] == "unverified" and publisher is not None:
                        publisher.publish_available(listing_id, revision)
                    else:
                        store.set_listing_status(listing_id, status, revision, origin="manual")
                elif path == "/api/repair-publication":
                    if publisher is None:
                        raise ValueError("Модуль публикации недоступен.")
                    publisher.repair_publication(int(body["id"]))
                elif path == "/api/republish":
                    if publisher is None:
                        raise ValueError("Модуль публикации недоступен.")
                    lid = int(body.get("listing_id") or body.get("id"))
                    publisher.republish(lid)
                elif path == "/api/clear-all-listings":
                    store.clear_all_listings_and_reset_baseline()
                elif path == "/api/delete-listings":
                    ids = body.get("ids")
                    if not isinstance(ids, list):
                        raise ValueError("Выберите объекты для удаления.")
                    store.delete_listings(ids)
                elif path == "/api/delete-sources":
                    ids = body.get("ids")
                    if not isinstance(ids, list):
                        raise ValueError("Выберите источники для удаления.")
                    store.delete_sources(ids)
                elif path == "/api/clear-raw-archive":
                    store.clear_raw_archive()
                elif path == "/api/approve-listings":
                    ids = body.get("ids")
                    if not isinstance(ids, list):
                        raise ValueError("Выберите объекты для подтверждения.")
                    for item_id in ids:
                        try:
                            listing_id = int(item_id)
                            current = store.get_listing_for_publication(listing_id)
                            if current["status"] != "available":
                                store.set_listing_status(listing_id, "available", current["revision"], origin="manual")
                        except Exception:
                            pass
                else:
                    return self.send(404, {"error": "Неизвестное действие."})
                self.send(200, {"ok": True})
            except Conflict as error:
                self.send(409, {"error": str(error)})
            except (ValueError, KeyError, TypeError, RuntimeError) as error:
                self.send(400, {"error": str(error)})
            except Exception:
                self.send(500, {"error": "Не удалось сохранить изменение. Повторите действие после проверки приложения."})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server, token


def _acquire_instance_lock(lock):
    try:
        if os.name == "nt":
            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write(" ")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK, errno.EPERM):
            raise RuntimeError("Приложение уже запущено. Откройте его существующее окно.") from None
        raise


def _node_executable():
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        bundled = Path(bundle_root) / ("node.exe" if os.name == "nt" else "node")
        if bundled.is_file():
            return str(bundled)
    return shutil.which("node") or ("node.exe" if os.name == "nt" else "/usr/local/bin/node")


def run_server(data_dir=None, port=0, parent_pid=None, ready_callback=None):
    data_dir = Path(data_dir or default_data_dir())
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (data_dir / "instance.lock").open("a+")
    try:
        _acquire_instance_lock(lock)
    except RuntimeError:
        lock.close()
        raise

    runtime = None
    server = None
    try:
        store = Store(data_dir)
        store.backup()
        store.recover()
        omniroute = OmniRouteClient(store)
        telegram = TelegramClient(store)
        collector = ZaloAgentCollector(data_dir, node=_node_executable())
        interpreter = RawArchiveInterpreter(store, omniroute)
        listing_builder = ListingBuilder(store, omniroute)
        publisher = TelegramPublisher(store, omniroute, telegram)
        decision_agent = ClaudeDecisionAgent(store, omniroute, publisher)
        runtime = Runtime(store, collector=collector, omniroute=omniroute, interpreter=interpreter,
                          listing_builder=listing_builder, telegram=telegram,
                          decision_agent=decision_agent, publisher=publisher, autostart=True)
        server, _ = build_server(store, runtime, port, omniroute, telegram, publisher)

        def shutdown(*_):
            threading.Thread(target=server.shutdown, daemon=True).start()

        if current_thread() is main_thread():
            signal.signal(signal.SIGTERM, shutdown)
            signal.signal(signal.SIGINT, shutdown)
        if parent_pid:
            def watch_parent():
                while os.getppid() == parent_pid:
                    threading.Event().wait(1)
                shutdown()
            threading.Thread(target=watch_parent, daemon=True).start()

        url = f"http://127.0.0.1:{server.server_port}"
        if ready_callback:
            serving_thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.2},
                name="botrieltor-http",
                daemon=True,
            )
            serving_thread.start()
            ready_callback(url, server.shutdown)
            serving_thread.join()
        else:
            print(json.dumps({"ready": url}), flush=True)
            server.serve_forever(poll_interval=0.2)
    finally:
        if runtime is not None:
            runtime.close()
        if server is not None:
            server.server_close()
        lock.close()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--parent-pid", type=int)
    args = parser.parse_args(argv)
    try:
        run_server(args.data_dir, args.port, args.parent_pid)
    except RuntimeError as error:
        print(json.dumps({"error": str(error)}), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
