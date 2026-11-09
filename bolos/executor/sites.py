"""Websites by the name the user says: well-known sites, spoken addresses ("github dot com"), and for
anything else the first search result (DuckDuckGo's "I'm feeling lucky")."""

import re
import unicodedata
from urllib.parse import quote, quote_plus

# Sites people name, mapped to their address. Keys are lowercase, without spaces.
SITES: dict[str, str] = {
    "amazon": "https://www.amazon.in", "flipkart": "https://www.flipkart.com", "myntra": "https://www.myntra.com",
    "meesho": "https://www.meesho.com", "ajio": "https://www.ajio.com", "nykaa": "https://www.nykaa.com",
    "swiggy": "https://www.swiggy.com", "zomato": "https://www.zomato.com", "blinkit": "https://blinkit.com",
    "zepto": "https://www.zeptonow.com", "bigbasket": "https://www.bigbasket.com",
    "irctc": "https://www.irctc.co.in", "makemytrip": "https://www.makemytrip.com", "redbus": "https://www.redbus.in",
    "goibibo": "https://www.goibibo.com", "ola": "https://www.olacabs.com", "uber": "https://www.uber.com",
    "paytm": "https://paytm.com", "phonepe": "https://www.phonepe.com", "gpay": "https://pay.google.com",
    "sbi": "https://www.onlinesbi.sbi", "onlinesbi": "https://www.onlinesbi.sbi", "hdfc": "https://www.hdfcbank.com",
    "icici": "https://www.icicibank.com", "axisbank": "https://www.axisbank.com",
    "gmail": "https://mail.google.com", "mail": "https://mail.google.com", "googledrive": "https://drive.google.com",
    "drive": "https://drive.google.com", "googlemaps": "https://maps.google.com", "maps": "https://maps.google.com",
    "googletranslate": "https://translate.google.com", "translate": "https://translate.google.com",
    "google": "https://www.google.com", "youtube": "https://www.youtube.com", "youtubemusic": "https://music.youtube.com",
    "wikipedia": "https://www.wikipedia.org", "github": "https://github.com", "stackoverflow": "https://stackoverflow.com",
    "linkedin": "https://www.linkedin.com", "facebook": "https://www.facebook.com", "twitter": "https://x.com",
    "x": "https://x.com", "instagram": "https://www.instagram.com", "reddit": "https://www.reddit.com",
    "netflix": "https://www.netflix.com", "hotstar": "https://www.hotstar.com", "jiohotstar": "https://www.hotstar.com",
    "jiocinema": "https://www.jiocinema.com", "primevideo": "https://www.primevideo.com",
    "spotify": "https://open.spotify.com", "jiosaavn": "https://www.jiosaavn.com", "gaana": "https://gaana.com",
    "cricbuzz": "https://www.cricbuzz.com", "espncricinfo": "https://www.espncricinfo.com",
    "cricinfo": "https://www.espncricinfo.com", "ndtv": "https://www.ndtv.com",
    "timesofindia": "https://timesofindia.indiatimes.com", "thehindu": "https://www.thehindu.com",
    "moneycontrol": "https://www.moneycontrol.com", "zerodha": "https://kite.zerodha.com", "kite": "https://kite.zerodha.com",
    "groww": "https://groww.in", "naukri": "https://www.naukri.com", "indeed": "https://in.indeed.com",
    "coursera": "https://www.coursera.org", "udemy": "https://www.udemy.com", "khanacademy": "https://www.khanacademy.org",
    "byjus": "https://byjus.com", "chatgpt": "https://chatgpt.com", "gemini": "https://gemini.google.com",
    "claude": "https://claude.ai", "digilocker": "https://www.digilocker.gov.in", "uidai": "https://uidai.gov.in",
    "aadhaar": "https://uidai.gov.in", "aadhar": "https://uidai.gov.in", "incometax": "https://www.incometax.gov.in",
    "passportseva": "https://www.passportindia.gov.in", "passport": "https://www.passportindia.gov.in",
    "bookmyshow": "https://in.bookmyshow.com", "practo": "https://www.practo.com", "1mg": "https://www.1mg.com",
    "pharmeasy": "https://pharmeasy.in", "canva": "https://www.canva.com", "overleaf": "https://www.overleaf.com",
    "whatsappweb": "https://web.whatsapp.com", "outlook": "https://outlook.live.com", "zoom": "https://app.zoom.us",
    "googlemeet": "https://meet.google.com", "meet": "https://meet.google.com", "telegram": "https://web.telegram.org",
    "discord": "https://discord.com/app", "notion": "https://www.notion.so", "quora": "https://www.quora.com",
    "pinterest": "https://www.pinterest.com", "weather": "https://weather.com", "news": "https://news.google.com",
}
# Where to search inside a site: {q} is the URL-encoded query.
SEARCH_URLS: dict[str, str] = {
    "amazon": "https://www.amazon.in/s?k={q}", "flipkart": "https://www.flipkart.com/search?q={q}",
    "myntra": "https://www.myntra.com/{path}", "meesho": "https://www.meesho.com/search?q={q}",
    "youtube": "https://www.youtube.com/results?search_query={q}", "youtubemusic": "https://music.youtube.com/search?q={q}",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search={q}", "github": "https://github.com/search?q={q}",
    "stackoverflow": "https://stackoverflow.com/search?q={q}", "linkedin": "https://www.linkedin.com/search/results/all/?keywords={q}",
    "reddit": "https://www.reddit.com/search/?q={q}", "twitter": "https://x.com/search?q={q}", "x": "https://x.com/search?q={q}",
    "spotify": "https://open.spotify.com/search/{path}", "jiosaavn": "https://www.jiosaavn.com/search/{path}",
    "gaana": "https://gaana.com/search/{path}", "naukri": "https://www.naukri.com/{path}-jobs",
    "indeed": "https://in.indeed.com/jobs?q={q}", "googlemaps": "https://www.google.com/maps/search/{q}",
    "maps": "https://www.google.com/maps/search/{q}", "swiggy": "https://www.swiggy.com/search?query={q}",
    "zomato": "https://www.zomato.com/search?q={q}", "bigbasket": "https://www.bigbasket.com/ps/?q={q}",
    "netflix": "https://www.netflix.com/search?q={q}", "hotstar": "https://www.hotstar.com/in/explore?search_query={q}",
    "pinterest": "https://www.pinterest.com/search/pins/?q={q}", "primevideo": "https://www.primevideo.com/search?phrase={q}",
    "jiocinema": "https://www.jiocinema.com/search/{path}", "quora": "https://www.quora.com/search?q={q}",
    "udemy": "https://www.udemy.com/courses/search/?q={q}", "coursera": "https://www.coursera.org/search?query={q}",
    "google": "https://www.google.com/search?q={q}", "news": "https://news.google.com/search?q={q}",
}
# Spoken site names that differ from the key ("amazon dot in" is handled as an address).
SITE_ALIASES = {
    "you tube": "youtube", "yt": "youtube", "wiki": "wikipedia", "stack overflow": "stackoverflow",
    "make my trip": "makemytrip", "red bus": "redbus", "book my show": "bookmyshow", "times of india": "timesofindia",
    "the hindu": "thehindu", "money control": "moneycontrol", "khan academy": "khanacademy", "jio saavn": "jiosaavn",
    "saavn": "jiosaavn", "prime video": "primevideo", "amazon prime": "primevideo", "jio hotstar": "hotstar",
    "jio cinema": "jiocinema", "google maps": "googlemaps", "google drive": "googledrive", "g mail": "gmail",
    "income tax": "incometax", "passport seva": "passportseva", "phone pe": "phonepe", "google pay": "gpay",
    "state bank": "sbi", "sbi bank": "sbi", "hdfc bank": "hdfc", "icici bank": "icici", "axis bank": "axisbank",
    "big basket": "bigbasket", "linked in": "linkedin", "face book": "facebook", "insta": "instagram",
    "whatsapp web": "whatsappweb", "google meet": "googlemeet", "chat gpt": "chatgpt", "youtube music": "youtubemusic",
    "espn": "espncricinfo", "e filing": "incometax",
}

# \b doesn't see Indic vowel signs and viramas as part of a word, so spell the word boundaries out.
_DOT = re.compile("\\s*(?<![\\w\u0900-\u0dff])(dot|डॉट|ಡಾಟ್|டாட்|డాట్)(?![\\w\u0900-\u0dff])\\s*", re.I)
_TLD = re.compile(r"^[\w-]+(\.[\w-]+)*\.(com|in|org|net|co|io|ai|gov|edu|app|dev|me|info|tv|us|uk|so|sbi)"
                  r"(\.[a-z]{2})?(/\S*)?$", re.I)
_NOISE = re.compile(r"\b(website|web site|site|page|official|ki|ka|ke|ko|pe|par|la|alli|nalli|lo|ah)\b", re.I)


def site_key(spoken: str) -> str:
    text = " ".join(_NOISE.sub(" ", unicodedata.normalize("NFC", spoken).casefold()).split())
    text = SITE_ALIASES.get(text, text)
    return re.sub("[^\\w\u0900-\u0963\u0966-\u0dff]", "", text)  # keeps Indic vowel signs and viramas


def as_address(spoken: str) -> str | None:
    """'github dot com', 'irctc.co.in', 'www.amazon.in/deals' -> an https URL; None if it isn't an address."""
    text = _DOT.sub(".", spoken.strip().casefold())
    text = re.sub(r"^(https?://)?(www\.)?", "", text.replace(" ", ""))
    if not _TLD.match(text):
        return None
    return f"https://{text}" if "/" in text or text.count(".") > 1 else f"https://www.{text}"


def url_for(spoken: str) -> str:
    """The address to open for a spoken site name."""
    if address := as_address(spoken):
        return address
    if (k := site_key(spoken)) in SITES:
        return SITES[k]
    return lucky_url(f"{spoken} official website")


def known(spoken: str) -> bool:
    return as_address(spoken) is not None or site_key(spoken) in SITES


def lucky_url(query: str) -> str:
    """Goes straight to the first result."""
    return f"https://duckduckgo.com/?q=%5C{quote_plus(query)}"


def search_url(site: str | None, query: str) -> str:
    """Search inside a named site when it has a search page, else the web (mentioning the site)."""
    if site:
        k = site_key(site)
        if k in SEARCH_URLS:
            return SEARCH_URLS[k].format(q=quote_plus(query), path=quote(query, safe=""))
        return google_url(f"{query} {site}")
    return google_url(query)


def google_url(query: str) -> str:
    return f"https://www.google.com/search?q={quote_plus(query)}"
