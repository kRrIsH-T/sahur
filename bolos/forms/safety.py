"""Is this page safe to fill? Address checks against official domains, lookalikes and risky fields.

This never certifies a page as safe: an official domain only means the address belongs to the organisation.
Warnings carry only the host and organisation names, never field values.
"""

import ipaddress
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from bolos.forms import kinds

# Official domains (exact or any subdomain) -> who they belong to. RBI reserves *.bank.in for banks.
OFFICIAL = {
    "gov.in": "the Government of India", "nic.in": "the Government of India", "mygov.in": "the Government of India",
    "uidai.gov.in": "UIDAI (Aadhaar)", "incometax.gov.in": "the Income Tax Department",
    "digilocker.gov.in": "DigiLocker", "epfindia.gov.in": "EPFO", "passportindia.gov.in": "Passport Seva",
    "bank.in": "a bank", "fin.in": "a financial company",
    "sbi": "SBI", "onlinesbi.sbi": "SBI", "sbi.co.in": "SBI", "onlinesbi.com": "SBI",
    "hdfcbank.com": "HDFC Bank", "icicibank.com": "ICICI Bank", "axisbank.com": "Axis Bank",
    "kotak.com": "Kotak Mahindra Bank", "pnbindia.in": "Punjab National Bank", "netpnb.com": "Punjab National Bank",
    "bankofbaroda.in": "Bank of Baroda", "bobibanking.com": "Bank of Baroda", "canarabank.com": "Canara Bank",
    "unionbankofindia.co.in": "Union Bank of India", "bankofindia.co.in": "Bank of India",
    "idbibank.in": "IDBI Bank", "yesbank.in": "Yes Bank", "indusind.com": "IndusInd Bank",
    "indianbank.in": "Indian Bank", "iob.in": "Indian Overseas Bank", "centralbankofindia.co.in": "Central Bank",
    "ucobank.com": "UCO Bank", "federalbank.co.in": "Federal Bank", "rbi.org.in": "the Reserve Bank of India",
    "npci.org.in": "NPCI (UPI)", "bhimupi.org.in": "BHIM UPI", "paytm.com": "Paytm", "phonepe.com": "PhonePe",
    "pay.google.com": "Google Pay", "irctc.co.in": "IRCTC", "licindia.in": "LIC", "indiapost.gov.in": "India Post",
}
# Names a scam page borrows. Short names must be a whole word or the start/end of a word in the host.
_BRANDS = {
    "sbi": "SBI", "uidai": "UIDAI (Aadhaar)", "aadhaar": "UIDAI (Aadhaar)", "aadhar": "UIDAI (Aadhaar)",
    "incometax": "the Income Tax Department", "hdfc": "HDFC Bank", "icici": "ICICI Bank", "axisbank": "Axis Bank",
    "kotak": "Kotak Mahindra Bank", "pnb": "Punjab National Bank", "baroda": "Bank of Baroda",
    "canara": "Canara Bank", "unionbank": "Union Bank of India", "idbi": "IDBI Bank", "yesbank": "Yes Bank",
    "indusind": "IndusInd Bank", "paytm": "Paytm", "phonepe": "PhonePe", "gpay": "Google Pay",
    "googlepay": "Google Pay", "npci": "NPCI (UPI)", "upi": "UPI", "bhim": "BHIM UPI", "rbi": "the Reserve Bank "
    "of India", "irctc": "IRCTC", "epfo": "EPFO", "digilocker": "DigiLocker", "kyc": "a bank",
    "rekyc": "a bank", "ekyc": "a bank", "netbanking": "a bank", "gov": "the Government of India",
    "govt": "the Government of India", "sarkari": "the Government of India", "pmkisan": "the Government of India",
    "licindia": "LIC", "indiapost": "India Post",
}
_SHORT = 4
# Page text that claims to be a bank or the government.
_CLAIMS = (
    (r"\bsbi\b|state bank", "SBI"), (r"aadhaa?r|uidai|आधार", "UIDAI (Aadhaar)"),
    (r"income tax|आयकर", "the Income Tax Department"), (r"\bhdfc\b", "HDFC Bank"), (r"\bicici\b", "ICICI Bank"),
    (r"\bpnb\b|punjab national", "Punjab National Bank"), (r"\bkyc\b|re-kyc|केवाईसी", "a bank"),
    (r"\bbank\b|बैंक|ಬ್ಯಾಂಕ್|வங்கி|బ్యాంక్|बँक|ബാങ്ക്|ব্যাংক|બેંક|ਬੈਂਕ", "a bank"),
    (r"government|govt|सरकार|ಸರ್ಕಾರ|அரசு|ప్రభుత్వ|शासन|സർക്കാർ|সরকার|સરકાર|ਸਰਕਾਰ", "the Government of India"),
)
_LOCAL = {"localhost", "127.0.0.1", "::1"}


@dataclass(frozen=True)
class SiteWarning:
    """One finding. `key` names a spoken prompt (site_*); `detail` holds only host/org/field labels."""

    key: str
    detail: dict = field(default_factory=dict)


Warning = SiteWarning  # the name the forms design uses


def host_of(url: str | None) -> str | None:
    """The lower-case host of a URL. Chromium's address bar can drop 'https://', so a bare host is accepted."""
    if not url:
        return None
    text = url.strip()
    if not re.match(r"^[a-z][a-z0-9+.-]*://", text, re.I):
        text = "//" + text
    try:
        host = urlsplit(text).hostname
    except ValueError:
        return None
    return host.rstrip(".").lower() if host else None


def scheme_of(url: str | None) -> str | None:
    match = re.match(r"^([a-z][a-z0-9+.-]*)://", (url or "").strip(), re.I)
    return match.group(1).lower() if match else None


def official_org(host: str | None) -> str | None:
    """Who owns `host` when it is an official domain (exact or a subdomain), else None."""
    if not host:
        return None
    best = None
    for domain, org in OFFICIAL.items():
        if host == domain or host.endswith("." + domain):
            if best is None or len(domain) > len(best[0]):
                best = (domain, org)
    if best and best[0] in {"bank.in", "fin.in"}:
        # RBI's bank.in only says 'a bank'; the bank's own name in the host says which one (sbi.bank.in).
        return claimed_org(host.removesuffix("." + best[0])) or best[1]
    return best[1] if best else None


def is_official(url: str | None) -> bool:
    return official_org(host_of(url)) is not None


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def claimed_org(host: str | None, form=None) -> str | None:
    """The organisation a page appears to be, from its address words, else from its title and headings."""
    if host:
        for word in re.split(r"[.\-_]+", host):
            for brand, org in _BRANDS.items():
                if word == brand or (len(brand) >= _SHORT and brand in word) or (
                        len(brand) < _SHORT and len(word) > len(brand) and
                        (word.startswith(brand) or word.endswith(brand)) and brand not in {"gov", "upi"}):
                    return org
    if form is not None:
        text = " ".join([getattr(form, "title", "") or "", *(getattr(form, "headings", None) or [])]).casefold()
        for pattern, org in _CLAIMS:
            if re.search(pattern, text):
                return org
    return None


def risky_fields(form) -> dict[str, list[str]]:
    """Concept -> labels of fields Bol OS never fills (OTP, PINs, CVV, passwords, card numbers, CAPTCHA)."""
    found: dict[str, list[str]] = {}
    for f in getattr(form, "fields", ()) or ():
        concept = kinds.classify(f)
        if kinds.sensitivity(concept) == kinds.NEVER_FILL:
            found.setdefault(concept, []).append(f.label)
    return found


def assess(url: str | None, form=None) -> list[SiteWarning]:
    """Warnings for a page, most serious first. An empty list means nothing suspicious was found, not 'safe'."""
    warnings: list[SiteWarning] = []
    host = host_of(url)
    scheme = scheme_of(url)
    risky = risky_fields(form) if form is not None else {}
    money = {k: v for k, v in risky.items() if k in {"otp", "pin", "cvv", "card_number"}}
    if host is None:
        warnings.append(SiteWarning("site_unknown"))
    elif host in _LOCAL:
        warnings.append(SiteWarning("site_local", {"host": host}))
    else:
        org = official_org(host)
        if host.startswith("xn--") or ".xn--" in host:
            warnings.append(SiteWarning("site_punycode", {"host": host}))
        if _is_ip(host):
            warnings.append(SiteWarning("site_ip", {"host": host}))
        if org is None:
            claim = claimed_org(host, form)
            if claim:
                warnings.append(SiteWarning("site_lookalike", {"host": host, "org": claim}))
            else:
                warnings.append(SiteWarning("site_not_listed", {"host": host}))
        else:
            warnings.append(SiteWarning("site_official", {"host": host, "org": org}))
        if scheme == "http":
            warnings.append(SiteWarning("site_http", {"host": host}))
    if money:
        warnings.append(SiteWarning("site_money_fields", {"labels": [label for labels in money.values()
                                                                     for label in labels]}))
    elif risky:
        warnings.append(SiteWarning("secrets", {"labels": [label for labels in risky.values() for label in labels]}))
    order = ("site_lookalike", "site_punycode", "site_ip", "site_http", "site_money_fields", "secrets",
             "site_not_listed", "site_unknown", "site_local", "site_official")
    return sorted(warnings, key=lambda w: order.index(w.key))


DANGER = {"site_lookalike", "site_punycode", "site_ip"}


def dangerous(warnings: list[SiteWarning]) -> bool:
    """A page the user should not fill at all, or an insecure page that asks for money secrets."""
    keys = {w.key for w in warnings}
    return bool(keys & DANGER) or ({"site_http", "site_money_fields"} <= keys)


def messages(warnings: list[SiteWarning], language: str | None) -> list[str]:
    from bolos.forms.prompts import prompt

    out = []
    for w in warnings:
        detail = {k: v for k, v in w.detail.items() if k in {"host", "org"}}
        out.append(prompt(w.key, language, **detail))
    return out
