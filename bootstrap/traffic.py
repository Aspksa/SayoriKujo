"""Passive network counters and console output; no network requests or packages."""

import ctypes
import shutil
import sys
import threading
import time


class _Guid(ctypes.Structure):
    _fields_ = [("data1", ctypes.c_uint32), ("data2", ctypes.c_uint16),
                ("data3", ctypes.c_uint16), ("data4", ctypes.c_ubyte * 8)]


class _IfRow(ctypes.Structure):
    # Fixed-width Win32 types also keep the layout correct in cross-platform tests.
    # https://learn.microsoft.com/windows/win32/api/netioapi/ns-netioapi-mib_if_row2
    _fields_ = [
        ("luid", ctypes.c_uint64), ("index", ctypes.c_uint32), ("guid", _Guid),
        ("alias", ctypes.c_uint16 * 257), ("description", ctypes.c_uint16 * 257),
        ("address_length", ctypes.c_uint32), ("address", ctypes.c_ubyte * 32),
        ("permanent_address", ctypes.c_ubyte * 32),
    ] + [(name, ctypes.c_uint32) for name in (
        "mtu", "type", "tunnel_type", "media_type", "physical_medium", "access", "direction",
    )] + [("flags", ctypes.c_ubyte)] + [(name, ctypes.c_uint32) for name in (
        "oper_status", "admin_status", "media_state",
    )] + [("network_guid", _Guid), ("connection_type", ctypes.c_uint32)] + [
        (name, ctypes.c_uint64) for name in (
            "tx_link_speed", "rx_link_speed", "rx", "in_unicast_packets", "in_other_packets",
            "in_discards", "in_errors", "in_unknown", "in_unicast", "in_multicast", "in_broadcast",
            "tx", "out_unicast_packets", "out_other_packets", "out_discards", "out_errors",
            "out_unicast", "out_multicast", "out_broadcast", "queue_length",
        )
    ]


class _IfTable(ctypes.Structure):
    _fields_ = [("count", ctypes.c_uint32), ("rows", _IfRow * 1)]


class WindowsInterfaces:
    """Read physical adapters once per sample, excluding loopback/filter/tunnel layers."""

    def __init__(self):
        if sys.platform != "win32":
            raise OSError("счётчики сетевых адаптеров доступны в Windows")
        self.api = ctypes.WinDLL("iphlpapi.dll")
        self.api.GetIfTable2.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        self.api.GetIfTable2.restype = ctypes.c_uint32
        self.api.FreeMibTable.argtypes = [ctypes.c_void_p]
        self.api.FreeMibTable.restype = None

    def read(self):
        pointer = ctypes.c_void_p()
        error = self.api.GetIfTable2(ctypes.byref(pointer))
        if error:
            raise OSError(error, "не удалось прочитать сетевые счётчики Windows")
        try:
            table = ctypes.cast(pointer, ctypes.POINTER(_IfTable)).contents
            rows = ctypes.cast(pointer.value + _IfTable.rows.offset,
                               ctypes.POINTER(_IfRow * table.count)).contents
            counters = {}
            for row in rows:
                if row.type in (24, 131) or not row.flags & 1 or row.flags & 2:
                    continue
                counters[row.luid] = (row.rx, row.tx)
            if not counters:
                raise OSError("физические сетевые адаптеры не найдены")
            return counters
        finally:
            self.api.FreeMibTable(pointer)


class AdapterSession:
    """Accumulate per-adapter deltas, never adapter lifetime counters."""

    def __init__(self):
        self.previous = None
        self.previous_time = None
        self.received = self.sent = 0
        self.rx_rate = self.tx_rate = 0.0
        self.incomplete = False

    def update(self, counters, now):
        rx = tx = 0
        if self.previous is not None:
            for identity, (received, sent) in counters.items():
                old = self.previous.get(identity)
                if old is None:
                    continue
                if received < old[0] or sent < old[1]:
                    self.incomplete = True
                rx += max(0, received - old[0])
                tx += max(0, sent - old[1])
        elapsed = now - self.previous_time if self.previous_time is not None else 0
        self.rx_rate = rx / elapsed if elapsed > 0 else 0.0
        self.tx_rate = tx / elapsed if elapsed > 0 else 0.0
        self.received += rx
        self.sent += tx
        self.previous = counters.copy()
        self.previous_time = now

    def unavailable(self):
        self.previous = None
        self.previous_time = None
        self.rx_rate = self.tx_rate = 0.0
        self.incomplete = True


class HttpTraffic:
    """Application HTTP bytes read/written, including headers; not TCP overhead."""

    def __init__(self):
        self.lock = threading.Lock()
        self.received = self.sent = 0

    def add(self, direction, amount):
        with self.lock:
            if direction == "rx":
                self.received += amount
            else:
                self.sent += amount

    def snapshot(self):
        with self.lock:
            return self.received, self.sent


class MeteredStream:
    def __init__(self, wrapped, traffic, direction):
        self.wrapped, self.traffic, self.direction = wrapped, traffic, direction

    def __getattr__(self, name):
        return getattr(self.wrapped, name)

    def readline(self, *args):
        data = self.wrapped.readline(*args)
        self.traffic.add(self.direction, len(data))
        return data

    def read(self, *args):
        data = self.wrapped.read(*args)
        self.traffic.add(self.direction, len(data))
        return data

    def write(self, data):
        amount = self.wrapped.write(data)
        self.traffic.add(self.direction, amount or 0)
        return amount


def size_text(amount):
    for unit in ("Б", "КиБ", "МиБ", "ГиБ", "ТиБ"):
        if amount < 1024 or unit == "ТиБ":
            return f"{amount:.1f} {unit}" if unit != "Б" else f"{amount:.0f} Б"
        amount /= 1024


class ConsoleOutput:
    def __init__(self, stream=None):
        self.stream = stream if stream is not None else sys.stdout
        self.interactive = self.stream.isatty()
        self.lock = threading.Lock()
        self.width = 0
        self.sink = None

    def _clear(self):
        if self.width:
            self.stream.write("\r" + " " * self.width + "\r")
            self.width = 0

    def message(self, text):
        with self.lock:
            self._clear()
            self.stream.write(text + "\n")
            self.stream.flush()
        if self.sink is not None:
            self.sink(text)

    def status(self, text):
        if not self.interactive:
            return
        with self.lock:
            self._clear()
            width = max(1, shutil.get_terminal_size((100, 25)).columns - 1)
            text = text[:width]
            self.stream.write("\r" + text)
            self.stream.flush()
            self.width = len(text)


class TrafficMonitor:
    def __init__(self, traffic, console, provider=None, interval=2, on_sample=None):
        self.traffic, self.console = traffic, console
        self.provider, self.interval = provider, interval
        self.session = AdapterSession()
        self.stop_event = threading.Event()
        self.thread = None
        self.error = None
        self.had_sample = False
        self.on_sample = on_sample

    def sample(self):
        try:
            if self.provider is None:
                self.provider = WindowsInterfaces()
            counters = self.provider.read()
            self.session.update(counters, time.monotonic())
            self.error = None
            self.had_sample = True
        except (OSError, AttributeError) as error:
            self.session.unavailable()
            self.error = str(error)
        if self.on_sample is not None:
            self.on_sample(self.snapshot())

    def snapshot(self):
        rx, tx = self.traffic.snapshot()
        return {
            "network_pc": {"received_bytes": self.session.received if self.had_sample else None,
                           "sent_bytes": self.session.sent if self.had_sample else None,
                           "available": self.had_sample and self.error is None,
                           "complete": self.had_sample and not self.session.incomplete and self.error is None},
            "local_http": {"received_bytes": rx, "sent_bytes": tx},
        }

    def status_text(self):
        rx, tx = self.traffic.snapshot()
        if self.error:
            network = "Сеть ПК: нет данных"
        else:
            network = (f"Сеть ПК ↓{self.session.rx_rate * 8 / 1_000_000:.2f} "
                       f"↑{self.session.tx_rate * 8 / 1_000_000:.2f} Мбит/с "
                       f"| учтено {size_text(self.session.received + self.session.sent)}")
        if self.session.incomplete and not self.error:
            network += "*"
        return network + f" | HTTP локально {size_text(rx + tx)}"

    def start(self):
        self.sample()
        self.console.message("[Трафик] Сеть ПК: все программы на физических адаптерах; текущая скорость, без спидтеста.")
        self.console.message("[Трафик] HTTP SayoriKujo — локальный обмен с браузером, включая заголовки. Сервер не обращается в интернет.")
        if self.error:
            self.console.message("[Трафик] Сеть ПК недоступна: " + self.error + ". HTTP учитывается отдельно.")
        else:
            self.console.message("[Трафик] " + self.status_text())
        self.thread = threading.Thread(target=self.run, name="traffic-monitor", daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop_event.wait(self.interval):
            self.sample()
            self.console.status(self.status_text())

    def stop(self):
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=3)
        self.sample()
        rx, tx = self.traffic.snapshot()
        if self.had_sample:
            suffix = " (учёт неполный)" if self.session.incomplete else ""
            self.console.message(f"[Трафик] Сеть ПК за сеанс: принято {size_text(self.session.received)}, "
                                 f"отправлено {size_text(self.session.sent)}, "
                                 f"всего {size_text(self.session.received + self.session.sent)}{suffix}.")
        else:
            self.console.message("[Трафик] Сеть ПК за сеанс: нет данных; расход не подменяется нулём.")
        self.console.message(f"[Трафик] HTTP SayoriKujo локально: принято {size_text(rx)}, "
                             f"отправлено {size_text(tx)}, всего {size_text(rx + tx)}.")
