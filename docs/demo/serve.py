"""Serve the Bol OS demo forms on 127.0.0.1 (never on the network).

    uv run python docs/demo/serve.py            # http://127.0.0.1:8765/kyc_bank.html
    uv run python docs/demo/serve.py --port 0   # any free port

The pages are a fictional bank's re-KYC form (English and Hindi) and a fake "KYC update" scam page, for testing
the form assistant by hand and from tests (tests/test_forms_fields.py starts it with `start()`). A submitted form
is answered with a short "received" page and its contents are discarded unread: nothing typed into a demo page is
stored or logged, even fake values.
"""

import argparse
import html
import secrets
import shutil
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PAGES = ("form_smoke.html", "kyc_bank.html", "kyc_hindi.html", "fake_kyc_scam.html")
MAX_BODY = 1 << 20
CHUNK = 64 * 1024

# The "submitted" state of each page. Nothing typed is echoed back; the reference number is random.
_RECEIVED = {
    "en": ("Submitted - Demo Grameen Bank", "Re-KYC request received",
           "Thank you. Your KYC update request has been received and will be processed in 2 working days. "
           "Reference number: {ref}. This is a demo page: nothing you entered was stored.", "Back to the form"),
    "hi": ("जमा हो गया - डेमो ग्रामीण बैंक", "री-केवाईसी अनुरोध मिल गया",
           "धन्यवाद। आपका केवाईसी अपडेट अनुरोध मिल गया है और 2 कार्य-दिवस में पूरा होगा। संदर्भ संख्या: {ref}। "
           "यह डेमो पेज है: आपकी भरी कोई जानकारी सेव नहीं हुई।", "फॉर्म पर वापस जाएँ"),
    "scam": ("This was a fake page - Bol OS demo", "This was a scam page (demo)",
             "A real bank never asks for your card number, CVV, ATM PIN, password or OTP on a web page. If you "
             "ever typed these into such a page, call your bank to block the card and report it at 1930 (national "
             "cyber crime helpline) or cybercrime.gov.in at once. Nothing you entered here was stored.",
             "Back to the page"),
}
_PAGE_LANG = {"kyc_hindi.html": "hi", "fake_kyc_scam.html": "scam"}
_RECEIVED_HTML = """<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="form.css"></head><body><main><h1>{heading}</h1>
<p class="lead" role="status">{text}</p>
<p><a href="{back}">{back_text}</a></p></main></body></html>"""


def received_page(back: str) -> str:
    """The confirmation page shown after submitting `back` (a path from PAGES, with a leading "/")."""
    kind = _PAGE_LANG.get(back.lstrip("/"), "en")
    title, heading, text, back_text = _RECEIVED[kind]
    ref = "DGB-RK-%06d" % secrets.randbelow(10 ** 6)
    return _RECEIVED_HTML.format(lang="hi" if kind == "hi" else "en", title=title, heading=heading,
                                 text=text.format(ref=ref), back=html.escape(back, quote=True), back_text=back_text)


class DemoHandler(SimpleHTTPRequestHandler):
    """Static files from this folder; POSTs are drained and thrown away."""

    def do_POST(self):  # noqa: N802 - http.server naming
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.send_error(400)
            return
        if not 0 <= length <= MAX_BODY:
            self.send_error(413)
            return
        # Drain the (size-checked) body before any reply, never parse or keep it. Closing a Windows socket with
        # unread data resets the connection, so an unread 404 reached the client as ConnectionAbortedError.
        self.rfile.read(length)
        if self.path != "/submit":
            self.send_error(404)
            return
        back = urlsplit(self.headers.get("Referer") or "/").path.lstrip("/")
        back = "/" + back if back in PAGES else "/form_smoke.html"
        body = received_page(back).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass  # the test browser was closed mid-request; nothing to report

    def copyfile(self, source, outputfile):
        # The base class reads in 1 MiB chunks on Windows; under memory pressure (model training running on the
        # same PC) that allocation itself failed with MemoryError. The pages are a few KB.
        shutil.copyfileobj(source, outputfile, CHUNK)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")  # edits to the pages show up on reload
        super().end_headers()

    def log_message(self, format, *args):  # noqa: A002 - signature from http.server
        pass  # quiet: request lines would only clutter test output


def start(port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    """Serve in a daemon thread. Returns (server, base URL ending in "/"); call server.shutdown() to stop."""
    server = ThreadingHTTPServer((HOST, port), partial(DemoHandler, directory=str(HERE)))
    threading.Thread(target=server.serve_forever, name="demo-forms", daemon=True).start()
    return server, f"http://{HOST}:{server.server_address[1]}/"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    port = parser.parse_args().port
    server = ThreadingHTTPServer((HOST, port), partial(DemoHandler, directory=str(HERE)))
    base = f"http://{HOST}:{server.server_address[1]}/"
    for page in PAGES:
        print(base + page)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
