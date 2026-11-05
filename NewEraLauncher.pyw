"""New Era — one-click desktop launcher (no PowerShell, no console windows).

Double-clicking the Desktop shortcut runs this file with `.venv-api\\Scripts\\pythonw.exe` and:
1. Prevents a second copy of the app from opening (single-instance lock).
2. Shows a splash screen with live startup status.
3. Starts MetaTrader 5 if it is not already running.
4. Starts the FastAPI + MT5 backend (uvicorn) hidden in the background, or reuses a healthy one.
   The backend auto-starts the 4-Step Waterfall screener AND the unified 7-bot fleet worker.
5. Opens the New Era terminal window.
6. When the window is closed, stops the backend it started (so no bots keep trading invisibly).

Logs: TradingAgentsData/runtime/launcher.log and TradingAgentsData/runtime/api_server.log
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UI_DIR = ROOT / "ui"
for _p in (str(ROOT), str(UI_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.chdir(ROOT)

API_HOST = "127.0.0.1"
API_PORT = 8787
API_URL = f"http://{API_HOST}:{API_PORT}"
WS_URL = f"ws://{API_HOST}:{API_PORT}/ws/logs"
HEALTH_URL = f"{API_URL}/health"
RUNTIME_DIR = ROOT / "TradingAgentsData" / "runtime"
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
LAUNCH_LOG = RUNTIME_DIR / "launcher.log"
API_LOG = RUNTIME_DIR / "api_server.log"
ICON_PATH = ROOT / "assets" / "newera.ico"
SPLASH_PNG = ROOT / "assets" / "newera_icon_256.png"
MT5_TERMINAL_CANDIDATES = [
    Path(r"C:\Program Files\MetaTrader 5\terminal64.exe"),
    Path(r"C:\Program Files\MetaTrader 5 IC Markets\terminal64.exe"),
    Path(r"C:\Program Files\IC Markets - MetaTrader 5\terminal64.exe"),
]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        with LAUNCH_LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def api_healthy() -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=2.5) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        return isinstance(data, dict) and data.get("status") == "ok" and "gemini_key_configured" in data
    except Exception:
        return False


def pids_listening_on_port(port: int) -> list[int]:
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True, creationflags=NO_WINDOW, timeout=8
        ).stdout
    except Exception:
        return []
    pids: set[int] = set()
    for ln in out.splitlines():
        parts = ln.split()
        if len(parts) >= 5 and parts[3].upper() == "LISTENING" and parts[1].endswith(f":{port}"):
            try:
                pids.add(int(parts[4]))
            except ValueError:
                pass
    return [p for p in pids if p > 0]


def process_running(image_name: str) -> bool:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/NH"],
            capture_output=True, text=True, creationflags=NO_WINDOW, timeout=8,
        ).stdout
        return image_name.lower() in out.lower()
    except Exception:
        return False


def ensure_mt5_running() -> str:
    if process_running("terminal64.exe"):
        return "MetaTrader 5 უკვე ჩართულია ✔"
    for cand in MT5_TERMINAL_CANDIDATES:
        if cand.is_file():
            try:
                subprocess.Popen([str(cand)], cwd=str(cand.parent), close_fds=True)
                log(f"Started MT5 terminal: {cand}")
                return "MetaTrader 5 ირთვება..."
            except OSError as exc:
                log(f"Failed to start MT5 {cand}: {exc}")
    return "MetaTrader 5 ვერ ვიპოვე — ჩართე ხელით (API მაინც ჩაირთვება)"


def backend_python() -> str:
    """Console-less python for uvicorn (pythonw) from the project venv."""
    venv = ROOT / ".venv-api" / "Scripts"
    for name in ("pythonw.exe", "python.exe"):
        cand = venv / name
        if cand.is_file():
            return str(cand)
    return sys.executable


def start_backend() -> subprocess.Popen | None:
    for pid in pids_listening_on_port(API_PORT):
        log(f"Stopping outdated/unhealthy process on port {API_PORT}: PID {pid}")
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, creationflags=NO_WINDOW)
    time.sleep(0.6)
    log_fh = API_LOG.open("a", encoding="utf-8")
    log_fh.write(f"\n===== New Era API start {datetime.now().isoformat()} =====\n")
    log_fh.flush()
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["NEWERA_API_PORT"] = str(API_PORT)
    proc = subprocess.Popen(
        [backend_python(), "-m", "uvicorn", "main_api:app", "--host", API_HOST, "--port", str(API_PORT)],
        cwd=str(ROOT),
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env=env,
        creationflags=NO_WINDOW,
    )
    log(f"Started backend PID {proc.pid}")
    return proc


def stop_backend(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    log(f"Stopping backend PID {proc.pid}")
    try:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
    except Exception:
        proc.kill()


def main() -> int:
    from PyQt6.QtCore import QLockFile, Qt, QTimer
    from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
    from PyQt6.QtWidgets import QApplication, QMessageBox, QSplashScreen

    app = QApplication(sys.argv)
    app.setApplicationName("New Era AI Quant Platform")
    if ICON_PATH.is_file():
        app.setWindowIcon(QIcon(str(ICON_PATH)))

    lock = QLockFile(str(RUNTIME_DIR / "newera_desktop.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        QMessageBox.information(None, "New Era", "New Era უკვე გახსნილია — შეამოწმე Taskbar.")
        return 0

    # Splash screen
    pix = QPixmap(560, 320)
    pix.fill(QColor("#0b1220"))
    painter = QPainter(pix)
    if SPLASH_PNG.is_file():
        logo = QPixmap(str(SPLASH_PNG)).scaled(
            150, 150, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        painter.drawPixmap(30, 60, logo)
    painter.setPen(QColor("#38bdf8"))
    painter.setFont(QFont("Segoe UI", 24, QFont.Weight.Bold))
    painter.drawText(205, 120, "NEW ERA 2.0")
    painter.setPen(QColor("#94a3b8"))
    painter.setFont(QFont("Segoe UI", 10))
    painter.drawText(207, 148, "AI Trading Terminal • 7-Bot Fleet • DEMO")
    painter.setPen(QColor("#1e293b"))
    painter.drawRect(0, 0, 559, 319)
    painter.end()
    splash = QSplashScreen(pix, Qt.WindowType.WindowStaysOnTopHint)
    splash.show()

    def status(msg: str) -> None:
        log(msg)
        splash.showMessage(
            "  " + msg, Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignLeft, QColor("#10b981")
        )
        app.processEvents()

    state: dict = {"proc": None, "started_at": time.time(), "window": None}

    status(ensure_mt5_running())

    if api_healthy():
        status("Backend უკვე მუშაობს ✔ — ვხსნი ტერმინალს...")
    else:
        status("ვრთავ FastAPI + MT5 Backend-ს და 7-ბოტიან ფლოტს...")
        try:
            state["proc"] = start_backend()
        except Exception as exc:
            log("Backend start failed:\n" + traceback.format_exc())
            QMessageBox.critical(None, "New Era", f"Backend ვერ ჩაირთო:\n{exc}\n\nლოგი: {API_LOG}")
            return 1

    def open_main_window() -> None:
        poll_timer.stop()
        try:
            status("ვხსნი New Era ტერმინალს...")
            import desktop_app  # noqa: WPS433 (imported late so the splash appears instantly)

            state["window"] = desktop_app.create_main_window(app, API_URL, WS_URL)
            splash.finish(state["window"])
            log("Main window opened")
        except Exception as exc:
            log("UI failed:\n" + traceback.format_exc())
            splash.close()
            QMessageBox.critical(None, "New Era", f"ტერმინალი ვერ გაიხსნა:\n{exc}\n\nლოგი: {LAUNCH_LOG}")
            stop_backend(state["proc"])
            app.quit()

    def poll_backend() -> None:
        elapsed = time.time() - state["started_at"]
        proc = state["proc"]
        if proc is not None and proc.poll() is not None:
            poll_timer.stop()
            splash.close()
            QMessageBox.critical(
                None, "New Era", f"Backend გაითიშა (code {proc.returncode}).\n\nნახე ლოგი:\n{API_LOG}"
            )
            app.quit()
            return
        if api_healthy():
            status("Backend ონლაინ ✔ (Waterfall Screener + 7 Bots Fleet)")
            open_main_window()
            return
        if elapsed > 60:
            status("Backend ნელა იტვირთება — ვხსნი ტერმინალს (თავისით დაუკავშირდება)...")
            open_main_window()
            return
        status(f"Backend იტვირთება... {int(elapsed)}წმ")

    poll_timer = QTimer()
    poll_timer.setInterval(700)
    poll_timer.timeout.connect(poll_backend)
    poll_timer.start()

    app.aboutToQuit.connect(lambda: stop_backend(state["proc"]))
    code = app.exec()
    stop_backend(state["proc"])
    lock.unlock()
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log("Fatal launcher error:\n" + traceback.format_exc())
        raise
