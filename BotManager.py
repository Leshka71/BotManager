# -*- coding: utf-8 -*-
import sys, os, json, subprocess, time, winreg, random, ctypes
import urllib.request, tempfile, threading
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

def load_config():
    if CONFIG_FILE.exists():
        try: return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except: pass
    return {"bots": [], "settings": {"autostart_app":False,"autostart_bots":True,
        "win_notifications":True,"sound":False,"minimize_to_tray":True,"auto_restart":True,
        "vk_notifications":False,"vk_token":"","vk_user_id":""}}

def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

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

APP_VERSION = "1.2.3"
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
        if self.bot.status == "running": self.sig_stop.emit(self.bot)
        else: self.sig_start.emit(self.bot)

    def _tick(self):
        self._up_lbl.setText(self.bot.uptime())
        self._ev_lbl.setText(str(self.bot.events))
        self._er_lbl.setText(str(self.bot.errors))

    def set_status(self, s):
        self.bot.status = s; self._badge_style(s)
        colors = {"running": GREEN, "stopped": DARK, "error": RED, "restarting": YELLOW}
        self.dot.set(colors.get(s, DARK))
        self.b_run.setText("Стоп" if s == "running" else "Старт")
        if s == "running":
            self.b_run.setStyleSheet(
                f"QPushButton{{background:{RED};border:none;color:#fff;border-radius:10px;"
                f"font-size:13px;font-weight:700;min-height:0;}} QPushButton:hover{{background:#d93b31;}}")
        else:
            self.b_run.setStyleSheet(
                f"QPushButton{{background:{GREEN};border:none;color:#000;border-radius:10px;"
                f"font-size:13px;font-weight:700;min-height:0;}} QPushButton:hover{{background:#2db34a;}}")

    def add_log(self, line):
        ts = datetime.now().strftime("%H:%M:%S"); lu = line.upper()
        if any(w in lu for w in ["ERROR","ОШИБКА","КРИТИЧ","EXCEPTION","FAILED","TRACEBACK"]):
            c = RED; self.bot.errors += 1
        elif any(w in lu for w in ["WARN","⚠"]): c = ORANGE
        elif any(w in lu for w in ["✅","▶","ЗАПУЩЕН","CONNECTED","ONLINE"]): c = GREEN
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
        p, _ = QFileDialog.getOpenFileName(self, "Выбери файл", "",
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
        path, _ = QFileDialog.getOpenFileName(self, "Выбери bot.py", "", "Python файлы (*.py)")
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

        def _run():
            try:
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = 0  # SW_HIDE
                result = subprocess.run(
                    py.split() + [path, "broadcast", text],
                    capture_output=True, text=True, timeout=90,
                    cwd=str(Path(path).parent),
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    startupinfo=si
                )
                return result.stdout + result.stderr
            except subprocess.TimeoutExpired:
                return "timeout"
            except Exception as e:
                return f"error:{e}"

        def _finish(output):
            self._bc_btn.setEnabled(True)
            self._bc_btn.setText("📢 Отправить всем")
            if "Рассылка завершена" in output:
                _set_status("✅ Отправлено!", GREEN)
                self._bc_text.clear()
            elif output.startswith("timeout"):
                _set_status("⚠ Таймаут (90 сек)", YELLOW)
            elif output.startswith("error:"):
                _set_status(f"❌ {output[6:90]}", RED)
            else:
                err = output.strip().splitlines()[-1] if output.strip() else "нет вывода"
                _set_status(f"❌ {err[:80]}", RED)

        def _thread():
            out = _run()
            QTimer.singleShot(0, lambda: _finish(out))

        threading.Thread(target=_thread, daemon=True).start()

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
        self._build_vk_section(cl)
        self._section(cl, "ИНТЕРФЕЙС", [
            ("Сворачивать в трей",    "minimize_to_tray"),
            ("Автоперезапуск ботов",  "auto_restart"),
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

        if not self._bots:
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
        for bot in self._bots:
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

    def _inp(self, k, v): self.s[k] = v; self.changed.emit(self.s)

    def _row_input(self, pl, text, key, placeholder=""):
        row = QWidget(); row.setFixedHeight(44)
        row.setStyleSheet(self._TR)
        rl = QHBoxLayout(row); rl.setContentsMargins(16, 0, 12, 0); rl.setSpacing(12)
        lb = QLabel(text)
        lb.setStyleSheet(f"color:{WHITE};font-size:13px;background:transparent;border:none;")
        e = QLineEdit(self.s.get(key, ""))
        e.setPlaceholderText(placeholder)
        e.setFixedSize(190, 28)
        e.setStyleSheet(
            f"QLineEdit{{background:#1c1c1e;border:1px solid #3a3a3c;border-radius:6px;"
            f"color:#ccc;font-size:12px;padding:0 8px;}}"
            f"QLineEdit:focus{{border-color:{BLUE};}}"
        )
        e.textChanged.connect(lambda v, k=key: self._inp(k, v))
        rl.addWidget(lb); rl.addStretch(); rl.addWidget(e)
        if pl is not None: pl.addWidget(row)
        return e

    def _build_vk_section(self, cl):
        lbl = QLabel("VK УВЕДОМЛЕНИЯ")
        lbl.setStyleSheet(f"color:{GRAY};font-size:11px;letter-spacing:1px;font-weight:600;"
                          f"background:transparent;border:none;margin-bottom:2px;")
        cl.addWidget(lbl)
        card = QWidget(); card.setStyleSheet(self._CARD)
        vl = QVBoxLayout(card); vl.setContentsMargins(0, 0, 0, 0); vl.setSpacing(0)
        vl.addWidget(self._row("VK уведомления", "vk_notifications"))
        vl.addWidget(self._div())
        self._row_input(vl, "Токен группы", "vk_token", "vk1.a.XXXXX...")
        vl.addWidget(self._div())
        self._row_input(vl, "Ваш VK ID", "vk_user_id", "123456789")
        cl.addWidget(card)

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

# ── Главное окно ──────────────────────────────────────────────────────────────
class App(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Bot Manager"); self.resize(1020, 660); self.setMinimumSize(700,500)
        _ico = RES_DIR / "icon.ico"
        if _ico.exists(): self.setWindowIcon(QIcon(str(_ico)))
        self.setStyleSheet(QSS)
        self.cfg = load_config(); self.bots = []; self.pages = {}; self.navs = {}
        self.cur = None; self._compact = False
        self._build(); self._tray(); self._load()
        # Таймер для отслеживания сигнала показа окна от второго экземпляра
        self._signal_timer = QTimer(self)
        self._signal_timer.timeout.connect(self._check_show_signal)
        self._signal_timer.start(500)
        QTimer.singleShot(3000, self._check_update)
        if self.cfg["settings"].get("autostart_bots"): QTimer.singleShot(800, self._autostart)
        QApplication.instance().installEventFilter(self)

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

    def _do_update(self):
        def _run():
            try:
                tmp = tempfile.mktemp(suffix=".exe", prefix="BotManager_Setup_")
                urllib.request.urlretrieve(self._upd_url, tmp)
                cmd = f'cmd /c timeout /t 3 /nobreak >nul && start "" "{tmp}"'
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

    def _quit(self):
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
        if tab=="add": self.stack.setCurrentWidget(self._addp)
        elif tab=="settings": self.stack.setCurrentWidget(self._setp)
        elif tab=="home": self.stack.setCurrentWidget(self._homep)
        elif tab=="snake": self.stack.setCurrentWidget(self._snakep)
        elif tab=="dino": self.stack.setCurrentWidget(self._dinop)
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

    def _dot(self, bot, c):
        nav = self.navs.get(bot.id)
        if nav: nav.set_dot(c)

    def _start(self, bot):
        if bot.process and bot.process.poll() is None: return
        path = bot.path
        cmd = [path]+(bot.args.split() if bot.args else []) if path.lower().endswith(".exe") else \
              bot.python.split()+[path]+(bot.args.split() if bot.args else [])
        try:
            env = {**os.environ,"PYTHONIOENCODING":"utf-8","PYTHONUTF8":"1","PYTHONUNBUFFERED":"1"}
            bot.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                cwd=str(Path(path).parent), creationflags=subprocess.CREATE_NO_WINDOW, env=env)
            _assign_to_job(bot.process.pid)
            bot.start_t = time.time(); bot.errors = 0; bot._alive = True
            page = self.pages.get(bot.id)
            if page: page.set_status("running"); page.add_log(f"▶ Запущен (PID {bot.process.pid})")
            self._dot(bot, GREEN)
            bot.reader = LogReader(bot.process)
            bot.reader.line.connect(lambda l, b=bot: self._log(b,l))
            bot.reader.done.connect(lambda b=bot: self._ended(b))
            bot.reader.start()
            # Через 2 минуты стабильной работы — сбрасываем счётчик падений
            QTimer.singleShot(120000, lambda b=bot: self._reset_fails(b))
        except Exception as ex:
            page = self.pages.get(bot.id)
            if page: page.add_log(f"✗ Ошибка: {ex}"); page.set_status("error")
            self._dot(bot, RED)

    def _stop(self, bot):
        bot._alive = False; bot.status = "stopped"
        exe_name = Path(bot.path).name if bot.path and bot.path.lower().endswith(".exe") else None
        if bot.process:
            pid = bot.process.pid
            try:
                subprocess.run(['taskkill', '/f', '/t', '/pid', str(pid)],
                               capture_output=True, timeout=5,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            except Exception:
                try: bot.process.kill()
                except: pass
            finally:
                try: bot.process.wait(timeout=2)
                except: pass
            bot.process = None
        bot.start_t = None
        page = self.pages.get(bot.id)
        if page: page.set_status("stopped"); page.add_log("⏹ Остановлен")
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

    def _send_vk(self, bot_name, status_text):
        s = self.cfg["settings"]
        if not s.get("vk_notifications"): return
        token   = s.get("vk_token",   "").strip()
        user_id = s.get("vk_user_id", "").strip()
        if not token or not user_id: return
        def _run():
            try:
                import urllib.parse
                msg = f"⚠ Bot Manager\nБот «{bot_name}» {status_text}"
                params = {
                    "user_id":    user_id,
                    "message":    msg,
                    "random_id":  int(time.time() * 1000),
                    "access_token": token,
                    "v": "5.131",
                }
                url  = "https://api.vk.com/method/messages.send"
                data = urllib.parse.urlencode(params).encode()
                req  = urllib.request.Request(url, data=data,
                       headers={"User-Agent": "BotManager/1.0"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    resp = json.loads(r.read())
                    if "error" in resp:
                        print(f"VK ошибка {resp['error'].get('error_code')}: "
                              f"{resp['error'].get('error_msg')}")
            except Exception as ex:
                print(f"VK уведомление не отправлено: {ex}")
        threading.Thread(target=_run, daemon=True).start()

    def _reset_fails(self, bot):
        if bot.process and bot.process.poll() is None:
            bot.restart_fails = 0

    def _ended(self, bot):
        if not bot._alive or bot.status == "stopped": return
        page = self.pages.get(bot.id)
        if bot.auto_restart and self.cfg["settings"].get("auto_restart", True):
            bot.restart_fails += 1
            if page:
                page.set_status("restarting")
                page.add_log(f"⚠ Упал ({bot.restart_fails}/3), перезапуск через 10 сек...")
            self._dot(bot, YELLOW)
            if self.cfg["settings"].get("win_notifications"):
                self.tray.showMessage("Bot Manager", f"⚠ {bot.name} упал",
                    QSystemTrayIcon.MessageIcon.Warning, 3000)
            if bot.restart_fails >= 3:
                self._send_vk(bot.name, f"не запускается — 3 попытки подряд провалились")
                bot.restart_fails = 0
            QTimer.singleShot(10000, lambda: self._start(bot) if bot._alive else None)
        else:
            if page: page.set_status("stopped"); page.add_log("⏹ Завершён")
            self._dot(bot, DARK)
            self._send_vk(bot.name, "завершён")

_SIGNAL_FILE = Path(tempfile.gettempdir()) / "BotManager_show_signal"

if __name__ == "__main__":
    # ── Single instance через Windows Mutex + файл-сигнал ─────────────────────
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "BotManagerSingleInstanceMutex")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        _SIGNAL_FILE.touch()   # сигнал первому экземпляру — покажи окно
        sys.exit(0)

    app = QApplication(sys.argv); app.setStyle("Fusion")
    win = App()
    win.show()
    code = app.exec()
    # Job Object убивает все дочерние процессы при выходе автоматически
    sys.exit(code)
