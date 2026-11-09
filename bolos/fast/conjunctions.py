"""Words that join two commands in one breath, in the 10 languages: "Chrome kholo aur weather batao",
"YouTube khol ke Arijit ke gaane chalao", "Notepad ತೆರೆ ಮತ್ತೆ meeting kal hai anta bari".

Two kinds:
- conjunctions ("aur", "phir", "uske baad", "mattu", "apram", "tarvata", "ani", "pinne", "tarpor", "ane",
  "te"): the boundary is the joiner itself, which belongs to neither command and is dropped.
- verb participles ("khol ke", "open karke", "teredu", "thirandhu", "terichi", "ughdun", "thurannu", "khule",
  "kholine"): "having opened X, do Y". The boundary is AFTER the participle and the first command keeps it,
  since "YouTube khol ke" is still a command System 1 understands ("open YouTube").

Romanised forms are listed in the spellings Prisma writes (several per word) and matched on the same folded
spelling System 1 uses (bolos.fast.features.fold: "phir"/"fir", "aamele"/"amele", "matthu"/"mattu"), so
one entry covers its common variants. Native-script forms are matched exactly (after NFC).

This lexicon only proposes where a command MIGHT split; bolos.compound accepts a split only when System 1
understands every part on its own, so a joiner word inside a title ("Tom and Jerry") or a message is safe.
"""

import re
from dataclasses import dataclass
from functools import cache

from bolos.fast.features import fold, normalize

# Conjunctions: "and", "then", "after that", "and also". Several are also ordinary words elsewhere ("te" is
# Punjabi "on", "matte" is Kannada "again", "or" is English "or"); bolos.compound's checks handle that.
CONJUNCTIONS: dict[str, list[str]] = {
    "hi": ["aur", "or", "aor", "phir", "fir", "phhir", "aur phir", "uske baad", "uske bad", "us ke baad",
           "iske baad", "iske bad", "uske baad mein", "baad mein", "baad me", "saath hi", "sath hi",
           "और", "फिर", "फ़िर", "उसके बाद", "इसके बाद", "उस के बाद", "बाद में", "साथ ही"],
    "en": ["and", "then", "and then", "after that", "afterwards", "after this", "also", "and also", "plus"],
    "kn": ["mattu", "matthu", "mathu", "matte", "matthe", "mathe", "aamele", "amele", "aamel", "aa mele",
           "aamyale", "nantara", "nanthara", "nanthra", "balika",
           "ಮತ್ತು", "ಮತ್ತೆ", "ಆಮೇಲೆ", "ಆಮೇಲ್", "ಆ ಮೇಲೆ", "ನಂತರ", "ಆನಂತರ", "ಬಳಿಕ"],
    "ta": ["apram", "appuram", "apparam", "aprom", "appram", "apuram", "piragu", "piraku", "adhukku apram",
           "adhukku appuram", "athukku appuram", "athukku apram", "adhan piragu", "athan piragu", "matrum",
           "pinnar",
           "அப்புறம்", "அப்றம்", "பிறகு", "மற்றும்", "அதுக்கு அப்புறம்", "அதுக்கப்புறம்", "அதன் பிறகு",
           "அதன்பிறகு", "பின்னர்"],
    "te": ["mariyu", "mariu", "tarvata", "tarvatha", "taruvata", "taruvatha", "tharvatha", "tarwata",
           "taravata", "aa tarvata", "aatarvata", "aa taruvata",
           "మరియు", "తర్వాత", "తరువాత", "ఆ తర్వాత", "ఆతర్వాత", "ఆ తరువాత"],
    "mr": ["ani", "aani", "mag", "magh", "maga", "nantar", "tyanantar", "tyaanantar", "tya nantar",
           "tyachya nantar", "tyachyanantar",
           "आणि", "मग", "नंतर", "त्यानंतर", "त्याच्यानंतर", "त्याच्या नंतर"],
    "ml": ["pinne", "pinneed", "pinneedu", "pinned", "ennittu", "ennit", "enittu", "athu kazhinju",
           "athukazhinju", "athinu shesham", "athinushesham", "koodathe", "kudathe",
           "പിന്നെ", "പിന്നീട്", "എന്നിട്ട്", "അതുകഴിഞ്ഞ്", "അതു കഴിഞ്ഞ്", "അതിനുശേഷം", "അതിനു ശേഷം", "കൂടാതെ"],
    "bn": ["aar", "ar", "tarpor", "tarpore", "tar por", "tar pore", "tarpar", "tarporei", "ebong", "ebang",
           "আর", "তারপর", "তারপরে", "তার পর", "এবং"],
    "gu": ["ane", "pachhi", "pachi", "pacchi", "pachhi thi", "tyar pachhi", "tyarpachhi", "tyarbad",
           "tyaar baad", "ane pachhi",
           "અને", "પછી", "પછીથી", "ત્યારબાદ", "ત્યાર બાદ", "ત્યાર પછી"],
    "pa": ["te", "ate", "phir", "fir", "pher", "us ton baad", "uston baad", "us to baad", "baad vich",
           "ਤੇ", "ਅਤੇ", "ਫਿਰ", "ਫੇਰ", "ਉਸ ਤੋਂ ਬਾਅਦ", "ਬਾਅਦ ਵਿੱਚ"],
}

# Verb participles ("having opened", "having done", "having put on"). Multi-word entries end with the
# participle marker: "khol ke" and "open karke" both put the boundary after their last word. Generic markers
# ("karke", "maadi", "panni", "chesi", "karun", "cheythu", "kore", "karine") cover "<any verb> karke".
PARTICIPLES: dict[str, list[str]] = {
    "hi": ["khol ke", "kholke", "khol kar", "kholkar", "kholkr", "karke", "kar ke", "chala ke", "chalake",
           "chalakar", "laga ke", "lagake", "lagakar", "dhundh ke", "dhoondh ke",
           "खोल के", "खोलके", "खोल कर", "खोलकर", "करके", "कर के", "चला के", "चलाकर", "लगा के", "लगाकर"],
    "en": [],  # English joins with "and"/"then": "open Chrome and search ..."
    "kn": ["teredu", "theredu", "terdu", "maadi", "madi", "haaki", "haki",
           "ತೆರೆದು", "ಮಾಡಿ", "ಹಾಕಿ"],
    "ta": ["thirandhu", "thiranthu", "thirandu", "thorandhu", "thoranthu", "torandhu", "terandhu", "panni",
           "pannitu", "pannittu", "pottu", "pottittu",
           "திறந்து", "பண்ணி", "பண்ணிட்டு", "போட்டு"],
    "te": ["terichi", "therichi", "teriche", "tericchi", "chesi", "chessi", "petti",
           "తెరిచి", "చేసి", "పెట్టి"],
    "mr": ["ughdun", "ughadun", "ughdoon", "ughadoon", "karun", "karoon", "lavun", "laavun",
           "उघडून", "करून", "लावून"],
    "ml": ["thurannu", "thuranu", "thurann", "thorannu", "cheythu", "cheyth", "cheythittu", "ittu",
           "തുറന്ന്", "തുറന്നു", "ചെയ്ത്", "ചെയ്തു", "ഇട്ട്"],
    "bn": ["khule", "khuley", "kore", "korey", "chaliye",
           "খুলে", "করে", "চালিয়ে"],
    "gu": ["kholine", "kholi ne", "karine", "kari ne", "chalavine",
           "ખોલીને", "ખોલી ને", "કરીને", "કરી ને", "ચલાવીને"],
    "pa": ["khol ke", "kholke", "karke", "kar ke", "chala ke",
           "ਖੋਲ੍ਹ ਕੇ", "ਖੋਲ ਕੇ", "ਖੋਲ੍ਹਕੇ", "ਕਰਕੇ", "ਕਰ ਕੇ", "ਚਲਾ ਕੇ"],
}

# "First ..." at the start of a compound command ("pehle Chrome kholo, phir ..."): dropped from step 1.
FIRST_WORDS: dict[str, list[str]] = {
    "hi": ["pehle", "pahle", "pehele", "sabse pehle", "पहले", "सबसे पहले"],
    "en": ["first", "firstly", "first of all"],
    "kn": ["modalu", "modlu", "modale", "ಮೊದಲು", "ಮೊದಲಿಗೆ"],
    "ta": ["mudhalla", "modhalla", "mudhalil", "mudalil", "முதல்ல", "முதலில்"],
    "te": ["mundu", "mundhu", "modata", "ముందు", "మొదట"],
    "mr": ["aadhi", "adhi", "pahila", "आधी", "पहिले"],
    "ml": ["aadyam", "adyam", "aadhyam", "ആദ്യം"],
    "bn": ["prothome", "prothom e", "প্রথমে"],
    "gu": ["pahela", "pehla", "pela", "પહેલા", "પહેલાં"],
    "pa": ["pehlan", "pehla", "ਪਹਿਲਾਂ", "ਪਹਿਲਾ"],
}

# The word that joins the replies of a multi-step command ("Chrome खोल दिया और Bangalore weather सर्च कर
# दिया"), in the script the replies are written in (bolos.responses).
AND_WORDS: dict[str, str] = {
    "hi": "और", "en": "and", "kn": "ಮತ್ತು", "ta": "மற்றும்", "te": "మరియు", "mr": "आणि", "ml": "പിന്നെ",
    "bn": "আর", "gu": "અને", "pa": "ਅਤੇ",
}


def and_word(language: str | None) -> str:
    return AND_WORDS.get(language or "", AND_WORDS["en"])


# Words: letters, digits, Indic letters with their vowel signs and viramas, joiners inside a word, and an
# apostrophe ("what's"). Sentence punctuation followed by a space (or the end) is a boundary of its own:
# "Chrome kholo, weather batao". A dot inside "amazon.in" is part of the word.
_WORD = "\\w\u0900-\u0dff\u200c\u200d'"
_TOKEN = re.compile(f"[{_WORD}]+(?:\\.[{_WORD}]+)*|[,;!?\u0964\u0965]|\\.(?=\\s|$)")
_PUNCT = {",", ";", "!", "?", ".", "\u0964", "\u0965"}


@dataclass(frozen=True)
class Token:
    text: str
    start: int
    end: int
    key: str  # comparable form: normalised, folded spelling ("" for punctuation)
    plain: str = ""  # normalised, not folded


@dataclass(frozen=True)
class Joiner:
    first: int  # index of its first token
    last: int  # index of its last token (inclusive)
    participle: bool
    languages: tuple[str, ...]
    text: str  # as written in the command


def word_key(word: str) -> str:
    return " ".join(fold(w) for w in normalize(word).split())


def tokens(text: str) -> list[Token]:
    out = []
    for m in _TOKEN.finditer(text):
        word = m.group()
        if word in _PUNCT:
            out.append(Token(word, m.start(), m.end(), ""))
        else:
            out.append(Token(word, m.start(), m.end(), word_key(word), normalize(word)))
    return out


@dataclass(frozen=True)
class _Entry:
    words: tuple[str, ...]
    participle: bool
    languages: tuple[str, ...]


def _index(groups: list[tuple[dict[str, list[str]], bool]]) -> dict[str, list[_Entry]]:
    """First word -> entries starting with it, longest first. One spelling listed for several languages
    ("phir": hi and pa) becomes one entry for all of them."""
    langs: dict[tuple[tuple[str, ...], bool], list[str]] = {}
    for table, participle in groups:
        for lang, phrases in table.items():
            for phrase in phrases:
                words = tuple(word_key(phrase).split())
                if words:
                    found = langs.setdefault((words, participle), [])
                    if lang not in found:
                        found.append(lang)
    index: dict[str, list[_Entry]] = {}
    for (words, participle), languages in langs.items():
        index.setdefault(words[0], []).append(_Entry(words, participle, tuple(languages)))
    for entries in index.values():
        entries.sort(key=lambda e: -len(e.words))
    return index


@cache
def _joiner_index() -> dict[str, list[_Entry]]:
    return _index([(CONJUNCTIONS, False), (PARTICIPLES, True)])


@cache
def _first_index() -> dict[str, list[_Entry]]:
    return _index([(FIRST_WORDS, False)])


# Folding is for spelling variants of the listed words, but it also maps some short English words onto
# short joiners: "the" folds to Punjabi "te", "any" to Marathi "ani", "ten" to "then". A romanised word of
# up to this many letters is a joiner only when written exactly as listed.
SHORT_WORD = 3


@cache
def _listed_spellings() -> frozenset[str]:
    tables = (CONJUNCTIONS, PARTICIPLES, FIRST_WORDS)
    return frozenset(w for table in tables for phrases in table.values() for p in phrases for w in normalize(p).split())


def _same_word(tok: Token, key: str) -> bool:
    if tok.key != key:
        return False
    return not (tok.plain.isascii() and len(tok.plain) <= SHORT_WORD) or tok.plain in _listed_spellings()


def _match_at(toks: list[Token], i: int, index: dict[str, list[_Entry]]) -> _Entry | None:
    for entry in index.get(toks[i].key, ()):
        n = len(entry.words)
        if i + n <= len(toks) and all(_same_word(toks[i + k], entry.words[k]) for k in range(n)):
            return entry
    return None


def find_joiners(toks: list[Token]) -> list[Joiner]:
    """Every joiner in the token list: sentence punctuation, conjunctions and participles. Where a longer
    phrase matches ("uske baad"), its words are not matched again on their own."""
    found: list[Joiner] = []
    i = 0
    while i < len(toks):
        tok = toks[i]
        if not tok.key:
            found.append(Joiner(i, i, False, (), tok.text))
            i += 1
            continue
        entry = _match_at(toks, i, _joiner_index())
        if entry is None:
            i += 1
            continue
        last = i + len(entry.words) - 1
        found.append(Joiner(i, last, entry.participle, entry.languages,
                            " ".join(t.text for t in toks[i:last + 1])))
        i = last + 1
    return found


def first_word_length(toks: list[Token], start: int) -> int:
    """How many tokens from start make a "first ..." phrase ("pehle", "sabse pehle"), else 0."""
    if start >= len(toks):
        return 0
    entry = _match_at(toks, start, _first_index())
    return len(entry.words) if entry else 0


def has_joiner(text: str) -> bool:
    """Whether text has a joiner with words on both sides, so that it could be two commands."""
    toks = tokens(text)
    return any(j.first > 0 and j.last < len(toks) - 1 for j in find_joiners(toks))
