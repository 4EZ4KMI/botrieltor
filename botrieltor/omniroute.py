from __future__ import annotations

import json
import hashlib
import base64
import ctypes
import os
import subprocess
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .paths import default_data_dir


class FileSecretStore:
    def __init__(self, helper: Path | None = None, service="local.botrieltor.omniroute", secrets_file: Path | None = None):
        self.service = service
        self.secrets_file = secrets_file or (default_data_dir() / ".secrets.json")
        self._lock = threading.Lock()
        self._loaded = False
        self._cached_secret = ""

    def _read_file(self):
        try:
            if self.secrets_file and self.secrets_file.is_file():
                data = json.loads(self.secrets_file.read_text())
                val = data.get(self.service, "")
                if val:
                    if os.name == "nt" and isinstance(val, dict) and val.get("protected") == "dpapi":
                        encrypted = base64.b64decode(val["value"], validate=True)
                        return _dpapi_unprotect(encrypted).decode("utf-8").strip()
                    return str(val).strip()
        except Exception:
            pass
        return ""

    def _write_file(self, secret):
        try:
            data = {}
            if self.secrets_file and self.secrets_file.is_file():
                try:
                    data = json.loads(self.secrets_file.read_text())
                except Exception:
                    pass
            if os.name == "nt":
                encrypted = _dpapi_protect(secret.encode("utf-8"))
                data[self.service] = {"protected": "dpapi", "value": base64.b64encode(encrypted).decode("ascii")}
            else:
                data[self.service] = secret
            if self.secrets_file:
                self.secrets_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                self.secrets_file.write_text(json.dumps(data, indent=2))
                if os.name != "nt":
                    self.secrets_file.chmod(0o600)
        except Exception as error:
            raise ValueError("Не удалось безопасно сохранить ключ локально.") from error

    def get(self):
        with self._lock:
            if not self._loaded:
                self._cached_secret = self._read_file()
                self._loaded = True
            return self._cached_secret

    def set(self, secret):
        secret = str(secret).strip()
        if not secret:
            raise ValueError("API-ключ пуст.")
        with self._lock:
            self._write_file(secret)
            self._cached_secret = secret
            self._loaded = True


def _dpapi_crypt(value: bytes, *, decrypt: bool) -> bytes:
    if os.name != "nt":
        raise OSError("DPAPI доступен только в Windows.")

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    source_buffer = ctypes.create_string_buffer(value)
    source = DataBlob(len(value), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    blob_pointer = ctypes.POINTER(DataBlob)
    crypt32.CryptProtectData.argtypes = [
        blob_pointer, ctypes.c_wchar_p, blob_pointer, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_uint32, blob_pointer,
    ]
    crypt32.CryptProtectData.restype = ctypes.c_int
    crypt32.CryptUnprotectData.argtypes = [
        blob_pointer, ctypes.POINTER(ctypes.c_wchar_p), blob_pointer,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, blob_pointer,
    ]
    crypt32.CryptUnprotectData.restype = ctypes.c_int
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        call = crypt32.CryptUnprotectData
        success = call(ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(result))
    else:
        call = crypt32.CryptProtectData
        success = call(ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(result))
    if not success:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))


def _dpapi_protect(value: bytes) -> bytes:
    return _dpapi_crypt(value, decrypt=False)


def _dpapi_unprotect(value: bytes) -> bytes:
    return _dpapi_crypt(value, decrypt=True)


KeychainSecretStore = FileSecretStore


class OmniRouteClient:
    ALLOWED_PREFIXES = ("agy/", "antigravity/")
    EMERGENCY_MODELS = ()
    MANUAL_CLAUDE_PREFIXES = ("agy/claude-", "antigravity/claude-")
    FALLBACK_MODELS = (
        "agy/gemini-3.7-flash-tiered", "antigravity/gemini-3.7-flash-tiered",
        "agy/gemini-3.7-flash-low", "antigravity/gemini-3.7-flash-low",
        "agy/gemini-3.6-flash-tiered", "antigravity/gemini-3.6-flash-tiered",
        "agy/gemini-3.5-flash-lite", "antigravity/gemini-3.5-flash-lite",
        "agy/gemini-3-flash", "antigravity/gemini-3-flash",
    )
    MIN_REQUEST_INTERVAL = 12.0
    RETRY_DELAYS = (30.0, 60.0)
    FAILURE_COOLDOWN = 120.0
    AGENT_SETTING_KEYS = {
        "archive": "omniroute_archive_model",
        "listing": "omniroute_listing_model",
        "decision": "omniroute_decision_model",
        "publisher": "omniroute_publisher_model",
    }
    DEFAULT_DECISION_MODEL = "agy/gemini-3.7-flash-tiered"

    def __init__(self, store, secrets=None):
        self.store = store
        self.secrets = secrets or KeychainSecretStore()
        self.connected = False
        self.available_models = []
        self.last_error = ""
        self.last_used_model = ""
        self.last_route_reason = ""
        self.fallback_count = 0
        self.rotation_count = 0
        self._request_lock = threading.Lock()
        self._rotation_lock = threading.Lock()
        self._rotation_index = 0
        self._last_request_at = 0.0
        self._cooldown_until = 0.0

    def snapshot(self):
        settings = self.store.get_settings("omniroute_base_url", "omniroute_model", *self.AGENT_SETTING_KEYS.values())
        # Snapshot is polled by the UI and must never read a secret on refresh.
        has_key = self.connected or bool(settings.get("omniroute_model"))
        cooldown_seconds = max(0, int(self._cooldown_until - time.monotonic() + 0.999))
        default_model = settings.get("omniroute_model", "")
        agent_models = {
            "archive": settings.get(self.AGENT_SETTING_KEYS["archive"], "") or default_model,
            "listing": settings.get(self.AGENT_SETTING_KEYS["listing"], "") or default_model,
            "decision": settings.get(self.AGENT_SETTING_KEYS["decision"], "") or self.DEFAULT_DECISION_MODEL,
            "publisher": settings.get(self.AGENT_SETTING_KEYS["publisher"], "") or default_model,
        }
        return {"base_url": settings.get("omniroute_base_url", "http://127.0.0.1:20128"),
                "model": settings.get("omniroute_model", ""), "has_key": has_key,
                "agent_models": agent_models,
                "connected": self.connected, "models": self.available_models, "error": self.last_error,
                "last_used_model": self.last_used_model, "fallback_count": self.fallback_count,
                "rotation_count": self.rotation_count, "last_route_reason": self.last_route_reason,
                "cooldown_seconds": cooldown_seconds}

    def _wait_for_request_slot(self):
        """Serialize model calls and keep request starts below five per minute."""
        with self._request_lock:
            current = time.monotonic()
            ready_at = max(self._cooldown_until, self._last_request_at + self.MIN_REQUEST_INTERVAL)
            if ready_at > current:
                time.sleep(ready_at - current)
            self._last_request_at = time.monotonic()

    def _schedule_backoff(self, attempt, retry_after=None):
        delay = self.RETRY_DELAYS[min(attempt, len(self.RETRY_DELAYS) - 1)]
        try:
            delay = max(delay, min(float(retry_after), self.FAILURE_COOLDOWN))
        except (TypeError, ValueError):
            pass
        self._cooldown_until = max(self._cooldown_until, time.monotonic() + delay)

    def _next_rotation_route(self, primary):
        # OmniRoute distributes the selected provider across its OAuth pool.
        # gweb remains an independent emergency path instead of receiving every
        # second normal request and requiring its browser session unnecessarily.
        return primary

    @classmethod
    def allowed_model(cls, model):
        return bool(str(model).strip())

    @classmethod
    def primary_model(cls, model):
        return cls.allowed_model(model)

    @classmethod
    def manual_claude_model(cls, model):
        return str(model).lower().startswith(cls.MANUAL_CLAUDE_PREFIXES)

    @staticmethod
    def _validate_url(value):
        value = str(value).strip().rstrip("/")
        parsed = urlsplit(value)
        local = parsed.hostname in ("127.0.0.1", "localhost", "::1")
        if parsed.scheme not in (("http", "https") if local else ("https",)) or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("Укажите локальный HTTP-адрес или защищённый HTTPS-адрес OmniRoute.")
        return value[:-3] if value.endswith("/v1") else value

    def configure(self, base_url, model="", api_key="", agent_models=None):
        base_url = self._validate_url(base_url)
        model = str(model).strip()
        agent_models = {key: str(value or "").strip() for key, value in (agent_models or {}).items()}
        if api_key:
            self.secrets.set(api_key)
        if not self.secrets.get():
            raise ValueError("Введите API-ключ OmniRoute.")
        values = {"omniroute_base_url": base_url, "omniroute_model": model or agent_models.get("listing", "")}
        for role, setting_key in self.AGENT_SETTING_KEYS.items():
            if role in agent_models:
                values[setting_key] = agent_models[role]
        self.store.set_settings(**values)
        return self.test()

    def test(self):
        settings = self.store.get_settings("omniroute_base_url", "omniroute_model", *self.AGENT_SETTING_KEYS.values())
        base_url = self._validate_url(settings.get("omniroute_base_url", "http://127.0.0.1:20128"))
        key = self.secrets.get()
        if not key:
            raise ValueError("Введите API-ключ OmniRoute.")
        request = Request(base_url + "/v1/models", headers={"Authorization": "Bearer " + key, "Accept": "application/json"})
        try:
            with urlopen(request, timeout=8) as response:
                payload = json.load(response)
        except HTTPError as error:
            if error.code in (401, 403):
                message = "OmniRoute отклонил API-ключ."
            else:
                message = f"OmniRoute вернул HTTP {error.code}."
            self.connected, self.last_error = False, message
            raise ValueError(message) from None
        except (URLError, TimeoutError, OSError, json.JSONDecodeError):
            self.connected, self.last_error = False, "OmniRoute недоступен по указанному адресу."
            raise ValueError(self.last_error) from None
        models = sorted({str(item.get("id")) for item in payload.get("data", []) if item.get("id")})
        if not models:
            self.connected, self.last_error = False, "OmniRoute не вернул доступных моделей."
            raise ValueError(self.last_error)
        selected = str(settings.get("omniroute_model", "")).strip()
        if selected and selected not in models:
            self.connected, self.last_error = False, "Выбранная модель отсутствует в OmniRoute."
            raise ValueError(self.last_error)
        if not selected:
            selected = next((value for value in models if self.primary_model(value)), "")
            if not selected:
                raise ValueError("OmniRoute не вернул доступных моделей.")
            self.store.set_settings(omniroute_model=selected)
        configured_agents = {
            role: str(settings.get(setting_key, "")).strip()
            for role, setting_key in self.AGENT_SETTING_KEYS.items()
        }
        for role in ("archive", "listing", "publisher"):
            value = configured_agents[role]
            if value and value not in models:
                raise ValueError(f"Выбранная модель агента «{role}» отсутствует в OmniRoute.")
        decision_model = configured_agents["decision"]
        if decision_model and decision_model not in models:
            raise ValueError("Выбранная модель агента решений отсутствует в OmniRoute.")
        self.available_models, self.connected, self.last_error = models, True, ""
        self.store.runtime_event("OmniRoute подключён. Модели агентов настроены.")
        return self.snapshot()

    def chat(self, messages, *, model=None, temperature=0, validator=None):
        """Single model gateway for every agent. Never call providers directly."""
        settings = self.store.get_settings("omniroute_base_url", "omniroute_model")
        primary = model or settings["omniroute_model"]
        if not self.primary_model(primary):
            raise ValueError("Модель агента не выбрана.")
        if not self.available_models:
            self.test()
        dispatch = primary if model else self._next_rotation_route(primary)
        rotated = dispatch != primary
        suffix = dispatch.split("/", 1)[-1]
        preferred = [dispatch, primary]
        preferred.extend(value for value in self.available_models if value.split("/", 1)[-1] == suffix)
        preferred.extend(self.FALLBACK_MODELS)
        candidates = []
        for value in preferred:
            if value in self.available_models and self.allowed_model(value) and value not in candidates:
                candidates.append(value)
        candidates = candidates[:8]
        last_error = None
        for candidate in candidates:
            payload = json.dumps({"model": candidate, "messages": messages, "temperature": temperature}).encode()
            request = Request(self._validate_url(settings["omniroute_base_url"]) + "/v1/chat/completions",
                              data=payload, method="POST",
                              headers={"Authorization": "Bearer " + self.secrets.get(), "Content-Type": "application/json",
                                       "Idempotency-Key": hashlib.sha256(payload).hexdigest()})
            try:
                self._wait_for_request_slot()
                with urlopen(request, timeout=50) as response:
                    result = json.load(response)
                if validator is not None:
                    try:
                        validator(result)
                    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
                        last_error = error
                        self.store.runtime_event(f"OmniRoute: модель {candidate} вернула непригодный ответ; пробуем резервный маршрут.")
                        continue
                self._cooldown_until = 0.0
                self.last_used_model = candidate
                if rotated and candidate == dispatch:
                    self.rotation_count += 1
                    self.last_route_reason = "rotation"
                elif candidate != dispatch:
                    self.fallback_count += 1
                    self.last_route_reason = "fallback"
                    self.store.runtime_event(f"OmniRoute: временный сбой {dispatch}; агент продолжил через {candidate}.")
                else:
                    self.last_route_reason = "primary"
                return result
            except HTTPError as error:
                if error.code not in (404, 408, 429, 500, 502, 503, 504):
                    raise
                code = error.code
                try:
                    error.read(65536)
                except Exception:
                    pass
                error.close()
                last_error = RuntimeError(f"OmniRoute вернул временный HTTP {code}.")
            except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
                last_error = error
        self._cooldown_until = max(self._cooldown_until, time.monotonic() + self.FAILURE_COOLDOWN)
        raise RuntimeError("Все доступные маршруты выбранной модели временно недоступны.") from last_error

    def one_off_claude(self, messages, *, model="agy/claude-opus-4-6-thinking-medium", temperature=0):
        """One explicit expensive call. Never selected by background agents or retries."""
        if not self.manual_claude_model(model):
            raise ValueError("Разовый сложный разбор разрешён только через Claude в agy/ или antigravity/.")
        if not self.available_models:
            self.test()
        if model not in self.available_models:
            raise ValueError("Выбранная Claude-модель отсутствует в OmniRoute.")
        settings = self.store.get_settings("omniroute_base_url")
        payload = json.dumps({"model": model, "messages": messages, "temperature": temperature}).encode()
        request = Request(self._validate_url(settings["omniroute_base_url"]) + "/v1/chat/completions",
                          data=payload, method="POST",
                          headers={"Authorization": "Bearer " + self.secrets.get(), "Content-Type": "application/json",
                                   "Idempotency-Key": hashlib.sha256(payload).hexdigest()})
        try:
            self._wait_for_request_slot()
            with urlopen(request, timeout=120) as response:
                result = json.load(response)
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            raise RuntimeError("Разовый запрос Claude не выполнен и не будет повторён автоматически.") from error
        self.last_used_model = model
        self.last_route_reason = "manual"
        return result

    def claude_decision(self, messages, *, model="agy/gemini-3.7-flash-tiered", temperature=0):
        """Run the autonomous publication/status decision agent through its selected model."""
        if not self.allowed_model(model):
            raise ValueError("Модель агента решений не выбрана.")
        if not self.available_models:
            self.test()
        suffix = model.split("/", 1)[-1]
        preferred = [model]
        preferred.extend(value for value in self.available_models if value.split("/", 1)[-1] == suffix)
        candidates = []
        for value in preferred:
            if value in self.available_models and self.allowed_model(value) and value not in candidates:
                candidates.append(value)
        if not candidates:
            raise ValueError("Выбранная модель агента решений отсутствует в OmniRoute.")
        settings = self.store.get_settings("omniroute_base_url")
        last_error = None
        for candidate in candidates[:4]:
            payload = json.dumps({"model": candidate, "messages": messages, "temperature": temperature}).encode()
            request = Request(self._validate_url(settings["omniroute_base_url"]) + "/v1/chat/completions",
                              data=payload, method="POST",
                              headers={"Authorization": "Bearer " + self.secrets.get(), "Content-Type": "application/json",
                                       "Idempotency-Key": hashlib.sha256(payload).hexdigest()})
            try:
                self._wait_for_request_slot()
                with urlopen(request, timeout=120) as response:
                    result = json.load(response)
                self.last_used_model = candidate
                self.last_route_reason = "claude"
                return result
            except HTTPError as error:
                if error.code not in (404, 408, 429, 500, 502, 503, 504):
                    raise
                try:
                    error.read(65536)
                except Exception:
                    pass
                error.close()
                last_error = error
            except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
                last_error = error
        raise RuntimeError("Все маршруты выбранной модели решений временно недоступны.") from last_error
