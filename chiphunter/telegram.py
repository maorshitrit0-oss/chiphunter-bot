"""Telegram bot output. Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.

Code spans (`...`) are tap-to-copy in Telegram on iPhone — used for questionnaire answers.
"""
from __future__ import annotations

import html
import json
import os
from pathlib import Path

from .http import post, get

API = "https://api.telegram.org/bot{token}/{method}"


def _call(method, **kw):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    r = post(API.format(token=token, method=method), **kw)
    return r.json()


def enabled() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def send(text: str, buttons: list[list[dict]] | None = None, chat_id=None, silent=False, force_reply: str | None = None,
         reply_to: int | None = None):
    data = {"chat_id": chat_id or os.environ["TELEGRAM_CHAT_ID"], "text": text[:4096], "parse_mode": "HTML",
            "disable_web_page_preview": True, "disable_notification": False}   # Maor wants every message to notify
    if buttons:
        data["reply_markup"] = {"inline_keyboard": buttons}
    if reply_to:
        data["reply_to_message_id"] = reply_to
        data["allow_sending_without_reply"] = True
    if force_reply is not None:   # opens the keyboard as a reply to this message (used to ask for a referrer's name)
        data["reply_markup"] = {"force_reply": True, "input_field_placeholder": force_reply}
    return _call("sendMessage", json=data)


def edit_buttons(message_id: int, rows: list[list[dict]], chat_id=None):
    """Replace the buttons under a message we sent earlier (e.g. mark a job as applied)."""
    return _call("editMessageReplyMarkup", json={"chat_id": chat_id or os.environ["TELEGRAM_CHAT_ID"],
                                                 "message_id": message_id, "reply_markup": {"inline_keyboard": rows}})


def send_document(path: Path, caption: str = "", chat_id=None, buttons=None, silent=False, reply_to=None):
    # curl_cffi has no `files=` support -> plain requests for multipart upload
    import requests
    import time
    for attempt in range(3):       # Telegram sometimes drops the upload connection -> try again, don't lose the job
        try:
            return _send_document_once(requests, path, caption, chat_id, buttons, reply_to)
        except Exception:
            if attempt == 2:
                raise
            time.sleep(3 * (attempt + 1))


def _send_document_once(requests, path, caption, chat_id, buttons, reply_to=None):
    with open(path, "rb") as f:
        r = requests.post(API.format(token=os.environ["TELEGRAM_BOT_TOKEN"], method="sendDocument"),
                          data={"chat_id": chat_id or os.environ["TELEGRAM_CHAT_ID"],
                                "caption": caption[:1024], "parse_mode": "HTML", "disable_notification": False,
                                **({"reply_to_message_id": reply_to, "allow_sending_without_reply": True} if reply_to else {}),
                                **({"reply_markup": json.dumps({"inline_keyboard": buttons})} if buttons else {})},
                          files={"document": (Path(path).name, f, "application/pdf")}, timeout=60)
    r.raise_for_status()
    return r.json()


def download_file(file_id: str, dest: Path) -> Path:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    info = _call("getFile", json={"file_id": file_id})["result"]
    r = get(f"https://api.telegram.org/file/bot{token}/{info['file_path']}")
    dest.write_bytes(r.content)
    return dest


def e(s) -> str:
    return html.escape(str(s or ""))
