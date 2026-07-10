# -*- coding: utf-8 -*-
import sys, os, json, subprocess, time, winreg, random, ctypes, traceback, shlex, shutil, socket, uuid, base64, math
import urllib.request, urllib.parse, tempfile, threading
# Должно быть выставлено ДО импорта QtWebEngineWidgets — иначе Chromium уже
# инициализирует GPU-композитор со стандартными флагами. На некоторых связках
# Windows/видеодрайвер GPU-композитор QtWebEngine падает при выгрузке процесса
# ("QDxgiVSyncService not destroyed in time"), роняя всё приложение без трассировки
# и без записи в журнал событий Windows — отключаем GPU-рендеринг для встроенного
# WebEngine (используется только для декоративной страницы "Динозаврик").
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtCore import QUrl
from datetime import datetime
from pathlib import Path

from PyQt6.QtWidgets import *
from PyQt6.QtCore import *
from PyQt6.QtGui import *

# Папка рядом с .exe (или .py при разработке) — для конфига и записываемых файлов
if getattr(sys, 'frozen', False):
    APP_DIR = Path(sys.executable).parent
    RES_DIR = Path(sys._MEIPASS)
else:
    APP_DIR = Path(__file__).parent
    RES_DIR = Path(__file__).parent

CONFIG_FILE = APP_DIR / "manager_config.json"

# Лог каждого бота на диске — окно логов в UI (QTextEdit) живёт только в памяти
# и теряется при перезапуске BotManager, так что если бот падает без трейсбека
# в консоли (например, процесс убит извне при обрыве VPN), разобраться постфактум
# было нечем. Пишем сюда те же строки, что летят в add_log(), с ротацией по размеру.
LOGS_DIR = APP_DIR / "logs"
MAX_BOT_LOG_BYTES = 2 * 1024 * 1024  # 2 МБ на бота — этого хватает поймать трейсбек/контекст падения

def _append_bot_log(bot_id, ts, line):
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        path = LOGS_DIR / f"{bot_id}.log"
        if path.exists() and path.stat().st_size > MAX_BOT_LOG_BYTES:
            path.write_bytes(path.read_bytes()[-MAX_BOT_LOG_BYTES // 2:])
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {line}\n")
    except Exception:
        pass

def split_bot_args(args_str):
    """Разбивает строку аргументов бота на список, поддерживая кавычки для
    аргументов с пробелами. Это Windows-путь, а не POSIX shell, поэтому '\\'
    не должен интерпретироваться как escape-символ (иначе '--config C:\\a\\b.json'
    ломается) — используем shlex с отключённым escape вместо plain shlex.split().
    Кавычки (") при этом всё ещё работают как разделитель (нужно для аргументов
    с пробелами вроде --name "My Bot") и поэтому вырезаются из результата —
    буквальный символ " внутри аргумента (не как разделитель) так же тихо
    пропадёт. Без явного escape-механизма нельзя одновременно поддержать оба
    случая, а escape вернул бы исходный баг с '\\', поэтому это осознанный
    компромисс, а не недосмотр."""
    if not args_str:
        return []
    try:
        lex = shlex.shlex(args_str, posix=True)
        lex.whitespace_split = True
        lex.escape = ""
        return list(lex)
    except ValueError:
        return args_str.split()

# ── Windows Job Object: убивает все дочерние процессы при закрытии BotManager ─
_job_handle = None

def _init_job_object():
    global _job_handle
    try:
        import ctypes.wintypes
        kernel32 = ctypes.windll.kernel32
        _job_handle = kernel32.CreateJobObjectW(None, None)
        if not _job_handle:
            return
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JobObjectExtendedLimitInformation   = 9

        class _BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit",     ctypes.c_int64),
                ("LimitFlags",             ctypes.wintypes.DWORD),
                ("MinimumWorkingSetSize",  ctypes.c_size_t),
                ("MaximumWorkingSetSize",  ctypes.c_size_t),
                ("ActiveProcessLimit",     ctypes.wintypes.DWORD),
                ("Affinity",               ctypes.c_size_t),
                ("PriorityClass",          ctypes.wintypes.DWORD),
                ("SchedulingClass",        ctypes.wintypes.DWORD),
            ]

        class _IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in (
                "ReadOperationCount","WriteOperationCount","OtherOperationCount",
                "ReadTransferCount","WriteTransferCount","OtherTransferCount",
            )]

        class _EXT(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BASIC),
                ("IoInfo",                _IO),
                ("ProcessMemoryLimit",    ctypes.c_size_t),
                ("JobMemoryLimit",        ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed",     ctypes.c_size_t),
            ]

        info = _EXT()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        kernel32.SetInformationJobObject(
            _job_handle, JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info)
        )
    except Exception:
        _job_handle = None

def _assign_to_job(pid):
    if not _job_handle:
        return
    try:
        kernel32 = ctypes.windll.kernel32
        PROCESS_ALL_ACCESS = 0x1F0FFF
        h = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
        if h:
            kernel32.AssignProcessToJobObject(_job_handle, h)
            kernel32.CloseHandle(h)
    except Exception:
        pass

_init_job_object()

# Строки в логе бота, которые НЕ считаем ошибкой (обычные обрывы сети со штатным
# автопереподключением) — редактируются через manager_config.json без пересборки .exe.
DEFAULT_BENIGN_ERROR_PATTERNS = ["CONNECTION TO REMOTE HOST WAS LOST", "PING PONG FAILED", "GETADDRINFO FAILED", "ATTEMPTING A RECONNECT", "READ TIMED OUT", "UNEXPECTED_EOF_WHILE_READING", "CANNOT WRITE TO CLOSING TRANSPORT", "WINERROR 10060", "BAD GATEWAY", "SESSION UNUSED", "PERSISTEDQUERYNOTFOUND", "TIMED OUT. (CONNECT TIMEOUT", "UNABLE TO DETECT IF YOU HAVE THE LATEST VERSION", "ОШИБКА ПОДКЛЮЧЕНИЯ К TELEGRAM"]
BENIGN_ERROR_PATTERNS = list(DEFAULT_BENIGN_ERROR_PATTERNS)

# Строки, которые вообще не показываем в окне лога бота (визуальный шум от
# сторонних библиотек вроде Twitch-Channel-Points-Miner-v2) — в отличие от
# BENIGN_ERROR_PATTERNS, эти строки не просто перестают считаться ошибкой, а
# скрываются из UI целиком. В файл на диске (LOGS_DIR) всё равно попадают —
# скрываем только отображение, не историю. Тоже редактируется через
# manager_config.json (ключ "hidden_log_patterns") без пересборки .exe.
DEFAULT_HIDDEN_LOG_PATTERNS = [
    "ERROR WHILE TRYING TO SEND MINUTE WATCHED",
    "INVALID RESPONSE FROM TWITCH",
    "PING PONG FAILED",
    "ON_CLOSE]: #0 - WEBSOCKET ЗАКРЫТ",
    "ПЕРЕПОДКЛЮЧЕНИЕ К СЕРВЕРУ TWITCH PUBSUB",
    "ERROR WITH GQLOPERATIONS",
]
HIDDEN_LOG_PATTERNS = list(DEFAULT_HIDDEN_LOG_PATTERNS)

def load_config():
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            migrate_vless_config(cfg)
            cfg.setdefault("settings", {}).setdefault("vless_autostart", True)
            return cfg
        except: pass
    # Дефолтная форма "vless" берётся из migrate_vless_config (передаём cfg без
    # ключа "vless"), а не дублируется тут отдельным литералом — раньше это были
    # два независимых места с одной и той же формой, которые рисковали разъехаться
    # при добавлении нового поля в будущем.
    return migrate_vless_config({"bots": [], "settings": {"autostart_app":False,"autostart_bots":True,
        "win_notifications":True,"sound":False,"minimize_to_tray":True,"auto_restart":True,
        "vless_autostart":True,
        "benign_error_patterns":list(DEFAULT_BENIGN_ERROR_PATTERNS),
        "hidden_log_patterns":list(DEFAULT_HIDDEN_LOG_PATTERNS)}})

def migrate_vless_config(cfg):
    """Переводит старый формат vless (одна строка raw_input) на список профилей,
    и следит, чтобы у каждого профиля был стабильный id (для старых профилей без
    id, сохранённых до его введения, — генерирует новый)."""
    v = cfg.get("vless", {})
    if "profiles" not in v:
        old_raw = (v.get("raw_input") or "").strip()
        profiles = [{"name": "Профиль 1", "raw_input": old_raw}] if old_raw else []
        cfg["vless"] = {"profiles": profiles, "active": 0 if profiles else -1,
                         "enabled": v.get("enabled", False)}
        v = cfg["vless"]
    for p in v.get("profiles", []):
        p.setdefault("id", str(uuid.uuid4()))
    v.setdefault("subscriptions", [])
    return cfg

def find_profile_index(profiles, profile_id):
    """Ищет позицию профиля по стабильному id (не по raw_input — он может
    совпадать у двух разных профилей — и не по позиционному индексу, который
    мог сместиться, пока где-то ждали результат асинхронной проверки).
    Возвращает -1, если профиль с таким id не найден (например, его удалили,
    пока шла проверка)."""
    return next((i for i, p in enumerate(profiles) if p.get("id") == profile_id), -1)

def save_config(cfg):
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"ERROR: не удалось сохранить конфиг: {e}")

# ── VLESS / Xray-прокси ─────────────────────────────────────────────────────────
XRAY_DIR = APP_DIR / "xray"
XRAY_EXE = XRAY_DIR / "xray.exe"
XRAY_CONFIG_FILE = XRAY_DIR / "config.json"
XRAY_SOCKS_PORT = 28808  # намеренно не 10808/1080 — эти порты часто заняты другими VPN-клиентами (Happ и т.п.)

# Файл-метка с текущим портом SOCKS5 — читают bot.py/BotTwitch01.py (на этой же
# машине), чтобы не хардкодить порт у себя и не требовать ручной синхронизации
# при каждом изменении XRAY_SOCKS_PORT здесь.
SOCKS_PORT_FILE = Path(tempfile.gettempdir()) / "BotManager_socks_port.txt"
try:
    SOCKS_PORT_FILE.write_text(str(XRAY_SOCKS_PORT), encoding="utf-8")
except Exception:
    pass

# Публичная открытая база geoip/geosite (проект Loyalsoldier/v2ray-rules-dat) —
# нужна Xray для правил маршрутизации вида geoip:ru / geosite:category-ru.
# Не хранится в самой программе — скачивается один раз при первом подключении.
GEOIP_URL = "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat"
GEOSITE_URL = "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat"

def ensure_geo_files(log_fn=None):
    """Скачивает geoip.dat/geosite.dat, если их ещё нет рядом с xray.exe."""
    XRAY_DIR.mkdir(parents=True, exist_ok=True)
    for url, dest in ((GEOIP_URL, XRAY_DIR / "geoip.dat"), (GEOSITE_URL, XRAY_DIR / "geosite.dat")):
        if dest.exists():
            continue
        if log_fn: log_fn(f"Скачиваю {dest.name}...")
        tmp = dest.with_suffix(".tmp")
        try:
            with urllib.request.urlopen(url, timeout=20) as resp, open(tmp, "wb") as f:
                shutil.copyfileobj(resp, f)
            tmp.replace(dest)
            if log_fn: log_fn(f"{dest.name} загружен")
        except Exception as e:
            try: tmp.unlink()
            except Exception: pass
            if log_fn: log_fn(f"✗ Не удалось скачать {dest.name}: {e}")

# ── Флаги стран для профилей ────────────────────────────────────────────────────
# Qt на Windows не умеет рисовать эмодзи-флаги (пара Regional Indicator Symbols) —
# показывает двухбуквенный код текстом вместо картинки. Поэтому иконку берём
# отдельно с flagcdn.com по ISO-коду и кэшируем локально.
FLAGS_DIR = APP_DIR / "flags"
FLAGCDN_URL_TMPL = "https://flagcdn.com/24x18/{code}.png"

def extract_flag_code(name):
    """Если имя начинается с эмодзи-флага (пара Regional Indicator Symbols),
    возвращает (код_ISO, остаток_имени_без_флага), иначе (None, имя). Ведущие
    пробелы перед флагом игнорируются — иначе " 🇷🇺 Russia" (с пробелом,
    например из-за URL-декодирования) не распознаётся как RU и российский
    сервер тихо проходит мимо фильтра EXCLUDED_COUNTRIES."""
    if not name:
        return None, name
    stripped = name.lstrip()
    if len(stripped) < 2:
        return None, name
    a, b = ord(stripped[0]), ord(stripped[1])
    if 0x1F1E6 <= a <= 0x1F1FF and 0x1F1E6 <= b <= 0x1F1FF:
        code = chr(a - 0x1F1E6 + ord('A')) + chr(b - 0x1F1E6 + ord('A'))
        return code, stripped[2:].lstrip()
    return None, name

def flag_path_for(code):
    return FLAGS_DIR / f"{code.lower()}.png"

def ensure_flag(code):
    """Скачивает иконку флага с flagcdn.com в кэш, если её ещё нет. Возвращает путь
    к файлу или None при ошибке."""
    path = flag_path_for(code)
    if path.exists():
        return path
    try:
        FLAGS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with urllib.request.urlopen(FLAGCDN_URL_TMPL.format(code=code.lower()), timeout=8) as resp, open(tmp, "wb") as f:
            shutil.copyfileobj(resp, f)
        tmp.replace(path)
        return path
    except Exception:
        try: tmp.unlink()
        except Exception: pass
        return None

def parse_vless_uri(uri):
    """Разбирает ссылку vless://uuid@host:port?params#remark в конфиг Xray-core
    с локальным SOCKS5-инбаундом (для остальных ботов) и VLESS-аутбаундом."""
    u = urllib.parse.urlparse(uri)
    if u.scheme != "vless":
        raise ValueError("Ссылка должна начинаться с vless://")
    user_id = u.username
    host = u.hostname
    port = u.port
    if not user_id or not host or not port:
        raise ValueError("В ссылке нет UUID/хоста/порта")
    q = urllib.parse.parse_qs(u.query)
    def qget(*names, default=""):
        for n in names:
            if n in q and q[n]:
                return q[n][0]
        return default

    security = qget("security", default="none")
    network = qget("type", default="tcp")
    stream_settings = {"network": network}

    if security == "reality":
        stream_settings["security"] = "reality"
        stream_settings["realitySettings"] = {
            "show": False,
            "fingerprint": qget("fp", default="chrome"),
            "serverName": qget("sni", "host", default=host),
            "publicKey": qget("pbk"),
            "shortId": qget("sid", default=""),
            "spiderX": qget("spx", default="/"),
        }
    elif security == "tls":
        stream_settings["security"] = "tls"
        stream_settings["tlsSettings"] = {
            "serverName": qget("sni", "host", default=host),
            "fingerprint": qget("fp", default="chrome"),
            "allowInsecure": False,
        }
    else:
        stream_settings["security"] = "none"

    if network == "tcp":
        stream_settings["tcpSettings"] = {"header": {"type": qget("headerType", default="none")}}
    elif network == "ws":
        stream_settings["wsSettings"] = {"path": qget("path", default="/"), "headers": {"Host": qget("host", default=host)}}
    elif network == "grpc":
        stream_settings["grpcSettings"] = {"serviceName": qget("serviceName", default="")}
    elif network == "xhttp":
        xhttp_settings = {
            "host": qget("host", default=host),
            "path": qget("path", default="/"),
            "mode": qget("mode", default="auto"),
        }
        extra_raw = qget("extra", default="")
        if extra_raw:
            try:
                xhttp_settings["extra"] = json.loads(extra_raw)
            except Exception:
                pass
        stream_settings["xhttpSettings"] = xhttp_settings

    flow = qget("flow", default="")
    user = {"id": user_id, "encryption": qget("encryption", default="none"), "level": 0}
    if flow:
        user["flow"] = flow

    # TCP keepalive на самом VLESS-соединении: NAT/файрволы по пути часто тихо
    # обрывают долго простаивающие соединения (например, long-poll Telegram),
    # не сообщая об этом ни одной стороне — приложение узнаёт об обрыве только
    # когда пытается что-то отправить и получает таймаут. Периодические
    # keepalive-пакеты поддерживают состояние в NAT-таблицах по пути и позволяют
    # обнаружить реально мёртвое соединение быстрее, не дожидаясь таймаута на
    # уровне бота.
    # tcpFastOpen сокращает задержку установки TCP-соединения на один RTT —
    # немного ускоряет переподключение после разрыва.
    # tcpKeepAliveIdle задаёт, через сколько секунд простоя шлётся ПЕРВЫЙ
    # keepalive-пакет — без него interval на части платформ фактически не
    # включает keepalive вовсе (нужны оба параметра).
    stream_settings["sockopt"] = {"tcpKeepAliveIdle": 60, "tcpKeepAliveInterval": 30, "tcpFastOpen": True}

    return {
        "log": {"loglevel": "warning"},
        # По умолчанию xray сам закрывает соединение после 300с простоя (connIdle) —
        # для ботов с редким трафиком это выглядит как беспричинный обрыв связи раз
        # в 5 минут. Поднимаем до 30 минут; handshake до 8с — на нестабильной сети
        # установка TLS/Reality-соединения может не укладываться в дефолтные 4с.
        "policy": {"levels": {"0": {"handshake": 8, "connIdle": 1800}}},
        "inbounds": [
            {
                "listen": "127.0.0.1",
                "port": XRAY_SOCKS_PORT,
                "protocol": "socks",
                "settings": {"auth": "noauth", "udp": True},
                "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]},
                "tag": "socks",
            }
        ],
        "outbounds": [
            {
                "protocol": "vless",
                "settings": {"vnext": [{"address": host, "port": port, "users": [user]}]},
                "streamSettings": stream_settings,
                "tag": "proxy",
            },
            {"protocol": "freedom", "settings": {}, "tag": "direct"},
        ],
    }

def build_xray_config(raw_input):
    """Принимает либо ссылку vless://..., либо готовый JSON-конфиг Xray (вставленный
    целиком) — и возвращает итоговый dict-конфиг с гарантированно известным нам
    портом SOCKS5-инбаунда (XRAY_SOCKS_PORT), чтобы остальные боты всегда знали,
    куда стучаться, независимо от того, что было в исходном конфиге."""
    raw_input = raw_input.strip()
    if not raw_input:
        raise ValueError("Пустая ссылка/конфиг")
    if raw_input.startswith("vless://"):
        return parse_vless_uri(raw_input)
    # Иначе считаем, что вставлен готовый JSON-конфиг Xray (например, экспортированный
    # из другого клиента вроде Happ) — нам из него нужны только outbounds/routing/dns,
    # а инбаунды заменяем на единственный свой SOCKS5. Чужие инбаунды (например,
    # внутренний API-порт того клиента) не нужны и могут конфликтовать по портам,
    # если тот клиент запущен параллельно.
    cfg = json.loads(raw_input)
    socks_inbounds = [i for i in cfg.get("inbounds", []) if i.get("protocol") == "socks"]
    if socks_inbounds:
        inbound = socks_inbounds[0]
        inbound["port"] = XRAY_SOCKS_PORT
        inbound["listen"] = "127.0.0.1"
        cfg["inbounds"] = [inbound]
    else:
        cfg["inbounds"] = [{
            "listen": "127.0.0.1", "port": XRAY_SOCKS_PORT, "protocol": "socks",
            "settings": {"auth": "noauth", "udp": True},
            "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]},
            "tag": "socks",
        }]
    # Многие экспортированные конфиги (например, из Happ) настраивают DNS только
    # через DoH (Cloudflare/Google). Некоторые провайдеры периодически на короткое
    # время режут именно HTTPS к самим DoH-резолверам — тогда домены (включая
    # Telegram) не резолвятся, хотя VLESS-туннель жив, и это выглядит как обрыв
    # связи у ботов. Добавляем обычный DNS как запасной — Xray пробует серверы по
    # порядку и переходит к следующему при ошибке/таймауте.
    dns_cfg = cfg.get("dns")
    if isinstance(dns_cfg, dict):
        servers = dns_cfg.setdefault("servers", [])
        plain_present = {s for s in servers if isinstance(s, str)}
        # Два независимых резервных DNS (не DoH) — если временная проблема у
        # одного (например, у Google) совпадёт с проверкой, второй (Cloudflare)
        # всё ещё может ответить.
        for fallback in ("8.8.8.8", "1.1.1.1"):
            if fallback not in plain_present:
                servers.append(fallback)
        # api.telegram.org — самый горячий домен для ботов, и каждый его резолв для
        # маршрутизации шёл через DoH (сам DoH — через туннель): на любую заминку
        # DoH-сервера все обращения к Telegram вставали до таймаута (наблюдалось
        # вживую: "failed to retrieve response for api.telegram.org ... context
        # deadline exceeded" ровно в момент "Ошибка подключения к Telegram" у ботов).
        # Закрепляем IP статически (тот же приём, каким конфиги Happ уже закрепляют
        # cloudflare-dns.com/dns.google) — резолв мгновенный и не зависит от DoH.
        # Безопасно: адрес анйкаст-фронта Telegram стабилен годами, и он влияет
        # только на ВЫБОР МАРШРУТА (домен всё равно передаётся VLESS-серверу и
        # резолвится удалённо заново) — даже устаревший IP не сломает соединение.
        hosts = dns_cfg.setdefault("hosts", {})
        hosts.setdefault("api.telegram.org", "149.154.167.220")
    # TCP keepalive на самом VLESS-соединении (см. parse_vless_uri) — NAT/файрволы
    # по пути часто тихо обрывают долго простаивающие соединения (long-poll
    # Telegram и т.п.), не сообщая об этом ни одной стороне.
    for ob in cfg.get("outbounds", []):
        if ob.get("protocol") == "vless":
            ss = ob.setdefault("streamSettings", {})
            sockopt = ss.setdefault("sockopt", {})
            sockopt.setdefault("tcpKeepAliveIdle", 60)
            sockopt.setdefault("tcpKeepAliveInterval", 30)
            sockopt.setdefault("tcpFastOpen", True)
    # connIdle=300 (дефолт xray) закрывает простаивающие соединения через 5 минут —
    # для ботов с редким трафиком это выглядит как беспричинные обрывы. Сливаем
    # аккуратно НА УРОВНЕ ПОЛЕЙ: конфиги из Happ обычно уже несут свою policy
    # (статистика трафика и т.п.) без connIdle у level 0 — заменять весь блок
    # нельзя (потеряется статистика), а setdefault всего блока не добавил бы
    # connIdle вовсе. Явно заданные пользователем значения не перетираются.
    lvl0 = cfg.setdefault("policy", {}).setdefault("levels", {}).setdefault("0", {})
    lvl0.setdefault("handshake", 8)
    lvl0.setdefault("connIdle", 1800)
    return cfg

def collect_backup_uris(xr_cfg, profiles, limit=2):
    """Выбирает до `limit` запасных vless://-профилей для балансировщика —
    исключая сервер, на который уже указывает главный аутбаунд конфига, и
    дубликаты по host:port (в подписках один сервер часто встречается дважды
    как TCP/WS-варианты — второй такой ничего не резервирует)."""
    main = next((o for o in xr_cfg.get("outbounds", []) if o.get("protocol") == "vless"), None)
    if main is None:
        return []
    try:
        vn = main["settings"]["vnext"][0]
        seen = {(vn.get("address"), vn.get("port"))}
    except Exception:
        seen = set()
    out = []
    for p in profiles:
        uri = (p.get("raw_input") or "").strip()
        if not uri.startswith("vless://"):
            continue  # JSON-конфиги как резерв не берём: у них своя маршрутизация,
                      # которую нельзя слить с активной без конфликтов
        ep = extract_vless_endpoint(uri)
        if not ep or ep in seen:
            continue
        seen.add(ep)
        out.append(uri)
        if len(out) >= limit:
            break
    return out

def add_failover_balancer(cfg, backup_uris):
    """Мгновенный failover ВНУТРИ xray вместо (точнее — поверх) внешнего:
    добавляет в конфиг запасные VLESS-аутбаунды + observatory (xray сам
    пингует каждый сервер через generate_204 каждые 15с) + балансировщик
    leastPing. Когда активный сервер перестаёт отвечать, новые соединения
    сразу идут через живой запасной — без ожидания 3 неудач health-check'а
    (~24с), без перезапуска процесса xray и без разрыва SOCKS-порта, т.е.
    боты замечают в худшем случае один неудавшийся запрос, а не минуту тишины.
    Внешний failover BotManager остаётся запасным контуром на случай, когда
    умерли ВСЕ серверы из балансировщика.

    Возвращает список тегов добавленных запасных аутбаундов (пустой — если
    добавлять было нечего; конфиг в этом случае не меняется)."""
    main = next((o for o in cfg.get("outbounds", []) if o.get("protocol") == "vless"), None)
    if main is None or not backup_uris:
        return []
    main_tag = main.get("tag") or "proxy"
    main["tag"] = main_tag
    added = []
    for i, uri in enumerate(backup_uris, start=1):
        try:
            ob = parse_vless_uri(uri)["outbounds"][0]
        except Exception:
            continue
        ob["tag"] = f"reserve-{i}"
        cfg["outbounds"].append(ob)
        added.append(ob["tag"])
    if not added:
        return []
    selector = [main_tag] + added
    cfg["observatory"] = {
        "subjectSelector": selector,
        "probeUrl": "https://www.gstatic.com/generate_204",
        "probeInterval": "15s",
        "enableConcurrency": True,
    }
    routing = cfg.setdefault("routing", {})
    routing.setdefault("balancers", []).append({
        "tag": "auto-failover",
        "selector": selector,
        # leastPing: живые сервера ранжируются по замеренному observatory
        # отклику, мёртвые (проба не прошла) исключаются из выбора вовсе.
        "strategy": {"type": "leastPing"},
    })
    rules = routing.setdefault("rules", [])
    # Всё, что конфиг раньше направлял на главный сервер по явному правилу,
    # теперь идёт через балансировщик (главный сервер — его участник, так что
    # пока он жив и быстрее всех, поведение то же самое, что раньше).
    for rule in rules:
        if rule.get("outboundTag") == main_tag:
            rule.pop("outboundTag", None)
            rule["balancerTag"] = "auto-failover"
    # Catch-all в конец: без него весь НЕсматченный трафик по умолчанию уходит
    # в ПЕРВЫЙ аутбаунд (главный сервер) напрямую, мимо балансировщика. Явные
    # правила выше (например, "русские домены → direct" из Happ-конфигов)
    # по-прежнему срабатывают первыми — порядок правил в xray имеет значение.
    rules.append({"type": "field", "network": "tcp,udp", "balancerTag": "auto-failover"})
    return added

def parse_subscription_body(body_bytes):
    """Разбирает тело ответа 'подписки' Xray/V2Ray: обычно это base64 с списком
    vless://-ссылок по одной на строку (иногда — уже готовый plain-текстовый
    список без base64). Возвращает список профилей {"id","name","raw_input",
    "country"} — по одному на каждую валидную vless://-ссылку, остальные схемы
    (ss://, trojan:// и т.п.) пропускаются, т.к. это приложение работает только
    с VLESS."""
    plain = body_bytes.decode("utf-8", errors="ignore")
    if "vless://" in plain:
        # Тело уже обычный текстовый список ссылок (некоторые панели отдают подписку без
        # base64) — base64.b64decode(..., validate=False) молча ВЫБРАСЫВАЕТ символы вне
        # base64-алфавита (":", "?", "#", "@" и т.п.) вместо ошибки, превращая валидный
        # plain-текст в нечитаемый мусор без единой распознанной vless://-строки, поэтому
        # для уже-текстового тела base64-декодирование вообще не пытаемся делать.
        decoded = plain
    else:
        try:
            padded = body_bytes + b"=" * (-len(body_bytes) % 4)
            decoded = base64.b64decode(padded, validate=True).decode("utf-8", errors="ignore")
        except Exception:
            decoded = plain
    profiles = []
    for line in decoded.splitlines():
        line = line.strip()
        if not line.startswith("vless://"):
            continue
        frag = urllib.parse.urlparse(line).fragment
        name = urllib.parse.unquote(frag) if frag else f"Профиль {len(profiles)+1}"
        country, name = extract_flag_code(name)
        profiles.append({"id": str(uuid.uuid4()), "name": name, "raw_input": line, "country": country})
    return profiles

def fetch_subscription(url, timeout=10):
    """Скачивает подписку по URL и возвращает (profiles, update_interval_hours,
    traffic, title). update_interval_hours берётся из заголовка ответа
    'profile-update-interval', если сервер его прислал, иначе None (тогда
    используется дефолт вызывающего кода). traffic — словарь {"download",
    "upload", "total"} в байтах из заголовка 'subscription-userinfo'
    (стандарт для подписок Xray/V2Ray), либо None, если сервер его не прислал.
    title — человеко-читаемое имя подписки из заголовка 'profile-title'
    (base64, тот же стандарт), либо None, если сервер его не прислал."""
    req = urllib.request.Request(url, headers={"User-Agent": "BotManager"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        update_hours = None
        try:
            raw_interval = resp.headers.get("profile-update-interval")
            if raw_interval:
                update_hours = float(raw_interval)
        except Exception:
            pass
        traffic = None
        raw_userinfo = resp.headers.get("subscription-userinfo")
        if raw_userinfo:
            traffic = {}
            for part in raw_userinfo.split(";"):
                part = part.strip()
                if "=" not in part:
                    continue
                k, v = part.split("=", 1)
                try:
                    # Один нестандартный/нечисловой параметр (например какой-то панели
                    # присылают expire= в неожиданном формате) не должен обнулять уже
                    # успешно распознанные download/upload/total — пропускаем только его.
                    traffic[k.strip()] = int(v.strip())
                except ValueError:
                    continue
        title = None
        raw_title = resp.headers.get("profile-title")
        if raw_title:
            try:
                # profile-title обычно "base64:<...>" (RFC 2231/8187-подобная форма) либо
                # просто голый base64 — поддерживаем оба варианта.
                b64_part = raw_title.split(":", 1)[1] if raw_title.startswith("base64:") else raw_title
                padded = b64_part + "=" * (-len(b64_part) % 4)
                title = base64.b64decode(padded).decode("utf-8").strip() or None
            except Exception:
                title = None
    return parse_subscription_body(body), update_hours, traffic, title

def extract_vless_endpoint(raw_input):
    """Достаёт host:port VLESS-сервера из ссылки vless:// или JSON-конфига —
    для быстрой проверки обычным TCP-подключением (как в большинстве VPN-клиентов),
    без поднятия временного xray-процесса."""
    raw_input = (raw_input or "").strip()
    try:
        if raw_input.startswith("vless://"):
            u = urllib.parse.urlparse(raw_input)
            if u.hostname and u.port:
                return u.hostname, u.port
        else:
            cfg = json.loads(raw_input)
            for ob in cfg.get("outbounds", []):
                if ob.get("protocol") == "vless":
                    vnext = ob.get("settings", {}).get("vnext", [])
                    if vnext and vnext[0].get("address") and vnext[0].get("port"):
                        return vnext[0]["address"], vnext[0]["port"]
    except Exception:
        pass
    return None

_NETWORK_LABELS = {"tcp": "TCP", "ws": "WS", "grpc": "GRPC", "xhttp": "XHTTP"}
_SECURITY_LABELS = {"reality": "Reality", "tls": "TLS", "none": ""}

def describe_vless_protocol(raw_input):
    """Короткое описание транспорта профиля вроде 'VLESS · WS · TLS' — для
    отображения под именем профиля в списке (как показывают другие VPN-клиенты,
    например Happ)."""
    raw_input = (raw_input or "").strip()
    try:
        if raw_input.startswith("vless://"):
            u = urllib.parse.urlparse(raw_input)
            q = urllib.parse.parse_qs(u.query)
            network = (q.get("type", ["tcp"])[0] or "tcp").lower()
            security = (q.get("security", ["none"])[0] or "none").lower()
        else:
            cfg = json.loads(raw_input)
            ob = next((o for o in cfg.get("outbounds", []) if o.get("protocol") == "vless"), None)
            if ob is None:
                return "VLESS"
            ss = ob.get("streamSettings", {})
            network = (ss.get("network") or "tcp").lower()
            security = (ss.get("security") or "none").lower()
        parts = ["VLESS", _NETWORK_LABELS.get(network, network.upper())]
        sec_label = _SECURITY_LABELS.get(security, security.upper() if security else "")
        if sec_label:
            parts.append(sec_label)
        return " · ".join(parts)
    except Exception:
        return "VLESS"

def format_bytes(n):
    """Компактно форматирует байты в ГБ/МБ, как счётчики трафика у VPN-клиентов."""
    if n is None:
        return "?"
    gb = n / (1024 ** 3)
    if gb >= 1:
        return f"{gb:.1f} ГБ"
    return f"{n / (1024 ** 2):.0f} МБ"

class GeoDownloader(QThread):
    """Качает geoip.dat/geosite.dat в фоне, не блокируя интерфейс."""
    log = pyqtSignal(str)
    done = pyqtSignal()
    def run(self):
        ensure_geo_files(log_fn=lambda m: self.log.emit(m))
        self.done.emit()

class FlagDownloader(QThread):
    """Качает недостающие иконки флагов стран в фоне, не блокируя интерфейс."""
    done = pyqtSignal()
    def __init__(self, codes):
        super().__init__()
        self.codes = codes
    def run(self):
        for code in self.codes:
            ensure_flag(code)
        self.done.emit()

def probe_xray_config(cfg, port, timeout=15):
    """Поднимает временный xray с уже собранным конфигом cfg на служебном порту и
    проверяет, что через него реально проходит трафик (не просто что процесс жив).
    Возвращает время отклика в секундах при успехе, иначе None. Может кинуть
    исключение, если xray.exe не удалось запустить — вызывающий код сам решает,
    как это залогировать. Общий код для VlessFailoverWorker и VlessVerifyWorker —
    раньше был продублирован в обоих (сборка конфига, запуск процесса, поллинг,
    очистка), что рисковало разъехаться при будущих правках."""
    for ib in cfg.get("inbounds", []):
        if ib.get("protocol") == "socks":
            ib["port"] = port
    XRAY_DIR.mkdir(parents=True, exist_ok=True)
    tmp_cfg_path = XRAY_DIR / f"probe_{port}.json"
    tmp_cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    proc = None
    try:
        proc = subprocess.Popen(
            [str(XRAY_EXE), "run", "-c", str(tmp_cfg_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        import httpx
        t0 = time.time()
        while time.time() - t0 < timeout:
            time.sleep(0.5)
            try:
                with httpx.Client(proxy=f"socks5://127.0.0.1:{port}", timeout=3) as c:
                    r = c.get("https://www.gstatic.com/generate_204")
                    if r.status_code == 204:
                        return time.time() - t0
            except Exception:
                continue
        return None
    finally:
        if proc:
            try: proc.kill()
            except Exception: pass
        try: tmp_cfg_path.unlink()
        except Exception: pass

class VlessFailoverWorker(QThread):
    """Параллельно тестирует резервные VLESS-профили (временные xray на служебных
    портах) и ждёт ответа от ВСЕХ (успеха или таймаута), а затем выбирает того, кто
    реально ответил быстрее всех — а не просто первого, кто успел откликнуться."""
    log = pyqtSignal(str)
    # (id, raw_input, name) профиля-победителя, все "" если ни один не поднялся.
    # Не индекс: список профилей может измениться (удаление/редактирование) за
    # время работы воркера, и позиционный индекс к моменту result рискует
    # указывать уже не на тот профиль (или выйти за границы списка). И не только
    # raw_input: два профиля могут случайно иметь одинаковую ссылку — id этого
    # не допускает.
    result = pyqtSignal(str, str, str)

    def __init__(self, profiles, active_index):
        super().__init__()
        self.profiles = list(profiles)
        self.active_index = active_index

    def run(self):
        candidates = [(i, p) for i, p in enumerate(self.profiles) if i != self.active_index]
        if not candidates:
            self.log.emit("Нет резервных профилей для переключения")
            self.result.emit("", "", "")
            return
        self.log.emit(f"Активный профиль упал — тестирую {len(candidates)} резервных...")

        results = {}  # idx -> время отклика в секундах (только успешные)
        results_lock = threading.Lock()

        def safe_log(msg):
            try:
                self.log.emit(msg)
            except RuntimeError:
                # Worker уже уничтожен (см. PingWorker) — probe()-поток "опоздал"
                # относительно join(timeout=16) в run(), репортить уже некому.
                pass

        def probe(idx, profile, port):
            name = profile.get("name") or f"Профиль {idx+1}"
            try:
                cfg = build_xray_config(profile.get("raw_input", ""))
            except Exception as e:
                safe_log(f"✗ '{name}': ошибка конфига — {e}")
                return
            try:
                # Не выходим досрочно, даже если другой профиль уже ответил — нужно
                # честное время ОТКЛИКА именно этого профиля, чтобы сравнить всех.
                elapsed = probe_xray_config(cfg, port, timeout=15)
            except Exception as e:
                safe_log(f"✗ '{name}': не удалось запустить — {e}")
                return
            if elapsed is None:
                return
            with results_lock:
                results[idx] = elapsed
            safe_log(f"✓ '{name}' поднялся за {elapsed:.1f} сек")

        threads = []
        for i, (idx, profile) in enumerate(candidates):
            port = 11900 + i
            th = threading.Thread(target=probe, args=(idx, profile, port), daemon=True)
            threads.append(th)
            th.start()
        for th in threads:
            th.join(timeout=20)  # с запасом сверх 15с опроса + до 3с httpx-таймаута

        if not results:
            self.log.emit("✗ Ни один резервный профиль не поднялся")
            self.result.emit("", "", "")
            return
        winner_idx = min(results, key=results.get)
        winner = self.profiles[winner_idx]
        self.result.emit(winner.get("id", ""), winner.get("raw_input", ""),
                          winner.get("name") or f"Профиль {winner_idx+1}")

class VlessHealthWorker(QThread):
    """Проверяет, что активный SOCKS5-прокси реально пропускает трафик — процесс
    xray может быть технически жив (не упал), но не пропускать ничего наружу
    (например, если удалённый VLESS-сервер перестал отвечать). Обычная проверка
    "упал ли процесс" такое не ловит, поэтому делаем отдельный лёгкий запрос.
    Проверяем ДВА независимых сервиса (Google и Cloudflare) и считаем связь
    рабочей, если ответил хотя бы один — иначе временная проблема именно у ОДНОГО
    из них (не у VLESS-сервера) могла бы ложно засчитаться как обрыв и запустить
    ненужный failover."""
    result = pyqtSignal(bool)
    PROBE_URLS = ("https://www.gstatic.com/generate_204", "https://cloudflare.com/cdn-cgi/trace")

    def run(self):
        import httpx
        for url in self.PROBE_URLS:
            try:
                with httpx.Client(proxy=f"socks5://127.0.0.1:{XRAY_SOCKS_PORT}", timeout=5) as c:
                    r = c.get(url)
                    if 200 <= r.status_code < 300:
                        self.result.emit(True)
                        return
            except Exception:
                continue
        self.result.emit(False)

class SubscriptionFetchWorker(QThread):
    """Скачивает и разбирает подписку (Xray/V2Ray subscription URL) в фоне, не
    блокируя интерфейс — сеть может ощутимо тормозить или зависнуть."""
    result = pyqtSignal(str, list, object, object, object)  # (subscription_id, profiles, update_hours_or_None, traffic_or_None, title_or_None)
    error = pyqtSignal(str, str)  # (subscription_id, сообщение об ошибке)

    def __init__(self, subscription_id, url):
        super().__init__()
        self.subscription_id = subscription_id
        self.url = url

    def run(self):
        try:
            profiles, update_hours, traffic, title = fetch_subscription(self.url)
            if not profiles:
                self.error.emit(self.subscription_id, "Подписка пуста или не содержит vless://-ссылок")
                return
            self.result.emit(self.subscription_id, profiles, update_hours, traffic, title)
        except Exception as e:
            self.error.emit(self.subscription_id, str(e))

class VlessVerifyWorker(QThread):
    """Перед РУЧНЫМ переключением на другой профиль ("Сделать активным") поднимает
    его во временном xray на служебном порту и проверяет, что он реально пропускает
    трафик — чтобы не обрывать уже рабочее соединение ради заведомо мёртвого
    профиля. Вся работа идёт синхронно в run() (без отдельных threading.Thread),
    поэтому гонки с уничтожением Qt-объекта тут в принципе невозможны."""
    result = pyqtSignal(bool)

    def __init__(self, raw_input):
        super().__init__()
        self.raw_input = raw_input

    def run(self):
        try:
            cfg = build_xray_config(self.raw_input)
            elapsed = probe_xray_config(cfg, 11898, timeout=15)
            self.result.emit(elapsed is not None)
        except Exception:
            self.result.emit(False)

class PingWorker(QThread):
    """Меряет обычный TCP-пинг (время подключения) до VLESS-сервера каждого
    профиля — как в большинстве VPN-клиентов (Happ и т.п.), без поднятия
    временного xray-процесса. НЕ переключает активный профиль. Профили
    проверяются ПО ОЧЕРЕДИ, а не все параллельно — с "Проверить все" на списке
    из 20+ профилей десятки одновременных подключений заметно подвешивали
    систему; последовательная проверка медленнее, но не создаёт этот всплеск.
    Вся работа идёт синхронно в run() (без отдельных threading.Thread), поэтому
    гонки с уничтожением Qt-объекта тут в принципе невозможны."""
    result = pyqtSignal(int, object)  # (индекс профиля, задержка в мс или None при неудаче)
    done = pyqtSignal()

    def __init__(self, indexed_profiles):
        super().__init__()
        self.indexed_profiles = list(indexed_profiles)  # [(idx, profile_dict), ...]

    def run(self):
        for i, (idx, profile) in enumerate(self.indexed_profiles):
            if i > 0:
                # Небольшая пауза между профилями — даже последовательные TCP-подключения
                # подряд (до ~20 штук при "Проверить все") создают заметный всплеск нагрузки
                # на сетевой стек/VPN-адаптер, из-за которого у ботов (Telegram-поллинг,
                # Twitch WebSocket) иногда на несколько секунд дребезжит соединение —
                # растягиваем проверку, чтобы снизить пиковую нагрузку.
                time.sleep(0.3)
            endpoint = extract_vless_endpoint(profile.get("raw_input", ""))
            if not endpoint:
                self.result.emit(idx, None)
                continue
            host, port = endpoint
            t0 = time.time()
            try:
                with socket.create_connection((host, port), timeout=5):
                    self.result.emit(idx, int((time.time() - t0) * 1000))
            except OSError:
                self.result.emit(idx, None)
        self.done.emit()

# ── Цвета ──────────────────────────────────────────────────────────────────────
BG     = "#1c1c1e"
SB     = "#161618"
CARD   = "#2c2c2e"
LINE   = "#38383a"
GREEN  = "#34C759"
RED    = "#ff453a"
YELLOW = "#ffd60a"
ORANGE = "#ff9f0a"
BLUE   = "#0a84ff"
WHITE  = "#ffffff"
GRAY   = "#8e8e93"
DARK   = "#48484a"
LOG    = "#111111"
HOVER  = "rgba(255,255,255,0.08)"
SEL    = "rgba(255,255,255,0.12)"

APP_VERSION = "1.4.1"
GITHUB_REPO = "Leshka71/BotManager"

QSS = f"""
QWidget {{ background: {BG}; color: {WHITE}; font-family: 'Segoe UI'; font-size: 13px; margin: 0; padding: 0; }}
QTextEdit {{ background: {LOG}; color: #ccc; border: none; font-family: Consolas; font-size: 12px; padding: 12px; }}
QLineEdit {{ background: {CARD}; color: {WHITE}; border: 1px solid #555; border-radius: 10px; padding: 8px 14px; font-size: 13px; }}
QLineEdit:focus {{ border-color: {BLUE}; }}
QPushButton {{ background: #3a3a3c; color: {WHITE}; border: none; border-radius: 10px; padding: 6px 16px; font-size: 13px; min-height: 0; }}
QPushButton:hover {{ background: #48484a; }}
QPushButton:pressed {{ background: #2c2c2e; }}
QScrollBar:vertical {{ background: transparent; width: 4px; border-radius: 2px; }}
QScrollBar::handle:vertical {{ background: #444; border-radius: 2px; min-height: 20px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ height: 0; }}
QMenu {{ background: #2c2c2e; border: 1px solid #555; border-radius: 12px; padding: 4px; }}
QMenu::item {{ padding: 8px 16px; border-radius: 8px; }}
QMenu::item:selected {{ background: {SEL}; }}
"""

# ── Утилиты ───────────────────────────────────────────────────────────────────
def h_line():
    f = QFrame(); f.setFrameShape(QFrame.Shape.HLine)
    f.setStyleSheet(f"background:{LINE};max-height:1px;border:none;"); return f

def label(text, size=13, color=WHITE, bold=False):
    l = QLabel(text)
    fw = "600" if bold else "400"
    l.setStyleSheet(f"color:{color};font-size:{size}px;font-weight:{fw};background:transparent;border:none;")
    return l

def btn(text, w=None, h=32, cls=None):
    b = QPushButton(text); b.setFixedHeight(h)
    if w: b.setFixedWidth(w)
    if cls: b.setProperty("class", cls); b.setStyleSheet(
        f"QPushButton{{background:{'#34C759' if cls=='green' else 'rgba(255,69,58,0.18)'};border:none;color:{'white' if cls=='green' else RED};border-radius:10px;padding:6px 16px;font-size:13px;font-weight:{'600' if cls=='green' else '500'};min-height:0;}}"
        f"QPushButton:hover{{background:{'#2ec945' if cls=='green' else 'rgba(255,69,58,0.28)'};}} QPushButton:pressed{{background:{'#28b83e' if cls=='green' else 'rgba(255,69,58,0.12)'};}}")
    return b

# ── Точка статуса ─────────────────────────────────────────────────────────────
class Dot(QWidget):
    def __init__(self, color=DARK, size=8):
        super().__init__(); self._c = color; self.setFixedSize(size, size)
    def set(self, c): self._c = c; self.update()
    def paintEvent(self, _):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(self._c))
        p.drawEllipse(0, 0, self.width(), self.height()); p.end()

class CloseBtn(QWidget):
    """Кнопка-крестик, нарисованная через QPainter — чёткая на любом размере/DPI,
    в отличие от Unicode-символа "✕", который в мелком кегле рендерится пиксельно."""
    clicked = pyqtSignal()
    def __init__(self, size=18, color=None, hover_color=None):
        super().__init__()
        self._size = size
        self._color = color or GRAY
        self._hover_color = hover_color or RED
        self._hovered = False
        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
    def enterEvent(self, e):
        self._hovered = True; self.update(); super().enterEvent(e)
    def leaveEvent(self, e):
        self._hovered = False; self.update(); super().leaveEvent(e)
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
    def paintEvent(self, _):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(self._hover_color if self._hovered else self._color))
        pen.setWidthF(1.6); pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        m = self._size * 0.3
        p.drawLine(QPointF(m, m), QPointF(self._size - m, self._size - m))
        p.drawLine(QPointF(self._size - m, m), QPointF(m, self._size - m))
        p.end()

def _draw_icon_glyph(p, kind, w, h, color):
    """Рисует одну из простых векторных иконок текущим painter'ом в
    прямоугольнике w×h — единый чёткий стиль вместо Unicode-символов (▶⟳✎···),
    которые в мелком кегле рендерятся пиксельно и непоследовательно (разная
    толщина линий у разных шрифтов/размеров), как это уже было решено для
    крестика удаления (CloseBtn) — здесь тот же подход для остальных кнопок."""
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    cx, cy = w / 2, h / 2
    col = QColor(color)
    if kind == "menu":
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(QBrush(col))
        r = w * 0.055
        for dx in (-w * 0.18, 0, w * 0.18):
            p.drawEllipse(QPointF(cx + dx, cy), r, r)
    elif kind == "add":
        pen = QPen(col); pen.setWidthF(max(1.6, w * 0.11))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        r = w * 0.26
        p.drawLine(QPointF(cx - r, cy), QPointF(cx + r, cy))
        p.drawLine(QPointF(cx, cy - r), QPointF(cx, cy + r))
    elif kind == "play":
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(QBrush(col))
        s = w * 0.26
        path = QPainterPath()
        path.moveTo(cx - s * 0.55, cy - s)
        path.lineTo(cx - s * 0.55, cy + s)
        path.lineTo(cx + s * 0.95, cy)
        path.closeSubpath()
        p.drawPath(path)
    elif kind == "edit":
        pen = QPen(col); pen.setWidthF(max(1.4, w * 0.10))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap); pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        r = w * 0.22
        p.drawLine(QPointF(cx - r, cy + r), QPointF(cx + r * 0.6, cy - r * 0.6))
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(QBrush(col))
        tip = QPainterPath()
        tip.moveTo(cx - r, cy + r)
        tip.lineTo(cx - r + w * 0.11, cy + r)
        tip.lineTo(cx - r, cy + r - w * 0.11)
        tip.closeSubpath()
        p.drawPath(tip)
    elif kind == "refresh":
        pen = QPen(col); pen.setWidthF(max(1.4, w * 0.09))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        r = w * 0.26
        rect = QRectF(cx - r, cy - r, r * 2, r * 2)
        p.drawArc(rect, -40 * 16, 280 * 16)
        ang = math.radians(-40)
        ax, ay = cx + r * math.cos(ang), cy - r * math.sin(ang)
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(QBrush(col))
        hs = w * 0.09
        head = QPainterPath()
        head.moveTo(ax, ay)
        head.lineTo(ax - hs * 1.5, ay - hs * 0.2)
        head.lineTo(ax - hs * 0.2, ay + hs * 1.3)
        head.closeSubpath()
        p.drawPath(head)
    elif kind == "signal":
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(QBrush(col))
        bw = w * 0.13
        heights = (w * 0.20, w * 0.32, w * 0.44)
        gap = w * 0.07
        total_w = bw * 3 + gap * 2
        x = cx - total_w / 2
        base_y = cy + w * 0.24
        for hgt in heights:
            p.drawRoundedRect(QRectF(x, base_y - hgt, bw, hgt), bw * 0.3, bw * 0.3)
            x += bw + gap
    elif kind == "copy":
        pen = QPen(col); pen.setWidthF(max(1.3, w * 0.07))
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        r = w * 0.06
        p.drawRoundedRect(QRectF(cx - w * 0.22, cy - w * 0.22, w * 0.32, w * 0.32), r, r)
        p.drawRoundedRect(QRectF(cx - w * 0.08, cy - w * 0.08, w * 0.32, w * 0.32), r, r)
    elif kind == "trash":
        pen = QPen(col); pen.setWidthF(max(1.3, w * 0.08))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap); pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        bx0, bx1 = cx - w * 0.16, cx + w * 0.16
        by0, by1 = cy - w * 0.08, cy + w * 0.24
        p.drawLine(QPointF(cx - w * 0.22, by0), QPointF(cx + w * 0.22, by0))
        p.drawLine(QPointF(cx - w * 0.08, by0), QPointF(cx - w * 0.08, cy - w * 0.16))
        p.drawRoundedRect(QRectF(bx0, by0, bx1 - bx0, by1 - by0), w * 0.03, w * 0.03)
    elif kind == "list":
        pen = QPen(col); pen.setWidthF(max(1.3, w * 0.08))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        for frac in (0.32, 0.5, 0.68):
            y = h * frac
            p.drawLine(QPointF(w * 0.2, y), QPointF(w * 0.8, y))

def make_icon(kind, color, size=16):
    """QIcon для пунктов QMenu — тот же векторный стиль, что и у IconBtn, только
    отрендеренный в QPixmap (QMenu не умеет напрямую использовать QWidget)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    _draw_icon_glyph(p, kind, size, size, color)
    p.end()
    return QIcon(pm)

class IconBtn(QWidget):
    """Кнопка-иконка на QPainter (см. _draw_icon_glyph) — замена Unicode-
    символам вроде ▶⟳✎, которые на мелком кегле выглядят пиксельно."""
    clicked = pyqtSignal()
    def __init__(self, kind, size=26, color=None, hover_color=None):
        super().__init__()
        self.kind = kind
        self._size = size
        self._color = color or GRAY
        self._hover_color = hover_color or WHITE
        self._hovered = False
        self._enabled_visual = True
        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
    def setEnabled(self, enabled):
        super().setEnabled(enabled)
        self._enabled_visual = enabled
        self.update()
    def enterEvent(self, e):
        self._hovered = True; self.update(); super().enterEvent(e)
    def leaveEvent(self, e):
        self._hovered = False; self.update(); super().leaveEvent(e)
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self.clicked.emit()
    def paintEvent(self, _):
        p = QPainter(self)
        if not self._enabled_visual:
            col = "#48484a"
        else:
            col = self._hover_color if self._hovered else self._color
        _draw_icon_glyph(p, self.kind, self._size, self._size, col)
        p.end()

class Overlay(QWidget):
    """Модальная панель поверх всего окна приложения — как диалоги в HAPP:
    НЕ отдельное окно Windows, а затемнение фона + центрированная карточка
    внутри самого приложения (поэтому её нельзя перетащить/свернуть отдельно
    от главного окна — так и задумано, это не баг). Создаётся заново на каждый
    вызов и уничтожается через dismiss()."""
    def __init__(self, parent_window, card_width=420, card_height=None):
        super().__init__(parent_window)
        self.setStyleSheet("background: rgba(0,0,0,0.55);")
        self.card = QWidget(self)
        self.card.setFixedWidth(card_width)
        if card_height is not None:
            self.card.setFixedHeight(card_height)
        self.card.setStyleSheet(f"background:{BG};border-radius:14px;border:1px solid #3a3a3c;")
        self.body = QVBoxLayout(self.card)
        self.body.setContentsMargins(20, 16, 20, 20)
        self.body.setSpacing(8)
        self.hide()
        parent_window.installEventFilter(self)

    def add_header(self, title_text):
        """Строка заголовка карточки: жирный текст + крестик закрытия — тут он
        не дублирует нативный (как раньше у QDialog), а единственный, потому
        что у самой панели больше нет никакого системного окна/рамки."""
        row = QHBoxLayout()
        row.addWidget(label(title_text, 16, WHITE, True))
        row.addStretch()
        b_close = CloseBtn(22)
        b_close.clicked.connect(self.dismiss)
        row.addWidget(b_close)
        self.body.addLayout(row)
        return b_close

    def eventFilter(self, obj, event):
        if obj is self.parentWidget() and event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self._reposition()
        return False

    def _reposition(self):
        pw = self.parentWidget()
        if pw is None:
            return
        self.setGeometry(0, 0, pw.width(), pw.height())
        # activate() форсирует layout карточки пересчитаться СЕЙЧАС, а не ждать своей
        # очереди в цикле событий — без этого adjustSize() на первом же открытии мог взять
        # ещё не пересчитанную высоту многострочных подписей с переносом (word-wrap
        # QLabel), из-за чего карточка получалась ниже, чем реально нужно её содержимому,
        # и элементы (кнопка "Добавить", предупреждение) налезали друг на друга.
        self.card.layout().activate()
        self.card.adjustSize()
        cx = max(0, (self.width() - self.card.width()) // 2)
        cy = max(20, (self.height() - self.card.height()) // 2)
        self.card.move(cx, cy)

    def open(self):
        self._reposition()
        self.raise_()
        self.show()
        # Первый adjustSize() сразу после заполнения карточки виджетами иногда всё равно
        # промахивается на 1 проход layout'а (особенно с word-wrap подписями) — досчитываем
        # ещё раз следующим тиком цикла событий, когда всё уже точно улеглось.
        QTimer.singleShot(0, self._reposition)

    def dismiss(self):
        self.hide()
        self.deleteLater()

    def mousePressEvent(self, e):
        # Клик по затемнённому фону не должен проваливаться сквозь панель к
        # содержимому под ней и намеренно не закрывает панель (как и раньше,
        # закрытие — только явным крестиком/кнопкой, чтобы не терять
        # введённые данные случайным кликом мимо).
        e.accept()

# ── Переключатель ─────────────────────────────────────────────────────────────
class Toggle(QWidget):
    toggled = pyqtSignal(bool)
    W, H, PAD = 38, 22, 2

    def __init__(self, on=False, w=None, h=None):
        super().__init__()
        self._on = on
        if w: self.W = w
        if h: self.H = h; self.PAD = max(2, h // 10)
        self.setFixedSize(self.W, self.H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def isChecked(self): return self._on
    def setChecked(self, v): self._on = v; self.update()
    def mousePressEvent(self, _): self._on = not self._on; self.toggled.emit(self._on); self.update()

    def paintEvent(self, _):
        from PyQt6.QtGui import QPainterPath
        from PyQt6.QtCore import QRectF

        p = QPainter(self)
        p.setRenderHints(QPainter.RenderHint.Antialiasing |
                         QPainter.RenderHint.SmoothPixmapTransform)
        p.setPen(Qt.PenStyle.NoPen)

        r = self.H / 2.0
        track = QPainterPath()
        track.addRoundedRect(QRectF(0, 0, self.W, self.H), r, r)
        p.setBrush(QColor("#34C759" if self._on else "#3a3a3c"))
        p.drawPath(track)

        d = self.H - self.PAD * 2
        x = float((self.W - self.PAD - d) if self._on else self.PAD)
        y = float(self.PAD)

        # Shadow layer under knob
        for i in range(3, 0, -1):
            sh = QPainterPath()
            sh.addEllipse(QRectF(x - 0.5, y + i * 0.5, d + 1, d + 1))
            p.setBrush(QColor(0, 0, 0, 18))
            p.drawPath(sh)

        knob = QPainterPath()
        knob.addEllipse(QRectF(x, y, d, d))
        p.setBrush(QColor("white"))
        p.drawPath(knob)
        p.end()

# ── Пункт сайдбара ────────────────────────────────────────────────────────────
class SideItem(QWidget):
    clicked = pyqtSignal()
    def __init__(self, icon="", text="", is_bot=False):
        super().__init__()
        self._sel = False; self._icon = icon; self._text = text; self._is_bot = is_bot
        self.setFixedHeight(34); self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet("background: transparent; border-radius: 7px;")

        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(10, 0, 10, 0); self._lay.setSpacing(10)

        if is_bot:
            self.dot = Dot(DARK, 8)
            self._lay.addWidget(self.dot)
        else:
            ic = QLabel(icon); ic.setFixedWidth(16)
            ic.setStyleSheet(f"color:{GRAY};font-size:14px;background:transparent;border:none;")
            self._lay.addWidget(ic)

        self.lbl = QLabel(text)
        self.lbl.setStyleSheet(f"color:{GRAY};font-size:13px;background:transparent;border:none;")
        self._lay.addWidget(self.lbl)
        # Stretch как управляемый spacer — в compact отключается чтобы иконка центрировалась
        self._spacer = QSpacerItem(0, 0, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self._lay.addItem(self._spacer)

    def set_sel(self, v):
        self._sel = v
        bg = SEL if v else "transparent"
        self.setStyleSheet(f"background:{bg};border-radius:7px;")
        self.lbl.setStyleSheet(f"color:{'white' if v else GRAY};font-size:13px;{'font-weight:500;' if v else ''}background:transparent;border:none;")

    def set_compact(self, compact):
        if getattr(self, '_is_compact', False) == compact:
            return
        self._is_compact = compact
        self.lbl.setVisible(not compact)
        if compact:
            # sidebar=46, карточка=42 (2px margin с каждой стороны)
            # центр: (42 - icon_width) / 2
            left = 17 if self._is_bot else 13   # dot=8px → 17, icon=16px → 13
            self._spacer.changeSize(0, 0, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            self._lay.setContentsMargins(left, 0, 0, 0)
            self._lay.setSpacing(0)
        else:
            self._spacer.changeSize(0, 0, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            self._lay.setContentsMargins(10, 0, 10, 0)
            self._lay.setSpacing(10)
        self._lay.invalidate()
        self.update()

    def set_dot(self, c):
        if self._is_bot: self.dot.set(c)

    def mousePressEvent(self, _): self.clicked.emit()
    def enterEvent(self, _):
        if not self._sel: self.setStyleSheet(f"background:{HOVER};border-radius:7px;")
    def leaveEvent(self, _):
        if not self._sel:
            self.setStyleSheet("background:transparent;border-radius:7px;")

# ── Читатель логов ────────────────────────────────────────────────────────────
class ProcessStartWorker(QThread):
    """Запускает subprocess.Popen в фоне, а не в GUI-потоке. Обычно CreateProcess
    занимает миллисекунды, но когда Windows под давлением на ресурсы (мало
    памяти/хендлов — как было в этой сессии из-за тяжёлых сторонних программ),
    он может занять секунды и даже больше — а поскольку раньше Popen вызывался
    прямо в обработчике клика "Старт"/"Рестарт", вся программа на это время
    зависала (весь Qt event loop блокируется, пока GUI-поток занят синхронным
    вызовом)."""
    result = pyqtSignal(object, object)  # (bot, subprocess.Popen)
    error = pyqtSignal(object, str)      # (bot, сообщение об ошибке)

    def __init__(self, bot, cmd, cwd, env):
        super().__init__()
        self.bot = bot
        self.cmd = cmd
        self.cwd = cwd
        self.env = env

    def run(self):
        try:
            proc = subprocess.Popen(
                self.cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                cwd=self.cwd, creationflags=subprocess.CREATE_NO_WINDOW, env=self.env,
            )
            self.result.emit(self.bot, proc)
        except Exception as e:
            self.error.emit(self.bot, str(e))

class ProcessStopWorker(QThread):
    """Убивает уже запущенный процесс (taskkill /f /t + wait) в фоне, а не в
    GUI-потоке — та же причина, что и у ProcessStartWorker: subprocess.run(
    ['taskkill', ...], timeout=5) раньше вызывался прямо в обработчике клика
    "Стоп"/"Рестарт" и мог подвесить всю программу на секунды, если Windows под
    давлением на ресурсы (или сам taskkill.exe медленно стартует). Ничего не
    сообщает по завершении — bot.process уже обнулён вызывающим кодом
    синхронно, до старта этого воркера, так что дожидаться результата убийства
    процесса никому не нужно."""
    def __init__(self, pid, proc):
        super().__init__()
        self.pid = pid
        self.proc = proc

    def run(self):
        try:
            subprocess.run(['taskkill', '/f', '/t', '/pid', str(self.pid)],
                           capture_output=True, timeout=5,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            try: self.proc.kill()
            except Exception: pass
        finally:
            try: self.proc.wait(timeout=2)
            except Exception: pass

class LogReader(QThread):
    line = pyqtSignal(str); done = pyqtSignal()
    def __init__(self, proc): super().__init__(); self.proc = proc
    def run(self):
        try:
            for raw in iter(self.proc.stdout.readline, b""):
                try: t = raw.decode("utf-8").rstrip()
                except:
                    try: t = raw.decode("cp1251").rstrip()
                    except: t = raw.decode("utf-8", errors="replace").rstrip()
                if t: self.line.emit(t)
        except: pass
        self.done.emit()

# ── Данные бота ───────────────────────────────────────────────────────────────
class Bot:
    def __init__(self, d):
        self.id = d.get("id", str(time.time_ns()))
        self.name = d.get("name", "Bot")
        self.path = d.get("path", "")
        self.args = d.get("args", "")
        self.python = d.get("python", "python")
        self.autostart = d.get("autostart", True)
        self.auto_restart = d.get("auto_restart", True)
        self.broadcast_path = d.get("broadcast_path", "")
        self.process = None; self.reader = None
        self.status = "stopped"; self.start_t = None
        self.events = 0; self.errors = 0; self._alive = True
        self.restart_fails = 0
        self._generation = 0  # растёт при каждом _start() — отсекает "запоздавшие"
                               # сигналы done() от уже заменённого процесса
    def cfg(self):
        return {"id":self.id,"name":self.name,"path":self.path,
                "args":self.args,"python":self.python,"autostart":self.autostart,
                "auto_restart":self.auto_restart,"broadcast_path":self.broadcast_path}
    def uptime(self):
        if not self.start_t: return "—"
        s = int(time.time()-self.start_t); h,m = s//3600,(s%3600)//60
        return f"{h}ч {m}м" if h else f"{m}м"

# ── Страница бота ─────────────────────────────────────────────────────────────
class BotPage(QWidget):
    sig_start = pyqtSignal(object); sig_stop = pyqtSignal(object)
    sig_restart = pyqtSignal(object); sig_delete = pyqtSignal(object)
    sig_settings_changed = pyqtSignal(object)
    sig_broadcast_line = pyqtSignal(str); sig_broadcast_done = pyqtSignal(str)
    _CARD  = "QWidget{background:#2c2c2e;border-radius:12px;border:none;}"
    _TR    = "QWidget{background:transparent;border:none;border-radius:0;}"
    _FIELD = (f"QLineEdit{{background:transparent;border:none;border-radius:0;"
              f"color:#fff;font-size:14px;padding:0;}} "
              f"QLineEdit:focus{{border:none;background:transparent;}}")
    _CARD_SETTINGS = "QWidget{background:#2c2c2e;border-radius:12px;border:1px solid #3a3a3c;}"

    def __init__(self, bot):
        super().__init__(); self.bot = bot
        root = QVBoxLayout(self); root.setContentsMargins(0,0,0,0); root.setSpacing(0)

        # ── Заголовок ──
        hdr = QWidget(); hdr.setFixedHeight(56)
        hdr.setStyleSheet(f"background:{BG};border-bottom:1px solid {LINE};")
        hl = QHBoxLayout(hdr); hl.setContentsMargins(16,0,12,0); hl.setSpacing(8)
        self.dot = Dot(DARK, 10); hl.addWidget(self.dot)
        self.title = QLabel(bot.name)
        self.title.setStyleSheet(f"color:{WHITE};font-size:15px;font-weight:700;background:transparent;border:none;")
        hl.addWidget(self.title)
        self.badge = QLabel("остановлен"); self.badge.setFixedHeight(22)
        self._badge_style("stopped"); hl.addWidget(self.badge)
        # ── Вкладки ──
        hl.addSpacing(12)
        self._tab_log = QPushButton("Логи");       self._tab_log.setFixedHeight(28)
        self._tab_set = QPushButton("⚙ Настройки"); self._tab_set.setFixedHeight(28)
        self._tab_log.setCursor(Qt.CursorShape.PointingHandCursor)
        self._tab_set.setCursor(Qt.CursorShape.PointingHandCursor)
        self._tab_log.clicked.connect(lambda: self._switch_tab(0))
        self._tab_set.clicked.connect(lambda: self._switch_tab(1))
        hl.addWidget(self._tab_log); hl.addWidget(self._tab_set)
        hl.addStretch()
        self.b_run = QPushButton("Старт"); self.b_run.setFixedSize(80, 32)
        self.b_run.setStyleSheet(
            f"QPushButton{{background:{GREEN};border:none;color:#000;border-radius:10px;"
            f"font-size:13px;font-weight:700;min-height:0;}} QPushButton:hover{{background:#2db34a;}}")
        self.b_run.clicked.connect(self._toggle); hl.addWidget(self.b_run)
        b_rs = QPushButton("Рестарт"); b_rs.setFixedSize(90, 32)
        b_rs.setStyleSheet("QPushButton{background:#3a3a3c;border:none;color:#fff;border-radius:10px;font-size:13px;min-height:0;} QPushButton:hover{background:#48484a;}")
        b_rs.clicked.connect(lambda: self.sig_restart.emit(self.bot)); hl.addWidget(b_rs)
        b_del = QPushButton("Удалить"); b_del.setFixedSize(90, 32)
        b_del.setStyleSheet(
            f"QPushButton{{background:rgba(255,69,58,0.18);border:none;color:{RED};"
            f"border-radius:10px;font-size:13px;font-weight:500;min-height:0;}}"
            f"QPushButton:hover{{background:rgba(255,69,58,0.28);}}")
        b_del.clicked.connect(lambda: self.sig_delete.emit(self.bot)); hl.addWidget(b_del)
        root.addWidget(hdr)

        # ── Стек вкладок ──
        self._tabs = QStackedWidget(); root.addWidget(self._tabs)

        # ── Вкладка 0: Логи ──────────────────────────────────────────────────
        sc0 = QScrollArea(); sc0.setWidgetResizable(True)
        sc0.setStyleSheet("border:none;background:transparent;")
        w0 = QWidget(); w0.setStyleSheet(f"background:{BG};")
        cl0 = QVBoxLayout(w0); cl0.setContentsMargins(16,16,16,16); cl0.setSpacing(16)
        cl0.setAlignment(Qt.AlignmentFlag.AlignTop)

        # Секция: статус
        self._sec(cl0, "СТАТУС")
        c1 = QWidget(); c1.setStyleSheet(self._CARD); c1.setFixedHeight(60)
        l1 = QHBoxLayout(c1); l1.setContentsMargins(0,0,0,0); l1.setSpacing(0)
        self._up_lbl = self._stat_col(l1, "Аптайм", "—", WHITE)
        l1.addWidget(self._vdiv())
        self._ev_lbl = self._stat_col(l1, "Событий", "0", GREEN)
        l1.addWidget(self._vdiv())
        self._er_lbl = self._stat_col(l1, "Ошибок", "0", RED)
        cl0.addWidget(c1)

        # Секция: логи
        self._sec(cl0, "ЛОГИ")
        c3 = QWidget(); c3.setStyleSheet(self._CARD)
        c3.setMinimumHeight(220)
        l3 = QVBoxLayout(c3); l3.setContentsMargins(0,0,0,0); l3.setSpacing(0)
        tb = QWidget(); tb.setFixedHeight(38)
        tb.setStyleSheet("background:transparent;border-bottom:1px solid #3a3a3c;")
        tbl = QHBoxLayout(tb); tbl.setContentsMargins(14,0,10,0)
        tbl.addWidget(label("ВЫВОД", 10, GRAY)); tbl.addStretch()
        for txt, fn in [("Копировать", lambda: QApplication.clipboard().setText(self.log_view.toPlainText())),
                        ("Очистить",   lambda: self.log_view.clear())]:
            b = QPushButton(txt); b.setFixedHeight(26)
            b.setStyleSheet(f"QPushButton{{background:transparent;border:none;color:{GRAY};"
                            f"font-size:12px;padding:0 8px;min-height:0;}} QPushButton:hover{{color:{WHITE};}}")
            b.clicked.connect(fn); tbl.addWidget(b)
        l3.addWidget(tb)
        self.log_view = QTextEdit(); self.log_view.setReadOnly(True)
        self.log_view.setStyleSheet("""
            QTextEdit {
                background: transparent; color: #ccc; border: none;
                font-family: Consolas; font-size: 13px; padding: 10px 14px; border-radius: 0;
            }
            QScrollBar:vertical {
                background: transparent; width: 6px;
                margin: 4px 2px; border-radius: 3px;
            }
            QScrollBar::handle:vertical {
                background: #484848; border-radius: 3px; min-height: 32px;
            }
            QScrollBar::handle:vertical:hover { background: #686868; }
            QScrollBar::handle:vertical:pressed { background: #888; }
            QScrollBar::sub-line:vertical, QScrollBar::add-line:vertical { height: 0; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
        """)
        l3.addWidget(self.log_view); cl0.addWidget(c3)
        sc0.setWidget(w0); self._tabs.addWidget(sc0)

        # ── Вкладка 1: Настройки ─────────────────────────────────────────────
        sc1 = QScrollArea(); sc1.setWidgetResizable(True)
        sc1.setStyleSheet("border:none;background:transparent;")
        sc1.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        w1 = QWidget(); w1.setStyleSheet(f"background:{BG};")
        cl1 = QVBoxLayout(w1); cl1.setContentsMargins(24,24,24,24); cl1.setSpacing(16)
        cl1.setAlignment(Qt.AlignmentFlag.AlignTop)

        self._sec_lbl(cl1, "ОСНОВНОЕ")
        card1 = QWidget(); card1.setStyleSheet(self._CARD_SETTINGS)
        cl1a = QVBoxLayout(card1); cl1a.setContentsMargins(0,0,0,0); cl1a.setSpacing(0)
        self.e_name = self._edit_row(cl1a, "Название",       bot.name)
        cl1a.addWidget(self._div())
        self.e_path = self._edit_row_browse(cl1a,            bot.path)
        cl1.addWidget(card1)

        self._sec_lbl(cl1, "ДОПОЛНИТЕЛЬНО")
        card2 = QWidget(); card2.setStyleSheet(self._CARD_SETTINGS)
        cl1b = QVBoxLayout(card2); cl1b.setContentsMargins(0,0,0,0); cl1b.setSpacing(0)
        self.e_args = self._edit_row(cl1b, "Аргументы",      bot.args,   "--arg value")
        cl1b.addWidget(self._div())
        self.e_py   = self._edit_row(cl1b, "Python команда", bot.python, "python")
        cl1.addWidget(card2)

        self._sec_lbl(cl1, "НАСТРОЙКИ")
        card3 = QWidget(); card3.setStyleSheet(self._CARD_SETTINGS)
        cl1c = QVBoxLayout(card3); cl1c.setContentsMargins(0,0,0,0); cl1c.setSpacing(0)
        self.tg_auto    = self._bot_toggle_row(cl1c, "Автозапуск при старте",      bot.autostart)
        cl1c.addWidget(self._div())
        self.tg_restart = self._bot_toggle_row(cl1c, "Автоперезапуск при падении", bot.auto_restart)
        cl1.addWidget(card3)

        self._sec_lbl(cl1, "РАССЫЛКА")
        card4 = QWidget(); card4.setStyleSheet(self._CARD_SETTINGS)
        cl1d = QVBoxLayout(card4); cl1d.setContentsMargins(0, 0, 0, 0); cl1d.setSpacing(0)

        # ── Путь к боту рассылки ─────────────────────────────────────────────────
        path_row = QWidget(); path_row.setFixedHeight(44)
        path_row.setStyleSheet("QWidget{background:transparent;border:none;}")
        path_rl = QHBoxLayout(path_row); path_rl.setContentsMargins(16, 0, 12, 0); path_rl.setSpacing(8)
        path_lbl = QLabel("Бот")
        path_lbl.setStyleSheet(f"color:{WHITE};font-size:13px;background:transparent;border:none;min-width:30px;")
        _field_ss = (f"QLineEdit{{background:#1c1c1e;border:1px solid #3a3a3c;border-radius:6px;"
                     f"color:#ccc;font-size:12px;padding:0 8px;}}"
                     f"QLineEdit:focus{{border-color:{BLUE};}}")
        self._bc_path_edit = QLineEdit(bot.broadcast_path)
        self._bc_path_edit.setPlaceholderText("C:/Users/.../bot.py")
        self._bc_path_edit.setStyleSheet(_field_ss)
        self._bc_path_edit.textChanged.connect(self._on_bc_path_changed)
        bc_browse = QPushButton("…"); bc_browse.setFixedSize(28, 28)
        bc_browse.setCursor(Qt.CursorShape.PointingHandCursor)
        bc_browse.setStyleSheet("QPushButton{background:#3a3a3c;border:none;color:#fff;border-radius:6px;"
                                "font-size:13px;font-weight:600;min-height:0;padding:0;}"
                                "QPushButton:hover{background:#48484a;}")
        bc_browse.clicked.connect(self._browse_bc_path)
        path_rl.addWidget(path_lbl); path_rl.addWidget(self._bc_path_edit, 1); path_rl.addWidget(bc_browse)
        cl1d.addWidget(path_row)
        cl1d.addWidget(self._div())

        # ── Текстовый ввод ───────────────────────────────────────────────────────
        txt_wrap = QWidget(); txt_wrap.setStyleSheet("QWidget{background:transparent;border:none;}")
        tw_l = QVBoxLayout(txt_wrap); tw_l.setContentsMargins(16, 10, 16, 10)
        self._bc_text = QTextEdit()
        self._bc_text.setPlaceholderText("Введи текст для рассылки...")
        self._bc_text.setFixedHeight(90)
        self._bc_text.setStyleSheet(
            f"QTextEdit{{background:#1c1c1e;border:1px solid #3a3a3c;border-radius:8px;"
            f"color:#ccc;font-size:13px;padding:6px 10px;}}"
            f"QTextEdit:focus{{border-color:{BLUE};}}"
        )
        tw_l.addWidget(self._bc_text)
        cl1d.addWidget(txt_wrap)
        cl1d.addWidget(self._div())

        # ── Кнопка отправки ──────────────────────────────────────────────────────
        btn_row = QWidget(); btn_row.setFixedHeight(52)
        btn_row.setStyleSheet("QWidget{background:transparent;border:none;}")
        btn_rl = QHBoxLayout(btn_row); btn_rl.setContentsMargins(16, 0, 16, 0)
        self._bc_status = QLabel("")
        self._bc_status.setStyleSheet(f"color:{GRAY};font-size:12px;background:transparent;border:none;")
        self._bc_btn = QPushButton("📢 Отправить всем")
        self._bc_btn.setFixedHeight(36)
        self._bc_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._bc_btn.setStyleSheet(
            f"QPushButton{{background:{GREEN};border:none;color:#000;border-radius:8px;"
            f"font-size:12px;font-weight:600;padding:0 14px;}}"
            f"QPushButton:hover{{background:#2db34a;}}"
            f"QPushButton:disabled{{background:#3a3a3c;color:{GRAY};}}"
        )
        self._bc_btn.clicked.connect(self._do_broadcast)
        btn_rl.addWidget(self._bc_status); btn_rl.addStretch(); btn_rl.addWidget(self._bc_btn)
        cl1d.addWidget(btn_row)
        cl1.addWidget(card4)

        sc1.setWidget(w1); self._tabs.addWidget(sc1)

        # Подключаем сигналы
        self.e_name.textChanged.connect(self._on_field_changed)
        self.e_path.textChanged.connect(self._on_field_changed)
        self.e_args.textChanged.connect(self._on_field_changed)
        self.e_py.textChanged.connect(self._on_field_changed)
        self.tg_auto.toggled.connect(lambda v: self._on_field_changed())
        self.tg_restart.toggled.connect(lambda v: self._on_field_changed())
        self.sig_broadcast_line.connect(lambda l: self.add_log(f"[Рассылка] {l}"))
        self.sig_broadcast_done.connect(self._on_broadcast_done)

        self._switch_tab(0)
        self.tmr = QTimer(); self.tmr.timeout.connect(self._tick); self.tmr.start(5000)

    def _switch_tab(self, idx):
        self._tabs.setCurrentIndex(idx)
        _on  = (f"QPushButton{{background:rgba(255,255,255,0.10);border:none;color:{WHITE};"
                f"border-radius:7px;font-size:12px;font-weight:600;padding:0 10px;min-height:0;}}"
                f"QPushButton:hover{{background:rgba(255,255,255,0.15);}}")
        _off = (f"QPushButton{{background:transparent;border:none;color:{GRAY};"
                f"border-radius:7px;font-size:12px;padding:0 10px;min-height:0;}}"
                f"QPushButton:hover{{color:{WHITE};}}")
        self._tab_log.setStyleSheet(_on  if idx == 0 else _off)
        self._tab_set.setStyleSheet(_on  if idx == 1 else _off)

    def _sec(self, pl, t):
        l = QLabel(t)
        l.setStyleSheet(f"color:{GRAY};font-size:11px;letter-spacing:1px;font-weight:600;"
                        f"background:transparent;border:none;margin-bottom:2px;")
        pl.addWidget(l)

    def _sec_lbl(self, pl, t):
        l = QLabel(t)
        l.setStyleSheet(f"color:{GRAY};font-size:11px;letter-spacing:1px;font-weight:600;"
                        f"background:transparent;border:none;margin-top:4px;margin-bottom:2px;")
        pl.addWidget(l)

    def _div(self):
        f = QFrame(); f.setFrameShape(QFrame.Shape.HLine)
        f.setStyleSheet("background:#3a3a3c;max-height:1px;border:none;margin-left:16px;"); return f

    def _vdiv(self):
        f = QFrame(); f.setFrameShape(QFrame.Shape.VLine)
        f.setFixedWidth(1)
        f.setStyleSheet("background:#3a3a3c;border:none;"); return f

    def _stat_col(self, pl, lbl, val, col):
        col_w = QWidget()
        col_w.setStyleSheet("QWidget{background:transparent;border:none;border-radius:0;}")
        vl = QVBoxLayout(col_w); vl.setContentsMargins(0,6,0,6); vl.setSpacing(3)
        vl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lb = QLabel(lbl); lb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lb.setStyleSheet(f"color:{GRAY};font-size:11px;background:transparent;border:none;")
        vl_lbl = QLabel(val); vl_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        vl_lbl.setStyleSheet(f"color:{col};font-size:17px;font-weight:700;background:transparent;border:none;")
        vl.addWidget(lb); vl.addWidget(vl_lbl)
        pl.addWidget(col_w, 1); return vl_lbl

    def _badge_style(self, s):
        styles = {
            "running":    (f"rgba(52,199,89,0.18)",  GREEN,  "работает"),
            "stopped":    ("rgba(72,72,74,0.4)",      GRAY,   "остановлен"),
            "error":      ("rgba(255,69,58,0.18)",    RED,    "ошибка"),
            "restarting": ("rgba(255,214,10,0.18)",   YELLOW, "перезапуск..."),
        }
        bg, col, txt = styles.get(s, styles["stopped"])
        self.badge.setText(txt)
        self.badge.setStyleSheet(
            f"background:{bg};color:{col};padding:2px 10px;border-radius:10px;"
            f"font-size:12px;font-weight:600;border:none;")

    def _toggle(self):
        if self.bot.status in ("running", "restarting"): self.sig_stop.emit(self.bot)
        else: self.sig_start.emit(self.bot)

    def _tick(self):
        self._up_lbl.setText(self.bot.uptime())
        self._ev_lbl.setText(str(self.bot.events))
        self._er_lbl.setText(str(self.bot.errors))

    def set_status(self, s):
        self.bot.status = s; self._badge_style(s)
        colors = {"running": GREEN, "stopped": DARK, "error": RED, "restarting": YELLOW}
        self.dot.set(colors.get(s, DARK))
        self.b_run.setText("Стоп" if s in ("running", "restarting") else "Старт")
        if s in ("running", "restarting"):
            self.b_run.setStyleSheet(
                f"QPushButton{{background:{RED};border:none;color:#fff;border-radius:10px;"
                f"font-size:13px;font-weight:700;min-height:0;}} QPushButton:hover{{background:#d93b31;}}")
        else:
            self.b_run.setStyleSheet(
                f"QPushButton{{background:{GREEN};border:none;color:#000;border-radius:10px;"
                f"font-size:13px;font-weight:700;min-height:0;}} QPushButton:hover{{background:#2db34a;}}")

    def add_log(self, line):
        ts = datetime.now().strftime("%H:%M:%S"); lu = line.upper()
        _append_bot_log(self.bot.id, ts, line)
        # HIDDEN_LOG_PATTERNS — строка всё равно записана на диск строкой выше,
        # просто не показываем её в окне лога (и не считаем в статистике ниже).
        if any(w in lu for w in HIDDEN_LOG_PATTERNS):
            return
        # BENIGN_ERROR_PATTERNS редактируется в manager_config.json (ключ
        # "benign_error_patterns") без пересборки — обрывы связи со штатным
        # автопереподключением не считаем "ошибкой" в статистике.
        if any(w in lu for w in ["ERROR","ОШИБКА","КРИТИЧ","EXCEPTION","FAILED","TRACEBACK"]) \
           and not any(w in lu for w in BENIGN_ERROR_PATTERNS):
            c = RED; self.bot.errors += 1
        elif any(w in lu for w in BENIGN_ERROR_PATTERNS): c = ORANGE
        elif any(w in lu for w in ["WARN","⚠"]): c = ORANGE
        elif any(w in lu for w in ["✅","▶","ЗАПУЩЕН","CONNECTED","ONLINE","ВОССТАНОВЛЕНО"]): c = GREEN
        elif "INFO" in lu: c = GRAY
        else: c = "#aeaeb2"
        sb = self.log_view.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 4
        self.log_view.append(f'<span style="color:#555">[{ts}]</span> <span style="color:{c}">{line}</span>')
        if at_bottom: sb.setValue(sb.maximum())
        if any(w in lu for w in ["CLAIM","WATCH","ОТПРАВЛЕНО","→","СОБЫТИЕ"]): self.bot.events += 1

    # ── Поля редактирования ───────────────────────────────────────────────────
    def _edit_row(self, pl, lbl_text, value="", placeholder=""):
        row = QWidget(); row.setFixedHeight(52)
        row.setStyleSheet("QWidget{background:transparent;border:none;border-radius:0;}")
        rl = QHBoxLayout(row); rl.setContentsMargins(16,0,16,0); rl.setSpacing(12)
        lb = QLabel(lbl_text); lb.setFixedWidth(140)
        lb.setStyleSheet(f"color:{WHITE};font-size:14px;background:transparent;border:none;")
        e = QLineEdit(value); e.setPlaceholderText(placeholder); e.setFixedHeight(36)
        e.setStyleSheet(self._FIELD); e.setAlignment(Qt.AlignmentFlag.AlignRight)
        rl.addWidget(lb); rl.addWidget(e)
        pl.addWidget(row); return e

    def _edit_row_browse(self, pl, value=""):
        row = QWidget(); row.setFixedHeight(52)
        row.setStyleSheet("QWidget{background:transparent;border:none;border-radius:0;}")
        rl = QHBoxLayout(row); rl.setContentsMargins(16,0,16,0); rl.setSpacing(12)
        lb = QLabel("Файл (.py / .exe)"); lb.setFixedWidth(140)
        lb.setStyleSheet(f"color:{WHITE};font-size:14px;background:transparent;border:none;")
        e = QLineEdit(value); e.setPlaceholderText("Путь к файлу"); e.setFixedHeight(36)
        e.setStyleSheet(self._FIELD); e.setAlignment(Qt.AlignmentFlag.AlignRight)
        bb = QPushButton("Обзор"); bb.setFixedSize(70, 30)
        bb.setStyleSheet(f"QPushButton{{background:#3a3a3c;border:none;color:{WHITE};"
                         f"border-radius:7px;font-size:12px;min-height:0;}}"
                         f"QPushButton:hover{{background:#48484a;}}")
        bb.clicked.connect(lambda: self._browse_path(e))
        rl.addWidget(lb); rl.addWidget(e); rl.addWidget(bb)
        pl.addWidget(row); return e

    def _bot_toggle_row(self, pl, text, on=True):
        row = QWidget(); row.setFixedHeight(52)
        row.setStyleSheet("QWidget{background:transparent;border:none;border-radius:0;}")
        rl = QHBoxLayout(row); rl.setContentsMargins(16,0,16,0)
        lb = QLabel(text)
        lb.setStyleSheet(f"color:{WHITE};font-size:14px;background:transparent;border:none;")
        tg = Toggle(on)
        rl.addWidget(lb); rl.addStretch(); rl.addWidget(tg)
        pl.addWidget(row); return tg

    def _browse_path(self, field):
        start_dir = str(Path(field.text()).parent) if field.text().strip() else ""
        p, _ = QFileDialog.getOpenFileName(self, "Выбери файл", start_dir,
                                           "Python/Exe (*.py *.exe);;All (*)")
        if p: field.setText(p)

    def _on_field_changed(self):
        name = self.e_name.text().strip()
        if name:
            self.bot.name = name
            self.title.setText(name)
        self.bot.path        = self.e_path.text().strip()
        self.bot.args        = self.e_args.text().strip()
        py = self.e_py.text().strip()
        self.bot.python      = py if py else "python"
        self.bot.autostart   = self.tg_auto.isChecked()
        self.bot.auto_restart= self.tg_restart.isChecked()
        self.sig_settings_changed.emit(self.bot)

    def _on_bc_path_changed(self, v):
        self.bot.broadcast_path = v.strip()
        self.sig_settings_changed.emit(self.bot)

    def _browse_bc_path(self):
        cur = self._bc_path_edit.text().strip() or self.bot.path
        start_dir = str(Path(cur).parent) if cur else ""
        path, _ = QFileDialog.getOpenFileName(self, "Выбери bot.py", start_dir, "Python файлы (*.py)")
        if path:
            self._bc_path_edit.setText(path)

    def _do_broadcast(self):
        path = self._bc_path_edit.text().strip()
        text = self._bc_text.toPlainText().strip()

        def _set_status(msg, color=GRAY):
            self._bc_status.setText(msg)
            self._bc_status.setStyleSheet(
                f"color:{color};font-size:12px;background:transparent;border:none;")

        if not path:
            _set_status("⚠ Укажи путь к боту в настройках", RED); return
        if not text:
            _set_status("⚠ Введи текст", RED); return

        self._bc_btn.setEnabled(False)
        self._bc_btn.setText("Отправляю...")
        _set_status("⏳ Рассылка идёт...", GRAY)

        py = self.bot.python if self.bot.python else "python"

        def _thread():
            output_lines = []
            try:
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = 0
                env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
                proc = subprocess.Popen(
                    py.split() + [path, "broadcast", text],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    cwd=str(Path(path).parent), env=env,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    startupinfo=si
                )
                for line in proc.stdout:
                    line = line.rstrip()
                    if line:
                        output_lines.append(line)
                        self.sig_broadcast_line.emit(line)
                proc.wait(timeout=180)
                full = "\n".join(output_lines)
            except subprocess.TimeoutExpired:
                proc.kill()
                full = "timeout"
                self.sig_broadcast_line.emit("⚠ Таймаут 180 сек")
            except Exception as e:
                full = f"error:{e}"
                self.sig_broadcast_line.emit(f"❌ {e}")

            self.sig_broadcast_done.emit(full)

        threading.Thread(target=_thread, daemon=True).start()

    def _on_broadcast_done(self, full):
        self._bc_btn.setEnabled(True)
        self._bc_btn.setText("📢 Отправить всем")

        def _set_status(msg, color=GRAY):
            self._bc_status.setText(msg)
            self._bc_status.setStyleSheet(
                f"color:{color};font-size:12px;background:transparent;border:none;")

        import re
        if full.startswith("timeout"):
            _set_status("⚠ Таймаут", YELLOW)
        elif full.startswith("error:"):
            _set_status(f"❌ {full[6:80]}", RED)
        elif "Рассылка завершена" in full:
            m = re.search(r"TG: (\d+)/(\d+), VK: (\d+)/(\d+)", full)
            if m:
                tg_ok, tg_tot = int(m.group(1)), int(m.group(2))
                vk_ok, vk_tot = int(m.group(3)), int(m.group(4))
                failed = (tg_tot - tg_ok) + (vk_tot - vk_ok)
                if failed == 0:
                    _set_status(f"✅ Все получили  TG:{tg_ok}  VK:{vk_ok}", GREEN)
                else:
                    _set_status(f"⚠ TG:{tg_ok}/{tg_tot}  VK:{vk_ok}/{vk_tot}  ({failed} не дошло)", YELLOW)
            else:
                _set_status("✅ Готово", GREEN)
            self._bc_text.clear()
        elif not full.strip():
            _set_status("❌ Нет вывода — проверь путь к боту", RED)
        else:
            last = full.strip().splitlines()[-1]
            _set_status(f"❌ {last[:80]}", RED)

# ── Страница добавления ───────────────────────────────────────────────────────
class AddPage(QWidget):
    added = pyqtSignal(dict)

    _CARD = f"QWidget{{background:#2c2c2e;border-radius:12px;border:1px solid #3a3a3c;}}"
    _FIELD = (f"QLineEdit{{background:transparent;border:none;border-radius:0;"
              f"color:#fff;font-size:14px;padding:0;}} "
              f"QLineEdit:focus{{border:none;background:transparent;}}")

    def __init__(self):
        super().__init__()
        root = QVBoxLayout(self); root.setContentsMargins(0,0,0,0); root.setSpacing(0)
        hdr = QWidget(); hdr.setFixedHeight(56)
        hdr.setStyleSheet(f"background:{BG};border-bottom:1px solid {LINE};")
        hl = QHBoxLayout(hdr); hl.setContentsMargins(20,0,20,0)
        hl.addWidget(label("Добавить бота", 15, WHITE, True)); root.addWidget(hdr)

        sc = QScrollArea(); sc.setWidgetResizable(True)
        sc.setStyleSheet("border:none;background:transparent;")
        w = QWidget(); w.setStyleSheet(f"background:{BG};")
        cl = QVBoxLayout(w); cl.setContentsMargins(24,24,24,24); cl.setSpacing(16)
        cl.setAlignment(Qt.AlignmentFlag.AlignTop)

        # ── Секция: основные ──
        self._sec_lbl(cl, "ОСНОВНОЕ")
        card1 = self._card()
        cl1 = QVBoxLayout(card1); cl1.setContentsMargins(0,0,0,0); cl1.setSpacing(0)
        self.f_name = self._row(cl1, "Название", "Мой бот")
        cl1.addWidget(self._divider())
        self.f_path = self._row_browse(cl1)
        cl.addWidget(card1)

        # ── Секция: дополнительно ──
        self._sec_lbl(cl, "ДОПОЛНИТЕЛЬНО")
        card2 = self._card()
        cl2 = QVBoxLayout(card2); cl2.setContentsMargins(0,0,0,0); cl2.setSpacing(0)
        self.f_args = self._row(cl2, "Аргументы", "--arg value")
        cl2.addWidget(self._divider())
        self.f_py   = self._row(cl2, "Python команда", "python")
        cl.addWidget(card2)

        # ── Секция: настройки ──
        self._sec_lbl(cl, "НАСТРОЙКИ")
        card3 = self._card()
        cl3 = QVBoxLayout(card3); cl3.setContentsMargins(0,0,0,0); cl3.setSpacing(0)
        self.ca = self._toggle_row(cl3, "Автозапуск при старте", True)
        cl3.addWidget(self._divider())
        self.cr = self._toggle_row(cl3, "Автоперезапуск при падении", True)
        cl.addWidget(card3)

        ab = QPushButton("Добавить бота"); ab.setFixedHeight(46)
        ab.setStyleSheet(
            f"QPushButton{{background:rgba(52,199,89,0.12);border:1px solid rgba(52,199,89,0.30);"
            f"border-radius:12px;color:{GREEN};font-size:14px;font-weight:600;min-height:0;}}"
            f"QPushButton:hover{{background:rgba(52,199,89,0.18);border-color:rgba(52,199,89,0.50);}}"
            f"QPushButton:pressed{{background:rgba(52,199,89,0.08);}}"
        )
        ab.clicked.connect(self._add); cl.addWidget(ab)
        sc.setWidget(w); root.addWidget(sc)

    def _sec_lbl(self, pl, t):
        l = QLabel(t)
        l.setStyleSheet(f"color:{GRAY};font-size:11px;letter-spacing:1px;font-weight:600;"
                        f"background:transparent;border:none;margin-top:4px;margin-bottom:2px;")
        pl.addWidget(l)

    def _card(self):
        c = QWidget(); c.setStyleSheet(self._CARD); return c

    def _divider(self):
        line = QFrame(); line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("background:#3a3a3c;max-height:1px;border:none;"
                           "margin-left:16px;"); return line

    def _row(self, pl, lbl, ph=""):
        row = QWidget(); row.setFixedHeight(52)
        row.setStyleSheet("QWidget{background:transparent;border:none;border-radius:0;}")
        rl = QHBoxLayout(row); rl.setContentsMargins(16,0,16,0); rl.setSpacing(12)
        lb = QLabel(lbl); lb.setFixedWidth(140)
        lb.setStyleSheet(f"color:{WHITE};font-size:14px;background:transparent;border:none;")
        e = QLineEdit(); e.setPlaceholderText(ph); e.setFixedHeight(36)
        e.setStyleSheet(self._FIELD)
        e.setAlignment(Qt.AlignmentFlag.AlignRight)
        rl.addWidget(lb); rl.addWidget(e)
        if pl is not None: pl.addWidget(row)
        return e

    def _row_browse(self, pl):
        row = QWidget(); row.setFixedHeight(52)
        row.setStyleSheet("QWidget{background:transparent;border:none;border-radius:0;}")
        rl = QHBoxLayout(row); rl.setContentsMargins(16,0,16,0); rl.setSpacing(12)
        lb = QLabel("Файл (.py / .exe)"); lb.setFixedWidth(140)
        lb.setStyleSheet(f"color:{WHITE};font-size:14px;background:transparent;border:none;")
        e = QLineEdit(); e.setPlaceholderText("Путь к файлу"); e.setFixedHeight(36)
        e.setStyleSheet(self._FIELD); e.setAlignment(Qt.AlignmentFlag.AlignRight)
        bb = QPushButton("Обзор"); bb.setFixedSize(70, 30)
        bb.setStyleSheet(f"QPushButton{{background:#3a3a3c;border:none;color:{WHITE};"
                         f"border-radius:7px;font-size:12px;min-height:0;}}"
                         f"QPushButton:hover{{background:#48484a;}}")
        bb.clicked.connect(self._browse)
        rl.addWidget(lb); rl.addWidget(e); rl.addWidget(bb)
        pl.addWidget(row); return e

    def _toggle_row(self, pl, text, on=True):
        row = QWidget(); row.setFixedHeight(52)
        row.setStyleSheet("QWidget{background:transparent;border:none;border-radius:0;}")
        rl = QHBoxLayout(row); rl.setContentsMargins(16,0,16,0)
        lb = QLabel(text)
        lb.setStyleSheet(f"color:{WHITE};font-size:14px;background:transparent;border:none;")
        tg = Toggle(on)
        rl.addWidget(lb); rl.addStretch(); rl.addWidget(tg)
        pl.addWidget(row); return tg

    def _browse(self):
        p,_ = QFileDialog.getOpenFileName(self,"Выбери файл","","Python/Exe (*.py *.exe);;All (*)")
        if p: self.f_path.setText(p)
        if p and not self.f_name.text(): self.f_name.setText(Path(p).stem)

    def _add(self):
        n = self.f_name.text().strip(); p = self.f_path.text().strip()
        if not n or not p: QMessageBox.warning(self,"Ошибка","Укажи название и путь"); return
        self.added.emit({"id":str(time.time_ns()),"name":n,"path":p,
            "args":self.f_args.text().strip(),"python":self.f_py.text().strip() or "python",
            "autostart":self.ca.isChecked(),"auto_restart":self.cr.isChecked()})
        self.f_name.clear(); self.f_path.clear(); self.f_args.clear()

# ── Страница настроек ─────────────────────────────────────────────────────────
class SetPage(QWidget):
    changed              = pyqtSignal(dict)
    bot_autostart_changed = pyqtSignal(str, bool)
    check_update_requested = pyqtSignal()
    install_update_requested = pyqtSignal()
    _CARD = "QWidget{background:#2c2c2e;border-radius:12px;border:none;}"
    _TR   = "QWidget{background:transparent;border:none;border-radius:0;}"
    _BTN_DEFAULT = (f"QPushButton{{background:#3a3a3c;border:none;color:#fff;border-radius:8px;"
                    f"font-size:12px;font-weight:600;padding:0 12px;}}"
                    f"QPushButton:hover{{background:#48484a;}}")
    _BTN_GREEN   = (f"QPushButton{{background:#34C759;border:none;color:#000;border-radius:8px;"
                    f"font-size:12px;font-weight:600;padding:0 12px;}}"
                    f"QPushButton:hover{{background:#2db34a;}}")

    def __init__(self, s, bots):
        super().__init__(); self.s = s; self._bots = bots
        self._bot_toggles = {}; self._bot_dots = {}
        self._update_mode = False
        root = QVBoxLayout(self); root.setContentsMargins(0,0,0,0); root.setSpacing(0)
        hdr = QWidget(); hdr.setFixedHeight(56)
        hdr.setStyleSheet(f"background:{BG};border-bottom:1px solid {LINE};")
        hl = QHBoxLayout(hdr); hl.setContentsMargins(20,0,20,0)
        hl.addWidget(label("Настройки", 17, WHITE, True)); root.addWidget(hdr)

        sc = QScrollArea(); sc.setWidgetResizable(True)
        sc.setStyleSheet("border:none;background:transparent;")
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        w = QWidget(); w.setStyleSheet(f"background:{BG};")
        cl = QVBoxLayout(w); cl.setContentsMargins(16,12,16,12); cl.setSpacing(10)
        cl.setAlignment(Qt.AlignmentFlag.AlignTop)

        # ── ЗАПУСК — с динамическим списком ботов ──
        self._build_launch_section(cl)

        self._section(cl, "УВЕДОМЛЕНИЯ", [
            ("Уведомления Windows", "win_notifications"),
            ("Звук при ошибке",     "sound"),
        ])
        self._section(cl, "ИНТЕРФЕЙС", [
            ("Сворачивать в трей",    "minimize_to_tray"),
        ])

        # ── ОБНОВЛЕНИЯ ──
        upd_lbl = QLabel("ОБНОВЛЕНИЯ")
        upd_lbl.setStyleSheet(f"color:{GRAY};font-size:11px;letter-spacing:1px;font-weight:600;"
                              f"background:transparent;border:none;margin-bottom:2px;")
        cl.addWidget(upd_lbl)

        upd_card = QWidget(); upd_card.setStyleSheet(self._CARD)
        upd_row = QWidget(); upd_row.setFixedHeight(48)
        upd_row.setStyleSheet(self._TR)
        upd_rl = QHBoxLayout(upd_row); upd_rl.setContentsMargins(16,0,12,0); upd_rl.setSpacing(8)

        ver_lbl = QLabel(f"v{APP_VERSION}")
        ver_lbl.setStyleSheet(f"color:{GRAY};font-size:13px;background:transparent;border:none;")
        self._upd_status = QLabel("")
        self._upd_status.setStyleSheet(f"color:{GRAY};font-size:12px;background:transparent;border:none;")

        self._upd_check_btn = QPushButton("Проверить обновления")
        self._upd_check_btn.setFixedHeight(32)
        self._upd_check_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._upd_check_btn.setStyleSheet(self._BTN_DEFAULT)
        self._upd_check_btn.clicked.connect(self._on_check_update_clicked)

        upd_rl.addWidget(ver_lbl)
        upd_rl.addWidget(self._upd_status)
        upd_rl.addStretch()
        upd_rl.addWidget(self._upd_check_btn)

        upd_vl = QVBoxLayout(upd_card); upd_vl.setContentsMargins(0,0,0,0); upd_vl.setSpacing(0)
        upd_vl.addWidget(upd_row)
        cl.addWidget(upd_card)
        sc.setWidget(w); root.addWidget(sc)

    # ── Строим секцию ЗАПУСК ──────────────────────────────────────────────────
    def _build_launch_section(self, cl):
        lbl = QLabel("ЗАПУСК")
        lbl.setStyleSheet(f"color:{GRAY};font-size:11px;letter-spacing:1px;font-weight:600;"
                          f"background:transparent;border:none;margin-bottom:2px;")
        cl.addWidget(lbl)

        card = QWidget(); card.setStyleSheet(self._CARD)
        vl = QVBoxLayout(card); vl.setContentsMargins(0,0,0,0); vl.setSpacing(0)
        vl.addWidget(self._row("Запуск при старте Windows",   "autostart_app"))
        vl.addWidget(self._div())
        vl.addWidget(self._row("Запускать ботов при старте",  "autostart_bots"))
        vl.addWidget(self._div())
        vl.addWidget(self._row("Подключать прокси при старте", "vless_autostart"))
        vl.addWidget(self._div())
        vl.addWidget(self._row("Автоперезапуск ботов", "auto_restart"))

        # Контейнер списка ботов (перестраивается при refresh_bots)
        self._bot_container = QWidget()
        self._bot_container.setStyleSheet("QWidget{background:transparent;border:none;}")
        self._bot_cl = QVBoxLayout(self._bot_container)
        self._bot_cl.setContentsMargins(0,0,0,0); self._bot_cl.setSpacing(0)
        vl.addWidget(self._bot_container)

        cl.addWidget(card)
        self.refresh_bots()

    # ── Перестройка списка ботов ──────────────────────────────────────────────
    def refresh_bots(self):
        # Чистим старое содержимое
        while self._bot_cl.count():
            item = self._bot_cl.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        self._bot_toggles.clear()
        self._bot_dots = {}

        # VLESS Proxy не показываем в этом списке — за его подключение при
        # старте уже отвечает отдельный пункт "Подключать прокси при старте"
        # выше; отдельный переключатель автозапуска тут был избыточным вторым
        # путём запуска того же самого процесса и только путал.
        bots = [b for b in self._bots if b.id != "__vless__"]
        if not bots:
            return

        # Разделитель + шапка с кнопками
        self._bot_cl.addWidget(self._div())
        btn_row = QWidget(); btn_row.setFixedHeight(38)
        btn_row.setStyleSheet(self._TR)
        brl = QHBoxLayout(btn_row); brl.setContentsMargins(16,0,16,0); brl.setSpacing(8)
        brl.addWidget(label("Выбрать:", 12, GRAY)); brl.addStretch()

        _btn_ss = (f"QPushButton{{background:#3a3a3c;border:none;color:{WHITE};border-radius:7px;"
                   f"font-size:12px;padding:0 12px;min-height:0;}}"
                   f"QPushButton:hover{{background:#48484a;}}")
        b_all  = QPushButton("Все вкл");  b_all.setFixedHeight(28)
        b_all.setStyleSheet(_btn_ss); b_all.clicked.connect(self._select_all)

        b_none = QPushButton("Все выкл"); b_none.setFixedHeight(28)
        b_none.setStyleSheet(_btn_ss); b_none.clicked.connect(self._select_none)

        brl.addWidget(b_all); brl.addWidget(b_none)
        self._bot_cl.addWidget(btn_row)

        # Строки ботов
        for bot in bots:
            self._bot_cl.addWidget(self._div_indent())
            row = QWidget(); row.setFixedHeight(40); row.setStyleSheet(self._TR)
            rl = QHBoxLayout(row); rl.setContentsMargins(28,0,16,0); rl.setSpacing(10)
            dot = Dot(GREEN if bot.autostart else DARK, 7)
            lb = QLabel(bot.name)
            lb.setStyleSheet(f"color:{WHITE};font-size:13px;background:transparent;border:none;")
            tg = Toggle(bot.autostart, w=36, h=21)
            tg.toggled.connect(lambda v, b=bot, d=dot: self._on_bot_tog(b, d, v))
            rl.addWidget(dot); rl.addWidget(lb); rl.addStretch(); rl.addWidget(tg)
            self._bot_toggles[bot.id] = tg
            self._bot_dots[bot.id]    = dot
            self._bot_cl.addWidget(row)

    def _on_bot_tog(self, bot, dot, val):
        dot.set(GREEN if val else DARK)
        self.bot_autostart_changed.emit(bot.id, val)

    def _select_all(self):
        ids = []
        for bot in list(self._bots):
            t = self._bot_toggles.get(bot.id)
            d = self._bot_dots.get(bot.id)
            if t:
                t.setChecked(True)
                if d: d.set(GREEN)
                ids.append((bot.id, True))
        for bot_id, val in ids:
            self.bot_autostart_changed.emit(bot_id, val)

    def _select_none(self):
        ids = []
        for bot in list(self._bots):
            t = self._bot_toggles.get(bot.id)
            d = self._bot_dots.get(bot.id)
            if t:
                t.setChecked(False)
                if d: d.set(DARK)
                ids.append((bot.id, False))
        for bot_id, val in ids:
            self.bot_autostart_changed.emit(bot_id, val)

    def _on_check_update_clicked(self):
        if self._update_mode:
            self._upd_check_btn.setText("⏳ Скачиваю...")
            self._upd_check_btn.setEnabled(False)
            self.install_update_requested.emit()
            return
        self._upd_check_btn.setEnabled(False)
        self._upd_status.setText("Проверяю...")
        self._upd_status.setStyleSheet(f"color:{GRAY};font-size:12px;background:transparent;border:none;")
        self.check_update_requested.emit()

    def show_update_available(self, ver):
        self._update_mode = True
        self._upd_status.setText(f"Доступно v{ver}")
        self._upd_status.setStyleSheet(f"color:{GREEN};font-size:12px;background:transparent;border:none;")
        self._upd_check_btn.setText(f"🔄 Обновить до v{ver}")
        self._upd_check_btn.setStyleSheet(self._BTN_GREEN)
        self._upd_check_btn.setEnabled(True)

    def set_update_status(self, text, color):
        self._update_mode = False
        self._upd_check_btn.setText("Проверить обновления")
        self._upd_check_btn.setStyleSheet(self._BTN_DEFAULT)
        self._upd_status.setText(text)
        self._upd_status.setStyleSheet(f"color:{color};font-size:12px;background:transparent;border:none;")
        self._upd_check_btn.setEnabled(True)

    def set_download_error(self):
        self._update_mode = False
        self._upd_check_btn.setText("❌ Ошибка — попробовать снова")
        self._upd_check_btn.setStyleSheet(self._BTN_DEFAULT)
        self._upd_check_btn.setEnabled(True)

    # ── Хелперы ───────────────────────────────────────────────────────────────
    def _section(self, cl, title, rows):
        lbl = QLabel(title)
        lbl.setStyleSheet(f"color:{GRAY};font-size:11px;letter-spacing:1px;font-weight:600;"
                          f"background:transparent;border:none;margin-bottom:2px;")
        cl.addWidget(lbl)
        card = QWidget(); card.setStyleSheet(self._CARD)
        vl = QVBoxLayout(card); vl.setContentsMargins(0,0,0,0); vl.setSpacing(0)
        for i, (text, key) in enumerate(rows):
            if i: vl.addWidget(self._div())
            vl.addWidget(self._row(text, key))
        cl.addWidget(card)

    def _div(self):
        f = QFrame(); f.setFrameShape(QFrame.Shape.HLine)
        f.setStyleSheet("background:#3a3a3c;max-height:1px;border:none;margin-left:16px;"); return f

    def _div_indent(self):
        f = QFrame(); f.setFrameShape(QFrame.Shape.HLine)
        f.setStyleSheet("background:#3a3a3c;max-height:1px;border:none;margin-left:28px;"); return f

    def _row(self, t, key):
        row = QWidget(); row.setFixedHeight(44)
        row.setStyleSheet(self._TR)
        rl = QHBoxLayout(row); rl.setContentsMargins(16,0,16,0)
        rl.addWidget(label(t, 13, WHITE)); rl.addStretch()
        tg = Toggle(self.s.get(key, False))
        tg.toggled.connect(lambda v, k=key: self._tog(k, v))
        rl.addWidget(tg); return row

    def _tog(self, k, v): self.s[k] = v; self.changed.emit(self.s)

# ── Змейка ────────────────────────────────────────────────────────────────────
class SnakeWidget(QWidget):
    score_updated = pyqtSignal(int)
    CELL, COLS, ROWS = 20, 32, 24

    def __init__(self):
        super().__init__()
        self.setFixedSize(self.COLS * self.CELL, self.ROWS * self.CELL)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._tmr = QTimer(self); self._tmr.timeout.connect(self._step)
        self._reset()

    def mousePressEvent(self, e):
        self.setFocus()
        super().mousePressEvent(e)

    def _reset(self):
        cx, cy = self.COLS // 2, self.ROWS // 2
        self._snake = [(cx, cy), (cx-1, cy), (cx-2, cy)]
        self._dir = (1, 0); self._ndir = (1, 0)
        self._food = self._new_food()
        self._score = 0; self._alive = True; self._going = False
        self.update()

    def _new_food(self):
        free = [(x,y) for x in range(self.COLS) for y in range(self.ROWS) if (x,y) not in self._snake]
        return random.choice(free) if free else (0, 0)

    def _step(self):
        if not self._alive: return
        self._dir = self._ndir
        hx, hy = self._snake[0]; nx, ny = hx+self._dir[0], hy+self._dir[1]
        if not (0 <= nx < self.COLS and 0 <= ny < self.ROWS) or (nx,ny) in self._snake:
            self._alive = False; self._tmr.stop(); self.update(); return
        self._snake.insert(0, (nx, ny))
        if (nx, ny) == self._food:
            self._score += 1; self.score_updated.emit(self._score)
            self._food = self._new_food()
            self._tmr.setInterval(max(70, 150 - self._score * 3))
        else:
            self._snake.pop()
        self.update()

    def keyPressEvent(self, e):
        k = e.key(); mv = None
        if k in (Qt.Key.Key_Up, Qt.Key.Key_W):     mv = (0, -1)
        elif k in (Qt.Key.Key_Down, Qt.Key.Key_S):  mv = (0,  1)
        elif k in (Qt.Key.Key_Left, Qt.Key.Key_A):  mv = (-1, 0)
        elif k in (Qt.Key.Key_Right, Qt.Key.Key_D): mv = (1,  0)
        if mv:
            if mv[0]+self._dir[0] != 0 or mv[1]+self._dir[1] != 0: self._ndir = mv
            if not self._going and self._alive: self._going = True; self._tmr.start(150)
        elif k == Qt.Key.Key_Space:
            if not self._alive: self._reset(); self.score_updated.emit(0)
            elif not self._going: self._going = True; self._tmr.start(150)
            elif self._tmr.isActive(): self._tmr.stop()
            else: self._tmr.start()

    def paintEvent(self, _):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        C = self.CELL
        p.fillRect(self.rect(), QColor("#0d0d0d"))
        p.setPen(QColor("#191919"))
        for x in range(0, self.width(), C): p.drawLine(x, 0, x, self.height())
        for y in range(0, self.height(), C): p.drawLine(0, y, self.width(), y)
        p.setPen(Qt.PenStyle.NoPen)
        fx, fy = self._food
        p.setBrush(QColor(RED)); p.drawEllipse(fx*C+3, fy*C+3, C-6, C-6)
        for i, (sx, sy) in enumerate(self._snake):
            p.setBrush(QColor("#32d74b") if i == 0 else QColor("#228a34" if i%2 else "#27a83e"))
            p.drawRoundedRect(sx*C+1, sy*C+1, C-2, C-2, 5, 5)
        if not self._going and self._alive:
            p.fillRect(self.rect(), QColor(0, 0, 0, 160))
            p.setPen(QColor(WHITE)); p.setFont(QFont("Segoe UI", 13, 700))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Нажми Space или стрелку для старта")
        elif not self._alive:
            p.fillRect(self.rect(), QColor(0, 0, 0, 170))
            p.setPen(QColor(RED)); p.setFont(QFont("Segoe UI", 24, 700))
            p.drawText(QRect(0, self.height()//2-55, self.width(), 45), Qt.AlignmentFlag.AlignCenter, "GAME OVER")
            p.setPen(QColor(WHITE)); p.setFont(QFont("Segoe UI", 13))
            p.drawText(QRect(0, self.height()//2-5, self.width(), 30), Qt.AlignmentFlag.AlignCenter, f"Счёт: {self._score}")
            p.setPen(QColor(GRAY)); p.setFont(QFont("Segoe UI", 11))
            p.drawText(QRect(0, self.height()//2+28, self.width(), 30), Qt.AlignmentFlag.AlignCenter, "Space — играть снова")
        p.end()

# ── Страница Главное ───────────────────────────────────────────────────────────
# ── Лобби ─────────────────────────────────────────────────────────────────────
class HomePage(QWidget):
    play_snake = pyqtSignal()
    play_dino  = pyqtSignal()

    def __init__(self):
        super().__init__()
        root = QVBoxLayout(self); root.setContentsMargins(0,0,0,0); root.setSpacing(0)
        hdr = QWidget(); hdr.setFixedHeight(52)
        hdr.setStyleSheet(f"background:{BG};border-bottom:1px solid {LINE};")
        hl = QHBoxLayout(hdr); hl.setContentsMargins(16,0,12,0)
        hl.addWidget(label("🎮 Главное", 15, WHITE, True))
        root.addWidget(hdr)
        ga = QWidget(); ga.setStyleSheet(f"background:{BG};")
        gl = QVBoxLayout(ga); gl.setAlignment(Qt.AlignmentFlag.AlignCenter); gl.setSpacing(32)
        t = label("Выбери игру", 20, WHITE, True)
        t.setAlignment(Qt.AlignmentFlag.AlignCenter); gl.addWidget(t)
        row = QWidget(); rl = QHBoxLayout(row); rl.setSpacing(32); rl.setContentsMargins(0,0,0,0)
        rl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        rl.addWidget(self._card("🐍", "Змейка",
            "Классическая змейка\nСобирай еду, не врезайся!", self.play_snake))
        rl.addWidget(self._card("🦕", "Динозаврик",
            "Прыгай через кактусы\nкак в Chrome без интернета!", self.play_dino))
        gl.addWidget(row); root.addWidget(ga)

    def _card(self, icon, title, desc, sig):
        card = QWidget(); card.setFixedSize(230, 210)
        card.setStyleSheet(f"QWidget{{background:{CARD};border-radius:14px;border:1px solid {LINE};}}")
        cl = QVBoxLayout(card); cl.setContentsMargins(20,20,20,20); cl.setSpacing(8)
        cl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ic = QLabel(icon); ic.setStyleSheet("font-size:50px;background:transparent;border:none;")
        ic.setAlignment(Qt.AlignmentFlag.AlignCenter); cl.addWidget(ic)
        tl = label(title, 16, WHITE, True); tl.setAlignment(Qt.AlignmentFlag.AlignCenter); cl.addWidget(tl)
        dl = label(desc, 11, GRAY); dl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dl.setWordWrap(True); cl.addWidget(dl)
        b = QPushButton("Играть"); b.setFixedHeight(34)
        b.setStyleSheet(f"QPushButton{{background:{GREEN};border:none;color:#000;border-radius:8px;"
                        f"font-size:13px;font-weight:600;min-height:0;}} QPushButton:hover{{background:#2ec945;}}")
        b.clicked.connect(sig.emit); cl.addWidget(b)
        return card

class SnakePage(QWidget):
    back = pyqtSignal()

    def __init__(self):
        super().__init__()
        root = QVBoxLayout(self); root.setContentsMargins(0,0,0,0); root.setSpacing(0)
        hdr = QWidget(); hdr.setFixedHeight(52)
        hdr.setStyleSheet(f"background:{BG};border-bottom:1px solid {LINE};")
        hl = QHBoxLayout(hdr); hl.setContentsMargins(16,0,12,0)
        bb = QPushButton("← Назад"); bb.setFixedHeight(28)
        bb.setStyleSheet(f"QPushButton{{background:transparent;border:none;color:{GRAY};font-size:12px;padding:0 8px;min-height:0;}} QPushButton:hover{{color:{WHITE};}}")
        bb.clicked.connect(self.back.emit); hl.addWidget(bb)
        hl.addWidget(label("🐍 Змейка", 15, WHITE, True)); hl.addStretch()
        self._slbl = label("Счёт: 0", 14, GREEN, True)
        hl.addWidget(self._slbl); root.addWidget(hdr)
        ga = QWidget(); ga.setStyleSheet(f"background:{BG};")
        gl = QVBoxLayout(ga); gl.setAlignment(Qt.AlignmentFlag.AlignCenter); gl.setSpacing(14)
        self.game = SnakeWidget()
        self.game.score_updated.connect(lambda s: self._slbl.setText(f"Счёт: {s}"))
        gl.addWidget(self.game, 0, Qt.AlignmentFlag.AlignCenter)
        hint = label("↑ ↓ ← →  /  WASD — движение  |  Space — старт / пауза", 11, GRAY)
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter); gl.addWidget(hint)
        root.addWidget(ga)

    def showEvent(self, e):
        super().showEvent(e)
        QTimer.singleShot(50, self.game.setFocus)

# ── Динозаврик ────────────────────────────────────────────────────────────────
class DinoWidget(QWidget):
    score_updated = pyqtSignal(int)
    GW, GH = 750, 230
    GROUND = 185
    GRAVITY  = 0.6          # как в оригинале — более «висячий» прыжок
    JUMP_V   = -13.5        # высота прыжка под новую гравитацию

    def __init__(self):
        super().__init__()
        self.setFixedSize(self.GW, self.GH)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._tmr = QTimer(self); self._tmr.setInterval(16); self._tmr.timeout.connect(self._step)
        self._hi = 0
        self._reset()

    def _reset(self):
        self._dy = float(self.GROUND - 60)
        self._dvy = 0.0; self._grnd = True
        self._obs = []; self._score = 0
        self._speed = 6.0           # стартовая скорость как в оригинале
        self._frame = 0
        self._alive = True; self._started = False
        self._spawn_t = self._next_gap()   # первый gap сразу правильный
        self.update()

    def _next_gap(self):
        # Gap масштабируется со скоростью → время между препятствиями ~1–1.8 сек
        return random.uniform(self._speed * 55, self._speed * 90)

    def _jump(self):
        if self._grnd: self._dvy = self.JUMP_V; self._grnd = False

    def _act(self):
        if not self._alive: self._reset()
        elif not self._started: self._started = True; self._tmr.start(); self._jump()
        else: self._jump()

    def _step(self):
        if not self._alive: return
        self._frame += 1; self._score += 1
        if self._frame % 6 == 0: self.score_updated.emit(self._score // 6)
        # Разгон медленный и плавный, как в оригинальном Chrome (макс ~13)
        self._speed = min(13.0, 6.0 + self._score * 0.0007)
        self._dvy += self.GRAVITY; self._dy += self._dvy
        if self._dy >= self.GROUND - 60:
            self._dy = float(self.GROUND - 60); self._dvy = 0.0; self._grnd = True
        self._spawn_t -= self._speed
        if self._spawn_t <= 0:
            h = random.randint(35, 65); w = random.randint(18, 28)
            self._obs.append([float(self.GW), float(self.GROUND - h), float(w), float(h)])
            self._spawn_t = self._next_gap()    # gap пересчитывается с текущей скоростью
        for o in self._obs: o[0] -= self._speed
        self._obs = [o for o in self._obs if o[0] > -60]
        dr = QRect(25, int(self._dy)+6, 32, 50)
        for o in self._obs:
            if dr.intersects(QRect(int(o[0])+3, int(o[1]), int(o[2])-4, int(o[3]))):
                self._alive = False; self._tmr.stop()
                self._hi = max(self._hi, self._score // 6); break
        self.update()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Space, Qt.Key.Key_Up, Qt.Key.Key_W): self._act()

    def mousePressEvent(self, e):
        self.setFocus(); self._act()

    def _draw_dino(self, p, x, y):
        c = QColor("#32d74b") if self._alive else QColor(GRAY)
        p.setBrush(c); p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(x, y+24, 36, 28, 4, 4)          # тело
        p.drawRoundedRect(x+14, y, 28, 24, 5, 5)           # голова
        p.drawRoundedRect(x-12, y+30, 15, 8, 3, 3)         # хвост
        p.setBrush(QColor("#000")); p.drawRect(x+34, y+6, 5, 5)  # глаз
        p.setBrush(c)
        leg = (self._frame // 6) % 2 if self._started and self._grnd and self._alive else 0
        if leg == 0:
            p.drawRect(x+6, y+52, 10, 14); p.drawRect(x+22, y+52, 10, 8)
        else:
            p.drawRect(x+6, y+52, 10, 8);  p.drawRect(x+22, y+52, 10, 14)

    def paintEvent(self, _):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("#0d0d0d"))
        p.setPen(QPen(QColor("#333"), 2)); p.drawLine(0, self.GROUND, self.GW, self.GROUND)
        p.setPen(QPen(QColor("#1e1e1e"), 2))
        off = int(self._frame * self._speed) % 60
        for i in range(-60, self.GW + 60, 60):
            p.drawLine(i - off, self.GROUND+6, i - off + 20, self.GROUND+6)
        p.setPen(Qt.PenStyle.NoPen)
        for o in self._obs:
            p.setBrush(QColor("#2d7a3a"))
            p.drawRoundedRect(int(o[0]), int(o[1]), int(o[2]), int(o[3]), 4, 4)
            arm_h = int(o[3]) // 3
            p.drawRoundedRect(int(o[0])-9, int(o[1])+arm_h, 9, 12, 3, 3)
            p.drawRoundedRect(int(o[0])+int(o[2]), int(o[1])+arm_h+10, 9, 12, 3, 3)
        self._draw_dino(p, 20, int(self._dy))
        sc = self._score // 6
        p.setPen(QColor(GRAY)); p.setFont(QFont("Segoe UI", 11, 600))
        p.drawText(QRect(0, 10, self.GW - 10, 25), Qt.AlignmentFlag.AlignRight,
                   f"HI {self._hi:05d}   {sc:05d}")
        if not self._started and self._alive:
            p.fillRect(self.rect(), QColor(0,0,0,150))
            p.setPen(QColor(WHITE)); p.setFont(QFont("Segoe UI", 14, 700))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Space / ↑ — старт")
        elif not self._alive:
            p.fillRect(self.rect(), QColor(0,0,0,160))
            p.setPen(QColor(WHITE)); p.setFont(QFont("Segoe UI", 18, 700))
            p.drawText(QRect(0, self.GH//2-45, self.GW, 38), Qt.AlignmentFlag.AlignCenter, "GAME OVER")
            p.setPen(QColor(GRAY)); p.setFont(QFont("Segoe UI", 11))
            p.drawText(QRect(0, self.GH//2+5, self.GW, 28), Qt.AlignmentFlag.AlignCenter, "Space — играть снова")
        p.end()


class DinoPage(QWidget):
    back = pyqtSignal()

    def __init__(self):
        super().__init__()
        root = QVBoxLayout(self); root.setContentsMargins(0,0,0,0); root.setSpacing(0)
        hdr = QWidget(); hdr.setFixedHeight(52)
        hdr.setStyleSheet(f"background:{BG};border-bottom:1px solid {LINE};")
        hl = QHBoxLayout(hdr); hl.setContentsMargins(16,0,12,0)
        bb = QPushButton("← Назад"); bb.setFixedHeight(28)
        bb.setStyleSheet(f"QPushButton{{background:transparent;border:none;color:{GRAY};font-size:12px;padding:0 8px;min-height:0;}} QPushButton:hover{{color:{WHITE};}}")
        bb.clicked.connect(self.back.emit); hl.addWidget(bb)
        hl.addWidget(label("🦕 Динозаврик", 15, WHITE, True)); hl.addStretch()
        root.addWidget(hdr)
        self.view = QWebEngineView()
        self.view.setStyleSheet(f"background:{BG};")
        from PyQt6.QtGui import QColor as _QColor
        self.view.page().setBackgroundColor(_QColor(BG))
        dino_path = RES_DIR / "dino" / "index.html"
        self.view.load(QUrl.fromLocalFile(str(dino_path)))
        self.view.loadFinished.connect(self._on_loaded)
        root.addWidget(self.view)

    def _on_loaded(self):
        self.view.page().runJavaScript("""
            var st = document.createElement('style');
            st.textContent = `
                html, body {
                    background: #1c1c1e !important;
                    overflow: hidden !important;
                    margin: 0 !important; padding: 0 !important;
                    width: 100% !important; height: 100% !important;
                }
                .icon-offline, #main-message, .error-code,
                #suggestions, #details, #sub-frame-error { display: none !important; }
                #main-frame-error, .interstitial-wrapper {
                    background: #1c1c1e !important;
                    display: flex !important; flex-direction: column !important;
                    align-items: center !important; justify-content: center !important;
                    width: 100% !important; height: 100vh !important;
                    padding: 0 !important; margin: 0 !important; max-width: none !important;
                }
                .runner-container { transition: none !important; }
            `;
            document.head.appendChild(st);

            // Глобальная функция — вызывается из Python при ресайзе
            window._dinoScale = function(vw, vh) {
                var runner = document.querySelector('.runner-container');
                if (!runner) return;
                var scale = Math.min(vw / 600, vh / 320) * 0.82;
                scale = Math.max(scale, 0.5);
                runner.style.transform = 'scale(' + scale + ')';
                runner.style.transformOrigin = 'center center';
            };

            // Первый вызов после инициализации игры
            setTimeout(function() {
                window._dinoScale(window.innerWidth, window.innerHeight);
            }, 400);
        """)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        vw = self.view.width()
        vh = self.view.height()
        # Пересчитываем масштаб из Python — надёжнее window.resize внутри WebEngine
        QTimer.singleShot(60, lambda: self.view.page().runJavaScript(
            f"if(window._dinoScale) window._dinoScale({vw},{vh});"
        ))
        QTimer.singleShot(120, self.view.setFocus)

    def showEvent(self, e):
        super().showEvent(e); QTimer.singleShot(50, self.view.setFocus)

# ── Страница: Сеть / Прокси (VLESS через Xray-core) ────────────────────────────
class NetPage(QWidget):
    sig_connect = pyqtSignal(str, str)   # raw_input, имя профиля
    sig_disconnect = pyqtSignal()
    sig_profiles_changed = pyqtSignal(list, int)   # (profiles, active_index)
    sig_verify_and_activate = pyqtSignal(str, str, str)  # id, raw_input, имя профиля
    sig_subscriptions_changed = pyqtSignal(list)   # (subscriptions,) — для сохранения в конфиг
    _CARD = "QWidget{background:#2c2c2e;border-radius:12px;border:none;}"
    _ROW  = "QWidget{background:#2c2c2e;border-radius:12px;border:1px solid #38383a;}"
    DEFAULT_SUB_UPDATE_HOURS = 6

    def __init__(self, profiles=None, active_index=-1, subscriptions=None):
        super().__init__()
        self.status = "stopped"; self.bot = None
        self.profiles = profiles or []
        self.active_index = active_index
        self.subscriptions = subscriptions or []
        self._flag_dl = None
        self._ping_results = {}  # idx -> мс (int) или "✗" при неудаче
        self._ping_worker = None
        self._ping_labels = {}  # idx -> QLabel строки (для точечного обновления без полной пересборки)
        self._pill_labels = {}  # idx -> QLabel "Активен/Резерв" той же строки
        self._sub_fetch_workers = {}  # subscription_id -> SubscriptionFetchWorker
        self._pending_sub_names = {}  # subscription_id -> имя, заданное вручную при добавлении
                                       # (приоритетнее над авто-определённым profile-title)
        root = QVBoxLayout(self); root.setContentsMargins(0,0,0,0); root.setSpacing(0)

        hdr = QWidget(); hdr.setFixedHeight(56)
        hdr.setStyleSheet(f"background:{BG};border-bottom:1px solid {LINE};")
        hl = QHBoxLayout(hdr); hl.setContentsMargins(16,0,12,0); hl.setSpacing(8)
        self.dot = Dot(DARK, 10); hl.addWidget(self.dot)
        title = QLabel("🌐 Сеть / Прокси")
        title.setStyleSheet(f"color:{WHITE};font-size:15px;font-weight:700;background:transparent;border:none;")
        hl.addWidget(title)
        self.badge = QLabel("отключено"); self.badge.setFixedHeight(22)
        self._badge_style("stopped"); hl.addWidget(self.badge)
        hl.addStretch()
        self.b_run = QPushButton("Подключить"); self.b_run.setFixedSize(120, 32)
        self.b_run.setStyleSheet(
            f"QPushButton{{background:{GREEN};border:none;color:#000;border-radius:10px;"
            f"font-size:13px;font-weight:700;min-height:0;}} QPushButton:hover{{background:#2db34a;}}")
        self.b_run.clicked.connect(self._toggle); hl.addWidget(self.b_run)
        root.addWidget(hdr)

        sc = QScrollArea(); sc.setWidgetResizable(True)
        sc.setStyleSheet("border:none;background:transparent;")
        w = QWidget(); w.setStyleSheet(f"background:{BG};")
        cl = QVBoxLayout(w); cl.setContentsMargins(16,16,16,16); cl.setSpacing(16)
        cl.setAlignment(Qt.AlignmentFlag.AlignTop)

        proxy_card = QWidget(); proxy_card.setStyleSheet(self._CARD); proxy_card.setFixedHeight(50)
        pcl = QHBoxLayout(proxy_card); pcl.setContentsMargins(14,0,14,0)
        pcl.addWidget(label("Локальный SOCKS5 для ботов:", 12, GRAY))
        self._proxy_addr_lbl = label(f"127.0.0.1:{XRAY_SOCKS_PORT}", 13, WHITE, True)
        pcl.addWidget(self._proxy_addr_lbl); pcl.addStretch()
        b_copy = QPushButton("Копировать"); b_copy.setFixedHeight(26)
        b_copy.setStyleSheet(f"QPushButton{{background:transparent;border:none;color:{GRAY};"
                              f"font-size:12px;padding:0 8px;min-height:0;}} QPushButton:hover{{color:{WHITE};}}")
        b_copy.clicked.connect(lambda: QApplication.clipboard().setText(self._proxy_addr_lbl.text()))
        pcl.addWidget(b_copy)
        cl.addWidget(proxy_card)

        info = QLabel(
            "Добавь один или несколько профилей — vless-ссылка (vless://...) или готовый "
            "JSON-конфиг Xray. Активный профиль поднимает локальный SOCKS5-прокси "
            f"127.0.0.1:{XRAY_SOCKS_PORT} для остальных ботов; если он упадёт — BotManager "
            "сам протестирует резервные профили и переключится на самый быстрый рабочий."
        )
        info.setWordWrap(True)
        info.setStyleSheet(f"color:{GRAY};font-size:12px;background:transparent;border:none;")
        cl.addWidget(info)

        sec_s_row = QHBoxLayout()
        sec_s = QLabel("ПОДПИСКИ")
        sec_s.setStyleSheet(f"color:{GRAY};font-size:11px;letter-spacing:1px;font-weight:600;"
                             f"background:transparent;border:none;")
        sec_s_row.addWidget(sec_s)
        sec_s_row.addStretch()
        b_add = IconBtn("add", 22, GRAY, BLUE)
        b_add.setToolTip("Добавить подписку или профиль")
        b_add.clicked.connect(self._start_add_unified)
        sec_s_row.addWidget(b_add)
        cl.addLayout(sec_s_row)
        card_subs = QWidget(); card_subs.setStyleSheet("background:transparent;border:none;")
        self._subs_l = QVBoxLayout(card_subs)
        self._subs_l.setContentsMargins(0,0,0,0); self._subs_l.setSpacing(10)
        cl.addWidget(card_subs)

        sec_row = QHBoxLayout()
        sec_p = QLabel("ПРОФИЛИ")
        sec_p.setStyleSheet(f"color:{GRAY};font-size:11px;letter-spacing:1px;font-weight:600;"
                             f"background:transparent;border:none;")
        sec_row.addWidget(sec_p)
        sec_row.addStretch()
        self._ping_all_btn = QPushButton("Проверить все"); self._ping_all_btn.setFixedHeight(22)
        self._ping_all_btn.setStyleSheet(f"QPushButton{{background:transparent;border:none;color:{GRAY};"
                                          f"font-size:11px;padding:0;min-height:0;}} QPushButton:hover{{color:{BLUE};}}")
        self._ping_all_btn.clicked.connect(self._ping_all)
        sec_row.addWidget(self._ping_all_btn)
        cl.addLayout(sec_row)

        card_profiles = QWidget(); card_profiles.setStyleSheet("background:transparent;border:none;")
        self._profiles_l = QVBoxLayout(card_profiles)
        self._profiles_l.setContentsMargins(0,0,0,0); self._profiles_l.setSpacing(10)
        cl.addWidget(card_profiles)

        # Логи живут в отдельном окне (как в HAPP), а не встроенной карточкой —
        # открываются через "···" в шапке страницы (см. _open_logs_dialog).
        self.log_view = QTextEdit(); self.log_view.setReadOnly(True)
        self.log_view.setStyleSheet(
            "QTextEdit{background:transparent;color:#ccc;border:none;"
            "font-family:Consolas;font-size:12px;padding:10px 14px;}"
        )
        self._logs_dialog = None

        sc.setWidget(w); root.addWidget(sc)
        self._render_profiles()
        self._render_subscriptions()

    def _open_logs_dialog(self):
        """Панель логов подключения — встроенная поверх окна (Overlay), как в HAPP,
        а не отдельное окно Windows. Виджет self.log_view переиспользуется (не
        пересоздаётся), чтобы при повторном открытии история не терялась — как и
        сама панель: создаётся один раз и просто скрывается/показывается снова
        (в отличие от остальных диалогов, её НЕ пересоздают на каждый вызов)."""
        if self._logs_dialog is None:
            dlg = Overlay(self.window(), card_width=620, card_height=460)
            v = dlg.body
            top = QHBoxLayout()
            top.addWidget(label("Логи подключения", 16, WHITE, True))
            top.addStretch()
            b_copy = QPushButton("Копировать"); b_copy.setFixedHeight(26)
            b_copy.setStyleSheet(f"QPushButton{{background:transparent;border:none;color:{GRAY};"
                                  f"font-size:12px;padding:0 8px;min-height:0;}} QPushButton:hover{{color:{WHITE};}}")
            b_copy.clicked.connect(lambda: QApplication.clipboard().setText(self.log_view.toPlainText()))
            top.addWidget(b_copy)
            b_clear = QPushButton("Очистить"); b_clear.setFixedHeight(26)
            b_clear.setStyleSheet(f"QPushButton{{background:transparent;border:none;color:{GRAY};"
                                   f"font-size:12px;padding:0 8px;min-height:0;}} QPushButton:hover{{color:{WHITE};}}")
            b_clear.clicked.connect(lambda: self.log_view.clear())
            top.addWidget(b_clear)
            b_close = CloseBtn(22)
            b_close.clicked.connect(dlg.hide)  # hide(), не dismiss() — панель кэшируется, не удаляется
            top.addWidget(b_close)
            v.addLayout(top)
            log_card = QWidget(); log_card.setStyleSheet(self._CARD)
            lc = QVBoxLayout(log_card); lc.setContentsMargins(0,0,0,0)
            lc.addWidget(self.log_view)
            v.addWidget(log_card, 1)
            self._logs_dialog = dlg
        self._logs_dialog.open()

    # ── подписки ─────────────────────────────────────────────────────────────
    def _render_subscriptions(self):
        self._clear_layout(self._subs_l)
        if not self.subscriptions:
            self._subs_l.addWidget(label("Нет подписок — можно добавить ниже", 12, GRAY))
            return
        for sub in self.subscriptions:
            traffic = sub.get("traffic")
            row = QWidget(); row.setStyleSheet(self._ROW)
            row.setFixedHeight(78 if traffic else 60)
            rl = QVBoxLayout(row); rl.setContentsMargins(14,10,12,10); rl.setSpacing(4)

            top = QHBoxLayout(); top.setSpacing(8)
            top.addWidget(label(sub.get("name") or "Подписка", 13, WHITE, True))
            top.addStretch()
            busy = sub["id"] in self._sub_fetch_workers
            b_menu = IconBtn("menu", 28, GRAY, WHITE)
            b_menu.setToolTip("Действия с подпиской")
            b_menu.setEnabled(not busy)
            b_menu.clicked.connect(lambda sid=sub["id"], b=b_menu: self._show_subscription_menu(sid, b))
            top.addWidget(b_menu)
            rl.addLayout(top)

            n_profiles = sum(1 for p in self.profiles if p.get("subscription_id") == sub["id"])
            interval = sub.get("update_interval_hours", self.DEFAULT_SUB_UPDATE_HOURS)
            sub_line = f"{n_profiles} профилей · обновление раз в {interval:g} ч"
            if busy:
                sub_line = "Обновляется..."
            rl.addWidget(label(sub_line, 11, GRAY))

            if traffic:
                used = traffic.get("download", 0) + traffic.get("upload", 0)
                total = traffic.get("total") or 0
                bar = QProgressBar(); bar.setFixedHeight(18); bar.setTextVisible(True)
                bar.setAlignment(Qt.AlignmentFlag.AlignCenter)
                if total:
                    bar.setRange(0, 100)
                    bar.setValue(min(100, int(used / total * 100)))
                    bar.setFormat(f"{format_bytes(used)} / {format_bytes(total)}")
                else:
                    bar.setRange(0, 1); bar.setValue(0)
                    bar.setFormat(f"{format_bytes(used)} / ∞")
                bar.setStyleSheet(
                    f"QProgressBar{{background:#1c1c1e;border:1px solid #3a3a3c;border-radius:9px;"
                    f"text-align:center;color:{WHITE};font-size:11px;}}"
                    f"QProgressBar::chunk{{background:{BLUE};border-radius:8px;}}")
                rl.addWidget(bar)
            self._subs_l.addWidget(row)

    def _show_subscription_menu(self, sub_id, anchor_btn):
        sub = next((s for s in self.subscriptions if s["id"] == sub_id), None)
        if not sub:
            return
        menu = QMenu(self)
        menu.setStyleSheet(
            f"QMenu{{background:#2c2c2e;color:{WHITE};border:1px solid #3a3a3c;border-radius:8px;padding:4px;}}"
            f"QMenu::item{{padding:6px 22px;border-radius:5px;font-size:13px;}}"
            f"QMenu::item:selected{{background:{BLUE};}}"
            f"QMenu::separator{{background:#3a3a3c;height:1px;margin:4px 6px;}}")
        act_refresh = menu.addAction(make_icon("refresh", WHITE), "Обновить")
        act_ping = menu.addAction(make_icon("signal", WHITE), "Тест пинга")
        act_copy = menu.addAction(make_icon("copy", WHITE), "Копировать URL")
        act_edit = menu.addAction(make_icon("edit", WHITE), "Редактировать")
        act_logs = menu.addAction(make_icon("list", WHITE), "Логи")
        menu.addSeparator()
        act_del = menu.addAction(make_icon("trash", RED), "Удалить")
        act_refresh.triggered.connect(lambda: self._refresh_subscription(sub_id))
        act_ping.triggered.connect(lambda: self._ping_subscription(sub_id))
        act_copy.triggered.connect(lambda: (QApplication.clipboard().setText(sub.get("url", "")),
                                             self.add_log("📋 URL подписки скопирован")))
        act_edit.triggered.connect(lambda: self._start_edit_subscription(sub_id))
        act_logs.triggered.connect(self._open_logs_dialog)
        act_del.triggered.connect(lambda: self._delete_subscription(sub_id))
        menu.exec(anchor_btn.mapToGlobal(anchor_btn.rect().bottomRight()))

    def _ping_subscription(self, sub_id):
        """'Тест пинга' у конкретной подписки — пингует только её профили, не
        трогая остальные (переиспользует тот же PingWorker/инфраструктуру, что
        и общая кнопка 'Проверить все')."""
        if self._ping_worker and self._ping_worker.isRunning():
            return
        indexed = [(i, p) for i, p in enumerate(self.profiles) if p.get("subscription_id") == sub_id]
        if not indexed:
            return
        for i, _ in indexed:
            self._ping_results[i] = "…"
        self._render_profiles()
        self._ping_worker = PingWorker(indexed)
        self._ping_worker.result.connect(self._on_ping_result)
        self._ping_worker.finished.connect(self._on_ping_finished)
        self._ping_worker.finished.connect(self._ping_worker.deleteLater)
        self._ping_worker.start()

    def _start_edit_subscription(self, sub_id):
        sub = next((s for s in self.subscriptions if s["id"] == sub_id), None)
        if not sub:
            return
        dlg = Overlay(self.window(), card_width=420)
        dlg.add_header("Редактирование подписки")
        v = dlg.body
        _field_css = ("QLineEdit{background:#1c1c1e;border:1px solid #3a3a3c;border-radius:8px;"
                      f"color:#fff;font-size:13px;padding:8px;}} QLineEdit:focus{{border-color:{BLUE};}}")
        v.addWidget(label("Имя подписки", 11, GRAY))
        name_edit = QLineEdit(sub.get("name", "")); name_edit.setStyleSheet(_field_css)
        v.addWidget(name_edit)
        v.addSpacing(6)
        v.addWidget(label("URL подписки", 11, GRAY))
        url_edit = QLineEdit(sub.get("url", "")); url_edit.setStyleSheet(_field_css)
        v.addWidget(url_edit)
        v.addSpacing(10)
        btn_row = QHBoxLayout(); btn_row.addStretch()
        b_cancel = QPushButton("Отмена"); b_cancel.setFixedHeight(32)
        b_cancel.setStyleSheet(f"QPushButton{{background:transparent;border:none;color:{GRAY};"
                                f"font-size:13px;padding:0 12px;min-height:0;}} QPushButton:hover{{color:{WHITE};}}")
        b_cancel.clicked.connect(dlg.dismiss)
        b_save = QPushButton("Сохранить"); b_save.setFixedHeight(32); b_save.setFixedWidth(120)
        b_save.setStyleSheet(f"QPushButton{{background:{BLUE};border:none;color:#fff;border-radius:8px;"
                              f"font-size:13px;font-weight:600;min-height:0;}} QPushButton:hover{{background:#3395ff;}}")

        def _save():
            new_name = name_edit.text().strip() or sub.get("name")
            new_url = url_edit.text().strip()
            url_changed = bool(new_url) and new_url != sub.get("url")
            sub["name"] = new_name
            if new_url:
                sub["url"] = new_url
            self._render_subscriptions()
            self.sig_subscriptions_changed.emit(self.subscriptions)
            dlg.dismiss()
            if url_changed:
                self.add_log(f"URL подписки «{new_name}» изменён — обновляю список серверов...")
                self._refresh_subscription(sub_id)

        b_save.clicked.connect(_save)
        btn_row.addWidget(b_cancel); btn_row.addWidget(b_save)
        v.addLayout(btn_row)
        dlg.open()

    # Российские сервера как VLESS-выход не годятся: Telegram заблокирован в РФ,
    # поэтому подключение через российский exit-node ломает именно то, ради чего
    # вообще нужен VPN. Такие профили из подписок просто не добавляем.
    EXCLUDED_COUNTRIES = {"RU"}

    def _on_sub_worker_finished(self, sub_id):
        # Снимаем "занят" И перерисовываем СРАЗУ здесь (а не в result/error-
        # хендлере) — finished эмитится QThread'ом строго ПОСЛЕ result/error,
        # так что рендер оттуда всегда видел бы воркер ещё "занятым", и кнопка
        # "Обновить" никогда не появлялась бы обратно (оставалось "Обновляю...").
        # wait() ДО pop(): finished эмитится изнутри потока чуть РАНЬШЕ, чем ОС
        # физически завершает поток — если тут же уронить последнюю Python-
        # ссылку (pop), сборщик мусора может уничтожить ещё не до конца
        # остановленный QThread ("QThread: Destroyed while thread is still
        # running"), а это фатально валит процесс (тот же класс бага, что
        # описан в _stop_background_workers).
        worker = self._sub_fetch_workers.get(sub_id)
        if worker is not None:
            worker.wait()
        self._sub_fetch_workers.pop(sub_id, None)
        self._render_subscriptions()

    def _start_add_unified(self):
        """Единственная кнопка "+" (в шапке ПОДПИСКИ) — стилизованная панель "Добавить
        конфигурацию" с выбором типа (как в HAPP: Подписка/Конфигурация/JSON/QR-код),
        вместо системного некрасивого QInputDialog и вместо двух разных кнопок/диалогов
        "Добавить подписку"/"Добавить профиль". "Конфигурация" и "JSON" обрабатываются
        одинаково (оба — одиночный профиль), это просто два разных пункта меню, как в
        HAPP; QR-код пока не поддержан — в приложении нет камеры/библиотеки для этого."""
        dlg = Overlay(self.window(), card_width=520)
        dlg.add_header("Добавить конфигурацию")
        v = dlg.body

        _line_css = ("QLineEdit{background:#1c1c1e;border:1px solid #3a3a3c;border-radius:8px;"
                     f"color:#fff;font-size:13px;padding:8px;}} QLineEdit:focus{{border-color:{BLUE};}}")
        _text_css = ("QTextEdit{background:#1c1c1e;border:1px solid #3a3a3c;border-radius:8px;"
                     f"color:#ccc;font-family:Consolas;font-size:12px;padding:8px;}} QTextEdit:focus{{border-color:{BLUE};}}")
        _combo_css = (
            "QComboBox{background:#1c1c1e;border:1px solid #3a3a3c;border-radius:8px;"
            f"color:#fff;font-size:13px;padding:8px;}} QComboBox:focus{{border-color:{BLUE};}}"
            "QComboBox::drop-down{border:none;width:24px;}"
            "QComboBox QAbstractItemView{background:#2c2c2e;color:#fff;border:1px solid #3a3a3c;"
            f"selection-background-color:{BLUE};outline:none;}}")

        v.addWidget(label("ТИП", 11, GRAY))
        type_combo = QComboBox()
        type_combo.addItems(["Подписка", "Конфигурация", "JSON", "QR-код"])
        type_combo.setStyleSheet(_combo_css)
        v.addWidget(type_combo)

        name_label = label("ИМЯ ПОДПИСКИ", 11, GRAY)
        v.addWidget(name_label)
        name_edit = QLineEdit()
        name_edit.setPlaceholderText("Необязательно — определится автоматически")
        name_edit.setStyleSheet(_line_css)
        v.addWidget(name_edit)

        body_label = label("URL ПОДПИСКИ", 11, GRAY)
        v.addWidget(body_label)
        # Без QStackedWidget: он растягивает ВСЕ свои страницы под размер самой большой
        # (тут — многострочное поле JSON высотой 160px), из-за чего однострочное поле URL
        # подписки становилось таким же огромным. Вместо этого просто show/hide нужного
        # поля по месту — тогда каждое сохраняет свой собственный естественный размер.
        url_edit = QLineEdit()
        url_edit.setPlaceholderText("https://...")
        url_edit.setStyleSheet(_line_css)
        v.addWidget(url_edit)
        profile_edit = QTextEdit()
        profile_edit.setPlaceholderText("vless://uuid@host:port?...  или  { ...JSON... }")
        profile_edit.setFixedHeight(220)
        profile_edit.setStyleSheet(_text_css)
        v.addWidget(profile_edit)
        qr_placeholder = QLabel("Сканирование QR-кода пока не поддерживается.\n"
                                 "Вставьте ссылку вручную, выбрав тип «Конфигурация».")
        qr_placeholder.setWordWrap(True)
        qr_placeholder.setStyleSheet(f"color:{GRAY};font-size:12px;background:transparent;"
                                      f"border:none;padding:16px 0;")
        v.addWidget(qr_placeholder)

        warn = QLabel("Не изменяйте параметры, если вы не уверены в их назначении и значениях.")
        warn.setWordWrap(True)
        warn.setStyleSheet(f"color:{GRAY};font-size:11px;background:transparent;border:none;")
        v.addWidget(warn)

        def _on_type_changed():
            # У Подписки — отдельное поле "Имя", как в HAPP. У Конфигурации/JSON/QR-кода
            # своего поля имени НЕТ вообще (имя профиля определяется автоматически из
            # ссылки/конфига) — там всего одно поле, подписанное именем самого типа.
            t = type_combo.currentText()
            is_sub = t == "Подписка"
            is_profile = t in ("Конфигурация", "JSON")
            name_label.setVisible(is_sub); name_edit.setVisible(is_sub)
            warn.setVisible(is_profile)
            url_edit.setVisible(is_sub)
            profile_edit.setVisible(is_profile)
            qr_placeholder.setVisible(not is_sub and not is_profile)
            if is_sub:
                body_label.setText("URL ПОДПИСКИ")
            elif is_profile:
                body_label.setText(t.upper())
            else:
                body_label.setText("QR-КОД")
            # Показ/скрытие полей меняет нужную высоту карточки (профиль занимает 160px,
            # подписка — однострочное поле) — без пересчёта карточка остаётся размера,
            # посчитанного при ПЕРВОМ открытии, и новое поле налезает на кнопку/текст
            # снизу вместо того, чтобы карточка выросла под него.
            dlg._reposition()
        type_combo.currentIndexChanged.connect(_on_type_changed)
        _on_type_changed()

        btn_row = QHBoxLayout(); btn_row.addStretch()
        b_save = QPushButton("Добавить"); b_save.setFixedHeight(32); b_save.setFixedWidth(120)
        b_save.setStyleSheet(f"QPushButton{{background:{BLUE};border:none;color:#fff;border-radius:8px;"
                              f"font-size:13px;font-weight:600;min-height:0;}} QPushButton:hover{{background:#3395ff;}}")

        def _save():
            t = type_combo.currentText()
            if t == "Подписка":
                url = url_edit.text().strip()
                if not url:
                    self.add_log("✗ Пустой URL подписки — не добавлено")
                    return
                sub_id = str(uuid.uuid4())
                custom_name = name_edit.text().strip()
                if custom_name:
                    self._pending_sub_names[sub_id] = custom_name
                worker = SubscriptionFetchWorker(sub_id, url)
                worker.result.connect(self._on_subscription_added)
                worker.error.connect(self._on_subscription_error)
                worker.finished.connect(lambda sid=sub_id: self._on_sub_worker_finished(sid))
                worker.finished.connect(worker.deleteLater)
                self._sub_fetch_workers[sub_id] = worker
                self.add_log(f"📡 Скачиваю подписку {url}...")
                worker.start()
            elif t in ("Конфигурация", "JSON"):
                raw = profile_edit.toPlainText().strip()
                if not raw:
                    self.add_log("✗ Пустая ссылка/конфиг — не добавлено")
                    return
                name = (name_edit.text().strip() or self._extract_remark(raw)
                        or f"Профиль {len(self.profiles)+1}")
                country, name = extract_flag_code(name)
                entry = {"id": str(uuid.uuid4()), "name": name, "raw_input": raw, "country": country}
                self.profiles.append(entry)
                if self.active_index == -1:
                    self.active_index = 0
                self._render_profiles()
                self.sig_profiles_changed.emit(self.profiles, self.active_index)
                self.add_log(f"✓ Профиль «{name}» добавлен")
            else:
                self.add_log("✗ Сканирование QR-кода пока не поддерживается")
                return
            dlg.dismiss()

        b_save.clicked.connect(_save)
        btn_row.addWidget(b_save)
        v.addLayout(btn_row)
        dlg.open()

    def _on_subscription_added(self, sub_id, profiles, update_hours, traffic, title=None):
        skipped = sum(1 for p in profiles if p.get("country") in self.EXCLUDED_COUNTRIES)
        profiles = [p for p in profiles if p.get("country") not in self.EXCLUDED_COUNTRIES]
        if skipped:
            self.add_log(f"⏭ Пропущено {skipped} российских серверов")
        for p in profiles:
            p["subscription_id"] = sub_id
        self.profiles.extend(profiles)
        custom_name = self._pending_sub_names.pop(sub_id, None)
        self.subscriptions.append({
            "id": sub_id,
            "url": self._sub_fetch_workers[sub_id].url if sub_id in self._sub_fetch_workers else "",
            "name": custom_name or title or f"Подписка ({len(profiles)})",
            "update_interval_hours": update_hours or self.DEFAULT_SUB_UPDATE_HOURS,
            "last_fetch": time.time(),
            "traffic": traffic,
        })
        if self.active_index == -1 and self.profiles:
            self.active_index = 0
        self.add_log(f"✓ Подписка добавлена — {len(profiles)} профилей")
        self._render_profiles(); self._render_subscriptions()
        self.sig_profiles_changed.emit(self.profiles, self.active_index)
        self.sig_subscriptions_changed.emit(self.subscriptions)

    def _on_subscription_error(self, sub_id, msg):
        self._pending_sub_names.pop(sub_id, None)
        self.add_log(f"✗ Не удалось скачать подписку: {msg}")
        self._render_subscriptions()

    def _refresh_subscription(self, sub_id):
        sub = next((s for s in self.subscriptions if s["id"] == sub_id), None)
        if not sub or sub_id in self._sub_fetch_workers:
            return
        worker = SubscriptionFetchWorker(sub_id, sub["url"])
        worker.result.connect(self._on_subscription_refreshed)
        worker.error.connect(self._on_subscription_error)
        worker.finished.connect(lambda sid=sub_id: self._on_sub_worker_finished(sid))
        worker.finished.connect(worker.deleteLater)
        self._sub_fetch_workers[sub_id] = worker
        self.add_log(f"📡 Обновляю подписку «{sub.get('name')}»...")
        worker.start()
        self._render_subscriptions()

    def _on_subscription_refreshed(self, sub_id, new_profiles, update_hours, traffic, title=None):
        if not any(s["id"] == sub_id for s in self.subscriptions):
            return  # подписку удалили, пока обновление шло в фоне — результат больше не нужен
        skipped = sum(1 for p in new_profiles if p.get("country") in self.EXCLUDED_COUNTRIES)
        new_profiles = [p for p in new_profiles if p.get("country") not in self.EXCLUDED_COUNTRIES]
        if skipped:
            self.add_log(f"⏭ Пропущено {skipped} российских серверов")
        # Сохраняем стабильный id у профилей, которые не изменились (совпадают по
        # raw_input) — иначе если обновлённый профиль был активным, привязка
        # "активный профиль" потеряется просто из-за пересборки списка.
        old = [p for p in self.profiles if p.get("subscription_id") == sub_id]
        old_by_raw = {p["raw_input"]: p for p in old}
        active_id = None
        if 0 <= self.active_index < len(self.profiles):
            active_id = self.profiles[self.active_index].get("id")
        merged = []
        for np in new_profiles:
            existing = old_by_raw.get(np["raw_input"])
            if existing:
                np["id"] = existing["id"]
            np["subscription_id"] = sub_id
            merged.append(np)
        self.profiles = [p for p in self.profiles if p.get("subscription_id") != sub_id] + merged
        # Восстанавливаем активный профиль по id, если он всё ещё есть в списке;
        # если пропал (сервер убрал его из подписки) — откатываемся на первый.
        if active_id is not None:
            idx = find_profile_index(self.profiles, active_id)
            self.active_index = idx if idx != -1 else (0 if self.profiles else -1)
        for sub in self.subscriptions:
            if sub["id"] == sub_id:
                sub["last_fetch"] = time.time()
                sub["name"] = f"Подписка ({len(merged)})"
                if update_hours:
                    sub["update_interval_hours"] = update_hours
                sub["traffic"] = traffic
                break
        self.add_log(f"✓ Подписка обновлена — {len(merged)} профилей")
        self._render_profiles(); self._render_subscriptions()
        self.sig_profiles_changed.emit(self.profiles, self.active_index)
        self.sig_subscriptions_changed.emit(self.subscriptions)

    def _delete_subscription(self, sub_id):
        active_id = None
        was_active_removed = False
        if 0 <= self.active_index < len(self.profiles):
            active_profile = self.profiles[self.active_index]
            active_id = active_profile.get("id")
            was_active_removed = active_profile.get("subscription_id") == sub_id
        # Если для sub_id сейчас идёт фоновое обновление (self._sub_fetch_workers) — воркер
        # не трогаем: ссылку на него нельзя убирать до его собственного finished (иначе Qt
        # может уничтожить ещё работающий QThread), а игнорировать его результат, если он
        # придёт уже после удаления, — задача guard'а в начале _on_subscription_refreshed.
        self.profiles = [p for p in self.profiles if p.get("subscription_id") != sub_id]
        self.subscriptions = [s for s in self.subscriptions if s["id"] != sub_id]
        if not self.profiles:
            self.active_index = -1
        elif was_active_removed:
            self.active_index = 0
        elif active_id is not None:
            # Активный профиль не из удалённой подписки, но его позиция в списке могла
            # сдвинуться — ищем заново по id, а не оставляем старый числовой индекс
            # (иначе он может указывать не на тот профиль или вообще выйти за границы).
            idx = find_profile_index(self.profiles, active_id)
            self.active_index = idx if idx != -1 else 0
        self._render_profiles(); self._render_subscriptions()
        self.sig_profiles_changed.emit(self.profiles, self.active_index)
        self.sig_subscriptions_changed.emit(self.subscriptions)
        if was_active_removed and self.profiles:
            if self.status in ("running", "restarting"):
                self.sig_disconnect.emit()
            name = self._profile_name(self.active_index)
            self.add_log(f"🔀 Активный профиль был из удалённой подписки — переключаюсь на «{name}»...")
            self.sig_connect.emit(self.profiles[self.active_index]["raw_input"], name)

    # ── профили ──────────────────────────────────────────────────────────────
    def _clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            wdg = item.widget()
            if wdg: wdg.deleteLater()

    def _render_profiles(self):
        self._clear_layout(self._profiles_l)
        self._ping_labels = {}
        self._pill_labels = {}
        if not self.profiles:
            self._profiles_l.addWidget(label("Нет профилей — добавь первый ниже", 12, GRAY))
            return
        missing_codes = []
        for i, p in enumerate(self.profiles):
            row = QWidget(); row.setStyleSheet(self._ROW); row.setFixedHeight(64)
            outer = QHBoxLayout(row); outer.setContentsMargins(14,8,12,8); outer.setSpacing(12)
            is_active = (i == self.active_index)
            code = p.get("country")
            if code:
                flag_path = flag_path_for(code)
                if flag_path.exists():
                    flag_lbl = QLabel(); flag_lbl.setFixedSize(24, 18)
                    flag_lbl.setPixmap(QPixmap(str(flag_path)))
                    flag_lbl.setScaledContents(True)
                    outer.addWidget(flag_lbl)
                else:
                    missing_codes.append(code)
            ping_val = self._ping_results.get(i)
            is_offline = ping_val is not None and ping_val != "…" and not isinstance(ping_val, int)

            # Левая колонка: имя+статус сверху, протокол (VLESS · WS · TLS) снизу
            # мелким серым — как показывают другие VPN-клиенты (Happ и т.п.).
            info_col = QVBoxLayout(); info_col.setSpacing(2)
            top_row = QHBoxLayout(); top_row.setSpacing(8)
            top_row.addWidget(label(p.get("name") or f"Профиль {i+1}", 13, WHITE, True))
            pill = QLabel("● Активен" if is_active else "Резерв")
            pill_color = GRAY
            if is_active:
                pill_color = RED if is_offline else GREEN
            pill.setStyleSheet(
                f"color:{pill_color};font-size:11px;font-weight:600;"
                f"background:transparent;border:none;")
            top_row.addWidget(pill)
            self._pill_labels[i] = pill
            top_row.addStretch()
            info_col.addLayout(top_row)
            info_col.addWidget(label(describe_vless_protocol(p.get("raw_input", "")), 11, GRAY))
            outer.addLayout(info_col, 1)

            if ping_val is not None:
                if ping_val == "…":
                    txt, col = "…", GRAY
                elif isinstance(ping_val, int):
                    txt, col = f"{ping_val} мс", GREEN
                else:
                    txt, col = "✗ офлайн", RED
                ping_lbl = QLabel(txt)
                ping_lbl.setStyleSheet(
                    f"color:{col};font-size:11px;font-weight:600;"
                    f"background:transparent;border:none;")
                outer.addWidget(ping_lbl)
                self._ping_labels[i] = ping_lbl
            # Один основной значок (подключиться) + один "···" со всеми
            # остальными действиями — вместо 4 мелких иконок в ряд, которые
            # сливались друг с другом и с фоном строки. Тот же паттерн, что
            # уже используется в строке подписки (_show_subscription_menu).
            if not is_active:
                b_act = IconBtn("play", 28, BLUE, "#3395ff")
                b_act.setToolTip("Сделать активным")
                b_act.clicked.connect(lambda idx=i: self._activate(idx))
                outer.addWidget(b_act)
            b_menu = IconBtn("menu", 28, GRAY, WHITE)
            b_menu.setToolTip("Действия с профилем")
            b_menu.clicked.connect(lambda idx=i, b=b_menu: self._show_profile_menu(idx, b))
            outer.addWidget(b_menu)
            self._profiles_l.addWidget(row)
        if missing_codes and not (self._flag_dl and self._flag_dl.isRunning()):
            self._flag_dl = FlagDownloader(list(set(missing_codes)))
            self._flag_dl.done.connect(self._on_flags_ready)
            self._flag_dl.finished.connect(self._on_flag_dl_finished)
            self._flag_dl.finished.connect(self._flag_dl.deleteLater)
            self._flag_dl.start()

    def _show_profile_menu(self, idx, anchor_btn):
        if idx >= len(self.profiles):
            return
        menu = QMenu(self)
        menu.setStyleSheet(
            f"QMenu{{background:#2c2c2e;color:{WHITE};border:1px solid #3a3a3c;border-radius:8px;padding:4px;}}"
            f"QMenu::item{{padding:6px 22px;border-radius:5px;font-size:13px;}}"
            f"QMenu::item:selected{{background:{BLUE};}}"
            f"QMenu::separator{{background:#3a3a3c;height:1px;margin:4px 6px;}}")
        act_ping = menu.addAction(make_icon("signal", WHITE), "Проверить пинг")
        act_edit = menu.addAction(make_icon("edit", WHITE), "Изменить")
        act_logs = menu.addAction(make_icon("list", WHITE), "Логи")
        menu.addSeparator()
        act_del = menu.addAction(make_icon("trash", RED), "Удалить")
        act_ping.triggered.connect(lambda: self._ping_one(idx))
        act_edit.triggered.connect(lambda: self._open_profile_dialog(idx))
        act_logs.triggered.connect(self._open_logs_dialog)
        act_del.triggered.connect(lambda: self._delete(idx))
        menu.exec(anchor_btn.mapToGlobal(anchor_btn.rect().bottomRight()))

    def _ping_one(self, idx):
        if self._ping_worker and self._ping_worker.isRunning(): return
        self._ping_results[idx] = "…"; self._render_profiles()
        self._ping_worker = PingWorker([(idx, self.profiles[idx])])
        self._ping_worker.result.connect(self._on_ping_result)
        self._ping_worker.finished.connect(self._on_ping_finished)
        self._ping_worker.finished.connect(self._ping_worker.deleteLater)
        self._ping_worker.start()

    def _ping_all(self):
        if (self._ping_worker and self._ping_worker.isRunning()) or not self.profiles: return
        self._ping_results = {i: "…" for i in range(len(self.profiles))}
        self._render_profiles()
        self._ping_worker = PingWorker(list(enumerate(self.profiles)))
        self._ping_worker.result.connect(self._on_ping_result)
        self._ping_worker.finished.connect(self._on_ping_finished)
        self._ping_worker.finished.connect(self._ping_worker.deleteLater)
        self._ping_worker.start()

    def _on_ping_result(self, idx, ms):
        self._ping_results[idx] = ms if ms is not None else "✗"
        self._update_ping_display(idx)

    def _update_ping_display(self, idx):
        """Точечно обновляет пинг ОДНОЙ строки, без пересборки всего списка.
        При "Проверить все" на 20+ профилях полная пересборка каждой строки на
        каждый результат была дорогой (десятки виджетов на каждый чих) и
        визуально выглядела так, будто все результаты появляются разом в
        конце, а не по одному, как реально идёт проверка."""
        ping_lbl = self._ping_labels.get(idx)
        if ping_lbl is None:
            return  # строка с этим idx сейчас не отрисована (список успел измениться)
        ping_val = self._ping_results.get(idx)
        if ping_val == "…":
            txt, col = "…", GRAY
        elif isinstance(ping_val, int):
            txt, col = f"{ping_val} мс", GREEN
        else:
            txt, col = "✗ офлайн", RED
        ping_lbl.setText(txt)
        ping_lbl.setStyleSheet(f"color:{col};font-size:11px;font-weight:600;background:transparent;border:none;")
        if idx == self.active_index:
            pill = self._pill_labels.get(idx)
            if pill is not None:
                is_offline = ping_val is not None and ping_val != "…" and not isinstance(ping_val, int)
                pill.setStyleSheet(
                    f"color:{RED if is_offline else GREEN};font-size:11px;font-weight:600;"
                    f"background:transparent;border:none;")

    def _on_ping_finished(self):
        # Обнуляем ссылку по finished (QThread гарантирует, что это эмитится
        # ТОЛЬКО после того, как run() реально завершился), а не по done
        # (эмитится последней строкой ВНУТРИ run() — между этим и фактическим
        # возвратом из run() есть окно, в которое Python может сборщиком мусора
        # уничтожить ещё физически работающий QThread, если это была последняя
        # ссылка на него: "QThread: Destroyed while thread is still running",
        # что фатально валит процесс).
        self._ping_worker = None

    def _on_flags_ready(self):
        self._render_profiles()

    def _on_flag_dl_finished(self):
        # wait() перед сбросом ссылки — см. комментарий в _on_sub_worker_finished:
        # finished эмитится чуть раньше физического завершения ОС-потока, ронять
        # последнюю Python-ссылку раньше времени рискует фатальным крашем Qt.
        if self._flag_dl is not None:
            self._flag_dl.wait()
        self._flag_dl = None

    def _extract_remark(self, raw):
        """Достаёт человеко-читаемое имя: remarks из JSON-конфига или #fragment из vless://."""
        try:
            if raw.startswith("vless://"):
                frag = urllib.parse.urlparse(raw).fragment
                if frag:
                    return urllib.parse.unquote(frag)
            else:
                cfg = json.loads(raw)
                if cfg.get("remarks"):
                    return cfg["remarks"]
        except Exception:
            pass
        return None

    def _open_profile_dialog(self, idx):
        """Встроенная поверх окна панель (Overlay) добавления/редактирования профиля —
        как в HAPP ("Редактирование конфигурации сервера"): большое поле конфига видно
        целиком без прокрутки страницы. Не отдельное окно Windows — как и все диалоги
        в HAPP, это затемнение + карточка внутри самого приложения."""
        is_new = (idx == -1)
        existing = None if is_new else self.profiles[idx]
        dlg = Overlay(self.window(), card_width=560)
        dlg.add_header("Добавление профиля" if is_new else "Редактирование конфигурации сервера")
        v = dlg.body

        _line_css = ("QLineEdit{background:#1c1c1e;border:1px solid #3a3a3c;border-radius:8px;"
                     f"color:#fff;font-size:13px;padding:8px;}} QLineEdit:focus{{border-color:{BLUE};}}")
        _text_css = ("QTextEdit{background:#1c1c1e;border:1px solid #3a3a3c;border-radius:8px;"
                     f"color:#ccc;font-family:Consolas;font-size:12px;padding:8px;}} QTextEdit:focus{{border-color:{BLUE};}}")

        v.addWidget(label("НАЗВАНИЕ", 11, GRAY))
        name_edit = QLineEdit(existing.get("name", "") if existing else "")
        name_edit.setPlaceholderText("Например «Финляндия»")
        name_edit.setStyleSheet(_line_css)
        v.addWidget(name_edit)

        v.addWidget(label("VLESS-ССЫЛКА ИЛИ JSON", 11, GRAY))
        body_edit = QTextEdit(existing.get("raw_input", "") if existing else "")
        body_edit.setPlaceholderText("vless://uuid@host:port?...  или  { ...JSON... }")
        body_edit.setFixedHeight(320)
        body_edit.setStyleSheet(_text_css)
        v.addWidget(body_edit)

        warn = QLabel("Не изменяйте параметры, если вы не уверены в их назначении и значениях.\n"
                       "Внимательно просмотрите разделы, связанные с маршрутами и портами, на предмет точности.")
        warn.setWordWrap(True)
        warn.setStyleSheet(f"color:{GRAY};font-size:11px;background:transparent;border:none;")
        v.addWidget(warn)

        btn_row = QHBoxLayout(); btn_row.addStretch()
        b_save = QPushButton("Сохранить"); b_save.setFixedHeight(32); b_save.setFixedWidth(120)
        b_save.setStyleSheet(f"QPushButton{{background:{BLUE};border:none;color:#fff;border-radius:8px;"
                              f"font-size:13px;font-weight:600;min-height:0;}} QPushButton:hover{{background:#3395ff;}}")

        def _save():
            raw = body_edit.toPlainText().strip()
            if not raw:
                self.add_log("✗ Пустая ссылка/конфиг — не сохранено")
                return
            name = (name_edit.text().strip() or self._extract_remark(raw)
                    or f"Профиль {len(self.profiles)+1}")
            country, name = extract_flag_code(name)
            existing_id = None
            sub_id = None
            if not is_new:
                country = country or existing.get("country")
                existing_id = existing.get("id")
                sub_id = existing.get("subscription_id")
            # Стабильный id (не raw_input!) — иначе два профиля с одинаковой ссылкой
            # неотличимы друг от друга при поиске "какой из них проверили/выбрали"
            # после асинхронного ожидания (см. find_profile_index).
            entry = {"id": existing_id or str(uuid.uuid4()), "name": name, "raw_input": raw, "country": country}
            if sub_id:
                entry["subscription_id"] = sub_id
            if is_new:
                self.profiles.append(entry)
                if self.active_index == -1:
                    self.active_index = 0
            else:
                # Панель немодальная, но фоновые воркеры (пинг, автообновление подписки,
                # failover) продолжают работать и могут пересобрать self.profiles, пока
                # панель открыта — ищем профиль заново по id, а не по "протухшему" idx,
                # захваченному в момент открытия панели.
                real_idx = find_profile_index(self.profiles, existing_id)
                if real_idx == -1:
                    self.add_log("✗ Профиль был изменён в фоне — сохранение отменено, попробуй ещё раз")
                    dlg.dismiss()
                    return
                self.profiles[real_idx] = entry
            self._render_profiles()
            self.sig_profiles_changed.emit(self.profiles, self.active_index)
            dlg.dismiss()

        b_save.clicked.connect(_save)
        btn_row.addWidget(b_save)
        v.addLayout(btn_row)
        body_edit.setFocus()
        dlg.open()

    def _delete(self, idx):
        was_active = (idx == self.active_index)
        self.profiles.pop(idx)
        if not self.profiles:
            self.active_index = -1
        elif was_active:
            self.active_index = 0
        elif idx < self.active_index:
            self.active_index -= 1
        self._render_profiles()
        self.sig_profiles_changed.emit(self.profiles, self.active_index)
        if was_active:
            if self.status in ("running", "restarting"):
                self.sig_disconnect.emit()
            if self.profiles:
                # Активный профиль удалили, но остались другие — сразу переключаемся
                # на новый активный, чтобы боты не остались без прокси до ручного
                # клика "Подключить".
                name = self._profile_name(self.active_index)
                self.add_log(f"🔀 Активный профиль удалён — переключаюсь на «{name}»...")
                self.sig_connect.emit(self.profiles[self.active_index]["raw_input"], name)

    def _activate(self, idx):
        if self.status in ("running", "restarting"):
            # Сейчас что-то реально работает — не рвём рабочее соединение вслепую.
            # Сначала просим App проверить новый профиль во временном xray, и только
            # при успехе переключаем active_index и коннектимся по-настоящему
            # (см. App._vless_verify_and_activate / _on_vless_verify_result).
            name = self._profile_name(idx)
            self.add_log(f"🧪 Проверяю профиль «{name}» перед переключением...")
            self.sig_verify_and_activate.emit(self.profiles[idx]["id"], self.profiles[idx]["raw_input"], name)
        else:
            self.active_index = idx
            self._render_profiles()
            self.sig_profiles_changed.emit(self.profiles, self.active_index)

    def active_raw_input(self):
        if 0 <= self.active_index < len(self.profiles):
            return self.profiles[self.active_index].get("raw_input", "")
        return ""

    def _profile_name(self, idx):
        if 0 <= idx < len(self.profiles):
            return self.profiles[idx].get("name") or f"Профиль {idx+1}"
        return "?"

    def set_active_index(self, idx):
        """Обновляет UI после автопереключения (failover) без повторной эмиссии сигнала."""
        self.active_index = idx
        self._render_profiles()

    def _badge_style(self, s):
        styles = {
            "running":    ("rgba(52,199,89,0.18)",  GREEN,  "подключено"),
            "stopped":    ("rgba(72,72,74,0.4)",     GRAY,   "отключено"),
            "error":      ("rgba(255,69,58,0.18)",   RED,    "ошибка"),
            "restarting": ("rgba(255,214,10,0.18)",  YELLOW, "переподключение..."),
        }
        bg, col, txt = styles.get(s, styles["stopped"])
        self.badge.setText(txt)
        self.badge.setStyleSheet(
            f"background:{bg};color:{col};padding:2px 10px;border-radius:10px;"
            f"font-size:12px;font-weight:600;border:none;")

    def set_status(self, s):
        self.status = s; self._badge_style(s)
        if self.bot: self.bot.status = s
        colors = {"running": GREEN, "stopped": DARK, "error": RED, "restarting": YELLOW}
        self.dot.set(colors.get(s, DARK))
        self.b_run.setText("Отключить" if s in ("running", "restarting") else "Подключить")
        if s in ("running", "restarting"):
            self.b_run.setStyleSheet(
                f"QPushButton{{background:{RED};border:none;color:#fff;border-radius:10px;"
                f"font-size:13px;font-weight:700;min-height:0;}} QPushButton:hover{{background:#d93b31;}}")
        else:
            self.b_run.setStyleSheet(
                f"QPushButton{{background:{GREEN};border:none;color:#000;border-radius:10px;"
                f"font-size:13px;font-weight:700;min-height:0;}} QPushButton:hover{{background:#2db34a;}}")

    def add_log(self, line):
        ts = datetime.now().strftime("%H:%M:%S")
        # Собственный лог xray.exe (при loglevel:"warning" в конфиге он пишет
        # предупреждения/ошибки прямо в stdout, который мы уже перехватываем)
        # начинается с даты вида "2026/07/08 14:19:59..." — этим он отличается
        # от наших сообщений с эмодзи-префиксами. Подсвечиваем отдельно, чтобы
        # сразу было видно, что это пишет сам xray, а не BotManager — как в
        # Happ отдельная вкладка "Лог ядра" для сообщений самого прокси.
        is_xray = len(line) > 4 and line[:4].isdigit() and line[4] == "/"
        lu = line.upper()
        if is_xray:
            if "[ERROR]" in lu: col = RED
            elif "[WARNING]" in lu: col = ORANGE
            else: col = "#7ab8ff"
            tag = '<span style="color:#5a8bbf;font-size:11px;">[xray]</span> '
        else:
            col = "#ccc"
            tag = ""
        self.log_view.append(f'<span style="color:#555">[{ts}]</span> {tag}<span style="color:{col}">{line}</span>')

    def _toggle(self):
        if self.status in ("running", "restarting"):
            self.sig_disconnect.emit()
        else:
            raw = self.active_raw_input()
            if not raw:
                self.add_log("✗ Нет активного профиля — добавь его выше")
                return
            self.sig_connect.emit(raw, self._profile_name(self.active_index))

# ── Главное окно ──────────────────────────────────────────────────────────────
class App(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Bot Manager"); self.resize(1020, 660); self.setMinimumSize(700,500)
        _ico = RES_DIR / "icon.ico"
        if _ico.exists(): self.setWindowIcon(QIcon(str(_ico)))
        self.setStyleSheet(QSS)
        self.cfg = load_config(); self.bots = []; self.pages = {}; self.navs = {}
        global BENIGN_ERROR_PATTERNS, HIDDEN_LOG_PATTERNS
        BENIGN_ERROR_PATTERNS = self.cfg["settings"].get("benign_error_patterns", DEFAULT_BENIGN_ERROR_PATTERNS)
        HIDDEN_LOG_PATTERNS = self.cfg["settings"].get("hidden_log_patterns", DEFAULT_HIDDEN_LOG_PATTERNS)
        self.cur = None; self._compact = False
        self._build(); self._tray(); self._load()
        # Таймер для отслеживания сигнала показа окна от второго экземпляра
        self._signal_timer = QTimer(self)
        self._signal_timer.timeout.connect(self._check_show_signal)
        self._signal_timer.start(500)
        QTimer.singleShot(3000, self._check_update)
        if self.cfg["settings"].get("autostart_bots"): QTimer.singleShot(800, self._autostart)
        QApplication.instance().installEventFilter(self)
        self._init_vless_health_check()
        self._init_subscription_auto_update()

    def _build(self):
        cw = QWidget(); self.setCentralWidget(cw)
        ml = QHBoxLayout(cw); ml.setContentsMargins(0,0,0,0); ml.setSpacing(0)

        # ── Сайдбар ──
        self._sb = QWidget(); self._sb.setFixedWidth(210)
        self._sb.setStyleSheet(f"background:{SB};border-right:1px solid {LINE};")
        sl = QVBoxLayout(self._sb); sl.setContentsMargins(0,0,0,0); sl.setSpacing(0)

        # Шапка
        top = QWidget(); top.setFixedHeight(56)
        top.setStyleSheet(f"background:{SB};border-bottom:1px solid {LINE};")
        tl = QHBoxLayout(top); tl.setContentsMargins(14,0,8,0); tl.setSpacing(8)
        tl.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        info = QVBoxLayout(); info.setSpacing(1); info.setContentsMargins(0,0,0,0)
        self._t1 = QLabel("Bot Manager")
        self._t1.setStyleSheet(f"color:{WHITE};font-size:13px;font-weight:700;background:transparent;border:none;")
        self._t2 = QLabel(f"v{APP_VERSION}")
        self._t2.setStyleSheet(f"color:{DARK};font-size:11px;background:transparent;border:none;")
        info.addWidget(self._t1); info.addWidget(self._t2)
        tl.addLayout(info)
        tl.addStretch()
        sl.addWidget(top)

        # ── Прокручиваемое содержимое сайдбара ──
        sb_sc = QScrollArea(); sb_sc.setWidgetResizable(True)
        sb_sc.setStyleSheet("border:none;background:transparent;")
        sb_sc.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sb_body = QWidget(); sb_body.setStyleSheet(f"background:transparent;")
        self._sb_cl = QVBoxLayout(sb_body)
        self._sb_cl.setContentsMargins(10,8,10,8); self._sb_cl.setSpacing(8)
        self._sb_cl.setAlignment(Qt.AlignmentFlag.AlignTop)

        _C = "QWidget{background:#2c2c2e;border-radius:12px;border:none;}"

        # Карточка: Главное
        hc = QWidget(); hc.setStyleSheet(_C)
        hcl = QVBoxLayout(hc); hcl.setContentsMargins(0,0,0,0); hcl.setSpacing(0)
        self._nhome = SideItem("🎮", "Главное")
        self._nhome.clicked.connect(lambda: self._show("home"))
        hcl.addWidget(self._nhome)
        self._sb_cl.addWidget(hc); self._home_card = hc

        # Метка БОТЫ
        self._blbl = QLabel("БОТЫ")
        self._blbl.setStyleSheet(f"color:{GRAY};font-size:11px;letter-spacing:1px;font-weight:600;"
                                 f"padding:4px 4px 0;background:transparent;border:none;")
        self._sb_cl.addWidget(self._blbl)

        # Карточка ботов (динамическая)
        self._bots_card = QWidget(); self._bots_card.setStyleSheet(_C)
        self._nw = QVBoxLayout(self._bots_card)
        self._nw.setContentsMargins(0,0,0,0); self._nw.setSpacing(0)
        self._sb_cl.addWidget(self._bots_card)

        # Карточка: Добавить бота
        ac = QWidget(); ac.setStyleSheet(_C)
        acl = QVBoxLayout(ac); acl.setContentsMargins(0,0,0,0); acl.setSpacing(0)
        self._nadd = SideItem("+", "Добавить бота")
        self._nadd.clicked.connect(lambda: self._show("add"))
        acl.addWidget(self._nadd)
        self._sb_cl.addWidget(ac); self._add_card = ac

        # Карточка: Сеть / Прокси
        netc = QWidget(); netc.setStyleSheet(_C)
        netcl = QVBoxLayout(netc); netcl.setContentsMargins(0,0,0,0); netcl.setSpacing(0)
        self._nnet = SideItem("🌐", "Сеть")
        self._nnet.clicked.connect(lambda: self._show("net"))
        netcl.addWidget(self._nnet)
        self._sb_cl.addWidget(netc); self._net_card = netc

        self._sb_cl.addStretch()
        sb_sc.setWidget(sb_body)
        sl.addWidget(sb_sc)

        # Настройки (нижняя карточка, вне скролла)
        sl.addWidget(h_line())
        sc_w = QWidget(); self._sc_wl = QVBoxLayout(sc_w)
        self._sc_wl.setContentsMargins(10,8,10,8); self._sc_wl.setSpacing(0)
        nc = QWidget(); nc.setStyleSheet(_C)
        ncl = QVBoxLayout(nc); ncl.setContentsMargins(0,0,0,0)
        self._nset = SideItem("⚙", "Настройки")
        self._nset.clicked.connect(lambda: self._show("settings"))
        ncl.addWidget(self._nset)
        self._sc_wl.addWidget(nc); sl.addWidget(sc_w)
        ml.addWidget(self._sb)

        # ── Кнопка свернуть/развернуть (по центру сайдбара) ──
        self._bcol = QPushButton("‹")
        self._bcol.setFixedSize(14, 40)
        self._bcol.setParent(self._sb)
        self._bcol.setStyleSheet(
            f"QPushButton{{background:rgba(255,255,255,0.07);border:none;color:{GRAY};font-size:18px;"
            f"border-radius:4px;padding:0;min-height:0;}}"
            f"QPushButton:hover{{background:rgba(255,255,255,0.15);color:{WHITE};}}"
        )
        self._bcol.clicked.connect(self._toggle_compact)
        self._sb.installEventFilter(self)

        # ── Стек ──
        self.stack = QStackedWidget(); ml.addWidget(self.stack)
        ep = QWidget(); ep.setStyleSheet(f"background:{BG};")
        el = QVBoxLayout(ep); el.setAlignment(Qt.AlignmentFlag.AlignCenter)
        el.addWidget(label("← Добавь первого бота", 16, DARK))
        self.stack.addWidget(ep)
        self._addp = AddPage(); self._addp.added.connect(self._on_add); self.stack.addWidget(self._addp)
        self._setp = SetPage(self.cfg["settings"], self.bots)
        self._setp.changed.connect(self._on_set)
        self._setp.bot_autostart_changed.connect(self._on_bot_autostart)
        self._setp.check_update_requested.connect(lambda: self._check_update(manual=True))
        self._setp.install_update_requested.connect(self._do_update)
        self.stack.addWidget(self._setp)
        self._homep = HomePage(); self.stack.addWidget(self._homep)
        self._snakep = SnakePage(); self.stack.addWidget(self._snakep)
        self._dinop = DinoPage(); self.stack.addWidget(self._dinop)
        self._homep.play_snake.connect(lambda: self._show("snake"))
        self._homep.play_dino.connect(lambda: self._show("dino"))
        self._snakep.back.connect(lambda: self._show("home"))
        self._dinop.back.connect(lambda: self._show("home"))
        _vless_cfg = self.cfg.get("vless", {})
        self._netp = NetPage(_vless_cfg.get("profiles", []), _vless_cfg.get("active", -1),
                              _vless_cfg.get("subscriptions", []))
        self._netp.sig_connect.connect(self._vless_connect)
        self._netp.sig_disconnect.connect(self._vless_disconnect)
        self._netp.sig_profiles_changed.connect(self._on_vless_profiles_changed)
        self._netp.sig_verify_and_activate.connect(self._vless_verify_and_activate)
        self._netp.sig_subscriptions_changed.connect(self._on_vless_subscriptions_changed)
        self.stack.addWidget(self._netp)
        self._vless_bot = None
        self._failover_worker = None
        self._verify_worker = None
        self._start_workers = set()  # ProcessStartWorker'ы, ещё не завершившиеся
        self._pending_starts = set()  # bot.id тех, для кого ProcessStartWorker ещё не ответил
        self._stop_workers = set()  # ProcessStopWorker'ы, ещё не завершившиеся
        self._log_readers = set()  # старые LogReader'ы, вытесненные рестартом, но ещё не finished

    def _toggle_compact(self):
        self._compact = not self._compact
        w = 46 if self._compact else 210
        self._sb.setFixedWidth(w)
        self._bcol.setText("›" if self._compact else "‹")
        self._t1.setVisible(not self._compact)
        self._t2.setVisible(not self._compact)
        self._blbl.setVisible(not self._compact)
        # Отступы карточек: в компактном режиме убираем боковые отступы
        m = 2 if self._compact else 10
        mb = 4 if self._compact else 8
        self._sb_cl.setContentsMargins(m, mb, m, mb)
        self._sb_cl.setSpacing(3 if self._compact else 8)
        # Нижняя карточка настроек — тоже обновляем отступы
        self._sc_wl.setContentsMargins(m, mb, m, mb)
        for i in range(self._nw.count()):
            item = self._nw.itemAt(i)
            if item and item.widget():
                w = item.widget()
                if hasattr(w, 'set_compact'):
                    w.set_compact(self._compact)
                else:
                    # разделитель (QFrame) — скрываем в compact режиме
                    w.setVisible(not self._compact)
        self._nadd.set_compact(self._compact)
        self._nset.set_compact(self._compact)
        self._nhome.set_compact(self._compact)
        QTimer.singleShot(0, self._reposition_toggle)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._reposition_toggle)

    def eventFilter(self, obj, event):
        if obj is self._sb and event.type() == QEvent.Type.Resize:
            self._reposition_toggle()
        if event.type() == QEvent.Type.KeyPress:
            if self.cur == "snake":
                self._snakep.game.keyPressEvent(event); return True
        return super().eventFilter(obj, event)

    def _reposition_toggle(self):
        h = self._sb.height()
        bh = self._bcol.height()
        self._bcol.move(self._sb.width() - self._bcol.width(), (h - bh) // 2)
        self._bcol.raise_()

    def _check_show_signal(self):
        if _SIGNAL_FILE.exists():
            try: _SIGNAL_FILE.unlink()
            except: pass
            self._show_win()

    # ── Автообновление ────────────────────────────────────────────────────────
    def _check_update(self, manual=False):
        def _run():
            try:
                url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
                req = urllib.request.Request(url, headers={"User-Agent": "BotManager"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = json.loads(r.read())
                latest = data.get("tag_name", "").lstrip("v")
                if latest and latest != APP_VERSION:
                    for asset in data.get("assets", []):
                        if asset["name"].endswith(".exe"):
                            self._upd_url = asset["browser_download_url"]
                            self._upd_ver = latest
                            QMetaObject.invokeMethod(self, "_show_update_btn",
                                                     Qt.ConnectionType.QueuedConnection)
                            break
                else:
                    if manual:
                        QMetaObject.invokeMethod(self, "_set_upd_status_ok",
                                                 Qt.ConnectionType.QueuedConnection)
            except Exception:
                if manual:
                    QMetaObject.invokeMethod(self, "_set_upd_status_err",
                                             Qt.ConnectionType.QueuedConnection)
        threading.Thread(target=_run, daemon=True).start()

    @pyqtSlot()
    def _set_upd_status_new(self):
        self._setp.set_update_status(f"Доступно v{self._upd_ver}", GREEN)

    @pyqtSlot()
    def _set_upd_status_ok(self):
        self._setp.set_update_status("Последняя версия", GRAY)

    @pyqtSlot()
    def _set_upd_status_err(self):
        self._setp.set_update_status("Ошибка проверки", RED)

    @pyqtSlot()
    def _show_update_btn(self):
        self._setp.show_update_available(self._upd_ver)
        if self.cfg["settings"].get("win_notifications"):
            self.tray.showMessage("Bot Manager",
                f"Доступно обновление v{self._upd_ver}",
                QSystemTrayIcon.MessageIcon.Information, 4000)

    def _download_release_asset(self, url, dest):
        """Скачивает файл релиза с GitHub. Прямое подключение здесь нередко не
        работает (GitHub у некоторых провайдеров недоступен без VPN — та же
        причина, по которой winget не смог скачать Inno Setup в этой же
        сессии) — если urlretrieve падает, а свой VLESS-прокси сейчас поднят,
        пробуем скачать через него curl'ом (--socks5-hostname), а не сдаёмся
        сразу."""
        try:
            urllib.request.urlretrieve(url, dest)
            return
        except Exception as direct_err:
            if not (self._vless_bot and self._vless_bot.status == "running"):
                raise
            curl = shutil.which("curl") or "curl.exe"
            try:
                result = subprocess.run(
                    [curl, "-L", "--socks5-hostname", f"127.0.0.1:{XRAY_SOCKS_PORT}",
                     "-o", dest, url, "--fail", "--connect-timeout", "15", "--max-time", "180"],
                    capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except Exception:
                raise direct_err
            if result.returncode != 0 or not Path(dest).exists():
                raise direct_err

    def _do_update(self):
        def _run():
            try:
                tmp = tempfile.mktemp(suffix=".exe", prefix="BotManager_Setup_")
                self._download_release_asset(self._upd_url, tmp)
                # /VERYSILENT — без мастера установки (выбор папки, "уже
                # существует" и т.п.), просто тихо ставит поверх и выходит.
                # UAC всё равно спросит один раз (это Windows, не установщик,
                # без code-signing сертификата не убрать) — дальше без диалогов.
                cmd = f'cmd /c timeout /t 3 /nobreak >nul && start "" "{tmp}" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART'
                subprocess.Popen(cmd, shell=True, creationflags=subprocess.CREATE_NO_WINDOW)
                QMetaObject.invokeMethod(self, "_quit", Qt.ConnectionType.QueuedConnection)
            except Exception:
                QMetaObject.invokeMethod(self, "_upd_reset_btn", Qt.ConnectionType.QueuedConnection)
        threading.Thread(target=_run, daemon=True).start()

    @pyqtSlot()
    def _upd_reset_btn(self):
        self._setp.set_download_error()

    def _tray(self):
        self.tray = QSystemTrayIcon(self)
        _ico = RES_DIR / "icon.ico"
        if _ico.exists():
            self.tray.setIcon(QIcon(str(_ico)))
        else:
            px = QPixmap(16,16); px.fill(QColor(GREEN))
            self.tray.setIcon(QIcon(px))
        self.tray.setToolTip("Bot Manager")
        m = QMenu()
        a1 = QAction("Открыть",self); a1.triggered.connect(self._show_win)
        a2 = QAction("Выйти",self); a2.triggered.connect(self._quit)
        m.addAction(a1); m.addSeparator(); m.addAction(a2)
        self.tray.setContextMenu(m)
        self.tray.activated.connect(lambda r: self._show_win() if r==QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.show()

    def _show_win(self): self.show(); self.activateWindow()
    def closeEvent(self, e):
        if self.cfg["settings"].get("minimize_to_tray"): e.ignore(); self.hide()
        else: self._quit()

    def changeEvent(self, e):
        super().changeEvent(e)

    def _stop_background_workers(self):
        """Останавливает ещё работающие фоновые QThread'ы (проверка VLESS-профиля,
        failover, пинг, health-check, докачка флагов/geo) перед выходом из
        приложения. Без этого Qt может уничтожить C++-объект потока, пока он
        физически ещё выполняется (например, пользователь закрывает программу во
        время ~15-20с проверки профиля) — это печатает "QThread: Destroyed while
        thread is still running" и обычно фатально валит процесс."""
        workers = [
            getattr(self, "_verify_worker", None),
            getattr(self, "_failover_worker", None),
            getattr(self, "_vless_health_worker", None),
            getattr(self, "_geo_downloader", None),
            getattr(self._netp, "_ping_worker", None),
            getattr(self._netp, "_flag_dl", None),
            *getattr(self._netp, "_sub_fetch_workers", {}).values(),
            *getattr(self, "_start_workers", set()),
            *getattr(self, "_stop_workers", set()),
            *getattr(self, "_log_readers", set()),
            *[getattr(b, "reader", None) for b in getattr(self, "bots", [])],
        ]
        for w in workers:
            if w is not None and w.isRunning():
                w.terminate()
                w.wait(2000)

    def _quit(self):
        self._stop_background_workers()
        pids = [b.process.pid for b in self.bots
                if b.process and b.process.poll() is None]
        for b in self.bots:
            b._alive = False; b.status = "stopped"; b.process = None
        def _kill():
            for pid in pids:
                try:
                    subprocess.run(['taskkill', '/f', '/t', '/pid', str(pid)],
                        capture_output=True, timeout=5,
                        creationflags=subprocess.CREATE_NO_WINDOW)
                except: pass
        if pids:
            threading.Thread(target=_kill, daemon=False).start()
        QApplication.quit()

    def _load(self):
        for d in self.cfg.get("bots",[]): self._reg(Bot(d))
        v = self.cfg.get("vless", {})
        profiles = v.get("profiles", [])
        active = v.get("active", -1)
        # Настройка "Подключать прокси при старте" — если включена, поднимаем активный
        # профиль всегда, независимо от того, было ли соединение вручную отключено
        # в прошлый раз (иначе легко забыть переподключить, а от прокси зависят другие боты).
        if self.cfg["settings"].get("vless_autostart", True) and 0 <= active < len(profiles):
            name = profiles[active].get("name") or f"Профиль {active+1}"
            QTimer.singleShot(500, lambda: self._vless_connect(profiles[active]["raw_input"], name))

    def _reg(self, bot):
        self.bots.append(bot)
        nav = SideItem("●", bot.name, is_bot=True)
        nav.clicked.connect(lambda b=bot: self._show(b.id))
        if self._compact: nav.set_compact(True)
        if self._nw.count() > 0:
            div = QFrame(); div.setFrameShape(QFrame.Shape.HLine)
            div.setStyleSheet("background:#3a3a3c;max-height:1px;border:none;margin-left:14px;")
            self._nw.addWidget(div)
        self._nw.addWidget(nav); self.navs[bot.id] = nav
        page = BotPage(bot)
        page.sig_start.connect(self._start); page.sig_stop.connect(self._stop)
        page.sig_restart.connect(self._restart); page.sig_delete.connect(self._delete)
        page.sig_settings_changed.connect(self._on_bot_settings_changed)
        self.stack.addWidget(page); self.pages[bot.id] = page
        self._setp.refresh_bots()
        if not self.cur: self._show(bot.id)

    def _on_bot_autostart(self, bot_id, val):
        for bot in self.bots:
            if bot.id == bot_id: bot.autostart = val; break
        for d in self.cfg["bots"]:
            if d.get("id") == bot_id: d["autostart"] = val; break
        # Собственная страница бота строит свой переключатель "Автозапуск при
        # старте" один раз при создании и не следит за изменениями отсюда —
        # без этого она показывала бы устаревшее значение, пока бота не
        # пересоздать. Toggle.setChecked() ничего не эмитит (эмитит только
        # клик мышью), так что зациклить сигналы этим нельзя.
        page = self.pages.get(bot_id)
        if page is not None and hasattr(page, "tg_auto"):
            page.tg_auto.setChecked(val)
        # Сохраняем с небольшой задержкой чтобы батчевые вызовы не конфликтовали
        if not getattr(self, '_save_timer', None):
            self._save_timer = QTimer(self)
            self._save_timer.setSingleShot(True)
            self._save_timer.timeout.connect(lambda: save_config(self.cfg))
        self._save_timer.start(200)

    def _on_bot_settings_changed(self, bot):
        # Обновляем название в сайдбаре
        nav = self.navs.get(bot.id)
        if nav: nav.lbl.setText(bot.name)
        # Список автозапуска на странице Настроек строится один раз при
        # регистрации бота и сам по себе не обновляется — без этого вызова
        # переключатель "автозапуск" в Настройках оставался в старом
        # состоянии после смены на собственной странице бота, пока не
        # добавить/удалить какого-нибудь бота (единственное, что раньше
        # вызывало refresh_bots()).
        self._setp.refresh_bots()
        # Синхронизируем в конфиге
        for d in self.cfg["bots"]:
            if d.get("id") == bot.id:
                d["name"]        = bot.name
                d["path"]        = bot.path
                d["args"]        = bot.args
                d["python"]      = bot.python
                d["autostart"]   = bot.autostart
                d["auto_restart"]= bot.auto_restart
                break
        # Дебаунс-сохранение 500 мс
        if not getattr(self, '_bot_cfg_save_timer', None):
            self._bot_cfg_save_timer = QTimer(self)
            self._bot_cfg_save_timer.setSingleShot(True)
            self._bot_cfg_save_timer.timeout.connect(lambda: save_config(self.cfg))
        self._bot_cfg_save_timer.start(500)

    def _show(self, tab):
        self.cur = tab
        for nid, nav in self.navs.items(): nav.set_sel(nid==tab)
        self._nadd.set_sel(tab=="add"); self._nset.set_sel(tab=="settings")
        self._nhome.set_sel(tab in ("home", "snake", "dino"))
        self._nnet.set_sel(tab=="net")
        if tab=="add": self.stack.setCurrentWidget(self._addp)
        elif tab=="settings": self.stack.setCurrentWidget(self._setp)
        elif tab=="home": self.stack.setCurrentWidget(self._homep)
        elif tab=="snake": self.stack.setCurrentWidget(self._snakep)
        elif tab=="dino": self.stack.setCurrentWidget(self._dinop)
        elif tab=="net": self.stack.setCurrentWidget(self._netp)
        elif tab in self.pages: self.stack.setCurrentWidget(self.pages[tab])
        else: self.stack.setCurrentWidget(self.stack.widget(0))

    def _on_add(self, d):
        bot = Bot(d); self._reg(bot)
        self.cfg["bots"].append(d); save_config(self.cfg)
        self._show(bot.id)
        if d.get("autostart"): self._start(bot)

    def _on_set(self, s):
        self.cfg["settings"] = s; save_config(self.cfg)
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,r"Software\Microsoft\Windows\CurrentVersion\Run",0,winreg.KEY_SET_VALUE)
            if s.get("autostart_app"): winreg.SetValueEx(key,"BotManager",0,winreg.REG_SZ,f'"{sys.executable}" "{__file__}"')
            else:
                try: winreg.DeleteValue(key,"BotManager")
                except: pass
            winreg.CloseKey(key)
        except: pass

    def _autostart(self):
        for b in self.bots:
            if b.autostart: self._start(b)

    def _ensure_vless_bot(self):
        if self._vless_bot is None:
            self._vless_bot = Bot({
                "id": "__vless__", "name": "VLESS Proxy",
                "path": str(XRAY_EXE), "args": f'run -c "{XRAY_CONFIG_FILE}"',
                "python": "python", "autostart": False, "auto_restart": True,
            })
            self.bots.append(self._vless_bot)
            self.pages["__vless__"] = self._netp
            self._netp.bot = self._vless_bot
        return self._vless_bot

    def _vless_connect(self, raw_input, profile_name=None):
        # Проверяем isRunning(), а не просто truthiness атрибута — ссылка на
        # воркер живёт до его finished (см. _on_verify_worker_finished/
        # _on_failover_worker_finished), а _on_vless_verify_result/
        # _vless_failover_done вызывают _vless_connect САМИ, пока их же атрибут
        # ещё не None. isRunning() при этом уже корректно вернёт False — поток
        # реально завершился к моменту, когда result успел дойти. Так что этот
        # guard блокирует только СТОРОННИЕ вызовы, пока верификация/failover ещё
        # физически выполняются (кнопка "Подключить", автостарт, переподключение
        # после удаления активного профиля) — которые раньше ничем не были
        # защищены и могли молча затереть конфиг/процесс, пока верификация или
        # failover ещё не закончились.
        if (self._verify_worker and self._verify_worker.isRunning()) or \
           (self._failover_worker and self._failover_worker.isRunning()):
            self._netp.add_log("Сейчас идёт проверка/переключение профиля — подожди немного и попробуй снова")
            return
        if getattr(self, "_geo_downloader", None) and self._geo_downloader.isRunning():
            self._netp.add_log("Сейчас докачиваются geoip/geosite базы — подожди немного и попробуй снова")
            return
        raw_input = (raw_input or "").strip()
        if not raw_input:
            self._netp.add_log("✗ Пустое поле — вставь vless:// ссылку или JSON")
            return
        label = f" «{profile_name}»" if profile_name else ""
        self._netp.add_log(f"🔌 Подключаюсь к профилю{label}...")
        if not XRAY_EXE.exists():
            self._netp.add_log(f"✗ Не найден {XRAY_EXE}")
            self._netp.set_status("error")
            return
        try:
            xr_cfg = build_xray_config(raw_input)
        except Exception as e:
            self._netp.add_log(f"✗ Ошибка конфигурации: {e}")
            self._netp.set_status("error")
            return
        # Запасные серверы прямо в конфиг xray (observatory + балансировщик):
        # при обрыве активного сервера трафик мгновенно уходит на живой запасной
        # без перезапуска процесса — боты не видят паузу в ~24с, которую давал
        # внешний failover. Ошибка здесь не критична: без балансировщика конфиг
        # остаётся рабочим в прежнем одиночном режиме.
        try:
            backups = collect_backup_uris(xr_cfg, self.cfg.get("vless", {}).get("profiles", []))
            added = add_failover_balancer(xr_cfg, backups)
            if added:
                self._netp.add_log(f"⚖ В балансировщик добавлено запасных серверов: {len(added)} — обрыв активного переживём без паузы")
        except Exception as e:
            self._netp.add_log(f"⚠ Балансировщик не добавлен ({e}) — работаю на одном сервере, как раньше")
        needs_geo = "geoip:" in raw_input or "geosite:" in raw_input
        have_geo = (XRAY_DIR / "geoip.dat").exists() and (XRAY_DIR / "geosite.dat").exists()
        if needs_geo and not have_geo:
            self._netp.add_log("В конфиге используются geoip:/geosite: правила — скачиваю базы...")
            dl = GeoDownloader()
            dl.log.connect(self._netp.add_log)
            dl.done.connect(lambda: self._finish_vless_connect(raw_input, xr_cfg))
            dl.finished.connect(dl.deleteLater)
            self._geo_downloader = dl  # держим ссылку, пока поток жив
            dl.start()
        else:
            self._finish_vless_connect(raw_input, xr_cfg)

    def _finish_vless_connect(self, raw_input, xr_cfg):
        try:
            XRAY_DIR.mkdir(parents=True, exist_ok=True)
            XRAY_CONFIG_FILE.write_text(json.dumps(xr_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            self._netp.add_log(f"✗ Не удалось записать конфиг: {e}")
            self._netp.set_status("error")
            return
        bot = self._ensure_vless_bot()
        self.cfg.setdefault("vless", {})["enabled"] = True
        save_config(self.cfg)
        # restart=True: при переключении профиля нужен новый процесс с новым
        # конфигом, а не старый, который тихо продолжит работать на прежнем сервере.
        self._start(bot, restart=True)

    def _vless_disconnect(self):
        if self._vless_bot is None: return
        self._stop(self._vless_bot)
        self._vless_health_fails = 0  # чтобы старые сбои не переносились на следующее подключение
        self.cfg.setdefault("vless", {})["enabled"] = False
        save_config(self.cfg)

    def _on_vless_profiles_changed(self, profiles, active_index):
        v = self.cfg.setdefault("vless", {})
        v["profiles"] = profiles
        v["active"] = active_index
        save_config(self.cfg)

    def _on_vless_subscriptions_changed(self, subscriptions):
        self.cfg.setdefault("vless", {})["subscriptions"] = subscriptions
        save_config(self.cfg)

    def _vless_verify_and_activate(self, profile_id, raw_input, name):
        """Ручное 'Сделать активным' — сначала проверяем новый профиль во временном
        xray, и только при успехе реально переключаемся (не рвём рабочее соединение
        ради заведомо мёртвого профиля)."""
        if self._verify_worker and self._verify_worker.isRunning():
            self._netp.add_log("Уже идёт проверка другого профиля — подожди...")
            return
        if self._failover_worker and self._failover_worker.isRunning():
            # Автопереключение уже идёт в фоне — обе операции в итоге пишут в один
            # и тот же XRAY_CONFIG_FILE и стартуют/останавливают один и тот же
            # процесс; без этой проверки они могли бы гоняться друг с другом и
            # тихо затирать результат друг друга.
            self._netp.add_log("Сейчас идёт автопереключение на резервный профиль — подожди...")
            return
        worker = VlessVerifyWorker(raw_input)
        worker.result.connect(lambda ok: self._on_vless_verify_result(ok, profile_id, raw_input, name))
        worker.finished.connect(self._on_verify_worker_finished)
        worker.finished.connect(worker.deleteLater)
        self._verify_worker = worker
        worker.start()

    def _on_verify_worker_finished(self):
        # Обнуляем ссылку по finished, а не в result-хендлере — finished эмитится
        # QThread'ом гарантированно ПОСЛЕ фактического завершения run(), тогда как
        # result.emit(...) — последняя строка ВНУТРИ run(), и между этим и реальным
        # возвратом из run() есть окно, в которое Python может сборщиком мусора
        # уничтожить ещё физически работающий QThread (если это была последняя
        # ссылка на него) — "QThread: Destroyed while thread is still running",
        # что фатально валит процесс.
        self._verify_worker = None

    def _on_vless_verify_result(self, ok, profile_id, raw_input, name):
        if not ok:
            self._netp.add_log(f"✗ Профиль «{name}» не отвечает — переключение отменено")
            return
        # Воркер проверял профиль ~15 сек — за это время список могли
        # отредактировать/удалить (UI во время проверки не блокируется). Ищем
        # профиль заново по стабильному id, а не доверяем позиции, захваченной в
        # момент клика (могла сместиться) — и не сравниваем raw_input напрямую:
        # два профиля могут случайно иметь одинаковую ссылку, id этого не допускает.
        idx = find_profile_index(self._netp.profiles, profile_id)
        if idx == -1:
            self._netp.add_log(f"✗ Профиль «{name}» был удалён, пока шла проверка — переключение отменено")
            return
        self._netp.add_log(f"✓ Профиль «{name}» рабочий — переключаюсь...")
        self._netp.set_active_index(idx)
        self.cfg.setdefault("vless", {})["active"] = idx
        save_config(self.cfg)
        self._vless_connect(raw_input, name)

    def _vless_failover(self):
        if self._failover_worker and self._failover_worker.isRunning():
            return  # предыдущее переключение ещё не завершилось
        if self._verify_worker and self._verify_worker.isRunning():
            # Пользователь как раз проверяет другой профиль вручную — не запускаем
            # параллельно автопереключение поверх него (см. _vless_verify_and_activate).
            self._netp.add_log("Идёт ручная проверка профиля — автопереключение отложено")
            return
        v = self.cfg.get("vless", {})
        profiles = v.get("profiles", [])
        active = v.get("active", -1)
        if len(profiles) < 2:
            return
        self._netp.add_log("Пробую переключиться на резервный профиль...")
        worker = VlessFailoverWorker(profiles, active)
        worker.log.connect(self._netp.add_log)
        worker.result.connect(self._vless_failover_done)
        worker.finished.connect(self._on_failover_worker_finished)
        worker.finished.connect(worker.deleteLater)
        self._failover_worker = worker
        worker.start()

    def _on_failover_worker_finished(self):
        # Обнуляем ссылку по finished, а не в result-хендлере (_vless_failover_done)
        # — finished эмитится QThread'ом гарантированно ПОСЛЕ фактического
        # завершения run(), тогда как result.emit(...) — последняя строка ВНУТРИ
        # run(), и между этим и реальным возвратом из run() есть окно, в которое
        # Python может сборщиком мусора уничтожить ещё физически работающий
        # QThread (если это была последняя ссылка на него) — "QThread: Destroyed
        # while thread is still running", что фатально валит процесс. Без этого
        # сброса self._failover_worker навсегда остаётся "занятым" после первого
        # же failover'а за сессию — все последующие вызовы _vless_failover() тихо
        # не срабатывали бы, сколько бы раз профиль потом ни падал.
        self._failover_worker = None

    def _vless_failover_done(self, profile_id, raw_input, name):
        if not profile_id:
            return
        # Воркер тестировал профили ~15-20 сек — за это время список могли
        # отредактировать/удалить (пока UI не заблокирован). Ищем победителя по
        # стабильному id, а не по индексу (мог сместиться/выйти за границы списка)
        # и не по raw_input (два профиля могут случайно иметь одинаковую ссылку).
        profiles = self.cfg["vless"]["profiles"]
        idx = find_profile_index(profiles, profile_id)
        if idx == -1:
            self._netp.add_log(f"✗ Профиль «{name}» был удалён, пока шла проверка — переключение отменено")
            return
        self.cfg["vless"]["active"] = idx
        save_config(self.cfg)
        self._netp.set_active_index(idx)
        # Успешное переключение — сбрасываем ОБА счётчика неудач (health-check и
        # крашей), не только тот, что вызвал этот failover. Иначе счётчик, который
        # не был причиной срабатывания, продолжает копиться и может почти сразу
        # запустить ещё один (уже не нужный) failover поверх свежепереключённого,
        # ещё не успевшего показать себя профиля.
        self._vless_health_fails = 0
        if self._vless_bot:
            self._vless_bot.restart_fails = 0
        # _vless_connect -> _finish_vless_connect стартует через _start(bot, restart=True),
        # так что "зомби"-процесс (жив, но не пропускает трафик) корректно
        # остановится сам — отдельный guard здесь больше не нужен.
        self._vless_connect(raw_input, name)

    def _init_vless_health_check(self):
        """Раз в 8 сек проверяет, что активный профиль реально пропускает трафик —
        не только что процесс xray жив (это отдельно от краш-детекции в _ended()).
        Было 30 сек (потом 15) — при 3 неудачах подряд для срабатывания failover
        требовалось до ~90 сек простоя; порог "3 подряд" не трогаем (чтобы не
        множить ложные срабатывания от единичных заминок), а считаем их быстрее:
        теперь до ~24 сек вместо ~90 в худшем случае."""
        self._vless_health_fails = 0
        self._vless_health_worker = None
        self._vless_health_timer = QTimer(self)
        self._vless_health_timer.timeout.connect(self._check_vless_health)
        self._vless_health_timer.start(8000)

    def _init_subscription_auto_update(self):
        """Раз в 30 мин проверяет, не пора ли обновить какую-то из подписок (по
        её собственному update_interval_hours, взятому из заголовка ответа
        сервера или дефолту), и если пора — тихо перекачивает её в фоне."""
        self._sub_update_timer = QTimer(self)
        self._sub_update_timer.timeout.connect(self._check_subscriptions_due)
        self._sub_update_timer.start(30 * 60 * 1000)

    def _check_subscriptions_due(self):
        now = time.time()
        for sub in list(self._netp.subscriptions):
            interval_s = sub.get("update_interval_hours", self._netp.DEFAULT_SUB_UPDATE_HOURS) * 3600
            if now - sub.get("last_fetch", 0) >= interval_s:
                self._netp._refresh_subscription(sub["id"])

    def _check_vless_health(self):
        if self._vless_health_worker and self._vless_health_worker.isRunning():
            return  # предыдущая проверка ещё не завершилась
        if not self._vless_bot or self._vless_bot.status != "running":
            self._vless_health_fails = 0
            return
        worker = VlessHealthWorker()
        worker.result.connect(self._on_vless_health_result)
        worker.finished.connect(self._on_health_worker_finished)
        worker.finished.connect(worker.deleteLater)
        self._vless_health_worker = worker
        worker.start()

    def _on_health_worker_finished(self):
        # См. _on_failover_worker_finished — обнуляем по finished, а не в
        # result-хендлере, чтобы не уничтожить ещё физически работающий QThread.
        self._vless_health_worker = None

    def _on_vless_health_result(self, ok):
        if not self._vless_bot or self._vless_bot.status != "running":
            return  # пользователь отключился (или переключил профиль), пока проверка
                     # была в полёте — результат уже неактуален, не считаем и не переключаем
        if ok:
            self._vless_health_fails = 0
            return
        self._vless_health_fails += 1
        self._netp.add_log(f"⚠ Активный профиль не пропускает трафик ({self._vless_health_fails}/3)")
        if self._vless_health_fails >= 3:
            self._vless_health_fails = 0
            self._netp.add_log("Профиль жив, но не работает — переключаюсь на резервный...")
            self._vless_failover()

    def _dot(self, bot, c):
        nav = self.navs.get(bot.id)
        if nav: nav.set_dot(c)

    def _start(self, bot, restart=False):
        """restart=False (по умолчанию): если процесс уже жив — просто ничего не
        делает (защита от повторного запуска повторным кликом). restart=True:
        останавливает уже живой ("зомби", например с устаревшим конфигом) процесс
        и сразу синхронно стартует новый — используется только для VLESS при
        смене профиля (_finish_vless_connect), где нужен новый процесс с новым
        конфигом немедленно. Это НЕ то же самое, что self._restart(bot) ниже —
        тот делает то же самое, но асинхронно с задержкой в 1.5с и UI-анимацией
        "переподключение", и используется для обычной ручной кнопки "Перезапустить"."""
        if bot.process and bot.process.poll() is None:
            if not restart:
                return  # уже запущен, повторный клик — не перезапускаем
            stop_worker = self._stop(bot)  # restart=True: живой ("зомби") процесс мешает новому конфигу
            if stop_worker is not None:
                # Не стартуем новый процесс, пока taskkill реально не завершится: для VLESS
                # (единственный вызывающий restart=True) старый и новый xray.exe делят один
                # и тот же фиксированный порт (XRAY_SOCKS_PORT) — если новый попытается
                # забиндиться раньше, чем старый освободит порт, bind молча провалится и
                # соединение "случайно" не поднимется сразу после смены профиля.
                stop_worker.finished.connect(lambda b=bot: self._start(b, restart=False))
                return
        if bot.id in self._pending_starts:
            return  # ProcessStartWorker уже запускает этот бот — bot.process ещё
                     # None, пока воркер не отработает, так что верхняя проверка
                     # его не поймает; без этого флага повторный клик до того, как
                     # воркер закончит, запустил бы ВТОРОЙ процесс параллельно.
        path = bot.path
        try:
            cmd = [path]+split_bot_args(bot.args) if path.lower().endswith(".exe") else \
                  split_bot_args(bot.python)+[path]+split_bot_args(bot.args)
            env = {**os.environ,"PYTHONIOENCODING":"utf-8","PYTHONUTF8":"1","PYTHONUNBUFFERED":"1"}
        except Exception as ex:
            page = self.pages.get(bot.id)
            if page: page.add_log(f"✗ Ошибка: {ex}"); page.set_status("error")
            self._dot(bot, RED)
            return
        # subprocess.Popen — в фоне (см. ProcessStartWorker): под давлением на
        # ресурсы Windows CreateProcess может занять заметное время, а раньше
        # он вызывался прямо здесь, в GUI-потоке, из-за чего клик "Старт"/
        # "Рестарт" мог подвесить всю программу на этот срок.
        self._pending_starts.add(bot.id)
        worker = ProcessStartWorker(bot, cmd, str(Path(path).parent), env)
        worker.result.connect(self._on_process_started)
        worker.error.connect(self._on_process_start_error)
        worker.finished.connect(lambda w=worker: self._start_workers.discard(w))
        worker.finished.connect(worker.deleteLater)
        self._start_workers.add(worker)
        worker.start()

    def _on_process_started(self, bot, proc):
        self._pending_starts.discard(bot.id)
        bot.process = proc
        _assign_to_job(bot.process.pid)
        bot.start_t = time.time(); bot.errors = 0; bot._alive = True
        bot._generation += 1
        gen = bot._generation
        page = self.pages.get(bot.id)
        if page: page.set_status("running"); page.add_log(f"▶ Запущен (PID {bot.process.pid})")
        self._dot(bot, GREEN)
        old_reader = bot.reader
        if old_reader is not None:
            # Старый LogReader мог физически ещё не закончиться (readline блокируется, пока
            # ОС не доставит EOF от уже убитого процесса) — держим ссылку до его СОБСТВЕННОГО
            # finished, а не отпускаем прямо здесь вместе с заменой bot.reader. Та же гонка
            # QThread, что уже чинили для PingWorker/FlagDownloader/VlessVerifyWorker и т.д.
            self._log_readers.add(old_reader)
            old_reader.finished.connect(lambda r=old_reader: self._log_readers.discard(r))
            old_reader.finished.connect(old_reader.deleteLater)
        bot.reader = LogReader(bot.process)
        bot.reader.line.connect(lambda l, b=bot: self._log(b,l))
        bot.reader.done.connect(lambda b=bot, g=gen: self._ended(b, g))
        bot.reader.start()
        # Через 2 минуты стабильной работы — сбрасываем счётчик падений
        QTimer.singleShot(120000, lambda b=bot: self._reset_fails(b))

    def _on_process_start_error(self, bot, msg):
        self._pending_starts.discard(bot.id)
        page = self.pages.get(bot.id)
        if page: page.add_log(f"✗ Ошибка: {msg}"); page.set_status("error")
        self._dot(bot, RED)

    def _stop(self, bot):
        """Возвращает ProcessStopWorker, если процесс реально останавливался (нужен
        вызывающему коду в _start(restart=True), чтобы дождаться фактического
        освобождения порта перед стартом нового процесса), иначе None."""
        bot._alive = False; bot.status = "stopped"
        worker = None
        if bot.process:
            proc = bot.process
            bot.process = None  # сразу и синхронно — код, проверяющий bot.process
                                 # (например _start), не должен ждать фактического
                                 # убийства процесса в фоне
            worker = ProcessStopWorker(proc.pid, proc)
            worker.finished.connect(lambda w=worker: self._stop_workers.discard(w))
            worker.finished.connect(worker.deleteLater)
            self._stop_workers.add(worker)
            worker.start()
        bot.start_t = None
        page = self.pages.get(bot.id)
        if page: page.set_status("stopped"); page.add_log("⏹ Остановлен")
        self._dot(bot, DARK)
        return worker
        self._dot(bot, DARK)

    def _restart(self, bot):
        self._stop(bot)
        page = self.pages.get(bot.id)
        if page: page.set_status("restarting"); page.add_log("🔄 Перезапуск через 1.5 сек...")
        self._dot(bot, YELLOW)
        QTimer.singleShot(1500, lambda: self._start(bot))

    def _delete(self, bot):
        r = QMessageBox.question(self,"Удалить",f"Удалить «{bot.name}»?",
            QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No)
        if r != QMessageBox.StandardButton.Yes: return
        self._stop(bot)
        nav = self.navs.pop(bot.id, None)
        if nav: self._nw.removeWidget(nav); nav.deleteLater()
        page = self.pages.pop(bot.id, None)
        if page: self.stack.removeWidget(page); page.deleteLater()
        self.bots.remove(bot)
        self.cfg["bots"] = [b for b in self.cfg["bots"] if b.get("id")!=bot.id]
        save_config(self.cfg)
        self._setp.refresh_bots()
        if self.bots: self._show(self.bots[0].id)
        else: self._show("add")

    def _log(self, bot, line):
        page = self.pages.get(bot.id)
        if page: page.add_log(line)

    def _reset_fails(self, bot):
        if bot.process and bot.process.poll() is None:
            bot.restart_fails = 0

    def _ended(self, bot, generation=None):
        if generation is not None and generation != bot._generation:
            return  # запоздавший сигнал от уже заменённого процесса — игнорируем
        if not bot._alive or bot.status == "stopped": return
        page = self.pages.get(bot.id)
        if bot.auto_restart and (bot.id == "__vless__" or self.cfg["settings"].get("auto_restart", True)):
            bot.restart_fails += 1
            will_failover = bot.restart_fails >= 3 and bot.id == "__vless__"
            if page:
                page.set_status("restarting")
                if will_failover:
                    page.add_log(f"⚠ Упал ({bot.restart_fails}/3) — переключаюсь на резервный профиль...")
                else:
                    page.add_log(f"⚠ Упал ({bot.restart_fails}/3), перезапуск через 10 сек...")
            self._dot(bot, YELLOW)
            if self.cfg["settings"].get("win_notifications"):
                self.tray.showMessage("Bot Manager", f"⚠ {bot.name} упал",
                    QSystemTrayIcon.MessageIcon.Warning, 3000)
            if bot.restart_fails >= 3:
                bot.restart_fails = 0
                if bot.id == "__vless__":
                    # Failover сам подключится к рабочему резервному профилю и поднимет
                    # процесс (_vless_failover_done → _vless_connect → _start), когда найдёт
                    # его — слепой рестарт через 10с той же трижды упавшей конфигурации тут
                    # не нужен и раньше гонялся с failover'ом, иногда перезапуская мёртвый
                    # профиль прямо посреди подбора резервного.
                    self._vless_failover()
                    return
            QTimer.singleShot(10000, lambda: self._start(bot) if bot._alive else None)
        else:
            if page: page.set_status("stopped"); page.add_log("⏹ Завершён")
            self._dot(bot, DARK)

_SIGNAL_FILE = Path(tempfile.gettempdir()) / "BotManager_show_signal"

if __name__ == "__main__":
    # ── Single instance через Windows Mutex + файл-сигнал ─────────────────────
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "BotManagerSingleInstanceMutex")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        _SIGNAL_FILE.touch()   # сигнал первому экземпляру — покажи окно
        sys.exit(0)

    def _excepthook(exc_type, exc_value, exc_tb):
        # Необработанное исключение в любом Qt-слоте иначе мгновенно убивает весь процесс.
        msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        print(f"ERROR: необработанное исключение:\n{msg}")
        try:
            (APP_DIR / "crash.log").write_text(msg, encoding="utf-8")
        except Exception:
            pass
    sys.excepthook = _excepthook

    app = QApplication(sys.argv); app.setStyle("Fusion")
    app.setQuitOnLastWindowClosed(False)  # не убивать процесс при hide() в трей
    win = App()
    win.show()
    code = app.exec()
    # Job Object убивает все дочерние процессы при выходе автоматически
    sys.exit(code)
