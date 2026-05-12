#!/usr/bin/env python3
"""NetworkHealth - check servers, services and internet connection.

Live TUI mode (default when stdout is a tty and -r is not 1):
  q          quit
  space      pause / resume
  r          force refresh now
  +/-        interval +/- 1s
  v          cycle verbosity
"""
import json
import os
import select
import socket
import ssl
import subprocess
import sys
import termios
import time
import tty
import urllib.error
import urllib.request

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"
GRAY = "\033[90m"

ALT_ON = "\033[?1049h"
ALT_OFF = "\033[?1049l"
CURSOR_HIDE = "\033[?25l"
CURSOR_SHOW = "\033[?25h"
CLEAR = "\033[2J"
HOME = "\033[H"
CLR_EOL = "\033[K"

OK = f"{GREEN}●{RESET}"
BAD = f"{RED}●{RESET}"
SKIP = f"{GRAY}○{RESET}"
PEND = f"{YELLOW}◌{RESET}"

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class Cfg:
    verbosity = 1
    interval = 3
    repeat = 1
    sslv = 0
    noerr = False
    log_changes_only = False
    live = None  # autodetected


def parse_args(argv):
    cfgfile = "checkServers.json"
    opts = []
    for a in argv[1:]:
        if a.startswith("-"):
            opts.append(a[1:])
        else:
            cfgfile = a
    apply_opts(opts)
    return cfgfile


def apply_opts(opts):
    for o in opts:
        if o.startswith("sslv"):
            Cfg.sslv = int(o[4:] or 0)
        elif o.startswith("v"):
            Cfg.verbosity = int(o[1:] or 1)
        elif o.startswith("i"):
            Cfg.interval = int(o[1:] or 0)
        elif o.startswith("r"):
            Cfg.repeat = int(o[1:] or 1)
        elif o == "noerr":
            Cfg.noerr = True
        elif o == "lc":
            Cfg.log_changes_only = True
        elif o == "nolive":
            Cfg.live = False
        elif o == "live":
            Cfg.live = True


def apply_cfg_entry(info):
    opts = []
    for item in info:
        if "=" in item:
            k, v = item.split("=", 1)
            opts.append(f"{k}{v}")
    apply_opts(opts)


def check_ping(host):
    try:
        r = subprocess.run(
            ["ping", "-c", "1", "-W", "2", host],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return r.returncode == 0
    except Exception as e:
        if not Cfg.noerr:
            sys.stderr.write(f"{RED}ping error: {e}{RESET}\n")
        return False


def check_tcp(hostport):
    if ":" not in hostport:
        return False
    host, port = hostport.rsplit(":", 1)
    try:
        with socket.create_connection((host, int(port)), timeout=2):
            return True
    except Exception:
        return False


def check_http(url):
    ctx = None
    if url.startswith("https") and not Cfg.sslv:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "NetworkHealth/1.0"})
        with urllib.request.urlopen(req, timeout=4, context=ctx) as resp:
            return 200 <= resp.status < 400
    except urllib.error.HTTPError as e:
        return 200 <= e.code < 400
    except Exception:
        return False


CHECKERS = {
    "ping": (check_ping, CYAN),
    "tcp": (check_tcp, BLUE),
    "http": (check_http, MAGENTA),
}


def label(entry):
    info = entry.get("info") or entry.get("host", "")
    host = entry.get("host", "")
    if info and info != host:
        return f"{BOLD}{info}{RESET} {DIM}{host}{RESET}"
    return f"{BOLD}{host}{RESET}"


def run_entries(entries, depth=0, parent_ok=True, out=None):
    indent = "  " * depth
    results = []
    for entry in entries:
        typ = entry.get("typ", "")
        if typ.startswith("#"):
            continue
        if typ == "rem":
            if Cfg.verbosity > 0:
                out.append(f"{indent}{GRAY}── {entry.get('info','')}{RESET}")
            continue
        if typ == "cfg":
            apply_cfg_entry(entry.get("info", []))
            continue
        if typ not in CHECKERS:
            if Cfg.verbosity > 1:
                out.append(f"{indent}{YELLOW}? unknown typ: {typ}{RESET}")
            continue

        fn, color = CHECKERS[typ]
        host = entry.get("host", "")

        if not parent_ok:
            out.append(f"{indent}{SKIP} {color}{typ:<4}{RESET} {label(entry)} {GRAY}skipped{RESET}")
            results.append(False)
            continue

        t0 = time.monotonic()
        ok = fn(host)
        dt = (time.monotonic() - t0) * 1000
        mark = OK if ok else BAD
        dt_col = GREEN if dt < 100 else (YELLOW if dt < 500 else RED)
        line = f"{indent}{mark} {color}{typ:<4}{RESET} {label(entry)} {dt_col}{dt:5.0f}ms{RESET}"
        if not ok and entry.get("infobad"):
            msg = entry["infobad"].lstrip("+")
            line += f"  {RED}↳ {msg}{RESET}"
        out.append(line)

        results.append(ok)
        sub = entry.get("sub")
        if sub:
            results.extend(run_entries(sub, depth + 1, parent_ok=ok, out=out))
    return results


def term_size():
    try:
        sz = os.get_terminal_size()
        return sz.columns, sz.lines
    except OSError:
        return 80, 24


def render_header(cols, tick, paused, next_in):
    ts = time.strftime("%H:%M:%S")
    spin = SPINNER[tick % len(SPINNER)]
    state = f"{YELLOW}paused{RESET}" if paused else f"{GREEN}{spin} live{RESET}"
    title = f"{BOLD}NetworkHealth{RESET}"
    countdown = "" if paused else f"  next {next_in}s"
    left = f" {title}  {state}  every {Cfg.interval}s{countdown}"
    right = f"{DIM}{ts}{RESET} "
    # strip ANSI for width calc
    plain_left = strip_ansi(left)
    plain_right = strip_ansi(right)
    pad = max(1, cols - len(plain_left) - len(plain_right))
    return left + " " * pad + right


def render_footer(cols, good, total):
    if total == 0:
        bar = ""
    else:
        width = max(10, cols - 30)
        filled = int(width * good / total)
        bar = f"{GREEN}{'█' * filled}{RED}{'█' * (width - filled)}{RESET}"
    color = GREEN if good == total else (YELLOW if good else RED)
    summary = f"{color}{good}/{total} ok{RESET}"
    hint = f"{DIM}q quit · space pause · r refresh · +/- interval{RESET}"
    return f"{summary}  {bar}\n{hint}"


def strip_ansi(s):
    out = []
    i = 0
    while i < len(s):
        if s[i] == "\033":
            while i < len(s) and s[i] not in "mHJK":
                i += 1
            i += 1
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


class RawTty:
    def __init__(self, fd):
        self.fd = fd
        self.saved = None

    def __enter__(self):
        if self.fd is not None and os.isatty(self.fd):
            self.saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        return self

    def __exit__(self, *exc):
        if self.saved is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)

    def read_key(self, timeout):
        if self.fd is None:
            time.sleep(timeout)
            return None
        r, _, _ = select.select([self.fd], [], [], timeout)
        if r:
            try:
                return os.read(self.fd, 1).decode("utf-8", "ignore")
            except OSError:
                return None
        return None


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def run_once(cfg):
    out = []
    results = run_entries(cfg, out=out)
    return results, out


def live_loop(cfg):
    fd = sys.stdin.fileno() if sys.stdin.isatty() else None
    sys.stdout.write(ALT_ON + CURSOR_HIDE)
    sys.stdout.flush()
    paused = False
    tick = 0
    last_run = 0.0
    results, body = [], ["… running first check …"]
    try:
        with RawTty(fd) as tty_in:
            while True:
                now = time.monotonic()
                due = (not paused) and (now - last_run >= Cfg.interval)
                if due or last_run == 0.0:
                    results, body = run_once(cfg)
                    last_run = time.monotonic()

                cols, rows = term_size()
                next_in = max(0, int(Cfg.interval - (time.monotonic() - last_run)))
                buf = [HOME, CLEAR, HOME]
                buf.append(render_header(cols, tick, paused, next_in) + "\n")
                buf.append(f"{DIM}{'─' * cols}{RESET}\n")
                max_body = rows - 5
                shown = body[:max_body]
                for line in shown:
                    buf.append(line + CLR_EOL + "\n")
                if len(body) > max_body:
                    buf.append(f"{DIM}… {len(body) - max_body} more lines hidden{RESET}\n")
                # pad to push footer down
                used = 2 + len(shown) + (1 if len(body) > max_body else 0)
                for _ in range(max(0, rows - used - 2)):
                    buf.append(CLR_EOL + "\n")
                good = sum(1 for r in results if r)
                buf.append(render_footer(cols, good, len(results)))
                sys.stdout.write("".join(buf))
                sys.stdout.flush()

                key = tty_in.read_key(0.1)
                tick += 1
                if key is None:
                    continue
                if key in ("q", "Q", "\x03", "\x04"):
                    break
                if key == " ":
                    paused = not paused
                if key in ("r", "R"):
                    last_run = 0.0
                if key == "+":
                    Cfg.interval = min(3600, Cfg.interval + 1)
                if key == "-":
                    Cfg.interval = max(1, Cfg.interval - 1)
                if key in ("v", "V"):
                    Cfg.verbosity = (Cfg.verbosity + 1) % 3
    finally:
        sys.stdout.write(CURSOR_SHOW + ALT_OFF)
        sys.stdout.flush()


def static_run(cfg):
    n = 0
    while True:
        n += 1
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"{DIM}{'─' * 60}{RESET}")
        print(f"{BOLD}NetworkHealth{RESET}  {DIM}{ts}{RESET}")
        print(f"{DIM}{'─' * 60}{RESET}")
        results, body = run_once(cfg)
        for line in body:
            print(line)
        good = sum(1 for r in results if r)
        total = len(results)
        color = GREEN if good == total else (YELLOW if good else RED)
        print(f"{DIM}────{RESET} {color}{good}/{total} ok{RESET}")
        if Cfg.repeat >= 0 and n >= Cfg.repeat:
            break
        if Cfg.interval > 0:
            time.sleep(Cfg.interval)
        else:
            break


def main():
    cfgfile = parse_args(sys.argv)
    try:
        cfg = load_config(cfgfile)
    except FileNotFoundError:
        sys.stderr.write(f"{RED}config file not found: {cfgfile}{RESET}\n")
        sys.exit(2)
    except json.JSONDecodeError as e:
        sys.stderr.write(f"{RED}invalid JSON in {cfgfile}: {e}{RESET}\n")
        sys.exit(2)

    if Cfg.live is None:
        Cfg.live = sys.stdout.isatty() and Cfg.repeat != 1

    if Cfg.live:
        live_loop(cfg)
    else:
        static_run(cfg)


if __name__ == "__main__":
    main()
