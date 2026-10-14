"""Telegram notifications (optional). Silent no-op if TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID unset.
Notifications are one-way: the bot never accepts commands from Telegram."""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger("desk.notify")


class Notifier:
    def __init__(self, token: str | None = None, chat_id: str | None = None):
        self.token = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id if chat_id is not None else os.environ.get("TELEGRAM_CHAT_ID")
        self.sent: list[str] = []      # kept for tests / debugging

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def send(self, text: str):
        self.sent.append(text)
        if not self.enabled:
            return
        try:
            requests.post(self._url("sendMessage"), timeout=15, data={
                "chat_id": self.chat_id, "text": f"🌙 Solunaro\n{text}"[:4000], "disable_web_page_preview": "true"})
        except requests.RequestException as e:
            log.warning("telegram send failed: %s", e)

    def send_document(self, path: str, caption: str = ""):
        self.sent.append(f"[doc] {os.path.basename(path)} {caption}")
        if not self.enabled:
            return
        try:
            with open(path, "rb") as f:
                requests.post(self._url("sendDocument"), timeout=60,
                              data={"chat_id": self.chat_id, "caption": f"🌙 Solunaro · {caption}"[:1000]},
                              files={"document": (os.path.basename(path), f, "text/html")})
        except (requests.RequestException, OSError) as e:
            log.warning("telegram document failed: %s", e)
