import threading
import time
import logging

from .store import Store, WAITING_PHOTOS_TTL_SECONDS

PHOTO_BURST_SETTLE_SECONDS = 30
logger = logging.getLogger(__name__)


class Runtime:
    def __init__(self, store: Store, collector=None, omniroute=None, interpreter=None, listing_builder=None,
                 telegram=None, decision_agent=None, publisher=None, autostart=False):
        self.store = store
        self.collector = collector
        self.omniroute = omniroute
        self.interpreter = interpreter
        self.listing_builder = listing_builder
        self.telegram = telegram
        self.decision_agent = decision_agent
        self.publisher = publisher
        self.active = False
        self.closing = threading.Event()
        self.cancel = threading.Event()
        self.cancel.set()
        self.lock = threading.Lock()
        self.last_scan = 0.0
        self.last_photo_expiry = 0.0
        self.photo_expiry_error_reported = False
        self.worker_errors = {}
        self.thread = threading.Thread(target=self.loop, name="collector", daemon=True)
        self.interpreter_thread = threading.Thread(target=self.interpret_loop, name="interpreter", daemon=True)
        self.automation_thread = threading.Thread(target=self.automation_loop, name="automation", daemon=True)
        self.thread.start()
        self.interpreter_thread.start()
        self.automation_thread.start()
        if autostart:
            self.start()

    def start(self):
        seed_complete = False
        with self.lock:
            if self.active:
                return
            # Pause is cooperative: do not clear cancellation until the old job
            # has returned, otherwise a rapid stop/start could revive it.
            with self.store.connect() as db:
                if db.execute("SELECT 1 FROM jobs WHERE state='running'").fetchone():
                    raise ValueError("Завершается текущая операция. Повторите запуск через несколько секунд.")
                if self.collector is not None:
                    db.execute("UPDATE jobs SET state='pending',detail='' WHERE state='blocked'")
                if self.interpreter is not None:
                    db.execute(
                        """UPDATE jobs SET state='pending',detail='Повтор разбора локального архива'
                           WHERE id IN (
                               SELECT max(failed.id) FROM jobs failed
                               WHERE failed.kind='interpret_raw' AND failed.state='failed'
                                 AND NOT EXISTS (
                                     SELECT 1 FROM jobs active
                                     WHERE active.source_id=failed.source_id
                                       AND active.kind=failed.kind
                                       AND active.state IN ('pending','running','blocked'))
                               GROUP BY failed.source_id,failed.kind)""")
                if self.listing_builder is not None:
                    db.execute(
                        """UPDATE jobs SET state='pending',detail='Повтор сборки карточек'
                           WHERE id IN (
                               SELECT max(failed.id) FROM jobs failed
                               WHERE failed.kind='build_listings' AND failed.state='failed'
                                 AND NOT EXISTS (
                                     SELECT 1 FROM jobs active
                                     WHERE active.source_id=failed.source_id
                                       AND active.kind=failed.kind
                                       AND active.state IN ('pending','running','blocked'))
                               GROUP BY failed.source_id,failed.kind)""")
            self.cancel.clear()
            self.active = True
            self.store.enqueue_collection()
            self.store.enqueue_interpretation()
            self.store.enqueue_listing_analysis()
            self.last_scan = time.monotonic()
            self.store.runtime_event("Рабочий режим включён. Выбранный агент принимает решения по новым объектам; подтверждённые объекты публикуются автоматически.")
            seed_complete = self.store.complete_latest_group_seed_if_ready()
        if seed_complete:
            self.pause()

    def pause(self):
        with self.lock:
            self.active = False
            self.cancel.set()
            self.store.runtime_event("Сбор приостановлен. Данные и позиции чтения сохранены.")

    def summary(self):
        with self.store.connect() as db:
            in_flight = bool(db.execute("SELECT 1 FROM jobs WHERE state='running'").fetchone())
        zalo_status = self.collector.status() if self.collector is not None else {"ready": False}
        waiting = self.store.get_settings("waiting_photos").get("waiting_photos")
        workers = {"collector": self.thread.is_alive(),
                   "interpreter": self.interpreter_thread.is_alive(),
                   "automation": self.automation_thread.is_alive()}
        if waiting and waiting.get("count"):
            label = f"📸 Получено {waiting['count']} фото · Ожидание текста..."
        elif not self.active:
            label = "Завершение операции" if in_flight else "Приостановлено"
        elif not workers["collector"]:
            label = "Поток сбора остановлен · перезапустите приложение"
        elif not workers["interpreter"]:
            label = "Поток обработки остановлен · перезапустите приложение"
        elif not workers["automation"]:
            label = "Поток публикации остановлен · перезапустите приложение"
        elif self.collector is None:
            label = "Ожидание подключения Zalo"
        elif self.worker_errors:
            worker, detail = next(iter(self.worker_errors.items()))
            label = f"Повторяю фоновую операцию ({worker}: {detail})"
        else:
            label = "Слежу за группой GINSENG"
        return {"active": self.active, "label": label, "workers": workers,
                "waiting_photos": waiting,
                "connections": {"zalo": bool(zalo_status.get("ready")),
                                "omniroute": bool(self.omniroute and self.omniroute.connected),
                                "telegram": bool(self.telegram and self.telegram.connected)},
                "zalo": zalo_status}

    def _worker_loop(self, name, tick):
        while not self.closing.wait(0.25):
            try:
                tick()
            except Exception as error:
                detail = f"{type(error).__name__}: {error}"
                if self.worker_errors.get(name) != detail:
                    logger.exception("Фоновый поток %s временно прервал цикл; повторю попытку", name)
                    try:
                        self.store.runtime_event(f"Фоновый поток {name} временно прервал операцию ({detail}); повторяю.")
                    except Exception:
                        logger.exception("Не удалось записать состояние фонового потока %s", name)
                self.worker_errors[name] = detail
                self.closing.wait(1.0)
            else:
                self.worker_errors.pop(name, None)

    def loop(self):
        self._worker_loop("сбор", self._collect_once)

    def _collect_once(self):
        if time.monotonic() - self.last_photo_expiry >= 1:
            self.last_photo_expiry = time.monotonic()
            try:
                self.store.expire_waiting_photos(max_age_seconds=WAITING_PHOTOS_TTL_SECONDS)
                self.photo_expiry_error_reported = False
            except Exception as error:
                if not self.photo_expiry_error_reported:
                    self.store.runtime_event(f"Не удалось очистить очередь фото ({type(error).__name__}).")
                    self.photo_expiry_error_reported = True
        with self.lock:
            if not self.active:
                return
            if time.monotonic() - self.last_scan >= 1:
                self.store.enqueue_collection()
                self.last_scan = time.monotonic()
            job = self.store.claim_job(only_kinds=("bind_source", "collect_archive", "collect_initial_scan", "collect_incremental"))
        if not job:
            return
        if self.collector is None:
            self.store.finish_job(job["id"], "blocked", "Нужно подключить управление интерфейсом Zalo. Чтение ещё не выполнялось.")
            return
        try:
            source, cursor = self.store.job_context(job)
            if job["kind"] == "bind_source":
                bound = self.collector.bind(source, self.cancel)
                if self.cancel.is_set():
                    self.store.finish_job(job["id"], "pending", "Продолжить при следующем запуске")
                    return
                self.store.commit_binding(source["id"], bound.external_ref, bound.observed_name, bound.observed_kind)
                self.store.finish_job(job["id"], "done")
                self.store.enqueue_collection()
                return
            collector_mode = ("collect_initial_scan" if job["kind"] in ("collect_archive", "collect_initial_scan")
                              and cursor and cursor.get("initial_scan_cutoff") else job["kind"])
            batch = self.collector.collect(source, collector_mode, cursor, self.cancel)
            if self.cancel.is_set():
                self.store.finish_job(job["id"], "pending", "Продолжить при следующем запуске")
                return
            if hasattr(batch, "captures"):
                captures = batch.captures
                cutoff_reached = bool((batch.cursor or {}).get("cutoff_reached")) if collector_mode == "collect_initial_scan" else False
                reached_history_top = bool((batch.cursor or {}).get("history_top_reached")) if collector_mode == "collect_initial_scan" else False
                # Unknown/non-message UI blocks are excluded by the collector's
                # fixed-window filter. Reaching the chat history top is therefore
                # a safe completion boundary even if Zalo exposes a few blocks
                # without timestamps.
                complete = bool(batch.complete and (cutoff_reached or reached_history_top)) if collector_mode == "collect_initial_scan" else batch.complete
                next_cursor = batch.cursor
                if collector_mode == "collect_initial_scan" and cursor:
                    next_cursor = dict(next_cursor or {})
                    next_cursor.setdefault("initial_scan_cutoff", cursor["initial_scan_cutoff"])
                    next_cursor["bootstrap_pages"] = int(cursor.get("bootstrap_pages", 0)) + 1
                self.store.commit_raw_batch(source["id"], job["kind"], captures, next_cursor, complete=complete)
                self.store.enqueue_interpretation()
                if complete and collector_mode == "collect_initial_scan":
                    self.store.complete_latest_group_seed_if_ready()
                if complete and collector_mode == "collect_initial_scan":
                    self.store.finalize_bootstrap_if_ready(source["id"])
            else:
                # Compatibility for test adapters and future structured imports.
                complete = batch.complete
                next_cursor = batch.cursor
                self.store.commit_batch(source["id"], job["kind"], batch.messages, batch.cursor, complete=batch.complete)
            self.store.finish_job(job["id"], "done" if complete else "pending")
            if (collector_mode == "collect_initial_scan" and cursor and not complete
                    and (reached_history_top or int((next_cursor or {}).get("bootstrap_pages", 0)) >= 100)):
                self.store.finish_job(job["id"], "blocked",
                    "Первичная проверка не достигла надёжной границы 24 часов. История остановлена и публикации заблокированы до ручной сверки.")
                return
            if complete:
                self.store.enqueue_interpretation()
        except Exception as error:
            self.store.finish_job(job["id"], "failed", f"Сбор остановлен: {type(error).__name__} ({error}).")

    def interpret_loop(self):
        self._worker_loop("разбор", self._interpret_once)

    def _interpret_once(self):
        with self.lock:
            if not self.active:
                return
            job = self.store.claim_job(only_kinds=("interpret_raw", "build_listings"),
                                       photo_settle_seconds=PHOTO_BURST_SETTLE_SECONDS)
        if not job:
            return
        worker = self.interpreter if job["kind"] == "interpret_raw" else self.listing_builder
        if worker is None:
            self.store.finish_job(job["id"], "blocked", "Нужно подключить агент разбора через OmniRoute.")
            return
        try:
            source, _ = self.store.job_context(job)
            worker.process(source, self.cancel)
            if self.cancel.is_set():
                self.store.finish_job(job["id"], "pending", "Продолжить при следующем запуске")
            else:
                more = (self.store.has_pending_raw(source["id"]) if job["kind"] == "interpret_raw"
                        else self.store.has_pending_messages(source["id"]))
                self.store.finish_job(job["id"], "done")
                self.store.finalize_bootstrap_if_ready(source["id"])
                if job["kind"] == "interpret_raw":
                    self.store.complete_latest_group_seed_if_ready()
                    self.store.enqueue_listing_analysis()
                if more and job["kind"] == "interpret_raw":
                    self.store.enqueue_interpretation()
                elif more:
                    self.store.enqueue_listing_analysis()
                elif job["kind"] == "build_listings" and self.store.complete_latest_group_seed_if_ready():
                    self.pause()
        except Exception as error:
            self.store.finish_job(job["id"], "failed", f"Разбор архива остановлен: {type(error).__name__} ({error}).")

    def automation_loop(self):
        self._worker_loop("автоматизация", self._automation_once)

    def _automation_once(self):
        with self.lock:
            active = self.active
        if not active:
            return
        if self.publisher is not None and self.publisher.process_outbox():
            return
        if self.decision_agent is not None:
            self.decision_agent.process_next(self.cancel)

    def close(self):
        self.active = False
        self.cancel.set()
        self.closing.set()
        self.thread.join(timeout=5)
        self.interpreter_thread.join(timeout=5)
        self.automation_thread.join(timeout=5)
        if self.collector is not None and hasattr(self.collector, "close"):
            self.collector.close()
        self.store.runtime_event("Приложение закрыто. При следующем открытии сбор будет приостановлен.")
