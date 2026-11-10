"""Recognise requests for local files without sending their names to a model."""

import re
import unicodedata

from bolos import actions as a
from bolos.responses import refuses_command

_FILE = (r"(?:p\s?d\s?f(?:\s+file)?|files?|documents?|spreadsheets?|presentations?|fail|"
         r"फ़ाइल|फाइल|दस्तावेज़|पीडीएफ|ಫೈಲ್|கோப்பு|ఫైల్|ফাইল|ફાઇલ|ഫയൽ|ਫਾਇਲ)")
_OPEN = (r"(?:open(?:\s+kar\s*(?:do|de|dijiye)|\s+karo)?|kholo|kholiye|khol\s*(?:do|de|dijiye)|"
         r"खोलो|खोलिए|खोल\s*(?:दो|दें|दीजिए)|ತೆರೆ|ತೆರೆಯಿರಿ|திற|திறக்கவும்|తెరువు|తెరవండి|"
         r"उघड|उघडा|খোলো|খুলুন|ખોલો|തുറക്കൂ|തുറക്കുക|ਖੋਲ੍ਹੋ)")
_FIND = (r"(?:find\s+karo|search\s+karo|dh?(?:oo|u)n?dh?(?:ho|o|hiye|iye|\s+do)|khojo|"
         r"(?:kahan|kahaan|kidhar)\s+hai|कहाँ है|कहां है|ढूँढो|ढूंढो|ढूँढ़ो|खोजो|खोजिए|"
         r"ಹುಡುಕಿ|தேடு|వెతుకు|शोधा|খুঁজুন|શોધો|തിരയൂ|ਲੱਭੋ)")
_POLITE = r"(?:(?:please|can you|could you|sahur|साहुर|zara|jara|ज़रा|जरा)[,\s]+)*"
_ENGLISH = re.compile(
    rf"{_POLITE}(?:(?P<open>open)(?:\s+(?:up|karo|kar\s*(?:do|de)))?"
    r"|find(?:\s+me)?|locate|look\s+for|search\s+for|show\s+me(?!\s+how\b)|reveal|where(?:'s| is))"
    r"\s+(?P<target>.+)",
    re.I,
)
_SUFFIX_COMMAND = re.compile(
    rf"{_POLITE}(?P<target>.+?)\s+(?:(?P<open>{_OPEN})|{_FIND})(?:\s+please)?", re.I,
)

_DOWNLOAD = r"(?:download|डाउनलोड)"
_PAST = rf"{_DOWNLOAD}\s+(?:ki|kiya|kiyaa|kari|kara|karaa|की|किया)(?:\s+(?:h|hai|he|tha|thi|है|था|थी))?"
_JUST = r"(?:abhi(?:\s+abhi)?|अभी(?:\s+अभी)?)"
_I = r"(?:maine|mai?n?\s+ne|mein\s+ne|mene|मैंने|मैं\s+ने)"
_PRONOUN = (r"(?:use|usse|ussey|usko|wo|woh|vo|voh|isko|ise|उसे|उसको|वो|वह|इसे|इसको|"
            r"(?:us|is|उस|इस)\s+(?:file|फ़ाइल|फाइल)\s+(?:ko|को))")
_DOWNLOAD_ITEM = rf"(?:{_FILE}|(?:(?:pan|पैन)\s+)?(?:form|फॉर्म|फ़ॉर्म))"
_RECENT_WORD = r"(?:latest|last|most recent|newest)"
_ENGLISH_RECENT = re.compile(
    rf"(?:my |the )?(?:{_RECENT_WORD} (?:download|downloaded {_FILE}|{_FILE} (?:i |you )?downloaded)"
    rf"|(?:just|(?:most )?recently) downloaded {_FILE}|{_FILE} (?:that )?i (?:just )?downloaded(?: just now)?"
    r"|what i just downloaded)",
    re.I,
)
_ENGLISH_RECENT_COMMAND = re.compile(
    rf"{_POLITE}i (?:just )?downloaded (?:a |one |the )?{_FILE}(?: just now)?[,.!?; ]+"
    r"(?:please )?open (?:it|that(?: file)?)",
    re.I,
)
_HINDI_RECENT = re.compile(
    rf"(?:(?:(?:{_I}|jo|जो)(?:\s+(?:{_I}|jo|जो))?(?:\s+{_JUST})?|{_JUST})\s+"
    rf"(?:(?:ek|koi|एक|कोई)\s+)?(?:{_DOWNLOAD_ITEM}\s+)?(?:{_I}\s+)?(?:{_JUST}\s+)?{_PAST}"
    rf"|{_JUST}\s+{_DOWNLOAD}\s+(?:ki hui|kiya hua|की हुई|किया हुआ)\s+{_FILE}"
    rf"|(?:{_JUST}\s+wali|अभी वाली)\s+{_DOWNLOAD}|(?:meri\s+)?{_RECENT_WORD}\s+download(?:ed)?(?:\s+{_FILE})?)"
    rf"(?:[,; ]+{_PRONOUN}(?:\s+{_FILE})?)?",
    re.I,
)

_FILE_PREFIX = re.compile(
    rf"^(?:(?:the|a|my|meri|mera|मेरी|मेरा)\s+)?(?:downloaded\s+)?(?P<kind>{_FILE})\s+"
    r"(?:(?:named|called|titled)\s+)?",
    re.I,
)
_FILE_SUFFIX = re.compile(
    rf"\s+(?:(?:naam\s+ki|naam\s+wali|naam\s+ka|नाम\s+की|नाम\s+वाली|ki|की|wali|वाली)\s+)?(?P<kind>{_FILE})$",
    re.I,
)
_POSSESSIVE = re.compile(r"^(?:the|my|meri|mera|मेरी|मेरा)\s+", re.I)
_TARGET_CASE = re.compile(r"\s+(?:ko|को)$", re.I)
_FILE_MANAGER = re.compile(
    r"(?:(?:the|my)\s+)?(?:file\s+(?:explorer|manager)|windows explorer|pdf\s+(?:reader|viewer))", re.I,
)
_UNNAMED = re.compile(
    rf"(?:(?:the|a|my|this|that|meri|mera|मेरी|मेरा)\s+)?"
    rf"(?:{_FILE}|it|{_PRONOUN}|named|called|titled|kaise|कैसे|on (?:my|this) (?:computer|pc))",
    re.I,
)
_PATH = re.compile(r"^(?:[a-z]:[\\/]|[\\/]|\.\.?[\\/])", re.I)
_REMOTE = re.compile(
    r"https?://|\s+online$|\b(?:in|on|using|via)\s+(?:google|youtube|amazon|the internet|the web|bing)\b|"
    r"\b(?:google|youtube|amazon|bing|flipkart)\s+(?:pe|par|mein)\b",
    re.I,
)
_OTHER_COMMAND = re.compile(
    r"\b(?:and then|then|aur phir|phir|fir)\s+(?:open|find|show|close|play|search)\b|"
    r"\b(?:kholo|karo|dhundo)\s+(?:aur|phir|fir)\b|\b(?:ko bolo|bolo ki|ko batao)\b",
    re.I,
)

# A bare extension is evidence of a file request. A typed name such as example.com remains a website cue.
_EXTENSIONS = frozenset("""
    7z avif avi bat bmp cmd csv doc docx exe gif heic html jpeg jpg js json lnk log m4a md mkv mov mp3 mp4
    msi odf odp ods odt pdf png ppt pptx ps1 py rar rtf svg tar tif tiff ts txt url vbs wav webm webp xls xlsx zip
""".split())
_SPOKEN_DOT = re.compile(r"(?P<name>.+?)\s+(?:dot|डॉट)\s+(?P<ext>[a-z0-9 ]+)$", re.I)
_PDF = re.compile(r"p\s?d\s?f(?:\s+file)?|पीडीएफ", re.I)
_QUOTES = {'"': '"', "“": "”", "‘": "’", "'": "'"}
_QUOTED_TEXT = re.compile(r'''"[^"\r\n]*"|“[^”\r\n]*”|‘[^’\r\n]*’|(?<!\w)'[^'\r\n]*'(?!\w)''')


def _unquote(text: str) -> str:
    if len(text) > 1 and _QUOTES.get(text[0]) == text[-1]:
        return text[1:-1].strip()
    return text


def _spoken_extension(target: str) -> str:
    if spoken := _SPOKEN_DOT.fullmatch(target):
        extension = spoken["ext"].replace(" ", "").casefold()
        if extension in _EXTENSIONS:
            return f"{spoken['name']}.{extension}"
    return target


def _named_query(target: str) -> str | None:
    target = _TARGET_CASE.sub("", target).strip()
    if (_FILE_MANAGER.fullmatch(target) or _UNNAMED.fullmatch(target)
            or _REMOTE.search(_QUOTED_TEXT.sub(" ", target))):
        return None
    target = _spoken_extension(target)
    kind = None
    if match := _FILE_PREFIX.match(target):
        kind = match["kind"]
        target = target[match.end():]
    elif match := _FILE_SUFFIX.search(target):
        kind = match["kind"]
        target = _POSSESSIVE.sub("", target[:match.start()])
    target = _unquote(target.strip())
    if not target or _UNNAMED.fullmatch(target):
        return None
    target = _spoken_extension(target)
    _, dot, extension = target.rpartition(".")
    if (dot and extension.casefold() in _EXTENSIONS) or _PATH.match(target):
        return target
    if kind and _PDF.fullmatch(kind):
        return f"{target}.pdf"
    return target if kind else None


def parse_file_request(text: str) -> a.FindFile | a.OpenRecentDownload | None:
    """Route clear local file requests before the generic app and web models."""
    command = unicodedata.normalize("NFC", text).strip().rstrip(".!?।॥").rstrip()
    unquoted = _QUOTED_TEXT.sub(" ", command)
    if not command or refuses_command(unquoted) or _OTHER_COMMAND.search(unquoted):
        return None
    if _ENGLISH_RECENT_COMMAND.fullmatch(" ".join(command.split())):
        return a.OpenRecentDownload()
    if match := _ENGLISH.fullmatch(command):
        target = match["target"]
        open_file = match["open"] is not None
    elif match := _SUFFIX_COMMAND.fullmatch(command):
        target = match["target"]
        open_file = match["open"] is not None
    else:
        return None
    recent = " ".join(target.split())
    if open_file and (_ENGLISH_RECENT.fullmatch(recent) or _HINDI_RECENT.fullmatch(recent)):
        return a.OpenRecentDownload()
    if query := _named_query(target):
        return a.FindFile(query=query, open=open_file)
    return None
