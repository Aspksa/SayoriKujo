"""Per-session local diagnostics. Each run owns its files; no shared database."""

from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import traceback
import uuid


ID_PATTERN = re.compile(r"[a-f0-9]{32}")
ACTIVE = {"starting", "running"}
STATUSES = {"starting": "Запускается", "running": "Работает", "stopped": "Остановлен",
            "failed": "Ошибка", "interrupted": "Прерван без завершения"}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, data):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.stem + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class SessionLock:
    def __init__(self, path):
        self.stream = path.open("a+b")
        try:
            self.stream.seek(0, os.SEEK_END)
            if not self.stream.tell():
                self.stream.write(b"\0")
                self.stream.flush()
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.stream.close()
            raise

    def close(self):
        if not self.stream.closed:
            try:
                self.stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            finally:
                self.stream.close()


class SessionStore:
    def __init__(self, root):
        self.root = Path(root)
        self.sessions = self.root / "sessions"
        self.logs = self.root / "logs"

    def prepare(self):
        self.sessions.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True)

    def records(self):
        records = []
        if not self.sessions.exists():
            return records
        for path in self.sessions.glob("*.json"):
            if not ID_PATTERN.fullmatch(path.stem):
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                if (isinstance(data, dict) and data.get("id") == path.stem
                        and isinstance(data.get("started_at"), str)):
                    records.append(data)
            except (OSError, ValueError):
                continue
        return sorted(records, key=lambda row: row["started_at"], reverse=True)

    def recover_and_prune(self, max_sessions=200, max_days=30):
        now = datetime.now(timezone.utc)
        completed = 0
        for data in self.records():
            identity = data["id"]
            try:
                started = datetime.fromisoformat(data["started_at"]).astimezone(timezone.utc)
                # PowerShell creates the record before handing execution to Python.
                if data.get("status") == "starting" and now - started < timedelta(minutes=2):
                    continue
                lock = SessionLock(self.sessions / (identity + ".lock"))
            except (OSError, ValueError):
                continue
            try:
                # Read again after locking, so a just-finished run is not overwritten.
                path = self.sessions / (identity + ".json")
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                if data.get("status") in ACTIVE:
                    data["status"] = "interrupted"
                    data["recovered_at"] = utc_now()
                    data["duration_is_final"] = False
                    atomic_json(path, data)
                completed += 1
                if completed > max_sessions or now - started > timedelta(days=max_days):
                    path.unlink(missing_ok=True)
                    for suffix in (".log", ".log.1", ".log.2"):
                        (self.logs / (identity + suffix)).unlink(missing_ok=True)
            except (OSError, ValueError, TypeError):
                pass
            finally:
                lock.close()
            if not (self.sessions / (identity + ".json")).exists():
                try:
                    (self.sessions / (identity + ".lock")).unlink(missing_ok=True)
                except OSError:
                    pass


class Session:
    def __init__(self, store, warning=None, checkpoint_seconds=10):
        self.store = store
        self.warning = warning or (lambda message: None)
        self.checkpoint_seconds = checkpoint_seconds
        self.mutex = threading.RLock()
        self.started_clock = time.monotonic()
        self.last_checkpoint = 0.0
        self.closed = False
        self.enabled = False
        self.warned = False
        self.file_lock = None
        self.identity = uuid.uuid4().hex
        self.boot_seconds = 0.0
        self.data = {"schema_version": 1, "id": self.identity, "started_at": utc_now(),
                     "status": "running", "stages": [], "duration_is_final": False}
        try:
            store.prepare()
            store.recover_and_prune()
            inherited = os.environ.get("SAYORI_SESSION_ID", "")
            if ID_PATTERN.fullmatch(inherited):
                path = store.sessions / (inherited + ".json")
                try:
                    initial = json.loads(path.read_text(encoding="utf-8-sig"))
                    if initial.get("id") == inherited and initial.get("status") == "starting":
                        self.identity = inherited
                        self.data = initial
                        self.boot_seconds = max(0.0, min(3600.0, float(os.environ.get("SAYORI_BOOT_SECONDS", "0"))))
                        imported_after = (datetime.now(timezone.utc) - datetime.fromisoformat(initial["started_at"])).total_seconds()
                        if 0 <= imported_after <= 3600:
                            self.boot_seconds = max(self.boot_seconds, imported_after)
                except (OSError, ValueError, AttributeError):
                    pass
            self.file_lock = SessionLock(store.sessions / (self.identity + ".lock"))
            self.data.update(id=self.identity, status="running", pid=os.getpid())
            self.data.setdefault("stages", [])
            self.enabled = True
            self.checkpoint(force=True)
        except (OSError, ValueError) as error:
            self._disable(error)

    def _disable(self, error):
        self.enabled = False
        if not self.warned:
            self.warned = True
            self.warning("[Журнал] Запись недоступна; запуск продолжается без сохранения: " + str(error))

    def log(self, text, level="INFO"):
        with self.mutex:
            if not self.enabled or self.closed:
                return
            try:
                path = self.store.logs / (self.identity + ".log")
                line = (utc_now() + " [" + level + "] " + text + "\n").encode("utf-8")
                # Three files, up to 256 KiB each, per run. No live traffic spam.
                if path.exists() and path.stat().st_size + len(line) > 256 * 1024:
                    (path.parent / (path.name + ".2")).unlink(missing_ok=True)
                    previous = path.parent / (path.name + ".1")
                    if previous.exists():
                        os.replace(previous, path.parent / (path.name + ".2"))
                    os.replace(path, previous)
                with path.open("ab") as stream:
                    stream.write(line[:256 * 1024])
            except OSError as error:
                self._disable(error)

    def error(self, error):
        self.log("".join(traceback.format_exception(type(error), error, error.__traceback__)), "ERROR")

    def stage(self, name, label, elapsed, status="ok"):
        with self.mutex:
            if self.closed:
                return
            self.data["stages"].append({"name": name, "label": label, "status": status,
                                        "seconds": round(max(0.0, elapsed), 4)})
            self.checkpoint(force=True)

    def checkpoint(self, snapshot=None, force=False):
        with self.mutex:
            if not self.enabled or self.closed:
                return
            now = time.monotonic()
            if snapshot is not None:
                self.data["traffic"] = snapshot
            if not force and now - self.last_checkpoint < self.checkpoint_seconds:
                return
            self.data["last_seen_at"] = utc_now()
            self.data["duration_seconds"] = round(self.boot_seconds + now - self.started_clock, 3)
            try:
                atomic_json(self.store.sessions / (self.identity + ".json"), self.data)
                self.last_checkpoint = now
            except (OSError, ValueError, TypeError) as error:
                self._disable(error)

    def startup_complete(self):
        with self.mutex:
            if self.closed:
                return
            elapsed = self.boot_seconds + time.monotonic() - self.started_clock
            self.data["startup_seconds"] = round(elapsed, 4)
            self.checkpoint(force=True)
            return elapsed

    def finish(self, status="stopped", exit_code=0, snapshot=None):
        with self.mutex:
            if self.closed:
                return
            try:
                self.data.update(status=status, exit_code=exit_code, ended_at=utc_now(), duration_is_final=True)
                self.checkpoint(snapshot, force=True)
            finally:
                self.closed = True
                if self.file_lock is not None:
                    self.file_lock.close()
                if self.enabled:
                    try:
                        self.store.recover_and_prune()
                    except OSError:
                        pass


def history_text(store):
    lines = ["История сеансов SayoriKujo", ""]
    for row in store.records():
        try:
            when = datetime.fromisoformat(row["started_at"]).astimezone().strftime("%d.%m.%Y %H:%M:%S")
            duration = max(0, int(row.get("duration_seconds", 0)))
            status = STATUSES.get(row.get("status"), "Неизвестно")
            lines.append(f"{when} | {status} | {duration // 3600:02}:{duration // 60 % 60:02}:{duration % 60:02} | {row['id'][:8]}")
        except (ValueError, TypeError):
            continue
    if len(lines) == 2:
        lines.append("Сеансов пока нет.")
    return "\n".join(lines)
