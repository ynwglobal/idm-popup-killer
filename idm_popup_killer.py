#!/usr/bin/env python3
"""
IDM Popup Killer (popup-only, batch-closing)
- Closes IDM popups; never kills IDMan.exe unless --allow-process-kill
  is passed AND you type 'kill'.
- Multiple popups are closed in parallel batches, not one at a time.
"""
import argparse, ctypes, ctypes.wintypes as wt, queue, shutil
import subprocess, sys, threading, time
from datetime import datetime

# ---------------- Win32 ----------------
user32   = ctypes.WinDLL("user32",   use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

def _sig(fn, a, r): fn.argtypes = a; fn.restype = r
_sig(user32.GetWindowTextLengthW,     [wt.HWND], ctypes.c_int)
_sig(user32.GetWindowTextW,           [wt.HWND, wt.LPWSTR, ctypes.c_int], ctypes.c_int)
_sig(user32.GetClassNameW,            [wt.HWND, wt.LPWSTR, ctypes.c_int], ctypes.c_int)
_sig(user32.IsWindowVisible,          [wt.HWND], wt.BOOL)
_sig(user32.IsWindow,                 [wt.HWND], wt.BOOL)
_sig(user32.GetWindow,                [wt.HWND, wt.UINT], wt.HWND)
_sig(user32.GetWindowThreadProcessId, [wt.HWND, ctypes.POINTER(wt.DWORD)], wt.DWORD)
_sig(user32.EnumWindows,              [EnumProc, wt.LPARAM], wt.BOOL)
_sig(user32.EnumChildWindows,         [wt.HWND, EnumProc, wt.LPARAM], wt.BOOL)
_sig(user32.PostMessageW,             [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM], wt.BOOL)
_sig(kernel32.OpenProcess,            [wt.DWORD, wt.BOOL, wt.DWORD], wt.HANDLE)
_sig(kernel32.CloseHandle,            [wt.HANDLE], wt.BOOL)
_sig(kernel32.QueryFullProcessImageNameW,
     [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)], wt.BOOL)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
GW_OWNER      = 4
WM_CLOSE      = 0x0010
WM_COMMAND    = 0x0111
WM_SYSCOMMAND = 0x0112
SC_CLOSE      = 0xF060
BM_CLICK      = 0x00F5
IDCANCEL      = 2
IDNO          = 7
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

REGISTER_HINTS = [
    "register", "buy now", "unregistered", "activate", "purchase",
    "license", "trial", "30-day", "30 day", "serial", "activation",
    "registration", "do you want to register",
]
DISMISS_BUTTONS = [
    "remind me later", "remind me", "later", "not now", "no thanks",
    "no", "close", "cancel", "dismiss", "ok",
]

def norm_text(s):
    return s.replace("&", "").strip().lower()

# ---------------- helpers ----------------
def get_window_text(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0: return ""
    b = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, b, n + 1)
    return b.value

def get_class_name(hwnd):
    b = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, b, 256)
    return b.value

def get_pid(hwnd):
    pid = wt.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value

def pid_to_name(pid):
    if not pid: return ""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h: return ""
    try:
        size = wt.DWORD(1024); buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
        return ""
    finally:
        kernel32.CloseHandle(h)

def child_texts(hwnd):
    out = []
    def cb(child, _):
        b = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(child, b, 512)
        t = b.value.strip()
        if t: out.append(t)
        return True
    user32.EnumChildWindows(hwnd, EnumProc(cb), 0)
    return out

def idm_dialog_windows():
    found = []
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd): return True
        if get_class_name(hwnd) != "#32770": return True
        if not get_window_text(hwnd): return True
        if "idman" not in pid_to_name(get_pid(hwnd)).lower(): return True
        found.append(hwnd)
        return True
    user32.EnumWindows(EnumProc(cb), 0)
    return found

def fingerprint(hwnd):
    texts = child_texts(hwnd)
    return {
        "hwnd": hwnd,
        "title": get_window_text(hwnd),
        "class": get_class_name(hwnd),
        "has_owner": bool(user32.GetWindow(hwnd, GW_OWNER)),
        "children": texts,
        "blob": (" ".join([get_window_text(hwnd)] + texts)).lower(),
    }

def classify(win):
    if not win or win["class"] != "#32770": return None
    if any(h in win["blob"] for h in REGISTER_HINTS): return "register"
    if win["has_owner"]: return "nag"
    return "main"

# ---------------- batch close ----------------
def fire_click(hwnd, wanted):
    """Post BM_CLICK to the child whose accelerator-stripped text matches.
    Non-blocking; returns True if a button was found and clicked."""
    target = []
    want = norm_text(wanted)
    def cb(child, _):
        b = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(child, b, 256)
        if norm_text(b.value) == want:
            target.append(child); return False
        return True
    user32.EnumChildWindows(hwnd, EnumProc(cb), 0)
    if target:
        user32.PostMessageW(target[0], BM_CLICK, 0, 0)
        return True
    return False

def fire_first_dismiss(hwnd):
    """Fire one click on the first matching dismiss button. Returns name."""
    for btn in DISMISS_BUTTONS:
        if fire_click(hwnd, btn):
            return btn
    return None

def close_popups_batch(wins, log, aggressive=True, pass_delay=0.15):
    """Parallel escalation across all popups. Returns survivors list."""
    if not wins: return []
    remaining = list(wins)

    # Pass 1 — first matching dismiss button for every popup
    for w in remaining:
        h = w["hwnd"]
        if not user32.IsWindow(h): continue
        btn = fire_first_dismiss(h)
        if btn: log(f"    hwnd={h} click {btn!r}")
    time.sleep(pass_delay)
    remaining = [w for w in remaining if user32.IsWindow(w["hwnd"])]

    # Pass 2 — alternate buttons (e.g. cancel if close was wrong)
    for w in remaining:
        h = w["hwnd"]
        for btn in ("no", "cancel", "ok", "close"):
            if not user32.IsWindow(h): break
            if fire_click(h, btn):
                log(f"    hwnd={h} click {btn!r} (pass 2)")
                break
    time.sleep(pass_delay)
    remaining = [w for w in remaining if user32.IsWindow(w["hwnd"])]

    if aggressive:
        # Pass 3 — WM_COMMAND IDNO (what the "No" button actually sends)
        for w in remaining:
            user32.PostMessageW(w["hwnd"], WM_COMMAND, IDNO, 0)
        time.sleep(pass_delay)
        remaining = [w for w in remaining if user32.IsWindow(w["hwnd"])]

        # Pass 4 — WM_COMMAND IDCANCEL
        for w in remaining:
            user32.PostMessageW(w["hwnd"], WM_COMMAND, IDCANCEL, 0)
        time.sleep(pass_delay)
        remaining = [w for w in remaining if user32.IsWindow(w["hwnd"])]

        # Pass 5 — WM_SYSCOMMAND SC_CLOSE
        for w in remaining:
            user32.PostMessageW(w["hwnd"], WM_SYSCOMMAND, SC_CLOSE, 0)
        time.sleep(pass_delay)
        remaining = [w for w in remaining if user32.IsWindow(w["hwnd"])]

    # Pass 6 — WM_CLOSE
    for w in remaining:
        user32.PostMessageW(w["hwnd"], WM_CLOSE, 0, 0)
    time.sleep(pass_delay * 2)
    remaining = [w for w in remaining if user32.IsWindow(w["hwnd"])]

    return remaining

def kill_idm(log):
    if not shutil.which("taskkill"):
        log("    taskkill not found"); return False
    r = subprocess.run(["taskkill", "/F", "/IM", "IDMan.exe"],
                       capture_output=True, text=True,
                       creationflags=CREATE_NO_WINDOW)
    if r.returncode == 0:
        log("    taskkill: killed IDMan.exe"); return True
    if r.returncode == 128:
        log("    taskkill: no process (already gone)"); return True
    log(f"    taskkill FAILED rc={r.returncode}: "
        f"{(r.stderr or r.stdout).strip()}")
    return False

# ---------------- monitor ----------------
class Monitor(threading.Thread):
    def __init__(self, args, log):
        super().__init__(daemon=True)
        self.args = args; self.log = log
        self._stop = threading.Event()
        self._last_action = 0.0
        self._counts = {"register": 0, "nag": 0, "fail": 0}

    def stop(self): self._stop.set()

    def run(self):
        self.log(f"[*] monitor running (interval={self.args.interval}s, "
                 f"cooldown={self.args.cooldown}s, "
                 f"batch_delay={self.args.batch_delay}s)")
        while not self._stop.is_set():
            try: self._scan_once()
            except Exception as e: self.log(f"[!] monitor error: {e}")
            for _ in range(int(self.args.interval * 10)):
                if self._stop.is_set(): break
                time.sleep(0.1)

    def _collect_targets(self):
        targets = []
        for hwnd in idm_dialog_windows():
            win = fingerprint(hwnd)
            cls = classify(win)
            if self.args.debug:
                self.log(f"[dbg] hwnd={hwnd} class={win['class']!r} "
                         f"owner={win['has_owner']} title={win['title']!r} -> {cls}")
            if cls in ("register", "nag"):
                targets.append((cls, win))
        return targets

    def _scan_once(self):
        now = time.time()
        targets = self._collect_targets()
        if not targets: return
        if now - self._last_action < self.args.cooldown:
            if self.args.debug: self.log("[dbg] cooldown active, skipping")
            return

        self.log(f"[!] {len(targets)} popup(s) found — batch closing")
        for cls, w in targets:
            self.log(f"    {cls.upper()} hwnd={w['hwnd']} title={w['title']!r}")
            if self.args.debug:
                self.log(f"        children={w['children']}")

        if self.args.dry_run:
            self.log("    (dry-run: no action)"); return

        aggressive = self.args.register_action in ("force", "kill")
        survivors = close_popups_batch(
            [w for _, w in targets], self.log,
            aggressive=aggressive, pass_delay=self.args.batch_delay,
        )

        for cls, _ in targets:
            self._counts[cls] += 1
        for w in survivors:
            self.log(f"    ! survived: hwnd={w['hwnd']} title={w['title']!r}")
            self._counts["fail"] += 1

        # ---- tight-poll for siblings that appear during the close ----
        deadline = time.time() + 2.0
        empty_streak = 0
        while time.time() < deadline and not self._stop.is_set():
            time.sleep(0.25)
            more = self._collect_targets()
            if not more:
                empty_streak += 1
                if empty_streak >= 3: break
                continue
            empty_streak = 0
            self.log(f"[!] {len(more)} more popup(s) — batch closing")
            for cls, w in more:
                self.log(f"    {cls.upper()} hwnd={w['hwnd']} title={w['title']!r}")
            close_popups_batch(
                [w for _, w in more], self.log,
                aggressive=aggressive, pass_delay=self.args.batch_delay,
            )
            for cls, _ in more:
                self._counts[cls] += 1

        self._last_action = time.time()

    def stats(self): return dict(self._counts)

# ---------------- interactive ----------------
def interactive(monitor, log, args):
    print()
    print("Commands:")
    print("  close|c|<Enter>  -> close visible IDM popups (batch)")
    print("  status           -> popups + stats")
    print("  kill             -> kill IDMan.exe (explicit only)")
    print("  quit|q           -> exit (or Ctrl+C)")
    print()
    if sys.stdin is None:
        log("[*] no stdin (pythonw); monitor-only mode"); return
    q = queue.Queue()
    def reader():
        while True:
            try: line = sys.stdin.readline()
            except Exception: return
            if not line: return
            q.put(line.strip().lower())
    threading.Thread(target=reader, daemon=True).start()
    try:
        while not monitor._stop.is_set():
            try: cmd = q.get(timeout=0.5)
            except queue.Empty: continue
            if cmd in ("quit", "q", "exit"): break
            elif cmd in ("", "close", "c"):
                targets = monitor._collect_targets()
                if not targets:
                    log("[*] no IDM popups to close")
                    continue
                log(f"[*] manual batch close ({len(targets)} popup(s))")
                close_popups_batch([w for _, w in targets], log,
                                   aggressive=True,
                                   pass_delay=args.batch_delay)
            elif cmd == "kill":
                log("[*] manual kill of IDMan.exe"); kill_idm(log)
            elif cmd == "status":
                targets = monitor._collect_targets()
                if not targets: log("  no IDM popups visible")
                for cls, w in targets:
                    log(f"  {cls} hwnd={w['hwnd']} title={w['title']!r}")
                log(f"  stats={monitor.stats()}")
            else:
                log(f"  unknown: {cmd!r}")
    except KeyboardInterrupt:
        pass

# ---------------- main ----------------
def build_logger(path, echo=True):
    fh = open(path, "a", encoding="utf-8") if path else None
    def _log(msg):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        if echo:
            try: print(line, flush=True)
            except Exception: pass
        if fh: fh.write(line + "\n"); fh.flush()
    return _log, fh

def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Close IDM popups in batch; never kill main IDM.")
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--cooldown", type=float, default=10.0)
    p.add_argument("--batch-delay", type=float, default=0.15,
                   help="seconds between escalation passes (default 0.5; lower=faster)")
    p.add_argument("--register-action", choices=["dismiss","force","kill"],
                   default="force")
    p.add_argument("--nag-action", choices=["dismiss","force"], default="dismiss")
    p.add_argument("--allow-process-kill", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--log-file", default=None)
    p.add_argument("--no-echo", action="store_true")
    args = p.parse_args(argv)
    if args.register_action == "kill" and not args.allow_process_kill:
        p.error("--register-action kill requires --allow-process-kill")
    return args

def main(argv=None):
    if sys.platform != "win32":
        print("Windows only."); return 1
    args = parse_args(argv)
    log, fh = build_logger(args.log_file, echo=not args.no_echo)
    try:
        log("IDM Popup Killer (batch, popup-only)")
        log(f"  register_action={args.register_action}  "
            f"nag_action={args.nag_action}  "
            f"allow_process_kill={args.allow_process_kill}")
        mon = Monitor(args, log); mon.start()
        try:
            interactive(mon, log, args)
            while mon.is_alive(): time.sleep(0.5)
        except KeyboardInterrupt:
            log("\n[*] Ctrl+C")
        finally:
            log("[*] stopping monitor...")
            mon.stop(); mon.join(timeout=3)
            log(f"[*] stats: {mon.stats()}")
            log("[*] bye.")
        return 0
    finally:
        if fh: fh.close()

if __name__ == "__main__":
    sys.exit(main())
