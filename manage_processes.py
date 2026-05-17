"""Start/stop all monitor processes from one command.

Usage:
    python manage_processes.py start
    python manage_processes.py stop
    python manage_processes.py restart
    python manage_processes.py status
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import webbrowser
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
PID_FILE = BASE_DIR / ".monitor_processes.json"
WEB_URL = "http://127.0.0.1:8000"
PROGRAMS = [
    ("worker", "worker.py"),
    ("market_realtime", "market_realtime.py"),
    ("web", "web.py"),
    ("auto_trader", "auto_trader.py"),
]


def _load_state() -> dict:
    if not PID_FILE.exists():
        return {}
    try:
        return json.loads(PID_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict):
    PID_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _pid_running(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _start_one(name: str, script: str) -> dict:
    path = BASE_DIR / script
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = open(str(log_dir / f"{name}.log"), "a")
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_CONSOLE

    proc = subprocess.Popen(
        [sys.executable, str(path)],
        cwd=str(BASE_DIR),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )
    return {
        "pid": proc.pid,
        "script": script,
        "log": str(log_dir / f"{name}.log"),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def start(open_browser: bool = True):
    state = _load_state()
    changed = False

    for name, script in PROGRAMS:
        old = state.get(name) or {}
        if _pid_running(old.get("pid")):
            print(f"{name}: already running pid={old['pid']}")
            continue
        info = _start_one(name, script)
        state[name] = info
        changed = True
        print(f"{name}: started pid={info['pid']}")
        time.sleep(0.8)

    if changed:
        _save_state(state)

    if open_browser:
        webbrowser.open(WEB_URL)
        print(f"browser: opened {WEB_URL}")


def _stop_pid(pid: int) -> bool:
    if not _pid_running(pid):
        return False
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        os.kill(pid, signal.SIGTERM)
    return True


def stop():
    state = _load_state()
    if not state:
        print("no saved processes")
        return

    for name, _script in reversed(PROGRAMS):
        info = state.get(name) or {}
        pid = info.get("pid")
        if not pid:
            print(f"{name}: no pid")
            continue
        if _stop_pid(int(pid)):
            print(f"{name}: stopped pid={pid}")
        else:
            print(f"{name}: not running pid={pid}")

    if PID_FILE.exists():
        PID_FILE.unlink()


def status():
    state = _load_state()
    if not state:
        print("no saved processes")
        return
    for name, script in PROGRAMS:
        info = state.get(name) or {}
        pid = info.get("pid")
        running = _pid_running(pid)
        print(f"{name}: {'running' if running else 'stopped'} pid={pid} script={script}")


def restart(open_browser: bool = True):
    """强制杀所有进程，等 3 分钟让端口释放，重启"""
    state = _load_state()
    for name, _script in reversed(PROGRAMS):
        info = state.get(name) or {}
        pid = info.get("pid")
        if pid and _pid_running(int(pid)):
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                )
            else:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except OSError:
                    pass
            print(f"{name}: killed pid={pid}")
    if PID_FILE.exists():
        PID_FILE.unlink()
    # 清理浏览器僵尸进程（Chromium/Chrome 残留）
    zombie_names = ("chrome", "chromium", "chromedriver", "playwright")
    for name in zombie_names:
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/IM", f"{name}.exe", "/T"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                )
            else:
                subprocess.run(
                    ["pkill", "-9", "-f", name],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                )
        except Exception:
            pass
    print("等待 3 分钟后重启...")
    time.sleep(180)
    start(open_browser=open_browser)


def main():
    parser = argparse.ArgumentParser(description="Manage Binance monitor processes.")
    parser.add_argument(
        "command",
        choices=("start", "stop", "restart", "status"),
        help="Action to perform.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open the web page after start/restart.",
    )
    args = parser.parse_args()

    if args.command == "start":
        start(open_browser=not args.no_browser)
    elif args.command == "stop":
        stop()
    elif args.command == "restart":
        restart(open_browser=not args.no_browser)
    elif args.command == "status":
        status()


if __name__ == "__main__":
    main()
