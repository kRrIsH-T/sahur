"""One command, start to finish: understand -> personalise -> confirm -> execute -> reply -> learn.

Understanding goes from cheapest to most expensive and stops at the first sure answer:
1. a habit: this exact command was worked out by System 2 before (bolos.preferences)
2. System 1: the local action model and slot tagger, when sure of the action and of every field
3. System 2 (the LLM), only for what System 1 is unsure of: the fields alone, or the whole command
Without System 2, System 1's own fields are still used to open, search and play; messages, typing and
clicks are never guessed.
"""

import logging
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from bolos import actions as a
from bolos.decide import Parse
from bolos.executor import Outcome
from bolos.responses import is_yes, join_replies, say, script_language, split_correction

log = logging.getLogger(__name__)
QUERY_ACTIONS = {"web_search", "play_media"}
NEVER_GUESSED = {"send_message", "type_text", "click"}


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


class Assistant:
    def __init__(self, parser: Parser | None, llm, runner: Runner, voice: Voice,
                 status: Status = lambda *_: None, contacts=None, preferences=None, learning: bool = True,
                 forms=None, history=None, guide=None, screenshot=None):
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
        self.language: str | None = None
        self._last: tuple[str, list[a.Action], str | None, str | None] | None = None
        self._announced = False

    # Understanding

    def understand(self, command: str) -> Understood | None:
        if self.preferences and (habit := self.preferences.recall(command)):
            return Understood(habit, "habit")
        parse = self.parser.parse(command) if self.parser else None
        return self._understand_parse(command, parse)

    def _understand_parse(self, command: str, parse: Parse | None) -> Understood | None:
        if parse is None or not parse.sure or (parse.action == "media_control" and not parse.control):
            return self._resolve(command)
        if parse.action == "unknown":
            return None
        if parse.action == "media_control":
            return Understood(a.MediaControl(control=parse.control), "system1")
        if not parse.unsure:
            action = _build(parse.action, parse.slots)
            return Understood(action, "system1") if action else None
        return self._fill(command, parse)

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

    def _fill(self, command: str, parse: Parse) -> Understood | None:
        """System 1 is sure of the action, not of all its fields."""
        if self.llm is not None:
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
            self.voice.speak(say("one_moment", self.language), self.language)
            self._announced = True
        self.status("state", "thinking")
        started = time.monotonic()
        try:
            return call()
        finally:
            self.status("thinking", time.monotonic() - started)

    def _plan(self, command: str, reported_language: str | None = None) -> list[Understood]:
        """Resolve every step before running any of them. A partial plan executes nothing."""
        from bolos.compound import MULTI, action_of, chain, needs_step_resolution, split
        from bolos.forms.intent import intent
        from bolos.guide.intent import is_guide_request, screen_language, takeover_goal

        # Resolve form and guide dialogues before they fall through to the general action model.
        name = intent(command)
        lesson = is_guide_request(command)
        if name and not (name == "upload_file" and lesson):
            self._detect_language(command, reported_language)
            return [Understood(a.parse_action({"action": name}), "system1")]
        if goal := takeover_goal(command):
            self._detect_language(command, reported_language)
            return [Understood(a.DriveScreen(goal=goal), "system1")]
        if screen_language(command):
            self._detect_language(command, reported_language)
            return [Understood(a.ReadScreen(), "system1")]
        if lesson:
            self._detect_language(command, reported_language)
            return [Understood(a.ShowHow(goal=command.strip()), "system1")]
        if self.preferences and (habit := self.preferences.recall(command)):
            return [Understood(habit, "habit")]
        parse = self.parser.parse(command) if self.parser else None
        if parse and parse.language and not script_language(command):
            self.language = parse.language
        self.status("language", self.language)
        if self.parser and (parts := split(command, self.parser, parse)):
            actions = [action_of(self.parser.parse(part)) for part in parts]
            if any(action is None for action in actions):
                return []
            return [Understood(action, "system1") for action in chain(actions)]
        unresolved = bool(self.parser and needs_step_resolution(command, self.parser, parse))
        if (parse and parse.action == MULTI) or unresolved:
            if not self.llm or not hasattr(self.llm, "resolve_steps"):
                return []
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
        understood = self._understand_parse(command, parse)
        return [understood] if understood else []

    def _detect_language(self, command: str, reported_language: str | None) -> None:
        """Use cues from the dialogue before falling back to the general language classifier."""
        from bolos.guide.intent import language_hint

        if reported_language:
            return
        detected = language_hint(command)
        if detected is None and (detector := getattr(type(self.parser), "detect_language", None)):
            detected = detector(self.parser, command)
        if detected:
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
            spoken = self.voice.speak(say("confirm_click", language, target=action.target), language)
        else:
            spoken = self.voice.speak(say("confirm_send", language, contact=action.contact, text=action.text), language)
        if spoken is False:
            return False  # an interrupted/failed readback cannot authorize sending or clicking
        followup = getattr(self.voice, "followup", self.voice.listen)
        reply = followup()
        self.status("reply", reply[0] if reply else "")
        return bool(reply) and is_yes(reply[0])

    def learn(self, command: str, understood: Understood) -> None:
        if not self.preferences or not self.learning:
            return
        action = understood.action
        if isinstance(action, a.PlayMedia) and action.app:
            self.preferences.note_media_app(command, action.query, action.app)
        # Only when System 2 chose the action too: a habit made from System 1's action would keep a wrong
        # one forever, past any later fix to the model.
        if understood.source == "system2":
            self.preferences.learn_habit(command, action)

    def handle(self, command: str, language: str | None, trigger: str | None = None) -> Outcome:
        outcome = self._handle(command, language, trigger)
        # A new command said in the middle of a guide ends it; it runs now, as if said on its own.
        pending = self.guide.pending if self.guide is not None else None
        if pending:
            self.guide.pending = None
            return self.handle(pending, None, trigger)
        return outcome

    def _handle(self, command: str, language: str | None, trigger: str | None = None) -> Outcome:
        self.language = (script_language(command) or language or self.language
                         or getattr(self.voice, "user_language", None) or "hi-IN")
        self._announced = False
        from bolos.forms.intent import intent

        form_intent = intent(command)
        self.status("heard", "[form request]" if form_intent else command)
        self.status("language", self.language)
        self.status("state", "understanding")
        started = time.monotonic()
        plan: list[Understood] = []
        executed: list[a.Action] = []
        outcome = Outcome(False, "error")
        correction_of = None
        try:
            if hasattr(self.runner, "begin"):
                self.runner.begin()
            correction = split_correction(command)
            if correction is not None:
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
                if fixed.action in {"unknown", "cancel"} | a.FORM_ACTIONS:
                    outcome = Outcome(False, "not_understood")
                    return self._reply(outcome, self.language)
                plan = [Understood(fixed, "system2")]
            else:
                if self.preferences:
                    self.preferences.begin_command()
                plan = self._plan(command, language)
            if not plan:
                outcome = Outcome(False, "not_understood")
                return self._reply(outcome, self.language)
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
                    outcome = self._execute(action, self.language, form_request=form_request)
                    if outcome.key == "screen":
                        question = command if len(plan) == 1 else ""
                        outcome = self._read_screen(outcome, self.language, question)
                    executed.append(action)
                    if outcome.ok:
                        # A whole compound must never become a habit for just its first/last step.
                        if not correction_of:
                            self.learn(command, Understood(action, understood.source if len(plan) == 1 else "system1"))
                    if isinstance(action, a.ReadScreen) and outcome.key in {
                        "screen", "screen_unavailable", "guide_form", "focus_changed",
                    }:
                        pass  # screen descriptions are spoken and shown by _read_screen
                    elif (action.action in a.FORM_ACTIONS and self.forms) or action.action in a.GUIDE_ACTIONS:
                        pass  # form sessions and guides already speak their own prompts and final result
                    else:
                        replies.append(say(outcome.key, self.language, **outcome.detail))
                if not outcome.ok:
                    if index < len(plan) - 1:
                        replies.append(say("steps_stopped", self.language))
                    break
            if replies:
                self.voice.speak(join_replies(replies, self.language), self.language)
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
                entry_id = self.history.record("[form request]" if private_form else command, self.language,
                                               source=plan[0].source if plan else None,
                                               steps=[step.action for step in plan], outcome=outcome.key,
                                               timings={"total": (time.monotonic() - started) * 1000},
                                               trigger=trigger, ok=outcome.ok)
            if executed and outcome.ok and all(s.action not in {"send_message", "type_text"} | a.DIALOGUE_ACTIONS
                                              for s in executed):
                self._last = (command, executed, self.language, entry_id)
            elif executed or split_correction(command) is None:
                self._last = None

    def _execute(self, action: a.Action, language: str | None, *, form_request: str | None = None) -> Outcome:
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
        if request and self.guide and not self.forms.has_fillable_form():
            from bolos.forms.intent import lookup_request

            outcome = self.guide.run(lookup_request(request), language, find_form=True)
            if outcome.key != "guide_form_ready":
                return outcome
        return self.forms.fill(language)

    def _show_how(self, action: a.ShowHow, language: str | None) -> Outcome:
        if self.guide is None:
            outcome = Outcome(False, "guide_unavailable")
            self.voice.speak(say(outcome.key, language), language)
            return outcome
        # The guide sends screenshots to System 2. A form with details already typed in stays with the local
        # form assistant, which never sends field values anywhere. (An empty box, like a chat app's message
        # box, is no reason to refuse: that check used to block every lesson.)
        if self.forms and self.forms.shows_personal_details():
            outcome = Outcome(False, "guide_form")
            self.voice.speak(say(outcome.key, language), language)
            return outcome
        outcome = self.guide.run(action.goal, language)
        if outcome.key == "guide_form_ready" and self.forms:
            # The lesson led to the form ("PAN card ke liye apply karna hai"): the private form session takes
            # over, asking for each answer and entering it only after a yes. No more screenshots from here.
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
        self.status("reply", spoken)
        self.voice.speak(spoken, language)
        return outcome

    def _reply(self, outcome: Outcome, language: str | None) -> Outcome:
        self.voice.speak(say(outcome.key, language, **outcome.detail), language)
        self.status("state", "done" if outcome.ok else outcome.key)
        return outcome


def _build(name: str, slots: dict[str, str]) -> a.Action | None:
    fields = {k: v for k, v in slots.items() if k in a.SLOT_FIELDS.get(name, []) and v}
    try:
        return a.parse_action({"action": name, **fields})
    except ValueError:
        log.info("System 1's fields don't make a valid %s", name)
        return None
