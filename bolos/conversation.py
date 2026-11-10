"""What was just said, so a follow-up makes sense: "Narendra Modi kaun hai?" then "unke baare mein search
karo", or "Spotify pe Pink Floyd chalao" then "ab YouTube pe chalao".

The last few turns are kept in memory only, for a few minutes, and go to System 2 with a command that needs
them. Nothing here is written to disk. Messages, dictated text, form answers and file names never enter it:
the pipeline does not add those turns.
"""

import re
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field

WINDOW_S = 180.0  # a follow-up comes soon after; later the same words start a new subject
MAX_TURNS = 4
MAX_REPLY_CHARS = 600  # of each remembered reply: enough to send or type an answer, not a page


@dataclass(frozen=True)
class Turn:
    said: str  # the user's words
    did: tuple[dict, ...]  # the actions taken, as plain dicts
    reply: str  # the conversational answer, if any
    at: float = field(default_factory=time.monotonic, compare=False)


class Conversation:
    def __init__(self, window_s: float = WINDOW_S, max_turns: int = MAX_TURNS, clock=time.monotonic):
        self.window_s = window_s
        self.clock = clock
        self._turns: deque[Turn] = deque(maxlen=max_turns)

    def add(self, said: str, did, reply: str) -> None:
        steps = tuple(step.model_dump(exclude_none=True) if hasattr(step, "model_dump") else dict(step)
                      for step in did)
        self._turns.append(Turn(said.strip(), steps, " ".join(reply.split())[:MAX_REPLY_CHARS], self.clock()))

    def recent(self) -> list[Turn]:
        """The turns a follow-up can refer to, oldest first: the run of turns that ends now, each within
        `window_s` of the next. An old conversation is forgotten the first time this finds it stale."""
        now = self.clock()
        if self._turns and now - self._turns[-1].at > self.window_s:
            self._turns.clear()
        return list(self._turns)

    def last(self) -> Turn | None:
        turns = self.recent()
        return turns[-1] if turns else None

    def clear(self) -> None:
        self._turns.clear()


# Words that point back at what was just said. Prisma writes speech in Latin letters; typed commands can be in
# native script. Only words that are rarely anything else: "se", "te", "or", "ki", "take" and "enu" are left
# out, and "use" (उसे) is kept although it is also English, because a wrong match only costs one System 2
# answer.
_BACK_REFERENCES = frozenset(unicodedata.normalize("NFC", word) for word in """
    it its that this these those them they their theirs him her his he she same there
    ise isse isko iska iski iske isme ismein isi inko inka inki inke inhe inhen
    use usse usko uska uski uske usme usmein usi unko unka unki unke unhe unhen
    yeh yah yahi yehi woh voh wahi vahi wohi vohi wahan vahan
    यह ये यही इसे इसको इसका इसकी इसके इसमें इनको इनका इनकी इनके इन्हें
    वह वो वही उसे उसको उसका उसकी उसके उसमें उनको उनका उनकी उनके उन्हें वहाँ वहां
    idu idanna idara idakke adu adanna adara adakke avaru avara avanu avana avalu avala alli
    ಇದು ಇದನ್ನು ಇದರ ಅದು ಅದನ್ನು ಅದರ ಅವರು ಅವರ ಅವನು ಅವನ ಅವಳು ಅವಳ ಅಲ್ಲಿ
    idhu ithu idhai ithai idhoda adhu athu adhai athai adhoda avar avaru avan avanga avangala avaroda ange
    இது இதை இதோட அது அதை அதோட அவர் அவன் அவள் அவங்க அவரோட அங்கே
    idi deenni dinni deeni adi daanni danni daani atanu atani aame aameni aayana aayanaki vaallu akkada
    ఇది దీన్ని దీని అది దాన్ని దాని అతను అతని ఆమె ఆయన వాళ్ళు అక్కడ
    hyala hyacha hyachi hyache tyala tyacha tyachi tyache tila ticha tyanna tyancha tithe
    हे ह्याला ह्याचा याला याचा त्याला त्याचा त्याची त्याचे तिला तिचा त्यांना त्यांचा तिथे
    ithine ithinte athine athinte avane avante avale avalude avarude avide
    ഇത് ഇതിനെ ഇതിന്റെ അത് അതിനെ അതിന്റെ അവൻ അവന്റെ അവൾ അവളുടെ അവർ അവരുടെ അവിടെ
    eta etake etar ota otake otar unar tader sekhane
    এটা এটাকে এটার ওটা ওটাকে ওটার তাকে ওকে উনি ওনার তাদের সেখানে
    tene tenu teno teni temne temnu tyan
    આ આને આનું આનો તે તેને તેનું તેનો તેમને તેમનું ત્યાં
    ehnu ehda ehdi ohnu ohda ohdi isnu usnu ohna ohnan othe
    ਇਹ ਇਹਨੂੰ ਇਹਦਾ ਇਸਨੂੰ ਉਹ ਉਹਨੂੰ ਉਹਦਾ ਉਸਨੂੰ ਉਹਨਾਂ ਉੱਥੇ
""".split())
_WORD = re.compile("[\\wऀ-ॣ०-෿‌‍]+")


def _words(text: str) -> list[str]:
    return _WORD.findall(unicodedata.normalize("NFC", text).casefold())


# The user asks for a search by name: open it at once, whatever the words around it are. Romanised spellings
# are matched by shape because Prisma writes what it hears ("dhundo", "dhoondho", "dhondho").
_SEARCH = re.compile(
    r"\b(?:search|sarch|serch|google|googal|gugal|find|look\s?up|browse|"
    r"dh[ou]+n?dh?\w*|kho+j\w*|khu?n?j\w*|tala+sh\w*|"  # Hindi, Bengali, Punjabi
    r"hudu?k\w*|th?ed[ui]\w*|ve[dt]h?[ua]k\w*|shodh\w*|th?ira(?:y|nj)\w*|labh\w*)\b"
    "|सर्च|गूगल|ढूंढ|ढूँढ|ढूंढ़|ढूँढ़|खोज|तलाश|शोध|ಹುಡುಕ|ಸರ್ಚ್|ಗೂಗಲ್|தேடு|தேட|கூகுள்|వెతుకు|వెతక|సెర్చ్|గూగుల్|"
    "തിരയ|തിരഞ്ഞ|സെർച്ച്|ഗൂഗിൾ|খোঁজ|খুঁজ|সার্চ|গুগল|શોધ|સર્ચ|ગૂગલ|ਲੱਭ|ਖੋਜ|ਸਰਚ|ਗੂਗਲ",
    re.IGNORECASE,
)
# What only today's web knows. A question about these is searched without asking System 2 first: it could
# only say that it does not know.
_LIVE = re.compile(
    r"\b(?:weather|forecast|temperature|mausam|mosam|taapmaan|tapman|barish|baarish|news|headlines?|khabar\w*|"
    r"samachar|score|scores|price|prices|rate|rates|daam|keemat|kimat|bhaav|bhav|stock|stocks|sensex|nifty|"
    r"traffic|pnr|havamaana|suddi|vaanilai|seithi|vaatavaranam|vartalu|havaman|batmya|kalavastha|vartha|"
    r"abohawa|khobor)\b"
    "|मौसम|तापमान|बारिश|खबर|ख़बर|समाचार|कीमत|क़ीमत|दाम|भाव|ಹವಾಮಾನ|ಸುದ್ದಿ|ಬೆಲೆ|வானிலை|செய்தி|விலை|వాతావరణం|వార్తలు|ధర|"
    "हवामान|बातम्या|किंमत|കാലാവസ്ഥ|വാർത്ത|വില|আবহাওয়া|খবর|দাম|હવામાન|સમાચાર|ભાવ|ਮੌਸਮ|ਖ਼ਬਰ|ਖਬਰ|ਕੀਮਤ",
    re.IGNORECASE,
)


def asks_for_search(command: str) -> bool:
    """Does the user name the search itself ("Google pe dhundho", "search karo")?"""
    return bool(_SEARCH.search(unicodedata.normalize("NFC", command)))


def needs_live_facts(command: str) -> bool:
    """Is it about something that changes by the hour (weather, news, prices, scores)?"""
    return bool(_LIVE.search(unicodedata.normalize("NFC", command)))


def refers_back(command: str) -> bool:
    """Does the command name something by a pronoun, so that it only makes sense after the one before it?
    "isko YouTube pe chalao", "tell me more about him", "ise Raju ko bhej do"."""
    return any(word in _BACK_REFERENCES for word in _words(command))
