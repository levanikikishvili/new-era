"""Telegram Real-Time Remote Control & Trade Alerts Module (`newera_api/telegram_bot.py`).

Reads `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` from `.env`.
Features:
- Real-time notifications on Trade Opened, Dynamic Position Management
  (Break-Even @ +1.0x ATR, 50% Partial Close @ +2.0x ATR, Trailing Stop), and Trade Closed.
- Optional chart/trade snapshot image attachment (`sendPhoto`) or rich HTML message (`sendMessage`).
- Remote command polling (`getUpdates`) from the user's phone:
  * `/status`    — Live MT5 Demo Account, Open Positions, Waterfall Gate & Gemini CIO ("ჯოი") status
  * `/close_all` — Emergency close of all open Demo positions
  * `/pause`     — Pause Autonomous Demo Execution & Auto-Scan
  * `/resume`    — Resume Autonomous Demo Execution
  * `/analyze`   — Request Georgian setup analysis from Gemini Quant Co-Pilot ("ჯოი")
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import ROOT, load_workspace_dotenv


class TelegramRemoteController:
    """Bi-directional Telegram Bot for trade alerts and remote phone commands."""

    def __init__(self, root: Path = ROOT) -> None:
        self.root = root
        self._last_update_id: int = 0
        self._polling_task: asyncio.Task[Any] | None = None
        self._running: bool = False
        self._command_handler: Callable[[str], Awaitable[str]] | None = None
        self._recent_notifications: list[dict[str, Any]] = []

    def _credentials(self) -> tuple[str, str]:
        load_workspace_dotenv(self.root)
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        return token, chat_id

    @property
    def is_configured(self) -> bool:
        token, chat_id = self._credentials()
        return bool(
            token
            and chat_id
            and not token.startswith("your_")
            and not chat_id.startswith("your_")
        )

    def status_summary(self) -> dict[str, Any]:
        token, chat_id = self._credentials()
        configured = self.is_configured
        return {
            "configured": configured,
            "polling_active": bool(self._running and configured),
            "chat_id_masked": f"...{chat_id[-4:]}" if len(chat_id) >= 4 else ("set" if chat_id else "not_set"),
            "commands": ["/status", "/close_all", "/pause", "/resume", "/analyze"],
            "recent_notifications": self._recent_notifications[-10:],
        }

    def set_command_handler(self, handler: Callable[[str], Awaitable[str]]) -> None:
        self._command_handler = handler

    def _record_notification(self, kind: str, text: str, delivered: bool) -> None:
        self._recent_notifications.append(
            {
                "kind": kind,
                "text": text,
                "delivered": delivered,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )
        if len(self._recent_notifications) > 50:
            self._recent_notifications = self._recent_notifications[-50:]

    def _send_telegram_http(
        self,
        text: str,
        *,
        chat_id_override: str | None = None,
        parse_mode: str = "HTML",
    ) -> bool:
        token, default_chat_id = self._credentials()
        target_chat = (chat_id_override or default_chat_id).strip()
        if not token or not target_chat or token.startswith("your_"):
            return False

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": target_chat,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=8.0) as resp:
                res = json.loads(resp.read().decode("utf-8", errors="replace"))
            return bool(res.get("ok"))
        except Exception:
            return False

    def _send_telegram_photo_http(
        self,
        photo_path: Path,
        caption: str,
        *,
        chat_id_override: str | None = None,
    ) -> bool:
        token, default_chat_id = self._credentials()
        target_chat = (chat_id_override or default_chat_id).strip()
        if not token or not target_chat or not photo_path.exists():
            return False

        boundary = f"----NewEraBoundary{int(time.time() * 1000)}"
        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        try:
            img_bytes = photo_path.read_bytes()
            parts: list[bytes] = []
            for field_name, field_val in (("chat_id", target_chat), ("caption", caption[:1000]), ("parse_mode", "HTML")):
                parts.append(f"--{boundary}\r\n".encode("utf-8"))
                parts.append(f'Content-Disposition: form-data; name="{field_name}"\r\n\r\n'.encode("utf-8"))
                parts.append(f"{field_val}\r\n".encode("utf-8"))

            parts.append(f"--{boundary}\r\n".encode("utf-8"))
            parts.append(
                f'Content-Disposition: form-data; name="photo"; filename="{photo_path.name}"\r\n'
                f"Content-Type: image/png\r\n\r\n".encode("utf-8")
            )
            parts.append(img_bytes)
            parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
            body = b"".join(parts)

            req = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=12.0) as resp:
                res = json.loads(resp.read().decode("utf-8", errors="replace"))
            return bool(res.get("ok"))
        except Exception:
            return False

    async def send_message(self, text: str, *, kind: str = "info", photo_path: Path | None = None) -> bool:
        delivered = False
        if self.is_configured:
            if photo_path is not None and photo_path.exists():
                delivered = await asyncio.to_thread(self._send_telegram_photo_http, photo_path, text)
            if not delivered:
                delivered = await asyncio.to_thread(self._send_telegram_http, text)
        self._record_notification(kind, text, delivered)
        return delivered

    async def notify_trade_opened(
        self,
        position: dict[str, Any],
        *,
        execution_source: str = "mt5_demo",
        waterfall: dict[str, Any] | None = None,
        screenshot_path: Path | None = None,
    ) -> bool:
        sym = position.get("symbol", "XAUUSD")
        side = position.get("side", "BUY")
        vol = position.get("volume", 0.01)
        open_px = position.get("price_open", 0.0)
        sl = position.get("sl", 0.0)
        tp = position.get("tp", 0.0)
        risk_usd = position.get("risk_usd", 0.0)
        reward_usd = position.get("reward_usd", 0.0)
        ticket = position.get("ticket", 0)
        wf = waterfall or {}
        prob = wf.get("win_probability_pct", "--")
        tier = wf.get("risk_tier", "DEMO")

        icon = "🟢" if side == "BUY" else "🔴"
        msg = (
            f"{icon} <b>NEWERA QUANT DEMO TRADE OPENED</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"• <b>Symbol:</b> <code>{sym}</code> ({side})\n"
            f"• <b>Ticket:</b> <code>#{ticket}</code> [{execution_source}]\n"
            f"• <b>Volume:</b> <code>{vol} Lot</code> ({tier})\n"
            f"• <b>Entry Price:</b> <code>{open_px}</code>\n"
            f"• <b>Stop Loss:</b> <code>{sl}</code> (-${risk_usd})\n"
            f"• <b>Take Profit:</b> <code>{tp}</code> (+${reward_usd})\n"
            f"• <b>Joey CIO Win-Prob:</b> <code>{prob}%</code>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📱 Commands: /status | /close_all | /pause"
        )
        return await self.send_message(msg, kind="trade.opened", photo_path=screenshot_path)

    async def notify_position_managed(self, action_Info: dict[str, Any]) -> bool:
        ticket = action_Info.get("ticket", 0)
        sym = action_Info.get("symbol", "XAUUSD")
        event_kind = action_Info.get("action_type", "DYNAMIC_SL")
        desc = action_Info.get("description", "")
        new_sl = action_Info.get("new_sl", 0.0)
        new_vol = action_Info.get("new_volume", 0.0)
        msg = (
            f"🛡️ <b>DYNAMIC POSITION MANAGER [{event_kind}]</b>\n"
            f"• <b>Symbol:</b> <code>{sym}</code> (#{ticket})\n"
            f"• <b>Update:</b> {desc}\n"
            f"• <b>New SL:</b> <code>{new_sl}</code> | <b>Volume:</b> <code>{new_vol} Lot</code>"
        )
        return await self.send_message(msg, kind="trade.managed")

    async def notify_trade_closed(self, payload: dict[str, Any]) -> bool:
        sym = payload.get("symbol", "ALL")
        cnt = payload.get("closed_count", 0)
        tickets = payload.get("closed_tickets", [])
        msg = (
            f"🏁 <b>DEMO POSITION(S) CLOSED</b>\n"
            f"• <b>Symbol:</b> <code>{sym}</code>\n"
            f"• <b>Closed Count:</b> <code>{cnt}</code>\n"
            f"• <b>Tickets:</b> <code>{tickets}</code>"
        )
        return await self.send_message(msg, kind="trade.closed")

    async def handle_command_text(self, raw_text: str) -> str:
        """Process a command like `/status`, `/close_all`, `/pause`, `/resume`, or `/analyze`."""
        cmd = raw_text.strip().split()[0].lower() if raw_text.strip() else "/status"
        if "@" in cmd:
            cmd = cmd.split("@", 1)[0]
        if self._command_handler is not None:
            return await self._command_handler(cmd)
        return f"Command handler not attached for {cmd}"

    async def start_polling(self) -> None:
        if self._running:
            return
        self._running = True
        self._polling_task = asyncio.create_task(self._poll_loop())

    async def stop_polling(self) -> None:
        self._running = False
        if self._polling_task is not None:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except (asyncio.CancelledError, Exception):
                pass
            self._polling_task = None

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                if not self.is_configured:
                    await asyncio.sleep(5.0)
                    continue
                updates = await asyncio.to_thread(self._fetch_updates_sync)
                for upd in updates:
                    upd_id = int(upd.get("update_id") or 0)
                    if upd_id >= self._last_update_id:
                        self._last_update_id = upd_id + 1
                    msg_obj = upd.get("message") or {}
                    text = str(msg_obj.get("text") or "").strip()
                    sender_chat = str((msg_obj.get("chat") or {}).get("id") or "")
                    if not text.startswith("/"):
                        continue
                    _, allowed_chat = self._credentials()
                    if allowed_chat and sender_chat and sender_chat != allowed_chat:
                        continue
                    reply = await self.handle_command_text(text)
                    if reply:
                        await asyncio.to_thread(
                            self._send_telegram_http,
                            reply,
                            chat_id_override=sender_chat or allowed_chat,
                        )
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(4.0)
            await asyncio.sleep(2.0)

    def _fetch_updates_sync(self) -> list[dict[str, Any]]:
        token, _ = self._credentials()
        if not token or token.startswith("your_"):
            return []
        url = f"https://api.telegram.org/bot{token}/getUpdates?offset={self._last_update_id}&timeout=1"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            if data.get("ok") and isinstance(data.get("result"), list):
                return data["result"]
        except Exception:
            pass
        return []
