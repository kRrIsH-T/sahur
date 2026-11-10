"""Turns a validated Action into effects on Windows. Nothing else touches the OS."""

from dataclasses import dataclass, field

from bolos import actions as a
from bolos.contacts import ContactBook


@dataclass
class Outcome:
    ok: bool
    key: str  # response template key, see bolos.responses
    detail: dict = field(default_factory=dict)


def foreground_window() -> int:
    import win32gui

    return win32gui.GetForegroundWindow()


class Executor:
    def __init__(self, contacts: ContactBook, catalog=None, preferences=None):
        from bolos.executor.apps import Catalog

        self.contacts = contacts
        self.catalog = catalog or Catalog()
        self.preferences = preferences
        self.window_when_heard: int | None = None
        self._opened_window = False

    def begin(self) -> None:
        """Called when a command is heard: typing and clicking must land in the window the user was
        looking at, not one that came to the front while the command was being understood."""
        self.window_when_heard = foreground_window()
        self._opened_window = False

    def execute(self, action: a.Action) -> Outcome:
        # Imported lazily so the decision side (and its tests) don't need a Windows desktop.
        from bolos.executor import media as media_sessions
        from bolos.executor import play, sites, uia, whatsapp

        if (isinstance(action, (a.TypeText, a.Click)) and (self._opened_window or (
                self.window_when_heard is not None and foreground_window() != self.window_when_heard))):
            return Outcome(False, "focus_changed", {})
        match action:
            case a.OpenApp(app=app):
                outcome = self.open_app(app)
                self._opened_window = outcome.ok
                return outcome
            case a.OpenWebsite(site=site, browser=browser):
                outcome = self.open_site(site, browser)
                self._opened_window = outcome.ok
                return outcome
            case a.FindFile() | a.OpenRecentDownload():
                outcome = self._open_or_find_file(action)
                self._opened_window = outcome.ok
                return outcome
            case a.SendMessage(contact=name, text=text):
                contact = self.contacts.find(name)
                if contact is None:
                    return Outcome(False, "contact_not_found", {"contact": name})
                whatsapp.open_draft(contact.phone, text)
                self._opened_window = True
                return Outcome(True, "message_draft", {"contact": contact.name})
            case a.WebSearch(query=query, site=site, browser=browser):
                self.open_url(sites.search_url(site, query), browser)
                self._opened_window = True
                if site:
                    return Outcome(True, "searched_on", {"query": query, "site": site})
                return Outcome(True, "searched", {"query": query})
            case a.PlayMedia(query=query, app=app):
                outcome = play.play(query, app, self.catalog)
                self._opened_window = True
                return outcome
            case a.MediaControl(control=control):
                if media_sessions.control(control):
                    return Outcome(True, f"control_{control}", {})
                return Outcome(False, "nothing_playing", {})
            case a.TypeText(text=text):
                try:
                    uia.type_text(text)
                except uia.ProtectedControl:
                    return Outcome(False, "protected_field", {})
                return Outcome(True, "typed", {})
            case a.Click(target=target):
                try:
                    label = uia.click(target)
                    self._opened_window = True
                    return Outcome(True, "clicked", {"target": label})
                except uia.ControlNotFound:
                    return Outcome(False, "control_not_found", {"target": target})
            case a.ReadScreen():
                from bolos.guide.screen import user_window

                hwnd = user_window()
                try:
                    text = uia.screen_text(hwnd=hwnd)
                except Exception:
                    text = ""  # still allow visual description when Windows cannot provide text
                return Outcome(True, "screen", {"text": text, "hwnd": hwnd})
            case a.Cancel():
                return Outcome(True, "cancelled", {})
            case _:
                return Outcome(False, "not_understood", {})

    def _open_or_find_file(self, action: a.FindFile | a.OpenRecentDownload) -> Outcome:
        from bolos.executor import files

        recent = isinstance(action, a.OpenRecentDownload)
        result = files.latest_download() if recent else files.find_file(action.query)
        if result.status != "found" or result.path is None:
            key = {
                "not_found": "download_not_found" if recent else "file_not_found",
                "ambiguous": "file_ambiguous",
                "pending": "download_pending",
                "invalid": "file_name_only",
                "unsafe": "file_unavailable" if recent else "file_name_only",
            }.get(result.status, "file_unavailable")
            return Outcome(False, key)
        should_open = recent or action.open
        if should_open and files.open_file(result.path):
            return Outcome(True, "download_opened" if recent else "file_opened")
        if files.reveal_file(result.path):
            return Outcome(True, "file_open_manually" if should_open else "file_found")
        return Outcome(False, "file_unavailable")

    def open_app(self, spoken: str) -> Outcome:
        """An installed app; else a website of that name ("Netflix kholo" without the app)."""
        from bolos.executor import apps, play, sites

        learned = self.preferences.app_names if self.preferences else None
        match = self.catalog.find(spoken, learned)
        if match is not None:
            if match.app.name == "Spotify":  # started so that "play X" can later press its buttons
                from bolos.executor import spotify

                try:
                    spotify.open_app()
                except spotify.SpotifyUnavailable:
                    apps.launch(match.app)
            else:
                apps.launch(match.app)
            # A word-prefix match that worked ("visual studio" -> Visual Studio Code) is remembered under the
            # user's name; a fuzzy spelling match is not, as it is the likeliest to be wrong.
            if match.score == apps.PREFIX_SCORE and self.preferences:
                self.preferences.learn_app_name(apps.key(spoken), match.app.name)
            return Outcome(True, "opened", {"app": match.app.name})
        if sites.known(spoken):
            play.open_url(sites.url_for(spoken))
            return Outcome(True, "opened_site", {"site": spoken})
        return Outcome(False, "app_not_found", {"app": spoken})

    def open_site(self, spoken: str, browser: str | None = None) -> Outcome:
        from bolos.executor import sites

        self.open_url(sites.url_for(spoken), browser)
        return Outcome(True, "opened_site", {"site": spoken})

    def open_url(self, url: str, browser: str | None = None) -> None:
        """In the browser an earlier step of the command opened, if any and installed; else the default."""
        from bolos.executor import apps, play

        if browser and apps.open_in_browser(browser, url, self.catalog):
            return
        play.open_url(url)
