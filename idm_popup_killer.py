#!/usr/bin/env python3
"""
IDM Popup Killer (popup-only, batch-closing)

Closes Internet Download Manager nag and registration dialogs.
Does not kill IDMan.exe unless --register-action kill is combined with
--allow-process-kill, or you type 'kill' at the interactive prompt.

Escalation is per window:
  register dialogs follow --register-action (dismiss, force, kill)
  nag dialogs follow --nag-action (dismiss, force)

dismiss = button clicks only. force = button clicks plus WM_COMMAND /
WM_SYSCOMMAND / WM_CLOSE. kill = taskkill IDMan.exe (register only).
"""
import argparse
import ctypes
import json
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import unicodedata
from datetime import datetime

# ---------------- Win32 widths ----------------
# Windows x64 keeps DWORD at 32 bits, while HWND / WPARAM / LPARAM follow
# the pointer. ctypes.c_long is 32-bit even on 64-bit Windows, so LPARAM
# must not be c_long or a child HWND stuffed into lParam gets truncated.
_PTR_SIZE = ctypes.sizeof(ctypes.c_void_p)
if _PTR_SIZE == 8:
    _UINT_PTR = ctypes.c_uint64
    _LONG_PTR = ctypes.c_int64
elif _PTR_SIZE == 4:
    _UINT_PTR = ctypes.c_uint32
    _LONG_PTR = ctypes.c_int32
else:
    raise SystemError("unsupported pointer width: %s" % _PTR_SIZE)

HWND = ctypes.c_void_p          # HANDLE-sized, exact pointer width
HINSTANCE = ctypes.c_void_p
WPARAM = _UINT_PTR              # UINT_PTR
LPARAM = _LONG_PTR              # LONG_PTR
LRESULT = _LONG_PTR
BOOL = ctypes.c_int             # Win32 BOOL is a 32-bit int, not C _Bool
DWORD = ctypes.c_uint32
UINT = ctypes.c_uint32
HANDLE = ctypes.c_void_p
LPWSTR = ctypes.c_wchar_p

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
EnumProc = ctypes.WINFUNCTYPE(BOOL, HWND, LPARAM)


def _sig(fn, args, restype):
    fn.argtypes = args
    fn.restype = restype


_sig(user32.GetWindowTextLengthW, [HWND], ctypes.c_int)
_sig(user32.GetWindowTextW, [HWND, LPWSTR, ctypes.c_int], ctypes.c_int)
_sig(user32.GetClassNameW, [HWND, LPWSTR, ctypes.c_int], ctypes.c_int)
_sig(user32.IsWindowVisible, [HWND], BOOL)
_sig(user32.IsWindow, [HWND], BOOL)
_sig(user32.GetWindow, [HWND, UINT], HWND)
_sig(user32.GetWindowThreadProcessId, [HWND, ctypes.POINTER(DWORD)], DWORD)
_sig(user32.EnumWindows, [EnumProc, LPARAM], BOOL)
_sig(user32.EnumChildWindows, [HWND, EnumProc, LPARAM], BOOL)
_sig(user32.PostMessageW, [HWND, UINT, WPARAM, LPARAM], BOOL)
_sig(kernel32.OpenProcess, [DWORD, BOOL, DWORD], HANDLE)
_sig(kernel32.CloseHandle, [HANDLE], BOOL)
_sig(kernel32.QueryFullProcessImageNameW,
     [HANDLE, DWORD, LPWSTR, ctypes.POINTER(DWORD)], BOOL)


def assert_handle_widths():
    """Fail fast if a handle type cannot carry a 64-bit HWND/WPARAM/LPARAM."""
    ptr = ctypes.sizeof(ctypes.c_void_p)
    for name, typ in (("HWND", HWND), ("WPARAM", WPARAM), ("LPARAM", LPARAM)):
        if ctypes.sizeof(typ) != ptr:
            raise SystemError(
                "%s is %s bytes; pointer width is %s"
                % (name, ctypes.sizeof(typ), ptr)
            )
    if ctypes.sizeof(BOOL) != 4 or ctypes.sizeof(DWORD) != 4:
        raise SystemError("BOOL/DWORD must stay 32-bit on Windows")


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
GW_OWNER = 4
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_SYSCOMMAND = 0x0112
SC_CLOSE = 0xF060
BM_CLICK = 0x00F5
IDCANCEL = 2
IDNO = 7
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Accelerator suffix used by Win32 / MUI: "Cancel", "&Cancel", "取消(&C)".
_ACCEL_SUFFIX = re.compile(r"[(（]\s*&[^)）]{0,4}[)）]")
_FORMAT_CHARS = dict.fromkeys(
    (
        0x200C,  # ZWNJ
        0x200D,  # ZWJ
        0x200E,  # LRM
        0x200F,  # RLM
        0x202A,  # LRE
        0x202B,  # RLE
        0x202C,  # PDF
        0x2066,  # LRI
        0x2067,  # RLI
        0x2069,  # PDI
        0xFEFF,  # BOM
        0x0640,  # Arabic tatweel
    ),
    None,
)

# (code, register-hint substrings, primary dismiss labels)
# Primary labels are tried before the short generic buttons so
# "Remind me later" wins over a bare "No" / "OK" on the same dialog.
_LOCALES = [
    ("en", [
        "register", "buy now", "unregistered", "activate", "purchase",
        "license", "trial", "30-day", "30 day", "serial", "activation",
        "registration", "do you want to register", "register it now",
        "not registered",
    ], [
        "remind me later", "remind me", "later", "not now", "no thanks",
        "no, thanks",
    ]),
    ("es", [
        "registrar", "compra ahora", "comprar ahora", "no registrado",
        "no registrada", "activar", "licencia", "periodo de prueba",
        "período de prueba", "numero de serie", "número de serie",
        "activacion", "activación", "registrarse",
    ], [
        "recordar mas tarde", "recordar más tarde",
        "recordarme mas tarde", "recordarme más tarde",
        "mas tarde", "más tarde", "ahora no", "no, gracias",
    ]),
    ("pt", [
        "registrar", "registar", "comprar agora", "nao registrado",
        "não registrado", "nao registrada", "não registrada",
        "nao registado", "não registado", "ativar", "licenca", "licença",
        "avaliacao", "avaliação", "versao de teste", "versão de teste",
        "numero de serie", "número de série", "activacao", "ativação",
    ], [
        "lembrar mais tarde", "lembrar-me mais tarde",
        "mais tarde", "agora nao", "agora não", "nao, obrigado",
        "não, obrigado",
    ]),
    ("ru", [
        "регистрац", "зарегистрир", "купить", "незарегистрир",
        "активац", "лиценз", "пробн", "серийн",
    ], [
        "напомнить позже", "напомнить позднее", "позже",
        "не сейчас", "нет, спасибо",
    ]),
    ("fr", [
        "enregistrer", "acheter", "non enregist", "activer",
        "licence", "periode d'essai", "période d'essai",
        "numero de serie", "numéro de série", "inscription",
    ], [
        "rappeler plus tard", "me le rappeler plus tard",
        "plus tard", "pas maintenant", "non merci",
    ]),
    ("de", [
        "registrier", "jetzt kaufen", "unregistriert", "nicht registriert",
        "aktivier", "lizenz", "testversion", "seriennummer",
    ], [
        "spater erinnern", "später erinnern", "spater", "später",
        "nicht jetzt", "nein danke",
    ]),
    ("ar", [
        "تسجيل", "غير مسجل", "شراء", "اشتري", "ترخيص", "تفعيل",
        "تجريب", "رقم تسلسلي", "التسجيل",
    ], [
        "ذكرني لاحقا", "ذكّرني لاحقاً", "لاحقا", "ليس الان", "ليس الآن",
        "لا شكرا", "لا شكراً",
    ]),
    ("vi", [
        "dang ky", "đăng ký", "chua dang ky", "chưa đăng ký",
        "mua ngay", "kich hoat", "kích hoạt", "giay phep", "giấy phép",
        "dung thu", "dùng thử", "so seri", "số seri",
    ], [
        "nhac toi sau", "nhắc tôi sau", "nhac lai sau", "nhắc lại sau",
        "de sau", "để sau", "khong phai bay gio", "không phải bây giờ",
    ]),
    ("zh-Hans", [
        "注册", "立即购买", "未注册", "激活", "许可证", "试用",
        "序列号", "购买", "登记",
    ], [
        "稍后提醒", "以后提醒", "稍后", "以后再说", "暂不", "暂不注册",
    ]),
    ("zh-Hant", [
        "註冊", "立即購買", "未註冊", "啟用", "許可證", "試用",
        "序號", "購買", "登記",
    ], [
        "稍後提醒", "以後提醒", "稍後", "以後再說", "暫不", "暫不註冊",
    ]),
    ("tr", [
        "kayit ol", "kayıt ol", "kaydol", "satin al", "satın al",
        "lisanssiz", "lisanssız", "etkinlestir", "etkinleştir",
        "lisans", "deneme", "seri numarasi", "seri numarası",
    ], [
        "daha sonra hatirlat", "daha sonra hatırlat", "hatirlat",
        "hatırlat", "daha sonra", "simdi degil", "şimdi değil",
    ]),
    ("id", [
        "belum terdaftar", "beli sekarang", "aktivasi", "lisensi",
        "uji coba", "nomor seri", "mendaftar", "registrasi",
    ], [
        "ingatkan saya nanti", "nanti saja", "jangan sekarang",
    ]),
    ("fa", [
        "ثبت نام", "ثبت‌نام", "خرید", "مجوز", "فعال سازی", "فعال‌سازی",
        "فعالسازی", "ازمایشی", "آزمایشی", "شماره سریال",
    ], [
        "بعدا یادآوری کن", "بعداً یادآوری کن", "یادآوری بعدی",
        "بعدا", "بعداً", "الان نه", "الآن نه",
    ]),
]

# Short labels used by pass 2, and appended after the longer phrases so a
# specific "later" button is preferred when both are present.
_FALLBACK_GROUPS = [
    ["no", "non", "nein", "nao", "não", "нет", "لا", "không", "否",
     "hayır", "tidak", "خیر"],
    ["cancel", "cancelar", "annuler", "abbrechen", "отмена", "إلغاء",
     "الغاء", "hủy", "取消", "iptal", "batal", "لغو", "انصراف"],
    ["close", "cerrar", "fechar", "fermer", "schließen", "закрыть",
     "إغلاق", "اغلاق", "đóng", "关闭", "關閉", "kapat", "tutup", "بستن"],
    ["dismiss", "dispensar"],
    ["ok", "okay", "tamam", "موافق", "确定", "確定"],
]

REGISTER_HINTS = []
DISMISS_BUTTONS = []
FALLBACK_BUTTONS = []
LOCALE_CODES = []


def strip_accel(text):
    """Drop Win32 accelerator markers without eating a literal ampersand.

    "&Cancel" -> "Cancel", "Rappeler plus &tard" -> "Rappeler plus tard",
    "取消(&C)" -> "取消", "AT&&T" -> "AT&T".
    """
    text = _ACCEL_SUFFIX.sub("", text)
    out = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "&":
            if i + 1 < n and text[i + 1] == "&":
                out.append("&")
                i += 2
            else:
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def norm_text(text):
    """Fold case and compatibility forms so Latin, Cyrillic, Arabic, and CJK compare cleanly.

    NFKC folds fullwidth Latin. casefold covers ß/Σ/İ. Combining marks are
    then dropped so "Más" and "Mas" meet, and Vietnamese tones do too.
    Arabic harakat and tatweel are removed. Non-Latin letters that do not
    have case (Chinese, Arabic, Persian) pass through unchanged.
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", text)
    folded = strip_accel(folded)
    folded = folded.translate(_FORMAT_CHARS)
    folded = folded.casefold().replace("đ", "d")
    decomposed = unicodedata.normalize("NFKD", folded)
    chars = []
    for ch in decomposed:
        if unicodedata.combining(ch):
            continue
        # Arabic harakat / Quranic marks that survive NFKC.
        if 0x064B <= ord(ch) <= 0x065F or ord(ch) in (0x0670, 0x06D6, 0x06D7, 0x06D8, 0x06D9, 0x06DA, 0x06DB, 0x06DC, 0x06DF, 0x06E0, 0x06E1, 0x06E2, 0x06E3, 0x06E4, 0x06E7, 0x06E8, 0x06EA, 0x06EB, 0x06EC, 0x06ED):
            continue
        chars.append(ch)
    return " ".join("".join(chars).split())


def _dedupe(items):
    out = []
    seen = set()
    for item in items:
        raw = str(item).strip()
        key = norm_text(raw)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(raw)
    return out


def install_builtin_lexicon():
    global REGISTER_HINTS, DISMISS_BUTTONS, FALLBACK_BUTTONS, LOCALE_CODES
    hints = []
    primary = []
    codes = []
    for code, locale_hints, locale_buttons in _LOCALES:
        codes.append(code)
        hints.extend(locale_hints)
        primary.extend(locale_buttons)
    generics = [label for group in _FALLBACK_GROUPS for label in group]
    REGISTER_HINTS = _dedupe(hints)
    DISMISS_BUTTONS = _dedupe(primary + generics)
    FALLBACK_BUTTONS = _dedupe(generics)
    LOCALE_CODES = codes


install_builtin_lexicon()


def load_hints(path):
    """Replace hint / button tables from a JSON file.

    Schema (every key optional):
        {"register_hints": ["registrar", ...],
         "dismiss_buttons": ["recordarme", ...],
         "fallback_buttons": ["cancelar", ...]}
    Unknown keys are ignored. A replaced dismiss list also becomes the
    pass-2 fallback list unless fallback_buttons is set explicitly.
    """
    global REGISTER_HINTS, DISMISS_BUTTONS, FALLBACK_BUTTONS
    with open(path, "r", encoding="utf-8-sig") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("hints file must contain a JSON object")
    if "register_hints" in data:
        REGISTER_HINTS = _dedupe(data["register_hints"])
    if "dismiss_buttons" in data:
        DISMISS_BUTTONS = _dedupe(data["dismiss_buttons"])
        FALLBACK_BUTTONS = list(DISMISS_BUTTONS)
    if "fallback_buttons" in data:
        FALLBACK_BUTTONS = _dedupe(data["fallback_buttons"])


def apply_extra_hints(text):
    """Merge comma-separated keywords and button labels into the live tables.

    Prefix a token with "hint:" or "btn:" to target one list.
    Unprefixed tokens are added to both, so a single flag can extend
    classification and clicking without editing the source.
    """
    if not text:
        return 0
    hints = []
    buttons = []
    for part in text.split(","):
        token = part.strip()
        if not token:
            continue
        lowered = token.casefold()
        if lowered.startswith("hint:"):
            hints.append(token.split(":", 1)[1].strip())
        elif lowered.startswith("btn:") or lowered.startswith("button:"):
            buttons.append(token.split(":", 1)[1].strip())
        else:
            hints.append(token)
            buttons.append(token)
    before_h = len(REGISTER_HINTS)
    before_b = len(DISMISS_BUTTONS)
    if hints:
        REGISTER_HINTS.extend(_dedupe(hints))
        # _dedupe on the extension only; drop overlaps with what was already there
        _reunique(REGISTER_HINTS)
    if buttons:
        DISMISS_BUTTONS.extend(buttons)
        FALLBACK_BUTTONS.extend(buttons)
        _reunique(DISMISS_BUTTONS)
        _reunique(FALLBACK_BUTTONS)
    return (len(REGISTER_HINTS) - before_h) + (len(DISMISS_BUTTONS) - before_b)


def _reunique(items):
    deduped = _dedupe(items)
    items[:] = deduped


def aggressive_for(cls, register_action, nag_action):
    """True when this class should climb the Win32 message ladder.

    kill is handled by the monitor before the ladder runs; if taskkill
    fails, registration dialogs still take the force path.
    """
    if cls == "register":
        return register_action in ("force", "kill")
    if cls == "nag":
        return nag_action == "force"
    return False


def stamp_actions(pairs, register_action, nag_action):
    """Attach per-window escalation onto each (class, fingerprint) pair."""
    stamped = []
    for cls, win in pairs:
        win["_class"] = cls
        win["_aggressive"] = aggressive_for(cls, register_action, nag_action)
        stamped.append(win)
    return stamped


# ---------------- helpers ----------------
def get_window_text(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def get_class_name(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def get_pid(hwnd):
    pid = DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def pid_to_name(pid):
    if not pid:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
        return ""
    finally:
        kernel32.CloseHandle(handle)


def child_texts(hwnd):
    out = []

    def cb(child, _):
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(child, buf, 512)
        text = buf.value.strip()
        if text:
            out.append(text)
        return True

    user32.EnumChildWindows(hwnd, EnumProc(cb), 0)
    return out


def idm_dialog_windows():
    found = []

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        if get_class_name(hwnd) != "#32770":
            return True
        if not get_window_text(hwnd):
            return True
        if "idman" not in pid_to_name(get_pid(hwnd)).lower():
            return True
        found.append(hwnd)
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return found


def fingerprint(hwnd):
    texts = child_texts(hwnd)
    raw = " ".join([get_window_text(hwnd)] + texts)
    return {
        "hwnd": hwnd,
        "title": get_window_text(hwnd),
        "class": get_class_name(hwnd),
        "has_owner": bool(user32.GetWindow(hwnd, GW_OWNER)),
        "children": texts,
        "blob": norm_text(raw),
    }


def classify(win):
    if not win or win["class"] != "#32770":
        return None
    blob = win["blob"]
    for hint in REGISTER_HINTS:
        folded = norm_text(hint)
        if folded and folded in blob:
            return "register"
    if win["has_owner"]:
        return "nag"
    return "main"


def _wait(stop_event, seconds):
    """Sleep, returning True if shutdown was requested during the wait."""
    if seconds < 0:
        seconds = 0
    if stop_event is None:
        time.sleep(seconds)
        return False
    return stop_event.wait(seconds)


# ---------------- batch close ----------------
def _child_labels(hwnd):
    found = []

    def cb(child, _):
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(child, buf, 512)
        text = buf.value.strip()
        if text:
            found.append((child, text, norm_text(text)))
        return True

    user32.EnumChildWindows(hwnd, EnumProc(cb), 0)
    return found


def fire_first_label(hwnd, labels):
    """Post BM_CLICK to the first child whose folded text matches, in label order."""
    wanted = []
    seen = set()
    for label in labels:
        key = norm_text(label)
        if key and key not in seen:
            seen.add(key)
            wanted.append(key)
    if not wanted:
        return None
    kids = _child_labels(hwnd)
    for key in wanted:
        for child, raw, folded in kids:
            if folded == key:
                user32.PostMessageW(child, BM_CLICK, 0, 0)
                return raw
    return None


def fire_first_dismiss(hwnd):
    return fire_first_label(hwnd, DISMISS_BUTTONS)


def _window_aggressive(win, default):
    if "_aggressive" in win:
        return bool(win["_aggressive"])
    return bool(default)


def close_popups_batch(wins, log, aggressive=None, pass_delay=0.15, stop_event=None):
    """Parallel escalation. Returns the windows that are still open.

    ``aggressive`` is the batch default used only when a window has no
    ``_aggressive`` key. The monitor stamps that key from the window class
    so a registration dialog and a nag in the same batch do not share a mode.

    Passes 1-2 (button clicks) always run.
    Passes 3-6 (IDNO, IDCANCEL, SC_CLOSE, WM_CLOSE) run only for windows
    whose action is force (or register kill after taskkill failed).
    """
    if not wins:
        return []
    remaining = list(wins)

    def aggr(win):
        return _window_aggressive(win, aggressive)

    def stopped():
        return stop_event is not None and stop_event.is_set()

    for win in remaining:
        hwnd = win["hwnd"]
        if stopped() or not user32.IsWindow(hwnd):
            continue
        clicked = fire_first_dismiss(hwnd)
        if clicked:
            log("    hwnd=%s click %r" % (hwnd, clicked))
    if _wait(stop_event, pass_delay):
        return [w for w in remaining if user32.IsWindow(w["hwnd"])]
    remaining = [w for w in remaining if user32.IsWindow(w["hwnd"])]

    for win in remaining:
        hwnd = win["hwnd"]
        if stopped():
            break
        clicked = fire_first_label(hwnd, FALLBACK_BUTTONS)
        if clicked:
            log("    hwnd=%s click %r (pass 2)" % (hwnd, clicked))
    if _wait(stop_event, pass_delay):
        return [w for w in remaining if user32.IsWindow(w["hwnd"])]
    remaining = [w for w in remaining if user32.IsWindow(w["hwnd"])]

    def post_aggressive(message, wparam, label):
        acted = False
        for win in remaining:
            if not aggr(win):
                continue
            hwnd = win["hwnd"]
            if not user32.IsWindow(hwnd):
                continue
            user32.PostMessageW(hwnd, message, wparam, 0)
            log("    hwnd=%s %s" % (hwnd, label))
            acted = True
        return acted

    if any(aggr(w) for w in remaining) and not stopped():
        if post_aggressive(WM_COMMAND, IDNO, "WM_COMMAND IDNO"):
            if _wait(stop_event, pass_delay):
                return [w for w in remaining if user32.IsWindow(w["hwnd"])]
            remaining = [w for w in remaining if user32.IsWindow(w["hwnd"])]
        if any(aggr(w) for w in remaining) and post_aggressive(WM_COMMAND, IDCANCEL, "WM_COMMAND IDCANCEL"):
            if _wait(stop_event, pass_delay):
                return [w for w in remaining if user32.IsWindow(w["hwnd"])]
            remaining = [w for w in remaining if user32.IsWindow(w["hwnd"])]
        if any(aggr(w) for w in remaining) and post_aggressive(WM_SYSCOMMAND, SC_CLOSE, "WM_SYSCOMMAND SC_CLOSE"):
            if _wait(stop_event, pass_delay):
                return [w for w in remaining if user32.IsWindow(w["hwnd"])]
            remaining = [w for w in remaining if user32.IsWindow(w["hwnd"])]
        if any(aggr(w) for w in remaining):
            post_aggressive(WM_CLOSE, 0, "WM_CLOSE")
            _wait(stop_event, pass_delay * 2)
            remaining = [w for w in remaining if user32.IsWindow(w["hwnd"])]

    return [w for w in remaining if user32.IsWindow(w["hwnd"])]


def kill_idm(log):
    if not shutil.which("taskkill"):
        log("    taskkill not found")
        return False
    result = subprocess.run(
        ["taskkill", "/F", "/IM", "IDMan.exe"],
        capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
    )
    if result.returncode == 0:
        log("    taskkill: killed IDMan.exe")
        return True
    if result.returncode == 128:
        log("    taskkill: no process (already gone)")
        return True
    detail = (result.stderr or result.stdout or "").strip()
    log("    taskkill FAILED rc=%s: %s" % (result.returncode, detail))
    return False


# ---------------- monitor ----------------
class Monitor(threading.Thread):
    def __init__(self, args, log, stop_event):
        super().__init__(daemon=True, name="idm-monitor")
        self.args = args
        self.log = log
        self._stop = stop_event
        self._recent = {}  # hwnd -> last attempt (per-window cooldown)
        self._counts = {"register": 0, "nag": 0, "fail": 0}

    def stop(self):
        self._stop.set()

    def run(self):
        self.log(
            "[*] monitor running (interval=%ss, per-window cooldown=%ss, "
            "batch_delay=%ss, languages=%s)"
            % (self.args.interval, self.args.cooldown, self.args.batch_delay,
               ",".join(LOCALE_CODES))
        )
        while not self._stop.is_set():
            try:
                self._scan_once()
            except Exception as exc:
                self.log("[!] monitor error: %s" % exc)
            self._stop.wait(self.args.interval)

    def _collect_targets(self):
        targets = []
        for hwnd in idm_dialog_windows():
            win = fingerprint(hwnd)
            cls = classify(win)
            if self.args.debug:
                self.log(
                    "[dbg] hwnd=%s class=%r owner=%s title=%r -> %s"
                    % (hwnd, win["class"], win["has_owner"], win["title"], cls)
                )
            if cls in ("register", "nag"):
                targets.append((cls, win))
        return targets

    def _eligible(self, targets, now):
        fresh = []
        for cls, win in targets:
            hwnd = win["hwnd"]
            last = self._recent.get(hwnd, 0.0)
            if now - last >= self.args.cooldown:
                fresh.append((cls, win))
            elif self.args.debug:
                self.log("[dbg] hwnd=%s in per-window cooldown, skipping" % hwnd)
        return fresh

    def _purge_recent(self, now):
        cutoff = now - max(self.args.cooldown, 0.5) * 4
        self._recent = {
            hwnd: stamp for hwnd, stamp in self._recent.items() if stamp > cutoff
        }

    def _close_pairs(self, pairs, now):
        for cls, win in pairs:
            self.log("    %s hwnd=%s title=%r action=%s"
                     % (cls.upper(), win["hwnd"], win["title"],
                        "force" if aggressive_for(
                            cls, self.args.register_action, self.args.nag_action
                        ) else "dismiss"))
            if self.args.debug:
                self.log("        children=%s" % (win["children"],))
            self._recent[win["hwnd"]] = now
        wins = stamp_actions(
            pairs, self.args.register_action, self.args.nag_action
        )
        survivors = close_popups_batch(
            wins, self.log, pass_delay=self.args.batch_delay, stop_event=self._stop,
        )
        for cls, _win in pairs:
            self._counts[cls] += 1
        for win in survivors:
            self.log("    ! survived: hwnd=%s title=%r class=%s"
                     % (win["hwnd"], win["title"], win.get("_class")))
            self._counts["fail"] += 1
        return survivors

    def _scan_once(self):
        if self._stop.is_set():
            return
        now = time.time()
        targets = self._collect_targets()
        if not targets:
            return
        fresh = self._eligible(targets, now)
        if not fresh:
            return

        self.log("[!] %s popup(s) found — batch closing" % len(fresh))
        if self.args.dry_run:
            for cls, win in fresh:
                self.log("    %s hwnd=%s title=%r" % (cls.upper(), win["hwnd"], win["title"]))
            self.log("    (dry-run: no action)")
            return

        # Process kill applies only to registration dialogs. Nag dialogs in
        # the same batch keep --nag-action. A failed taskkill falls through
        # to the force ladder for the registration windows.
        if self.args.register_action == "kill" and any(cls == "register" for cls, _w in fresh):
            self.log("[!] --register-action kill: terminating IDMan.exe")
            if kill_idm(self.log):
                for cls, win in fresh:
                    self._counts[cls] += 1
                    self._recent[win["hwnd"]] = now
                self._purge_recent(now)
                return
            self.log("    taskkill failed; registration dialogs use force escalation")

        self._close_pairs(fresh, now)
        if self._stop.is_set():
            self._purge_recent(now)
            return

        deadline = time.time() + 2.0
        empty_streak = 0
        while time.time() < deadline and not self._stop.is_set():
            if self._stop.wait(0.25):
                break
            now2 = time.time()
            more = self._eligible(self._collect_targets(), now2)
            if not more:
                empty_streak += 1
                if empty_streak >= 3:
                    break
                continue
            empty_streak = 0
            self.log("[!] %s more popup(s) — batch closing" % len(more))
            self._close_pairs(more, now2)

        self._purge_recent(now)

    def stats(self):
        return dict(self._counts)


# ---------------- interactive ----------------
class _StdinReader(threading.Thread):
    """Read commands until the shared shutdown event is set.

    On a real console the thread polls the keyboard so Ctrl+C does not
    leave it blocked inside readline. Piped stdin uses readline and exits
    on EOF or on the next line after shutdown.
    """

    def __init__(self, out_queue, stop_event):
        super().__init__(daemon=True, name="idm-stdin")
        self._queue = out_queue
        self._stop = stop_event
        self._console = False

    def run(self):
        try:
            self._console = (
                sys.platform == "win32"
                and sys.stdin is not None
                and sys.stdin.isatty()
            )
        except Exception:
            self._console = False
        if self._console:
            self._run_console()
        else:
            self._run_pipe()

    def _run_pipe(self):
        while not self._stop.is_set():
            try:
                line = sys.stdin.readline()
            except Exception:
                return
            if self._stop.is_set():
                return
            if not line:
                self._stop.set()
                return
            self._queue.put(line.strip().casefold())

    def _run_console(self):
        import msvcrt
        buf = []
        while not self._stop.is_set():
            if not msvcrt.kbhit():
                if self._stop.wait(0.05):
                    return
                continue
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                if msvcrt.kbhit():
                    msvcrt.getwch()
                continue
            if ch == "\x03":  # Ctrl+C delivered as input
                self._stop.set()
                return
            if ch in ("\r", "\n"):
                try:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                except Exception:
                    pass
                self._queue.put("".join(buf).strip().casefold())
                buf = []
                continue
            if ch == "\x08":
                if buf:
                    buf.pop()
                    try:
                        sys.stdout.write("\b \b")
                        sys.stdout.flush()
                    except Exception:
                        pass
                continue
            if ch == "\x1b":
                continue
            buf.append(ch)
            try:
                sys.stdout.write(ch)
                sys.stdout.flush()
            except Exception:
                pass


def _console_print(text=""):
    try:
        print(text)
    except Exception:
        pass


def interactive(monitor, log, args, shutdown):
    _console_print()
    _console_print("Commands:")
    _console_print("  close|c|<Enter>  -> close visible IDM popups (per-class action)")
    _console_print("  status|s         -> popups + stats")
    _console_print("  kill             -> kill IDMan.exe (explicit only)")
    _console_print("  quit|q           -> exit (or Ctrl+C)")
    _console_print()
    if sys.stdin is None:
        log("[*] no stdin (pythonw); monitor-only mode")
        while not shutdown.is_set():
            shutdown.wait(0.5)
        return

    commands = queue.Queue()
    reader = _StdinReader(commands, shutdown)
    reader.start()
    try:
        while not shutdown.is_set() and not monitor._stop.is_set():
            try:
                cmd = commands.get(timeout=0.5)
            except queue.Empty:
                continue
            if shutdown.is_set():
                break
            if cmd in ("quit", "q", "exit"):
                break
            elif cmd in ("", "close", "c"):
                targets = monitor._collect_targets()
                if not targets:
                    log("[*] no IDM popups to close")
                    continue
                log("[*] manual batch close (%s popup(s))" % len(targets))
                wins = stamp_actions(
                    targets, args.register_action, args.nag_action
                )
                close_popups_batch(
                    wins, log, pass_delay=args.batch_delay, stop_event=shutdown,
                )
            elif cmd == "kill":
                log("[*] manual kill of IDMan.exe")
                kill_idm(log)
            elif cmd == "status" or cmd == "s":
                targets = monitor._collect_targets()
                if not targets:
                    log("  no IDM popups visible")
                for cls, win in targets:
                    mode = "force" if aggressive_for(
                        cls, args.register_action, args.nag_action
                    ) else "dismiss"
                    log("  %s hwnd=%s title=%r action=%s"
                        % (cls, win["hwnd"], win["title"], mode))
                log("  stats=%s" % (monitor.stats(),))
            else:
                log("  unknown: %r" % (cmd,))
    finally:
        shutdown.set()
        reader.join(timeout=0.5)


# ---------------- main ----------------
def build_logger(path, echo=True):
    handle = open(path, "a", encoding="utf-8") if path else None

    def _log(msg):
        line = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg)
        if echo:
            try:
                print(line, flush=True)
            except Exception:
                pass
        if handle:
            handle.write(line + "\n")
            handle.flush()

    return _log, handle


def _install_signals(stop_event):
    def handler(signum, frame):
        # A second Ctrl+C during join/cleanup must not abort the finally block.
        if stop_event.is_set():
            return
        stop_event.set()
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, handler)
    if hasattr(signal, "SIGBREAK"):
        try:
            signal.signal(signal.SIGBREAK, handler)
        except (ValueError, OSError):
            pass
    return handler


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Close IDM popups in batch; never kill main IDM unless asked.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--interval", type=float, default=2.0,
                        help="seconds between scans")
    parser.add_argument(
        "--cooldown", type=float, default=2.0,
        help="per-window seconds before the same HWND is tried again "
             "(other popups are not frozen)",
    )
    parser.add_argument("--batch-delay", type=float, default=0.15,
                        help="seconds between escalation passes")
    parser.add_argument("--register-action", choices=["dismiss", "force", "kill"],
                        default="force",
                        help="registration dialogs: button clicks, full "
                             "message ladder, or taskkill")
    parser.add_argument("--nag-action", choices=["dismiss", "force"],
                        default="dismiss",
                        help="owned nag dialogs that are not registration prompts")
    parser.add_argument("--allow-process-kill", action="store_true",
                        help="required together with --register-action kill")
    parser.add_argument("--dry-run", action="store_true",
                        help="log matches without clicking or closing")
    parser.add_argument("--debug", action="store_true",
                        help="log window fingerprints and cooldown skips")
    parser.add_argument("--log-file", default=None,
                        help="append UTF-8 log lines to this path")
    parser.add_argument("--no-echo", action="store_true",
                        help="do not print log lines to the console")
    parser.add_argument(
        "--hints-file", default=None,
        help="JSON file with register_hints, dismiss_buttons, fallback_buttons",
    )
    parser.add_argument(
        "--extra-hints", default="",
        help="comma-separated extra keywords and button labels; "
             "prefix hint: or btn: to target one list, otherwise both",
    )
    args = parser.parse_args(argv)
    if args.register_action == "kill" and not args.allow_process_kill:
        parser.error("--register-action kill requires --allow-process-kill")
    if args.interval <= 0:
        parser.error("--interval must be > 0")
    if args.cooldown < 0:
        parser.error("--cooldown must be >= 0")
    if args.batch_delay < 0:
        parser.error("--batch-delay must be >= 0")
    return args


def main(argv=None):
    if sys.platform != "win32":
        print("Windows only.")
        return 1
    args = parse_args(argv)
    assert_handle_widths()
    log, handle = build_logger(args.log_file, echo=not args.no_echo)
    shutdown = threading.Event()
    _install_signals(shutdown)
    try:
        log("IDM Popup Killer (batch, popup-only)")
        log("  register_action=%s  nag_action=%s  allow_process_kill=%s"
            % (args.register_action, args.nag_action, args.allow_process_kill))
        log("  cooldown=%ss per HWND  languages=%s"
            % (args.cooldown, ",".join(LOCALE_CODES)))
        if args.hints_file:
            try:
                load_hints(args.hints_file)
                log("  loaded custom hints from %s" % args.hints_file)
            except Exception as exc:
                log("[!] failed to load hints file: %s" % exc)
                return 1
        if args.extra_hints:
            added = apply_extra_hints(args.extra_hints)
            log("  extra-hints applied (%s new entries)" % added)
        log("  lexicon: %s register hints, %s dismiss buttons"
            % (len(REGISTER_HINTS), len(DISMISS_BUTTONS)))
        mon = Monitor(args, log, shutdown)
        mon.start()
        try:
            interactive(mon, log, args, shutdown)
        except KeyboardInterrupt:
            log("\n[*] Ctrl+C")
        finally:
            log("[*] stopping monitor...")
            shutdown.set()
            mon.stop()
            mon.join(timeout=3)
            log("[*] stats: %s" % (mon.stats(),))
            log("[*] bye.")
        return 0
    finally:
        if handle:
            handle.close()


if __name__ == "__main__":
    sys.exit(main())
