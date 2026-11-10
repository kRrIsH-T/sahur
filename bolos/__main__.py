"""Entry point.

    python -m bolos              tap or hold the hotkey (needs GNANI_API_KEY)
    python -m bolos --text       type commands instead of speaking (replies are spoken if a Gnani key is set)
    python -m bolos --dry-run    show the action that would run, without touching the computer
"""

import argparse
import logging
import sys
import threading
from zipfile import BadZipFile

from bolos.config import ROOT, load_settings
from bolos.contacts import ContactBook
from bolos.executor import Executor, Outcome
from bolos.pipeline import Assistant

log = logging.getLogger("bolos")


class DryRun:
    def execute(self, action) -> Outcome:
        print(f"[dry-run] would execute {action.model_dump_json()}")
        return Outcome(True, "dry_run")


DECIDER_MODES = ("system1", "fast", "llm", "laya")


def make_parser(settings, llm, mode: str | None = None):
    """System 1 for the pipeline, or None when System 2 decides everything. "system1" and "fast" are the
    same parser; "fast" just runs without System 2."""
    mode = (mode or settings.decider).strip().lower()
    if mode not in DECIDER_MODES:
        sys.exit(f"Unknown DECIDER {mode!r}; choose one of {', '.join(DECIDER_MODES)}")
    if mode == "llm":
        if llm is None:
            sys.exit("DECIDER=llm needs System 2: the codex or claude CLI, or GEMINI_API_KEY / LLM_API_KEY in .env")
        return None
    if mode == "laya":
        from bolos.decide import LayaParser

        print("Loading the Laya decision model...")
        return LayaParser(settings.laya_model, settings.confidence_threshold)
    from bolos.decide import FastParser

    return FastParser(settings.fast_model, settings.slot_model)


def make_language_detector(parser):
    from bolos.decide import FastParser
    from bolos.fast.model import LanguageModel

    if isinstance(parser, FastParser) and parser.model.languages:
        return parser.detect_language
    try:
        return LanguageModel().detect
    except (OSError, ValueError, KeyError, TypeError, BadZipFile):
        log.info("Automatic reply language detection is unavailable. Sahur will use the language in Settings.")
        return None  # Assistant still checks the text's script and uses the selected language as a fallback.


APP_HINTS = ["WhatsApp", "Chrome", "YouTube", "Notepad", "Calculator", "File Explorer", "Settings", "Camera",
             "Word", "Excel", "Spotify"]
MAX_KEYTERMS = 200  # Prisma's limit


def recognition_hints(contacts: ContactBook, app_names: list[str] = ()) -> list[str]:
    """Words Prisma should favour: contact names, common apps, then the apps on this PC (else "Chrome
    khol do" can become "room"). Shorter installed names first, since the list is capped."""
    hints, seen = [], set()
    for name in contacts.names() + APP_HINTS + sorted(app_names, key=len):
        if name.casefold() not in seen and len(hints) < MAX_KEYTERMS:
            hints.append(name)
            seen.add(name.casefold())
    return hints


class SpokenVoice:
    """One streaming Listener for commands, automatic follow-ups, and half duplex cached replies."""

    def __init__(self, settings, contacts: ContactBook, app_names: list[str] = (), status=lambda *_: None,
                 wake_enabled: bool = False, listener=None, silent: bool = False):
        from bolos.listen import Listener

        self.user_language = settings.user_language
        self.last_heard = None
        self.wake = None
        self.silent = silent
        if wake_enabled:
            from bolos.wake import WakeDetector

            try:
                self.wake = WakeDetector.load()
            except Exception:
                log.warning("Wake word model could not be loaded; tap or hold the hotkey")
            if self.wake is None:
                log.warning("Wake word is unavailable: no trained model is installed. Tap or hold the hotkey.")
        self.listener = listener or Listener(settings, recognition_hints(contacts, app_names), status,
                                             wake=self.wake, speak_replies=not silent)

    def start(self):
        self.listener.start()
        return self

    def close(self):
        self.listener.close()

    def speak(self, text, language):
        print(f"sahur: {text}")
        return self.silent or self.listener.speak(text, language)

    def listen(self):
        return self._reply(self.listener.next_command())

    def followup(self):
        return self._reply(self.listener.followup())

    def poll(self, timeout: float):
        """A command if the user starts one within `timeout` seconds (hotkey or wake word), else None at once:
        how a running guide hears "ho gaya" or "ruko" while it watches the screen."""
        return self._reply(self.listener.next_command(timeout))

    def _reply(self, heard, private=False):
        if not private:
            self.last_heard = heard
        if heard is None or not heard.text:
            return None
        if not private:
            print(f"you ({heard.trigger}, {heard.via}): {heard.text}")
        # The pipeline's local language detector decides; Prisma's transliterated output has no code.
        return heard.text, None

    def listen_private(self, hints=()):
        """A form answer: never logged, with `hints` (likely words) favoured by recognition for this turn."""
        with self.listener.private(hints):
            return self._reply(self.listener.followup(), private=True)


class TypedVoice:
    """Reads commands from the terminal. Speaks replies with Timbre when a key is configured."""

    def __init__(self, settings, silent=False):
        self.user_language = settings.user_language
        self.device = None  # the same speaker's sounddevice index, for replies
        self.tts = None
        if settings.has_gnani and not silent:
            from bolos.tts_cache import CachedTimbre

            self.tts = CachedTimbre(settings)

    def speak(self, text, language):
        print(f"sahur: {text}")
        if self.tts:
            from bolos.audio import play_wav

            try:
                return play_wav(self.tts.synthesize(text, language), self.device)
            except Exception:  # synthesis, network, cache and missing audio devices must not end typed mode
                log.warning("Spoken reply unavailable; the reply is shown in the terminal")
                return False
        return True

    def listen(self):
        line = input("you: ").strip().lstrip("﻿")  # piped input on Windows can start with a BOM
        return (line, None) if line else None

    followup = listen

    def poll(self, timeout: float):
        """A typed line if the user has started typing one, else None after `timeout` seconds."""
        import msvcrt
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if msvcrt.kbhit():
                return self.listen()
            time.sleep(0.02)
        return None

    def listen_private(self, hints=()):
        import getpass

        if not sys.stdin.isatty():
            return None  # do not fall back to echoing sensitive values from redirected input
        try:
            text = getpass.getpass("Private answer (hidden): ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        return (text, None) if text else None

    def start(self):
        return self

    def close(self):
        pass


def main() -> None:
    cli = argparse.ArgumentParser(prog="bolos")
    cli.add_argument("--text", action="store_true", help="type commands instead of speaking")
    cli.add_argument("--dry-run", action="store_true", help="don't execute actions (and don't learn from them)")
    cli.add_argument("--no-overlay", action="store_true")
    cli.add_argument("--no-speech", action="store_true", help="keep typed-mode replies in the terminal")
    cli.add_argument("--wake-word", action="store_true", help="enable Hey Bol OS when a trained model is installed")
    cli.add_argument("--dashboard", action="store_true", help="open the settings and statistics page at start")
    cli.add_argument("--no-dashboard", action="store_true", help="do not serve the dashboard")
    args = cli.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    from bolos.controls import Controls
    from bolos.user_settings import UserSettings, env_defaults, settings_path

    # What was changed on the dashboard (settings.json) wins over .env.
    store = UserSettings.load(settings_path(), env_defaults(load_settings()))
    settings = load_settings(store.overrides())
    controls = Controls(store, dry_run=args.dry_run)
    if not args.text and not settings.has_gnani:
        message = ("Voice setup is incomplete. Add your Gnani speech key to GNANI_API_KEY in the .env file "
                   "in the Sahur folder, then start Sahur again.\nTo try typing, run uv run python -m bolos --text")
        if args.no_dashboard:
            sys.exit(message)
        from bolos.dashboard.__main__ import main as open_settings

        print(message)
        try:
            open_settings([])
        except OSError:
            sys.exit("Sahur could not open its Settings page.")
        return

    from bolos.executor.apps import Catalog
    from bolos.llm import make_llm
    from bolos.preferences import Preferences

    contacts = ContactBook.load(settings.contacts_path)
    llm = None if args.dry_run else make_llm(settings)
    if llm is None:
        log.warning("No System 2 (codex CLI or LLM key): only what System 1 is sure of will run")
    else:
        log.info("System 2: %s", type(llm).__name__)
    catalog = Catalog(ROOT / ".cache" / "apps.json")
    preferences = Preferences(settings.preferences_path)
    # Dry runs are entirely local, including when the configured decider normally asks an API.
    parser = make_parser(settings, llm, "fast" if args.dry_run else None)
    language_detector = make_language_detector(parser)
    if settings.decider.strip().lower() == "fast":
        llm = None  # System 1 only: unsure commands are not executed, and nothing goes to System 2

    overlay = None
    if not args.no_overlay and not args.text:
        from bolos.overlay import Overlay

        overlay = Overlay(settings.hotkey, click_only=True, api_model=settings.llm_model
                          if settings.llm_provider == "openai" else None)
    buddy = None
    if overlay:
        from bolos.buddy import Buddy

        buddy = Buddy(visible=bool(store.get("buddy_visible")), on_click=overlay.toggle_details,
                      on_move=overlay.move_details,
                      position_path=settings_path().with_name(".cache") / "sahur-position.json")

    def show_status(kind, value=None):
        if overlay:
            overlay.set(kind, value)
        if buddy:
            buddy.set(kind, value)

    status = controls.status(show_status)
    voice = (TypedVoice(settings, silent=args.dry_run or args.no_speech) if args.text else
             SpokenVoice(settings, contacts, catalog.names(), status,
                         wake_enabled=args.wake_word or settings.wake_enabled, silent=args.dry_run))
    from bolos.history import History, history_path

    forms = None
    if not args.dry_run:
        from bolos.forms.session import ConsentFile, FieldHighlight, FormAssistant

        consent = None if args.text else ConsentFile(settings_path().parent / ".cache" / "form-speech-consent")
        forms = FormAssistant(voice, status, highlight=FieldHighlight(buddy), consent=consent,
                              ask_consent=not args.text)
    runner = DryRun() if args.dry_run else Executor(contacts, catalog, preferences)
    guide = None
    if not args.dry_run:
        from bolos.guide import screen
        from bolos.guide.session import Guide

        # The guide opens official sites the executor's way, stops taking screenshots when a form shows typed
        # details, and hands over to the private form session when the form it led to appears.
        # Screenshots go to System 2 only while the dashboard's "Let System 2 see the screen" is on.
        guide = Guide(llm, voice, status, open_url=runner.open_url, private=forms.shows_personal_details,
                      form_ready=forms.ready_to_fill, form_visible=forms.has_fillable_form,
                      look=lambda with_image: screen.observe(with_image=with_image and controls.images_allowed()),
                      answer_look=lambda: screen.observe(with_image=controls.images_allowed(),
                                                         foreground_only=True),
                      buddy=buddy)
    history = None if args.dry_run else History(history_path(settings.preferences_path))
    assistant = Assistant(parser, llm, runner, voice, status=status, contacts=contacts, preferences=preferences,
                          learning=not args.dry_run, forms=forms, guide=guide,
                          history=history, language_detector=language_detector)
    controls.voice, controls.overlay, controls.history, controls.buddy = voice, overlay, history, buddy
    controls.apply_all()  # the saved microphone, speaker, card and history choices, before the devices open
    dashboard = None
    if not args.no_dashboard:
        from bolos.dashboard.server import Dashboard

        try:
            dashboard = Dashboard(controls, history_path(settings.preferences_path))
            print(f"Dashboard: {dashboard.start()}")
        except OSError:
            log.warning("The dashboard could not start", exc_info=True)
            dashboard = None
    stopped = threading.Event()

    def loop():
        hotkey = getattr(getattr(voice, "listener", None), "hotkey", settings.hotkey)
        instructions = "Type a command." if args.text else f"Tap or hold {hotkey} and speak."
        if getattr(voice, "wake", None):
            instructions += ' Or say "Hey Bol OS".'
        if not startup_issue:
            print(f"Ready. {instructions}")
        try:
            while not stopped.is_set():
                heard = voice.listen()
                if heard and not stopped.is_set():
                    metadata = getattr(voice, "last_heard", None)
                    assistant.handle(*heard, trigger=metadata.trigger if metadata else "typed")
        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            stopped.set()
            voice.close()
            if overlay:
                overlay.close()

    try:
        voice.start()
        startup_issue = None if args.text else voice.listener.device_issue()
        if startup_issue:
            print(startup_issue[1])
        if dashboard and (args.dashboard or startup_issue):
            import webbrowser

            try:
                webbrowser.open(dashboard.url)
            except (OSError, webbrowser.Error):
                log.warning("Open %s to use Sahur Settings", dashboard.url)
        if buddy:
            buddy.start()
            # Show the greeting only when the shortcut can actually be used.
            welcomed = settings_path().with_name(".bolos-welcomed")
            if not startup_issue and not welcomed.exists():
                controls.welcome()
                try:
                    welcomed.touch()
                except OSError:
                    log.warning("Could not remember that the hello was shown", exc_info=True)
        if overlay:
            worker = threading.Thread(target=loop, daemon=True, name="bolos-commands")
            worker.start()
            overlay.run()
        else:
            loop()
    except KeyboardInterrupt:
        pass
    finally:
        stopped.set()
        voice.close()
        if guide:
            guide.close()
        if buddy:
            buddy.close()
        if overlay:
            overlay.close()
        if dashboard:
            dashboard.close()


if __name__ == "__main__":
    main()
