"""Every app on this PC, found by the name the user says.

The catalog is the Start menu (desktop programs and Store apps alike, via Get-StartApps), cached on disk so
the first command doesn't wait for PowerShell, and refreshed in the background. Spoken names are matched
loosely: informal names ("vs code", "files"), speech slips ("power point", "what's app"), native script
("क्रोम") and the names this user taught it (see bolos.preferences).
"""

import difflib
import json
import logging
import os
import re
import subprocess
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


class AppNotFound(Exception):
    pass


@dataclass(frozen=True)
class App:
    name: str  # as shown in the Start menu
    app_id: str  # launched through shell:AppsFolder


@dataclass(frozen=True)
class Match:
    app: App
    score: float  # 1.0 exact or alias; PREFIX_SCORE when the spoken words start the name; lower if fuzzy


# Spoken names that don't resemble the Start menu name, mapped to candidates in order of preference.
ALIASES: dict[str, tuple[str, ...]] = {
    "chrome": ("Google Chrome",), "google": ("Google Chrome",), "browser": ("Google Chrome", "Microsoft Edge"),
    "internet": ("Google Chrome", "Microsoft Edge"), "edge": ("Microsoft Edge",),
    "vs code": ("Visual Studio Code",), "vscode": ("Visual Studio Code",), "code": ("Visual Studio Code",),
    "visual studio code": ("Visual Studio Code",),
    "files": ("File Explorer",), "file": ("File Explorer",), "file manager": ("File Explorer",),
    "my computer": ("File Explorer",), "this pc": ("File Explorer",), "folder": ("File Explorer",),
    "folders": ("File Explorer",), "explorer": ("File Explorer",), "downloads": ("File Explorer",),
    "cmd": ("Command Prompt",), "command line": ("Command Prompt", "Terminal"), "terminal": ("Terminal",),
    "calc": ("Calculator",), "calculater": ("Calculator",), "hisab": ("Calculator",),
    "ppt": ("PowerPoint",), "power point": ("PowerPoint",), "ms word": ("Word",), "microsoft word": ("Word",),
    "ms excel": ("Excel",), "microsoft excel": ("Excel",), "teams": ("Microsoft Teams",),
    "store": ("Microsoft Store",), "play store": ("Microsoft Store",), "app store": ("Microsoft Store",),
    "gallery": ("Photos",), "photo": ("Photos",), "pictures": ("Photos",), "images": ("Photos",),
    "alarm": ("Clock",), "alarms": ("Clock",), "timer": ("Clock",), "stopwatch": ("Clock",),
    "recorder": ("Sound Recorder",), "voice recorder": ("Sound Recorder",), "screenshot": ("Snipping Tool",),
    "snipping": ("Snipping Tool",), "notes": ("Sticky Notes", "OneNote", "Notepad"),
    "setting": ("Settings",), "control": ("Control Panel",), "mail": ("Outlook",), "email": ("Outlook",),
    "music": ("Spotify", "Media Player"), "video player": ("Media Player",), "player": ("Media Player",),
    "keyboard": ("On-Screen Keyboard",), "screen keyboard": ("On-Screen Keyboard",),
    "zoom in": ("Magnifier",), "python": ("IDLE (Python 3.13)", "Python 3.13"),
    "what's app": ("WhatsApp",), "what is app": ("WhatsApp",), "whats app": ("WhatsApp",), "watsapp": ("WhatsApp",),
    # Native script, for typed commands (Prisma transliterates speech to Latin script).
    "क्रोम": ("Google Chrome",), "व्हाट्सएप": ("WhatsApp",), "व्हाट्सऐप": ("WhatsApp",), "नोटपैड": ("Notepad",),
    "कैलकुलेटर": ("Calculator",), "कैमरा": ("Camera",), "सेटिंग्स": ("Settings",), "स्पॉटिफाई": ("Spotify",),
    "ಕ್ರೋಮ್": ("Google Chrome",), "ವಾಟ್ಸಾಪ್": ("WhatsApp",), "ಕ್ಯಾಲ್ಕುಲೇಟರ್": ("Calculator",), "ಕ್ಯಾಮೆರಾ": ("Camera",),
    "குரோம்": ("Google Chrome",), "வாட்ஸ்அப்": ("WhatsApp",), "கால்குலேட்டர்": ("Calculator",), "கேமரா": ("Camera",),
    "క్రోమ్": ("Google Chrome",), "వాట్సాప్": ("WhatsApp",), "కాలిక్యులేటర్": ("Calculator",),
}

# Start menu entries nobody means when they say an app's name: uninstallers, documents and links.
_NOT_AN_APP = re.compile(r"\b(uninstall|faq|readme|release notes|documentation|samples|website|web site|"
                         r"wiki|howto|learn more|troubleshooter)\b", re.I)
_DOCUMENT_ID = re.compile(r"\.(chm|url|txt|pdf|html?|rtf)$", re.I)
# Admin and system tools are opened only by their name or an alias, never by a loose match: "office" must not
# open "Office Language Preferences", nor "computer" "Computer Management".
_ADMIN_TOOL = re.compile(r"\b(management|preferences|administrative|diagnostic|editor|services|policy|"
                         r"configuration|odbc|verifier|defragment|recovery|scheduler|monitor|viewer|initiator|"
                         r"firewall|registry)\b", re.I)
_FILLER = re.compile(r"\b(app|apps|application|program|software|wala|wali|vala|the|my|microsoft)\b")
_NON_WORD = re.compile("[^\\w\\s\u0900-\u0963\u0966-\u0dff]")  # keeps Indic vowel signs and viramas

FUZZY_CUTOFF = 0.86  # "spotfy" -> Spotify (0.92), but not "steam" -> Teams (0.8) or "photoshop" -> Photos
FUZZY_LENGTH_GAP = 2  # and the names must be about as long: "whatsapp web" is not "whatsapp"
PREFIX_SCORE = 0.9
PREFIX_SHARE = 0.6  # a spoken word must cover most of the name word it starts: "obs" is not "obsidian"


def key(name: str) -> str:
    """Comparable form of an app name: NFC, casefold, no punctuation or filler words."""
    text = _NON_WORD.sub(" ", unicodedata.normalize("NFC", name).casefold())
    return " ".join(_FILLER.sub(" ", text).split()) or " ".join(text.split())


def _compact(name: str) -> str:
    return key(name).replace(" ", "")


def _alias_key(spoken: str) -> str:
    """Aliases are matched on the whole phrase, filler words included ("my computer", "what's app")."""
    return " ".join(unicodedata.normalize("NFC", spoken).casefold().split())


def is_app(app: "App") -> bool:
    return not _NOT_AN_APP.search(app.name) and not _DOCUMENT_ID.search(app.app_id)


class Catalog:
    def __init__(self, cache_path: Path | None = None, apps: list[App] | None = None):
        self.cache_path = cache_path
        self._apps: list[App] = apps or []
        self._lock = threading.Lock()
        self._listed_once = apps is not None
        if apps is None:
            self._apps = self._load_cache()
            threading.Thread(target=self.refresh, daemon=True).start()

    @property
    def apps(self) -> list[App]:
        with self._lock:
            if self._apps or self._listed_once:
                return self._apps
        self.refresh()  # first run with nothing cached: wait once; later failures retry in the background
        return self._apps

    def names(self) -> list[str]:
        return [a.name for a in self.apps if is_app(a)]

    def _load_cache(self) -> list[App]:
        try:
            return [App(**a) for a in json.loads(self.cache_path.read_text(encoding="utf-8"))]
        except (OSError, ValueError, TypeError, AttributeError):
            return []

    def refresh(self) -> None:
        try:
            apps = start_menu_apps()
        except (OSError, subprocess.SubprocessError, ValueError):
            log.warning("Could not list Start menu apps", exc_info=True)
            return
        finally:
            self._listed_once = True
        with self._lock:
            self._apps = apps
        if self.cache_path:
            try:
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                self.cache_path.write_text(json.dumps([a.__dict__ for a in apps], ensure_ascii=False),
                                           encoding="utf-8")
            except OSError:
                log.warning("Could not cache the app list", exc_info=True)

    def find(self, spoken: str, learned: dict[str, str] | None = None) -> Match | None:
        """The installed app the user most likely means, or None."""
        by_key: dict[str, App] = {}
        for app in self.apps:
            if is_app(app):
                by_key.setdefault(key(app.name), app)
        k = key(spoken)
        if not k:
            return None
        # What the user said exactly beats anything remembered: a name learned from an old loose match must
        # not hide an app installed later under that very name.
        if k in by_key:
            return Match(by_key[k], 1.0)
        compact = k.replace(" ", "")
        for app_key, app in by_key.items():
            if app_key.replace(" ", "") == compact:
                return Match(app, 1.0)
        for wanted in (*ALIASES.get(_alias_key(spoken), ()), *ALIASES.get(k, ()), (learned or {}).get(k)):
            if wanted and (hit := by_key.get(key(wanted))):
                return Match(hit, 1.0)
        loose = {app_key: app for app_key, app in by_key.items() if not _ADMIN_TOOL.search(app.name)}
        # "zoom" -> "Zoom Workplace", "visual studio" -> "Visual Studio Code": the spoken words start
        # words of the name. Prefer the shortest name (the main app, not "... Installer").
        words = k.split()
        prefixed = [(len(app_key), app) for app_key, app in loose.items() if _words_prefix(words, app_key.split())]
        if prefixed:
            return Match(min(prefixed, key=lambda p: p[0])[1], PREFIX_SCORE)
        best, best_score = None, 0.0
        for app_key, app in loose.items():
            if abs(len(app_key.replace(" ", "")) - len(compact)) > FUZZY_LENGTH_GAP:
                continue
            score = max(difflib.SequenceMatcher(None, compact, app_key.replace(" ", "")).ratio(),
                        difflib.SequenceMatcher(None, k, app_key).ratio())
            if score > best_score:
                best, best_score = app, score
        return Match(best, best_score) if best and best_score >= FUZZY_CUTOFF else None


def _words_prefix(spoken: list[str], name: list[str]) -> bool:
    """The spoken words start consecutive words of the name, anywhere in it ("powershell" ->
    "Windows PowerShell"), each covering most of its word."""
    if not spoken or len(spoken) > len(name) or len("".join(spoken)) < 3:
        return False
    for i in range(len(name) - len(spoken) + 1):
        if all(n.startswith(s) and len(s) >= PREFIX_SHARE * len(n)
               for s, n in zip(spoken, name[i:i + len(spoken)], strict=True)):
            return True
    return False


def start_menu_apps() -> list[App]:
    """Every Start menu entry: name and AppID."""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-StartApps | ConvertTo-Json -Compress"],
        capture_output=True, text=True, encoding="utf-8", check=True, timeout=60,
    ).stdout
    apps = json.loads(out) if out.strip() else []
    if isinstance(apps, dict):
        apps = [apps]
    return [App(a["Name"], a["AppID"]) for a in apps if a.get("Name") and a.get("AppID")]


def launch(app: App) -> None:
    if app.app_id.lower().endswith(".exe") and os.path.isabs(app.app_id):
        os.startfile(app.app_id)
    else:
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app.app_id}"])


# Browsers a website or web search can be opened in by name (a multi-step command's earlier step opened it):
# the exe's App Paths name, and where installers put it under Program Files / Program Files (x86) /
# LocalAppData when App Paths has no entry.
BROWSERS: dict[str, tuple[str, tuple[str, ...]]] = {
    "chrome": ("chrome.exe", (r"Google\Chrome\Application\chrome.exe",)),
    "edge": ("msedge.exe", (r"Microsoft\Edge\Application\msedge.exe",)),
    "firefox": ("firefox.exe", (r"Mozilla Firefox\firefox.exe",)),
    "brave": ("brave.exe", (r"BraveSoftware\Brave-Browser\Application\brave.exe",)),
    "opera": ("opera.exe", (r"Programs\Opera\opera.exe", r"Programs\Opera\launcher.exe", r"Opera\opera.exe",
                            r"Opera\launcher.exe")),
}
_INSTALL_ROOTS = ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "LOCALAPPDATA")


def _app_path(exe: str) -> str | None:
    """The registered location of exe (HKEY_CURRENT_USER first, then the machine), from App Paths."""
    import winreg

    sub = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, sub) as k:
                value = winreg.QueryValue(k, None)
        except OSError:
            continue
        if value:
            return value.strip().strip('"')
    return None


def browser_exe(browser: str, catalog: "Catalog | None" = None) -> str | None:
    """Path of the browser's exe ("Chrome", "Edge", "Firefox", "Brave", "Opera"), or None if not installed.
    Looks in App Paths, then the usual install folders, then a Start menu entry that is the exe itself."""
    entry = BROWSERS.get(browser.strip().casefold())
    if entry is None:
        return None
    exe, relative = entry
    if (path := _app_path(exe)) and os.path.isfile(path):
        return path
    for root in _INSTALL_ROOTS:
        base = os.environ.get(root)
        for rel in relative if base else ():
            if os.path.isfile(path := os.path.join(base, rel)):
                return path
    for app in catalog.apps if catalog else ():
        if os.path.isabs(app.app_id) and os.path.basename(app.app_id).casefold() == exe:
            return app.app_id
    return None


def open_in_browser(browser: str, url: str, catalog: "Catalog | None" = None) -> bool:
    """Open url in that browser (a new tab when it is already running). False if the browser isn't
    installed; the caller then uses the default browser."""
    if path := browser_exe(browser, catalog):
        subprocess.Popen([path, url])
        return True
    if browser.strip().casefold() == "edge":  # always present on Windows, through its URL protocol
        os.startfile(f"microsoft-edge:{url}")
        return True
    log.info("Browser %s not found, using the default one", browser)
    return False

