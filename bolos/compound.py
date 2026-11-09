"""Two or three commands in one breath: "Chrome kholo aur Bangalore ka weather search karo",
"YouTube khol ke Arijit ke gaane chalao", "Notepad ತೆರೆ ಮತ್ತೆ meeting kal hai anta bari".

`split` cuts a command into the commands it is made of, using System 1 itself as the judge: a cut is kept
only if System 1 understands every part surely and completely on its own. That one rule keeps the joiner
words that are not joiners safe: "Tom and Jerry chalao" has no part "Tom" that is a command. Two more rules
come from how System 1 read the whole command: a cut never falls inside a message or dictated text
("bolo ki main aa raha hoon aur khana kha lunga"), nor inside a field System 1 is sure spans the joiner
("search for salt and pepper chicken recipe"). The one exception is a field that starts where the command
starts and so swallows a whole first step ("ಕ್ರೋಮ್ ತೆರೆ ಮತ್ತು ಬೆಂಗಳೂರು ಹವಾಮಾನ ಹುಡುಕು" read as one search for
all of it): that is the tagger failing, and the cut is allowed when every part asks for something beyond
its names. When no cut works, the caller acts on System 1's single action if it is sure of it, else hands
the whole command to System 2 (bolos.llm.LLM.resolve_steps).

A split is tried even when System 1 is sure of the whole as ONE action: an action model without the "multi"
class reads most compound commands as one of their steps, surely ("Calculator kholo aur Excel kholo" ->
open_app Calculator), which would silently drop the second step. The parts check is what keeps single
commands whole (see the numbers in the round 3 report).

Speed: candidate parts are first screened with System 1's action model alone (about a fifth of a full
parse), splits are ranked on those confidences, and the slot tagger runs only on the parts of the best
splits until one passes.

`chain` then runs context forward from step to step (`carry_over`): the browser step 1 opened is where
step 2 searches, the service step 1 opened is where step 2 plays, the site step 1 opened is where step 2
searches; an "open YouTube" step whose next step plays on YouTube is folded into that step.
"""

import logging
from dataclasses import dataclass
from typing import Protocol

from bolos import actions as a
from bolos.decide import Parse
from bolos.fast.conjunctions import Token, find_joiners, first_word_length, tokens

log = logging.getLogger(__name__)

MULTI = "multi"  # System 1's class for a compound command (bolos.fast.model.MULTI)
MAX_PARTS = 3
# More candidate cut points than this is a long ramble, not a two- or three-step command: System 2 reads it
# instead, and the search stays within a few tens of milliseconds.
MAX_CUTS = 8
MIN_PART_WORDS = 2  # a single word is never a step here: "Romeo" alone parses, surely, as a media control
NOT_A_STEP = {"unknown", "cancel", MULTI}
DICTATION = {"send_message", "type_text"}  # their text is the user's own words, never cut
NAMED_OPENS = {"open_app", "open_website"}


class Parser(Protocol):
    def parse(self, command: str) -> Parse: ...


@dataclass(frozen=True)
class _Cut:
    end: int  # the first part ends before this token
    start: int  # the next part starts at this token
    strict: bool = False  # inside a field that swallowed the first step: every part must ask for something


def split(command: str, parser: Parser, whole: Parse | None = None) -> list[str] | None:
    """The commands in a compound command, in order (2 or 3), or None to treat it as one command.

    whole is System 1's parse of the entire command, when the caller already has it. Each cut is at a joiner
    (conjunctions dropped, participles kept with the part before them); every part must parse surely and
    completely as an executable action that asks for something (not just a name). Of the splits that pass,
    the one with the most parts wins (a part that still holds two commands means System 1 misread it as
    one), then the one whose least confident part is most confident.
    """
    toks = tokens(command)
    while toks and not toks[-1].key:  # a full stop or "?" at the end belongs to no part
        toks.pop()
    while toks and not toks[0].key:
        toks.pop(0)
    begin = _first_step_begins(toks)
    cuts = [cut for cut in _cuts(toks) if cut.end > begin]
    if not cuts:
        return None
    whole = whole or parser.parse(command)
    cuts = [c for c in (_field_check(command, toks, whole, cut, begin) for cut in cuts) if c is not None]
    if not cuts or len(cuts) > MAX_CUTS:
        return None
    judge = _Judge(command, toks, parser)
    for n_parts in range(MAX_PARTS, 1, -1):  # most parts first: the long two-part ranges are often never read
        candidates = []
        for ranges, strict in _partitions(cuts, begin, len(toks), n_parts):
            confidences = []
            for r in ranges:
                if (confidence := judge.quick(r)) is None:
                    break
                confidences.append(confidence)
            else:
                candidates.append((min(confidences), sum(confidences) / n_parts, ranges, strict))
        candidates.sort(key=lambda c: c[:2], reverse=True)
        for *_, ranges, strict in candidates:
            if all(judge.ok(r, strict) for r in ranges):
                return [judge.text(r) for r in ranges]
    log.info("no split of %r works", command)
    return None


def needs_step_resolution(command: str, parser: Parser, whole: Parse | None = None) -> bool:
    """Whether a failed split must go to System 2 instead of silently doing one step.

    An older action model may confidently read ``Chrome kholo aur ...`` as only
    opening Chrome. If the left side of an unprotected boundary is independently
    a complete request, the rest needs understanding before anything runs.
    Names/titles alone do not establish such a request, and boundaries inside
    confidently recognized dictation or query fields retain their protection.
    Call this only after ``split`` failed to produce a complete plan.
    """
    toks = tokens(command)
    while toks and not toks[-1].key:
        toks.pop()
    while toks and not toks[0].key:
        toks.pop(0)
    begin = _first_step_begins(toks)
    cuts = [cut for cut in _cuts(toks) if cut.end > begin]
    if not cuts:
        return False
    whole = whole or parser.parse(command)
    judge = _Judge(command, toks, parser)
    for cut in cuts:
        cut = _field_check(command, toks, whole, cut, begin)
        if cut is None:
            continue
        left = (begin, cut.end)
        # Require actual request words for this conservative fallback, even
        # outside a swallowed field: "Harry Potter and ..." is not evidence of
        # two commands merely because a model can search for "Harry Potter".
        if judge.quick(left) is not None and judge.ok(left, strict=True):
            return True
    return False


def _first_step_begins(toks: list[Token]) -> int:
    """Where step 1 starts: after a leading conjunction ("aur Chrome kholo, phir ...", "and then ...") and
    a "first" word ("pehle Chrome kholo phir ...")."""
    begin = 0
    for j in find_joiners(toks):
        if j.first != begin or j.participle:
            break
        begin = j.last + 1
    return begin + first_word_length(toks, begin)


def _cuts(toks: list[Token]) -> list[_Cut]:
    """Where the command may split. Conjunctions and punctuation next to a cut belong to neither part
    ("kholo, aur phir ..."); a participle stays with the part before it ("khol ke | ...")."""
    joiners = find_joiners(toks)
    dropped = {i for j in joiners if not j.participle for i in range(j.first, j.last + 1)}
    cuts: list[_Cut] = []
    for j in joiners:
        end, start = (j.last + 1, j.last + 1) if j.participle else (j.first, j.last + 1)
        while end > 0 and end - 1 in dropped:
            end -= 1
        while start < len(toks) and start in dropped:
            start += 1
        cut = _Cut(end, start)
        if 0 < end and start < len(toks) and cut not in cuts:
            cuts.append(cut)
    return cuts


def _field_check(command: str, toks: list[Token], whole: Parse, cut: _Cut, begin: int) -> _Cut | None:
    """The cut, or None where System 1, reading the whole command, put words from both sides of it into one
    field: the text of a message or dictation it is sure is one ("bolo ki main aa raha hoon aur khana kha
    lunga"), or any field of an action it is sure of completely ("salt and pepper chicken recipe batao"). The
    joiner is then part of what the user wants said or found. A field (not a message's text) that starts
    where the command starts swallowed a whole first step, verb and all: the cut stays, marked strict."""
    if whole.action in DICTATION and whole.sure:
        values, may_swallow = [whole.slots.get("text")], False
    elif whole.complete:
        values, may_swallow = list(whole.slots.values()), True
    else:
        return cut
    left, right = toks[cut.end - 1], toks[cut.start]
    for value in values:
        if value and (at := command.find(value)) >= 0 and at < left.end and at + len(value) > right.start:
            if not (may_swallow and at <= toks[begin].start):
                return None
            cut = _Cut(cut.end, cut.start, strict=True)
    return cut


def _partitions(cuts: list[_Cut], begin: int, end: int, n_parts: int):
    """(token ranges [(a, b), ...], whether any cut used is strict) for n_parts parts, cuts in order."""
    ordered = sorted(cuts, key=lambda c: c.end)

    def rec(start: int, after: int, left: int):
        if left == 1:
            yield [(start, end)], False
            return
        for k in range(after, len(ordered)):
            cut = ordered[k]
            if cut.end <= start:
                continue
            for rest, strict in rec(cut.start, k + 1, left - 1):
                yield [(start, cut.end), *rest], strict or cut.strict

    yield from rec(begin, 0, n_parts)


@dataclass(frozen=True)
class _Step:
    parse: Parse
    action: a.Action
    only_names: bool  # no word outside its fields: "Harry Potter" as open_app asks for nothing


class _Judge:
    """Decides whether a part is a step on its own, running each model at most once per part."""

    def __init__(self, command: str, toks: list[Token], parser: Parser):
        self.command, self.toks, self.parser = command, toks, parser
        self._quick: dict[tuple[int, int], float | None] = {}
        self._steps: dict[tuple[int, int], _Step | None] = {}

    def text(self, r: tuple[int, int]) -> str:
        start, end = r
        return self.command[self.toks[start].start:self.toks[end - 1].end].strip()

    def _words(self, r: tuple[int, int]) -> list[Token]:
        return [t for t in self.toks[r[0]:r[1]] if t.key]

    def quick(self, r: tuple[int, int]) -> float | None:
        """System 1's action-model confidence that the part is a step, or None if it is not one: a part it is
        unsure of, or reads as no step, is out before the slot tagger runs. A parser without a separate
        action model gives a full parse instead."""
        if r not in self._quick:
            self._quick[r] = self._quick_check(r)
        return self._quick[r]

    def _quick_check(self, r: tuple[int, int]) -> float | None:
        if len(self._words(r)) < MIN_PART_WORDS:
            return None
        model = getattr(self.parser, "model", None)
        if model is None or not hasattr(model, "predict") or not hasattr(model, "accepts"):
            step = self.step(r)
            return None if step is None else _confidence(step.parse)
        pred = model.predict(self.text(r))
        if pred.action in NOT_A_STEP or not model.accepts(pred):
            return None
        return float(pred.action_confidence)

    def step(self, r: tuple[int, int]) -> _Step | None:
        if r not in self._steps:
            self._steps[r] = self._full_check(r)
        return self._steps[r]

    def _full_check(self, r: tuple[int, int]) -> _Step | None:
        text = self.text(r)
        parse = self.parser.parse(text)
        if parse.action in NOT_A_STEP or not parse.complete:
            return None
        action = action_of(parse)
        if action is None:
            return None
        return _Step(parse, action, _only_names(text, self._words(r), self.toks[r[0]].start, parse))

    def ok(self, r: tuple[int, int], strict: bool) -> bool:
        """A sure, complete step. An open must ask for something beyond its names ("Shah Rukh" is no app to
        open); in a strict split every part must (a search that is all query is a piece of a title)."""
        step = self.step(r)
        if step is None:
            return False
        return not (step.only_names and (strict or step.parse.action in NAMED_OPENS))


def _confidence(parse: Parse) -> float:
    return 1.0 if parse.confidence is None else float(parse.confidence)


def action_of(parse: Parse) -> a.Action | None:
    """The executable action of a sure, complete parse; None if its fields don't make a valid one."""
    if parse.action == "media_control":
        return a.MediaControl(control=parse.control) if parse.control in a.CONTROLS else None
    fields = {k: v for k, v in parse.slots.items() if k in a.SLOT_FIELDS.get(parse.action, []) and v}
    try:
        return a.parse_action({"action": parse.action, **fields})
    except ValueError:
        return None


def _only_names(text: str, words: list[Token], offset: int, parse: Parse) -> bool:
    """The part is nothing but its fields ("Harry Potter" as open_app): no word asks for anything, so it is
    a piece of a title or a list, not a command."""
    if not parse.slots:
        return False
    covered: list[tuple[int, int]] = []
    for value in parse.slots.values():
        if value and (i := text.find(value)) >= 0:
            covered.append((offset + i, offset + i + len(value)))
    return all(any(lo <= w.start and w.end <= hi for lo, hi in covered) for w in words)


# Context carried from one step to the next.

# Browsers a following web search or website opens in, by the names users say (bolos.executor.apps knows
# how to start each with an address).
BROWSER_NAMES: dict[str, str] = {
    "chrome": "Chrome", "googlechrome": "Chrome", "crome": "Chrome", "krome": "Chrome",
    "क्रोम": "Chrome", "ಕ್ರೋಮ್": "Chrome", "குரோம்": "Chrome", "க்ரோம்": "Chrome", "క్రోమ్": "Chrome",
    "ക്രോം": "Chrome", "ক্রোম": "Chrome", "ક્રોમ": "Chrome", "ਕ੍ਰੋਮ": "Chrome",
    "edge": "Edge", "microsoftedge": "Edge", "msedge": "Edge", "एज": "Edge", "ಎಡ್ಜ್": "Edge", "எட்ஜ்": "Edge",
    "ఎడ్జ్": "Edge", "എഡ്ജ്": "Edge", "এজ": "Edge", "એજ": "Edge", "ਐਜ": "Edge",
    "firefox": "Firefox", "mozilla": "Firefox", "mozillafirefox": "Firefox", "firefoxbrowser": "Firefox",
    "फ़ायरफ़ॉक्स": "Firefox", "फायरफॉक्स": "Firefox",
    "brave": "Brave", "bravebrowser": "Brave", "ब्रेव": "Brave",
    "opera": "Opera", "operabrowser": "Opera", "operagx": "Opera", "ओपेरा": "Opera",
}
# Music and video services a following play_media without a service plays on.
MEDIA_SERVICES: dict[str, str] = {
    "youtube": "YouTube", "yt": "YouTube", "यूट्यूब": "YouTube", "यूटयूब": "YouTube", "ಯೂಟ್ಯೂಬ್": "YouTube",
    "யூடியூப்": "YouTube", "యూట్యూబ్": "YouTube", "യൂട്യൂബ്": "YouTube", "ইউটিউব": "YouTube",
    "યુટ્યુબ": "YouTube", "યૂટ્યૂબ": "YouTube", "ਯੂਟਿਊਬ": "YouTube",
    "spotify": "Spotify", "spotfy": "Spotify", "spotifi": "Spotify", "स्पॉटिफाई": "Spotify",
    "स्पोटिफाई": "Spotify", "ಸ್ಪಾಟಿಫೈ": "Spotify", "ஸ்பாட்டிஃபை": "Spotify", "ஸ்பாட்டிபை": "Spotify",
    "స్పాటిఫై": "Spotify", "സ്പോട്ടിഫൈ": "Spotify", "স্পটিফাই": "Spotify", "સ્પોટિફાય": "Spotify",
    "ਸਪੋਟੀਫਾਈ": "Spotify",
    "youtubemusic": "YouTube Music", "ytmusic": "YouTube Music", "jiosaavn": "JioSaavn", "saavn": "JioSaavn",
    "gaana": "Gaana", "wynk": "Wynk Music", "wynkmusic": "Wynk Music", "applemusic": "Apple Music",
    "amazonmusic": "Amazon Music", "netflix": "Netflix", "hotstar": "Hotstar", "jiohotstar": "Hotstar",
    "primevideo": "Prime Video", "jiocinema": "JioCinema",
}


def _name_key(spoken: str) -> str:
    from bolos.executor.sites import site_key

    return site_key(spoken.replace("browser", " ") if spoken.isascii() else spoken)


def browser_name(spoken: str | None) -> str | None:
    """"google chrome", "क्रोम", "Brave browser" -> the browser's name; None if it is not a browser."""
    if not spoken:
        return None
    return BROWSER_NAMES.get(_name_key(spoken)) or BROWSER_NAMES.get(spoken.strip().casefold())


def media_service(spoken: str | None) -> str | None:
    """"YouTube", "यूट्यूब", "jio saavn" -> the service's name; None if it is not a media service."""
    return MEDIA_SERVICES.get(_name_key(spoken)) if spoken else None


def _searchable_site(spoken: str | None) -> str | None:
    """A site with its own search page (bolos.executor.sites.SEARCH_URLS), as named; else None. Only those:
    "Gmail kholo aur weather batao" must not search Google for "weather Gmail"."""
    from bolos.executor.sites import SEARCH_URLS, site_key

    if not spoken:
        return None
    if service := media_service(spoken):
        if site_key(service) in SEARCH_URLS:
            return service
    return spoken if site_key(spoken) in SEARCH_URLS else None


def _browser_of(action: a.Action) -> str | None:
    if isinstance(action, a.OpenApp):
        return browser_name(action.app)
    if isinstance(action, (a.OpenWebsite, a.WebSearch)):
        return action.browser
    return None


def _service_of(action: a.Action) -> str | None:
    if isinstance(action, a.OpenApp):
        return media_service(action.app)
    if isinstance(action, a.OpenWebsite):
        return media_service(action.site)
    if isinstance(action, a.PlayMedia):
        return action.app  # "Spotify pe Radiohead chalao, phir Coldplay chalao"
    return None


def _site_of(action: a.Action) -> str | None:
    if isinstance(action, a.OpenApp):
        return _searchable_site(action.app)
    if isinstance(action, (a.OpenWebsite, a.WebSearch)):
        return _searchable_site(action.site)
    return None


def carry_over(previous: a.Action, next_action: a.Action) -> a.Action:
    """next_action with what the previous step set up filled in where the user left it unsaid:
    - a browser the previous step opened (or used) is where a website or web search opens;
    - a music or video service it opened (or played on) is where play_media without a service plays;
    - a site with a search page it opened (or searched) is where a web search without a site searches.
    What the user said in the next step always wins."""
    update: dict[str, str] = {}
    if isinstance(next_action, (a.OpenWebsite, a.WebSearch)) and not next_action.browser:
        if browser := _browser_of(previous):
            update["browser"] = browser
    if isinstance(next_action, a.PlayMedia) and not next_action.app:
        if service := _service_of(previous):
            update["app"] = service
    if isinstance(next_action, a.WebSearch) and not next_action.site:
        if site := _site_of(previous):
            update["site"] = site
    return next_action.model_copy(update=update) if update else next_action


def chain(steps: list[a.Action]) -> list[a.Action]:
    """The steps to run, in order: each with context carried from the one before it (carry_over), and an
    "open X" step folded into a next step that plays or searches on X anyway ("YouTube kholo aur Arijit ke
    gaane chalao" plays on YouTube once, instead of opening YouTube and then a second YouTube tab). A browser
    opened first stays a step of its own: the user asked for it, and the next step opens in it."""
    out: list[a.Action] = []
    for step in steps:
        if out:
            step = carry_over(out[-1], step)
            if _opens_what_uses(out[-1], step):
                out[-1] = step
                continue
        out.append(step)
    return out


def _opens_what_uses(opened: a.Action, step: a.Action) -> bool:
    if isinstance(opened, a.OpenApp):
        name = opened.app
    elif isinstance(opened, a.OpenWebsite):
        name = opened.site
    else:
        return False
    if browser_name(name):
        return False
    used = step.app if isinstance(step, a.PlayMedia) else step.site if isinstance(step, a.WebSearch) else None
    return bool(used) and _same_name(name, used)


def _same_name(x: str, y: str) -> bool:
    """Two spoken names of the same app, site or service ("YouTube", "youtube", "यूट्यूब")."""
    def canon(name: str) -> str:
        return (media_service(name) or browser_name(name) or _name_key(name)).casefold()

    return canon(x) == canon(y)
