from __future__ import annotations

import logging
import os
import queue
import sys
import threading
from pathlib import Path

from botrieltor.paths import default_data_dir
from botrieltor.server import run_server


def _show_error(message: str) -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "BotRieltor", 0x10)
    except Exception:
        print(message, file=sys.stderr)


def main() -> int:
    if os.name != "nt":
        _show_error("Windows-сборку BotRieltor нужно запускать в Windows.")
        return 1

    data_dir = default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=data_dir / "backend.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        encoding="utf-8",
    )

    try:
        import webview
    except ImportError:
        _show_error("Не удалось загрузить интерфейс. Пересоберите BotRieltor на Windows.")
        return 1

    events: queue.Queue = queue.Queue()

    def announce_ready(url, stop_server):
        events.put(("ready", url, stop_server))

    def serve():
        try:
            run_server(data_dir=data_dir, ready_callback=announce_ready)
        except Exception as error:
            logging.exception("Не удалось запустить локальный сервис BotRieltor")
            events.put(("error", str(error), None))

    server_thread = threading.Thread(target=serve, name="botrieltor-server", daemon=True)
    server_thread.start()

    event = None
    while event is None:
        try:
            event = events.get(timeout=0.25)
        except queue.Empty:
            if not server_thread.is_alive():
                _show_error("Локальный сервис BotRieltor остановился. Подробности записаны в backend.log.")
                return 1

    if event[0] == "error":
        _show_error(f"Не удалось запустить BotRieltor: {event[1]}")
        return 1

    _, url, stop_server = event
    try:
        webview.create_window(
            "BotRieltor — Нячанг",
            url,
            width=1190,
            height=830,
            min_size=(860, 650),
            text_select=True,
        )
        webview.start(gui="edgechromium")
    except Exception as error:
        logging.exception("Окно BotRieltor не запустилось")
        _show_error(
            "Не удалось открыть окно BotRieltor. Установите Microsoft Edge WebView2 Runtime "
            f"и запустите приложение снова.\n\nПодробности: {error}"
        )
    finally:
        stop_server()
        server_thread.join(timeout=15)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
