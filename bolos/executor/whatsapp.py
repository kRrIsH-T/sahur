"""Hand a message draft to WhatsApp Desktop for the user to review and send."""

import os
from urllib.parse import quote


def send_url(phone: str, text: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    return f"whatsapp://send?phone={digits}&text={quote(text, safe='')}"


def open_draft(phone: str, text: str) -> None:
    """Ask WhatsApp to open the draft without acting on the current chat or keyboard focus."""
    os.startfile(send_url(phone, text))
