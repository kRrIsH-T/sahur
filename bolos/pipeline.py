"""One command, start to finish: understand -> personalise -> confirm -> execute -> reply -> learn.

Understanding goes from cheapest to most expensive and stops at the first sure answer:
1. a habit: this exact command was worked out by System 2 before (bolos.preferences)
2. System 1: the local action model and slot tagger, when sure of the action and of every field
3. System 2 (the LLM), only for what System 1 is unsure of: the fields alone, or the whole command
Without System 2, System 1's own fields are still used to open, search and play; messages, typing and
clicks are never guessed.

System 2 also holds the conversation (bolos.conversation, LLM.converse): it answers a question aloud instead
of opening a search, and gives a follow-up its missing words ("unke baare mein search karo"). It is asked
with the last few turns when a command points back at them, asks something, is chatter System 1 would
drop, or names an app this PC does not have.
"""

import logging
import re
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from bolos import actions as a
from bolos.conversation import Conversation, Turn, asks_for_search, needs_live_facts, refers_back
from bolos.decide import FastParser, Parse
from bolos.executor import Outcome
from bolos.responses import is_yes, join_replies, refuses_command, say, script_language, split_correction

log = logging.getLogger(__name__)
QUERY_ACTIONS = {"web_search", "play_media"}
NEVER_GUESSED = {"send_message", "type_text", "click"}
# Turns the conversation never keeps: the user's own words to someone, what is on their screen or in their
# files and forms, and a cancel, which says nothing about what comes next.
NOT_REMEMBERED = ({"send_message", "type_text", "read_screen", "drive_screen", "cancel"}
                  | a.FORM_ACTIONS | a.FILE_ACTIONS)
# What System 2 may choose when System 1 was sure of something else, or when the user only said "haan" to an
# offer: things that are said, opened, searched or played. Typing, clicking and messages need the user's own
# clear words.
HARMLESS = {"answer", "web_search", "open_app", "open_website", "play_media", "media_control", "cancel"}
MAX_COMMAND_CHARS = 4096  # keep pasted documents out of the speech-oriented slot tagger
SHORT_COMMAND_WORDS = 3  # too few for the local language detector to be trusted over the conversation


class Parser(Protocol):
    def parse(self, command: str) -> Parse: ...


class Voice(Protocol):
    def speak(self, text: str, language: str | None) -> None: ...
    def listen(self) -> tuple[str, str | None] | None: ...


class Runner(Protocol):
    def execute(self, action: a.Action) -> Outcome: ...


Status = Callable[[str, str], None]  # (field, value), e.g. ("action", "send_message")


@dataclass(frozen=True)
class Understood:
    action: a.Action
    source: str  # "habit", "system1", "system2" (it chose the action) or "system2_fields" (it only filled them)
    # False when the conversation helped System 2: "isko YouTube pe chalao" means something else next time,
    # so it must not become a habit.
    learnable: bool = True


class Assistant:
    def __init__(self, parser: Parser | None, llm, runner: Runner, voice: Voice,
                 status: Status = lambda *_: None, contacts=None, preferences=None, learning: bool = True,
                 forms=None, history=None, guide=None, screenshot=None,
                 language_detector: Callable[[str], str | None] | None = None):
        self.parser = parser  # None: System 2 decides everything (DECIDER=llm)
        self.llm = llm
        self.runner = runner
        self.voice = voice
        self.status = status
        self.contacts = contacts
        self.preferences = preferences
        self.learning = learning  # off for dry runs: nothing happened, so there is nothing to learn
        self.forms = forms
        self.history = history if learning else None
        self.guide = guide  # bolos.guide.session.Guide: "dikhao" requests
        self.screenshot = screenshot  # () -> Path | None: a picture of the screen for "screen par kya hai"
        self.language_detector = language_detector
        if self.language_detector is None and isinstance(parser, FastParser):
            self.language_detector = parser.detect_language
        self.language: str | None = None
        self._language_sure = False  # did the script (or the caller) settle this command's language?
        self._wait_language: str | None = None  # the language of "one moment", when not the command's own
        self.conversation = Conversation()  # in memory only: what a follow-up can refer to
        self._last: tuple[str, list[a.Action], str | None, str | None] | None = None
        self._announced = False

    # Understanding

    def understand(self, command: str) -> Understood | None:
        if self.preferences and (habit := self.preferences.recall(command)):
            return Understood(habit, "habit")
        parse = self.parser.parse(command) if self.parser else None
        return self._understand_parse(command, parse)

    def _understand_parse(self, command: str, parse: Parse | None, remote: bool = True) -> Understood | None:
        """`remote` False: System 2 was already asked about this command and had nothing usable."""
        if parse is None or not parse.sure or (parse.action == "media_control" and not parse.control):
            return self._resolve(command) if remote else None
        if parse.action == "unknown":
            return None
        if parse.action == "media_control":
            return Understood(a.MediaControl(control=parse.control), "system1")
        if not parse.unsure:
            action = _build(parse.action, parse.slots)
            return Understood(action, "system1") if action else None
        return self._fill(command, parse, remote)

    def _resolve(self, command: str) -> Understood | None:
        """System 2 picks the action and its fields."""
        if self.llm is None:
            return None
        from bolos.llm import SlotError

        try:
            action = self._thinking(lambda: self.llm.resolve(command))
        except SlotError as e:
            log.info("System 2 gave no usable action (%s)", type(e).__name__)
            return None
        except Exception:  # quota, network, timeout: don't act on a guess
            log.warning("System 2 unavailable")
            return None
        return None if action.action == "unknown" else Understood(action, "system2")

    def _fill(self, command: str, parse: Parse, remote: bool = True) -> Understood | None:
        """System 1 is sure of the action, not of all its fields."""
        if self.llm is not None and remote:
            from bolos.llm import SlotError

            try:
                action = self._thinking(lambda: self.llm.fill(parse.action, command, a.SLOT_FIELDS[parse.action]))
                return Understood(action, "system2_fields")
            except SlotError as e:  # e.g. "Raju ko message karo" with no message: ask again, don't guess
                log.info("Could not fill %s (%s)", parse.action, type(e).__name__)
                return None
            except Exception:
                log.warning("System 2 unavailable, using System 1's own fields")
        if parse.action in NEVER_GUESSED:
            return None
        slots = dict(parse.slots)
        if parse.action in QUERY_ACTIONS and not slots.get("query"):
            slots["query"] = command  # searching or playing the whole command still works
        action = _build(parse.action, slots)
        return Understood(action, "system1") if action else None

    def _thinking(self, call):
        """Acknowledge the wait before a remote decision; the overlay owns its running timer."""
        if not self._announced:
            language = self._wait_language or self.language
            self.voice.speak(say("one_moment", language), language)
            self._announced = True
        self.status("state", "thinking")
        started = time.monotonic()
        try:
            return call()
        finally:
            self.status("thinking", time.monotonic() - started)

    def _plan(self, command: str, known_language: str | None = None) -> list[Understood]:
        """Resolve every step before running any of them. A partial plan executes nothing."""
        if len(command) > MAX_COMMAND_CHARS:
            return []
        from bolos.compound import MULTI, action_of, chain, needs_step_resolution, split
        from bolos.file_intent import parse_file_request
        from bolos.forms.intent import intent
        from bolos.guide.intent import is_guide_request, screen_language, takeover_goal

        if refuses_command(command):
            return [Understood(a.Cancel(), "system1")]
        turns = self.conversation.recent()
        if is_yes(command) or command.strip().rstrip(".!?").casefold() == "go ahead":
            # A yes on its own cannot authorize a new action, with one exception: Sahur's last answer made
            # an offer ("aap kahein to settings khol doon"), and what it offered is harmless.
            if (turns and any(step.get("action") == "answer" for step in turns[-1].did)
                    and self.llm is not None and hasattr(self.llm, "converse")):
                return self._converse(command, turns, only=HARMLESS)
            return []
        if command.lstrip().casefold().startswith(("close ", "please close ")):
            return []  # There is no direct close command. Never turn it into opening the app.
        if file_action := parse_file_request(command):
            return [Understood(file_action, "system1")]
        # Resolve form and guide dialogues before they fall through to the general action model.
        name = intent(command)
        lesson = is_guide_request(command)
        if name and not (name == "upload_file" and lesson):
            self._apply_dialogue_language(command, known_language)
            return [Understood(a.parse_action({"action": name}), "system1")]
        if goal := takeover_goal(command):
            self._apply_dialogue_language(command, known_language)
            return [Understood(a.DriveScreen(goal=goal), "system1")]
        if screen_language(command):
            self._apply_dialogue_language(command, known_language)
            return [Understood(a.ReadScreen(), "system1")]
        if lesson:
            self._apply_dialogue_language(command, known_language)
            return [Understood(a.ShowHow(goal=command.strip()), "system1")]
        if self.preferences and (habit := self.preferences.recall(command)):
            return [Understood(habit, "habit")]
        if turns and (again := _same_lesson_again(command, turns[-1])):
            return [Understood(again, "system1")]
        parse = self.parser.parse(command) if self.parser else None
        if parse and parse.language and self.language_detector is None and not known_language:
            self.language = parse.language
            self.status("language", self.language)
        # System 2 is asked about a command at most once. When it has nothing usable, what System 1 is sure
        # of still runs, and the rest is asked again rather than guessed. (Form, guide and file requests
        # were settled above without touching it.)
        chat = self.llm is not None and hasattr(self.llm, "converse")
        asked = False
        # A follow-up: only the conversation says what "isko" or "unke baare mein" is. "Ise rok do" needs no
        # help: the music playing is the only thing it can mean.
        if chat and turns and refers_back(command) and not _is_control(parse):
            asked = True
            only = HARMLESS if parse and parse.sure and parse.action == "web_search" else None
            if steps := self._converse(command, turns, parse, only=only):
                return steps
        if self.parser and (parts := split(command, self.parser, parse)):
            actions = [action_of(self.parser.parse(part)) for part in parts]
            if any(action is None for action in actions):
                return []
            return [Understood(action, "system1") for action in chain(actions)]
        if chat and not asked and self._worth_a_conversation(command, parse):
            asked = True
            if steps := self._converse(command, turns, parse, only=HARMLESS):
                return steps
        unresolved = bool(self.parser and needs_step_resolution(command, self.parser, parse))
        if (parse and parse.action == MULTI) or unresolved:
            if asked or not self.llm or not hasattr(self.llm, "resolve_steps"):
                return []
            if chat:
                return self._converse(command, turns, parse)
            try:
                actions = self._thinking(lambda: self.llm.resolve_steps(command))
                if not actions or len(actions) > 3 or any(s.action in {"unknown", "cancel"} for s in actions):
                    return []
                return [Understood(step, "system2") for step in chain(actions)]
            except Exception:
                log.warning("Could not resolve the command's steps")
                return []
        # System 2 can find boundaries the local splitter could not understand. Keep its one-command
        # fallback for backends that implement only resolve().
        if (parse is None or not parse.sure) and self.llm and hasattr(self.llm, "resolve_steps"):
            if asked:
                return []
            if chat:
                return self._converse(command, turns, parse)
            try:
                actions = self._thinking(lambda: self.llm.resolve_steps(command))
                if not actions or len(actions) > 3 or any(s.action == "unknown" for s in actions):
                    return []
                if len(actions) > 1 and any(s.action == "cancel" for s in actions):
                    return []
                return [Understood(step, "system2") for step in chain(actions)]
            except Exception:
                log.warning("Could not resolve the command")
                return []
        understood = self._understand_parse(command, parse, remote=not asked)
        return [understood] if understood else []

    def _worth_a_conversation(self, command: str, parse: Parse | None) -> bool:
        """Is this a command System 1 is sure of, but that a spoken answer would serve better?
        - a question: System 1 only knows how to search for it. A request to search, or for live facts
          (weather, news, prices), still opens the search at once.
        - chatter System 1 drops as "unknown" ("tum kaun ho", "thank you", "brightness kam karo").
        - "open X" where this PC has no X: often a misheard question ("Are you code expert?").
        - "play" without a clear thing to play: "ek joke sunao" asks Sahur to tell one."""
        if parse is None or not parse.sure:
            return False  # an unsure command is asked about anyway
        if parse.action == "web_search":
            return not (parse.slots.get("site") or asks_for_search(command) or needs_live_facts(command))
        if parse.action == "unknown":
            return bool(_SPOKEN_WORD.search(command))
        if parse.action == "play_media":
            return "query" in parse.unsure
        if parse.action == "open_app" and not parse.unsure:
            knows_app = getattr(self.runner, "knows_app", None)
            return callable(knows_app) and not knows_app(parse.slots.get("app", ""))
        return False

    def _converse(self, command: str, turns: list[Turn], parse: Parse | None = None, *,
                  only: set[str] | None = None) -> list[Understood]:
        """System 2's steps for the command, with the conversation in front of it. Nothing when it is down,
        does not know either, or chose an action outside `only`: the caller falls back to what System 1 is
        sure of."""
        from bolos.compound import chain

        # The names of this PC's apps go along only with a request to open one, where a misheard name
        # ("Cidian kholu") can then be matched to the real app.
        app_names = getattr(self.runner, "app_names", None)
        apps = app_names() if parse and parse.action == "open_app" and callable(app_names) else ()
        try:
            heard = self._thinking(lambda: self.llm.converse(
                command, self.language, turns, language_sure=self._language_sure, apps=apps))
        except Exception as e:  # quota, network, timeout, an unusable answer: don't act on a guess
            log.warning("System 2 could not follow the conversation (%s)", type(e).__name__)
            return []
        if heard.language and not self._language_sure:
            # System 2 read the words where the local detector could not: reply in the language it heard.
            self.language = heard.language
            self.status("language", self.language)
        actions = heard.steps
        if not actions or len(actions) > 3 or any(step.action == "unknown" for step in actions):
            return []
        if len(actions) > 1 and any(step.action == "cancel" for step in actions):
            return []
        if only is not None and any(step.action not in only for step in actions):
            log.info("System 2 chose %s where only harmless actions are taken", [s.action for s in actions])
            return []
        # With the conversation's help the same words mean something else next time: never a habit.
        return [Understood(step, "system2", learnable=not turns) for step in chain(actions)]

    def _apply_dialogue_language(self, command: str, known_language: str | None) -> None:
        """Use the dialogue's language-specific wording when the transcript gives no explicit language."""
        from bolos.guide.intent import language_hint

        if not known_language and (detected := language_hint(command)):
            self.language = detected
            self.status("language", detected)

    # Acting

    def personalise(self, command: str, action: a.Action) -> a.Action:
        """Fill in what the user didn't say from what they usually do."""
        if isinstance(action, a.PlayMedia) and not action.app and self.preferences:
            return action.model_copy(update={"app": self.preferences.media_app(command, action.query)})
        return action

    def resolve_contact(self, action: a.Action) -> a.Action | None:
        """Swap the spoken name for the contact that will really receive the message, so the read-back
        names that person. None if nobody matches."""
        if action.action != "send_message" or self.contacts is None:
            return action
        contact = self.contacts.find(action.contact)
        return action.model_copy(update={"contact": contact.name}) if contact else None

    def confirm(self, action: a.Action, language: str | None) -> bool:
        if action.action not in a.RISKY_ACTIONS and not isinstance(action, a.Click):
            return True
        self.status("state", "confirming")
        if isinstance(action, a.Click):
            prompt = say("confirm_click", language, target=action.target)
            self.status("reply", prompt)
            spoken = self.voice.speak(prompt, language)
        else:
            spoken = self.voice.speak(say("confirm_send", language, contact=action.contact, text=action.text), language)
        if spoken is False:
            return False  # an interrupted or failed readback cannot authorize the action
        followup = getattr(self.voice, "followup", self.voice.listen)
        reply = followup()
        return bool(reply) and is_yes(reply[0])

    def learn(self, command: str, understood: Understood) -> None:
        if not self.preferences or not self.learning:
            return
        action = understood.action
        if isinstance(action, a.PlayMedia) and action.app:
            self.preferences.note_media_app(command, action.query, action.app)
        # Only when System 2 chose the action too: a habit made from System 1's action would keep a wrong
        # one forever, past any later fix to the model.
        if understood.source == "system2" and understood.learnable:
            self.preferences.learn_habit(command, action)

    def handle(self, command: str, language: str | None, trigger: str | None = None) -> Outcome:
        outcome = self._handle(command, language, trigger)
        # A new command said in the middle of a guide ends it; it runs now, as if said on its own.
        pending = self.guide.pending if self.guide is not None else None
        if pending:
            self.guide.pending = None
            return self.handle(pending, None, trigger)
        return outcome

    def _set_language(self, command: str, language: str | None) -> str | None:
        # Habits and corrections can reply without running the action parser.
        known_language = script_language(command) or language
        detected = self.language_detector(command) if not known_language and self.language_detector else None
        # Only the script, or a language given with the command, is certain. The local detector's guess is
        # right for most commands but not for short ones ("and Tamil Nadu?" is not Tamil), so when System 2
        # is asked anyway it reads the words itself and the guess is only its fallback.
        self._language_sure = bool(known_language)
        # "One moment" is said before System 2 has read the words. For a few words in the middle of a
        # conversation the detector's guess is the least reliable ("and Tamil Nadu?" is not Tamil), so that
        # line stays in the conversation's language; the reply itself follows the command's.
        short = len(command.split()) <= SHORT_COMMAND_WORDS
        previous = self.language if not known_language and short and self.conversation.recent() else None
        self.language = (known_language or detected or self.language
                         or getattr(self.voice, "user_language", None) or "hi-IN")
        self._wait_language = previous
        self.status("language", self.language)
        return known_language

    def _handle(self, command: str, language: str | None, trigger: str | None = None) -> Outcome:
        self._announced = False
        if len(command) > MAX_COMMAND_CHARS:
            self.status("heard", "")
            return self._reply(Outcome(False, "not_understood"), language or self.language
                               or getattr(self.voice, "user_language", None))
        from bolos.forms.intent import intent

        form_intent = intent(command)
        self.status("heard", "[form request]" if form_intent else command)
        self.status("state", "understanding")
        started = time.monotonic()
        known_language = self._set_language(command, language)
        plan: list[Understood] = []
        executed: list[a.Action] = []
        outcome = Outcome(False, "error")
        correction_of = None
        answer = ""  # only the answer is eligible for a later "type that" or "send that"
        try:
            if hasattr(self.runner, "begin"):
                self.runner.begin()
            correction = split_correction(command)
            if correction is not None and not refuses_command(command):
                previous = self._last
                if self.preferences:
                    forgot = self.preferences.forget_last() if self.learning else self.preferences.has_undo()
                else:
                    forgot = False
                if not correction:
                    outcome = Outcome(True, "forgot" if forgot else "nothing_to_forget")
                    self._last = None
                    return self._reply(outcome, self.language)
                # A correction to a compound is ambiguous: ask for the complete command, never redo a
                # random step. Personal message/dictation contents are not retained for correction.
                if not previous or len(previous[1]) != 1 or not self.llm or not hasattr(self.llm, "correct"):
                    outcome = Outcome(False, "not_understood")
                    return self._reply(outcome, self.language)
                correction_of = previous
                fixed = self._thinking(lambda: self.llm.correct(previous[0], previous[1][0], command))
                if fixed.action in {"unknown", "cancel"} | a.FORM_ACTIONS | a.FILE_ACTIONS:
                    outcome = Outcome(False, "not_understood")
                    return self._reply(outcome, self.language)
                plan = [Understood(fixed, "system2")]
            else:
                if self.preferences:
                    self.preferences.begin_command()
                plan = self._plan(command, known_language)
            if not plan:
                outcome = Outcome(False, "not_understood")
                return self._reply(outcome, self.language)
            if form_intent and plan[0].action.action in a.FILE_ACTIONS:
                form_intent = None
                self.status("heard", command)
            # Dialogues must run alone: otherwise the later step could submit or overwrite the form.
            if len(plan) > 1 and any(step.action.action in a.DIALOGUE_ACTIONS for step in plan):
                outcome = Outcome(False, "not_understood")
                return self._reply(outcome, self.language)
            self.status("decision", {"source": plan[0].source, "ms": (time.monotonic() - started) * 1000,
                                     "steps": [step.action.model_dump(exclude_none=True) for step in plan]})
            replies = []
            for index, understood in enumerate(plan):
                action = self.personalise(command, understood.action)
                resolved = self.resolve_contact(action)
                if resolved is None:
                    outcome = Outcome(False, "contact_not_found", {"contact": action.contact})
                    replies.append(say(outcome.key, self.language, **outcome.detail))
                elif not self.confirm(resolved, self.language):
                    outcome = Outcome(False, "not_sent" if isinstance(resolved, a.SendMessage) else "cancelled")
                    replies.append(say(outcome.key, self.language))
                else:
                    action = resolved
                    # "Screen par kya hai" on a form: the local form guidance, which never sends what is typed
                    # in it anywhere. Only a real form counts: any text box used to (a chat app's message box),
                    # so every screen was explained as "a form".
                    if (isinstance(action, a.ReadScreen) and self.forms
                            and (self.forms.shows_personal_details() or self.forms.ready_to_fill())):
                        action = a.ExplainForm()
                    self.status("action", action.model_dump_json())
                    self.status("state", "executing")
                    form_request = command if form_intent == "fill_form" else None
                    try:
                        outcome = self._execute(action, self.language, form_request=form_request)
                    except Exception:
                        log.warning("Could not execute %s", action.action)
                        outcome = Outcome(False, "error")
                    if outcome.key == "screen":
                        question = command if len(plan) == 1 else ""
                        outcome = self._read_screen(outcome, self.language, question)
                    executed.append(action)
                    if outcome.ok:
                        # A whole compound must never become a habit for just its first/last step.
                        if not correction_of:
                            self.learn(command, Understood(action, understood.source if len(plan) == 1
                                                           else "system1", understood.learnable))
                    if isinstance(action, a.ReadScreen) and outcome.key in {
                        "screen", "screen_unavailable", "guide_form", "focus_changed",
                    }:
                        pass  # screen descriptions are spoken and shown by _read_screen
                    elif outcome.key != "error" and (
                        (action.action in a.FORM_ACTIONS and self.forms) or action.action in a.GUIDE_ACTIONS
                    ):
                        pass  # form sessions and guides already speak their own prompts and final result
                    elif isinstance(action, a.Answer) and outcome.ok:
                        answer = action.text
                        replies.append(action.text)  # Sahur's own sentences, said as they are
                    else:
                        replies.append(say(outcome.key, self.language, **outcome.detail))
                if not outcome.ok or outcome.key == "message_draft":
                    if index < len(plan) - 1:
                        replies.append(say("steps_stopped", self.language))
                        outcome = Outcome(False, outcome.key, outcome.detail)
                    break
            if replies:
                text = join_replies(replies, self.language)
                self.status("reply", text)
                self.voice.speak(text, self.language)
            self.status("state", "done" if outcome.ok else outcome.key)
            if outcome.ok and correction_of and executed and self.learning:
                if self.preferences:
                    self.preferences.learn_correction(correction_of[0], executed[0], correction_of[2])
                if self.history and correction_of[3]:
                    self.history.mark_corrected(correction_of[3], executed)
                    if executed[0].action in {"send_message", "type_text"}:
                        # The original command might contain newly recognized private text.
                        correction_of = None
            return outcome
        except Exception:
            # Exception messages can contain a form value or dictated text; don't persist them.
            log.warning("Command failed")
            outcome = Outcome(False, "error")
            return self._reply(outcome, self.language)
        finally:
            entry_id = None
            if self.history:
                # Use the plan as well as the executed steps to redact a cancelled message, too.
                private_form = form_intent or any(s.action.action in a.FORM_ACTIONS for s in plan)
                private_file = any(s.action.action in a.FILE_ACTIONS for s in plan)
                transcript = "[file request]" if private_file else "[form request]" if private_form else command
                steps = [s.action.model_dump(exclude={"query"}) if s.action.action in a.FILE_ACTIONS else s.action
                         for s in plan]
                entry_id = self.history.record(transcript, self.language,
                                               source=plan[0].source if plan else None,
                                               steps=steps, outcome=outcome.key,
                                               timings={"total": (time.monotonic() - started) * 1000},
                                               trigger=trigger, ok=outcome.ok)
            if executed and outcome.ok and all(
                s.action not in {"cancel", "send_message", "type_text"} | a.DIALOGUE_ACTIONS | a.FILE_ACTIONS
                for s in executed
            ):
                self._last = (command, executed, self.language, entry_id)
            elif executed or split_correction(command) is None:
                self._last = None
            if executed and not form_intent and all(s.action.action not in NOT_REMEMBERED for s in plan):
                self.conversation.add(command, executed, answer)

    def _execute(self, action: a.Action, language: str | None, *, form_request: str | None = None) -> Outcome:
        if isinstance(action, a.Answer):
            return Outcome(True, "answered")  # nothing to do on the computer: the words are the answer
        if isinstance(action, a.ShowHow):
            return self._show_how(action, language)
        if isinstance(action, a.DriveScreen) and self.guide:
            return self.guide.drive(action.goal, language, lambda label: self.confirm(a.Click(target=label), language))
        if isinstance(action, a.DriveScreen) and self.learning:
            return self._reply(Outcome(False, "guide_unavailable"), language)
        if action.action in a.FORM_ACTIONS and self.forms:
            if action.action == "explain_form":
                return self.forms.explain(language)
            if action.action == "fill_form":
                return self._fill_form(language, form_request)
            if action.action == "check_site":
                return self.forms.check_site(language)
            return self.forms.upload(language)
        return self.runner.execute(action)

    def _fill_form(self, language: str | None, request: str | None) -> Outcome:
        from bolos.guide.intent import new_pan_request

        if request and self.guide and (new_pan_request(request) or not self.forms.has_fillable_form()):
            from bolos.forms.intent import lookup_request

            outcome = self.guide.run(lookup_request(request), language, find_form=True)
            return self._fill_found_form(outcome, language)
        return self.forms.fill(language)

    def _show_how(self, action: a.ShowHow, language: str | None) -> Outcome:
        from bolos.guide.intent import new_pan_request, settings_toggle_lesson, wallpaper_lesson, wants_form

        if self.guide is None:
            outcome = Outcome(False, "guide_unavailable")
            self.voice.speak(say(outcome.key, language), language)
            return outcome
        # These known destinations open locally. General lessons must still leave private windows alone.
        goal = action.goal
        if self.forms and not (new_pan_request(goal) or settings_toggle_lesson(goal) or wallpaper_lesson(goal)):
            if self.forms.shows_personal_details():
                key = "guide_form" if wants_form(goal) else "guide_switch_app"
                return self._reply(Outcome(False, key), language)
        return self._fill_found_form(self.guide.run(goal, language), language)

    def _fill_found_form(self, outcome: Outcome, language: str | None) -> Outcome:
        if outcome.key == "guide_form_ready" and self.forms:
            if expected_url := outcome.detail.get("expected_url"):
                return self.forms.fill(language, expected_url=expected_url)
            return self.forms.fill(language)
        return outcome

    def _read_screen(self, outcome: Outcome, language: str | None, question: str) -> Outcome:
        text = outcome.detail["text"]
        spoken = text
        answer = getattr(self.guide, "answer", None)
        if self.llm and callable(answer):
            if result := answer(question or "Describe the current screen", language, text,
                                expected_window=outcome.detail.get("hwnd")):
                return result
        elif self.llm:
            shot = self.screenshot() if self.screenshot else None
            try:
                if text.strip() or shot is not None:
                    spoken = self._thinking(lambda: self.llm.describe_screen(text, language, image=shot,
                                                                             question=question)) or text
            except Exception:  # reading the raw text aloud beats saying nothing
                log.warning("Could not summarise the screen")
            finally:
                if shot is not None:
                    shot.unlink(missing_ok=True)  # the screenshot exists only for this one call
        if not spoken.strip():
            outcome = Outcome(False, "screen_unavailable")
            spoken = say(outcome.key, language)
        spoken = _brief(spoken)
        self.status("reply", spoken)
        self.voice.speak(spoken, language)
        return outcome

    def _reply(self, outcome: Outcome, language: str | None) -> Outcome:
        text = say(outcome.key, language, **outcome.detail)
        self.status("reply", text)
        self.voice.speak(text, language)
        self.status("state", "done" if outcome.ok else outcome.key)
        return outcome


_SPOKEN_WORD = re.compile(r"[^\W\d_]{2,}")  # at least one real word: not silence, a cough or a lone digit


def _is_control(parse: Parse | None) -> bool:
    """A media control System 1 is sure of ("ise rok do"): it acts on what is playing, whatever came before."""
    return bool(parse and parse.sure and parse.action == "media_control" and parse.control)


def _same_lesson_again(command: str, last: Turn) -> a.ShowHow | None:
    """"Phir se dikhao" right after a lesson: the same lesson once more. Without this the words alone mean
    "play it again" to System 1."""
    from bolos.guide.intent import control

    if len(last.did) == 1 and last.did[0].get("action") == "show_how" and control(command) == "repeat":
        return a.ShowHow(goal=last.did[0]["goal"])
    return None


READBACK_MAX_CHARS = 400  # a whole window's text read aloud is minutes long and too much for one spoken reply


def _brief(text: str, limit: int = READBACK_MAX_CHARS) -> str:
    """The start of `text`, cut at a word boundary, for when the screen is read back without System 2."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "..."


def _build(name: str, slots: dict[str, str]) -> a.Action | None:
    fields = {k: v for k, v in slots.items() if k in a.SLOT_FIELDS.get(name, []) and v}
    try:
        return a.parse_action({"action": name, **fields})
    except ValueError:
        log.info("System 1's fields don't make a valid %s", name)
        return None
