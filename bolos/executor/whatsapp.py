"""WhatsApp Desktop: open a chat with the message pre-filled, then press Enter to send."""

import os
import time
from urllib.parse import quote

from pywinauto import Desktop
from pywinauto.keyboard import send_keys

# Time for WhatsApp to open the chat and place the draft in the message box.
DRAFT_SETTLE_SECONDS = 2.5


def send_url(phone: str, text: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    return f"whatsapp://send?phone={digits}&text={quote(text)}"


def send(phone: str, text: str) -> None:
    os.startfile(send_url(phone, text))
    window = Desktop(backend="uia").window(title_re=r"^WhatsApp.*")
    window.wait("exists visible", timeout=15)
    window.set_focus()
    time.sleep(DRAFT_SETTLE_SECONDS)
    send_keys("{ENTER}")
