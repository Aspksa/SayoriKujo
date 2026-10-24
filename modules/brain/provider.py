"""Cloud.ru transport and user-local API-key storage. Standard library only."""

import base64
import ctypes
import json
import os
from pathlib import Path
import ssl
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener


ENDPOINTS = ("https://foundation-models.api.cloud.ru/v1", "https://inference.api.cloud.ru/v1")


class BrainError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class _Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def dpapi(data, decrypt=False):
    buffer = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = _Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_Blob)]
    function.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise BrainError("Windows не смогла открыть хранилище ключа. Сохраните ключ заново.")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


class KeyStore:
    def __init__(self, path):
        self.path = Path(path)

    @property
    def protection(self):
        return "Windows DPAPI, текущий пользователь" if os.name == "nt" else "Локальный файл с правами 0600"

    def save(self, value):
        if not isinstance(value, str) or not 8 <= len(value.strip()) <= 8192 or any(c.isspace() for c in value.strip()):
            raise BrainError("Укажите Key Secret без пробелов и переносов строк.")
        value = value.strip()
        record = ({"kind": "dpapi", "value": base64.b64encode(dpapi(value.encode())).decode()}
                  if os.name == "nt" else {"kind": "plain", "value": value})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(record, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            if os.name != "nt":
                self.path.chmod(0o600)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def get(self):
        try:
            record = json.loads(self.path.read_text(encoding="utf-8"))
            if record["kind"] == "dpapi":
                if os.name != "nt":
                    raise BrainError("Этот ключ защищён Windows. Введите его на этом компьютере заново.")
                return dpapi(base64.b64decode(record["value"]), decrypt=True).decode()
            if record["kind"] == "plain" and os.name != "nt":
                return str(record["value"])
            raise BrainError("Формат ключа не поддерживается. Сохраните ключ заново.")
        except FileNotFoundError:
            return ""
        except (OSError, ValueError, KeyError, TypeError, UnicodeError):
            raise BrainError("Ключ не удалось прочитать. Сохраните его заново.") from None

    def delete(self):
        self.path.unlink(missing_ok=True)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise BrainError("Cloud.ru перенаправляет запрос. Проверьте выбранный адрес API.", 502)


class CloudClient:
    def __init__(self, endpoint, key, opener=None):
        if endpoint not in ENDPOINTS:
            raise BrainError("Выберите один из адресов Cloud.ru.")
        if not key:
            raise BrainError("Сначала сохраните API-ключ Cloud.ru.")
        self.endpoint, self.key = endpoint, key
        self.opener = opener or build_opener(_NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))

    def request(self, path, payload=None):
        body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        request = Request(self.endpoint + path, data=body, headers={
            "Authorization": "Bearer " + self.key, "Content-Type": "application/json",
            "Accept": "application/json", "User-Agent": "SayoriKujo/1.0",
        })
        try:
            with self.opener.open(request, timeout=75) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise BrainError("Ответ модели слишком велик.", 502)
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise BrainError("Cloud.ru вернул неожиданный формат ответа.", 502)
                return result
        except HTTPError as error:
            messages = {401: "Ключ не принят Cloud.ru. Проверьте Key Secret и сервис ключа.",
                        403: "Ключу не хватает доступа к сервису или модели.",
                        404: "Адрес API или модель не найдены. Обновите список моделей.",
                        429: "Cloud.ru ограничил частоту запросов или квоту. Повторите позже."}
            detail = ""
            if error.code == 400:
                try:
                    record = json.loads(error.read(65536))
                    reason = record.get("error", record)
                    detail = str(reason.get("message", "")) if isinstance(reason, dict) else ""
                except (ValueError, OSError):
                    pass
            detail = detail.replace(self.key, "[ключ скрыт]")[:500]
            message = messages.get(error.code, "Ошибка Cloud.ru, HTTP " + str(error.code) + ".")
            if error.code == 400:
                message = "Модель отклонила запрос (HTTP 400). Проверьте, что выбрана текстовая chat-модель."
                if detail:
                    message += " " + detail
            raise BrainError(message, 502) from None
        except (URLError, TimeoutError, OSError):
            raise BrainError("Нет ответа от Cloud.ru. Проверьте интернет и доступность сервиса.", 502) from None
        except (ValueError, UnicodeError):
            raise BrainError("Cloud.ru вернул некорректный JSON.", 502) from None

    def models(self):
        rows = self.request("/models").get("data", [])
        if not isinstance(rows, list):
            raise BrainError("Список моделей имеет неожиданный формат.", 502)
        results = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                continue
            identity = row["id"]
            label = (identity + " " + str(row.get("type", "")) + " " + str(row.get("task", ""))).casefold()
            non_chat = any(term in label for term in (
                "embedding", "embed-", "bge-", "rerank", "whisper", "gigaasr", "salutespeech",
                "text-to-speech", "speech-to-text", "stable-diffusion", "flux", "image-generation", "tts",
            ))
            results.append({"id": identity, "chat_candidate": not non_chat})
        return sorted(results, key=lambda row: (not row["chat_candidate"], row["id"].casefold()))

    def complete(self, model, messages):
        if not isinstance(model, str) or not model.strip():
            raise BrainError("Выберите модель в настройках Cloud.ru.")
        # Minimal compatible payload: no temperature/max_tokens/tools/JSON-mode assumptions.
        result = self.request("/chat/completions", {"model": model, "messages": messages, "stream": False})
        try:
            content = result["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "\n".join(block.get("text", "") for block in content
                                    if isinstance(block, dict) and block.get("type") == "text")
            if not isinstance(content, str) or not content.strip():
                raise ValueError()
            return content.strip(), result.get("usage", {})
        except (KeyError, IndexError, TypeError, ValueError):
            raise BrainError("Модель не вернула текст ответа. Выберите текстовую chat-модель.", 502) from None
