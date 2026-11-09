"""Playing music and video on the service the user names (or usually uses).

- YouTube: the first video for the query, in the browser.
- Spotify: the desktop app's search, then the top result is started; what is playing is read back from
  Windows' media session, so the reply names the song that really started (or says it didn't).
- Other services (JioSaavn, Gaana, YouTube Music, ...): their search page in the browser.
- An app the service name matches on this PC (VLC, Media Player, ...): opened.
"""

import logging
import re
import webbrowser
from urllib.parse import quote, quote_plus

import requests

from bolos.executor import Outcome, apps, media, sites

log = logging.getLogger(__name__)

_VIDEO_ID = re.compile(r'"videoId":"([A-Za-z0-9_-]{11})"')

SERVICE_ALIASES = {"yt": "youtube", "you tube": "youtube", "yt music": "youtubemusic", "saavn": "jiosaavn",
                   "jio saavn": "jiosaavn", "wynk music": "wynk", "apple": "applemusic", "amazon music": "amazonmusic",
                   "prime music": "amazonmusic", "spotify app": "spotify", "spotify music": "spotify",
                   "spotfy": "spotify", "spotifi": "spotify", "amazon prime": "primevideo", "prime": "primevideo",
                   "prime video": "primevideo", "amazon": "primevideo", "jio hotstar": "hotstar",
                   "disney hotstar": "hotstar", "jio cinema": "jiocinema"}
WEB_SEARCH = {
    "youtubemusic": ("YouTube Music", "https://music.youtube.com/search?q={q}", "https://music.youtube.com"),
    "jiosaavn": ("JioSaavn", "https://www.jiosaavn.com/search/{path}", "https://www.jiosaavn.com"),
    "gaana": ("Gaana", "https://gaana.com/search/{path}", "https://gaana.com"),
    "wynk": ("Wynk Music", "https://wynk.in/music/search?q={q}", "https://wynk.in/music"),
    "applemusic": ("Apple Music", "https://music.apple.com/in/search?term={q}", "https://music.apple.com"),
    "amazonmusic": ("Amazon Music", "https://music.amazon.in/search/{path}", "https://music.amazon.in"),
}


def open_url(url: str) -> None:
    webbrowser.open(url)


def service_key(app: str | None) -> str:
    if not app:
        return "youtube"
    k = " ".join(apps.key(app).split())
    return SERVICE_ALIASES.get(k, k.replace(" ", ""))


def play(query: str, app: str | None, catalog) -> Outcome:
    service = service_key(app)
    if service == "youtube":
        return play_youtube(query)
    if service == "spotify":
        return play_spotify(query, catalog)
    if service in WEB_SEARCH:
        name, search, home = WEB_SEARCH[service]
        open_url(search.format(q=quote_plus(query), path=quote(query, safe="")) if query else home)
        return Outcome(True, "searched_on", {"query": query, "site": name}) if query \
            else Outcome(True, "opened_site", {"site": name})
    if query and service in sites.SEARCH_URLS:  # Netflix, Hotstar, Prime Video, ...: their own search
        open_url(sites.search_url(service, query))
        return Outcome(True, "searched_on", {"query": query, "site": app})
    match = catalog.find(app) if app else None
    if match is not None and match.app.name == "Spotify":  # "music app", a misheard "Spotify"
        return play_spotify(query, catalog)
    if match is not None:  # a media app on this PC that we can't drive: at least open it
        apps.launch(match.app)
        return Outcome(True, "opened", {"app": match.app.name})
    return play_youtube(query)


def youtube_results_url(query: str) -> str:
    return f"https://www.youtube.com/results?search_query={quote_plus(query)}"


def first_youtube_video(query: str) -> str | None:
    """The first video on the YouTube results page, or None if it can't be read."""
    try:
        html = requests.get(
            youtube_results_url(query), timeout=8,
            headers={"Accept-Language": "en-IN,en;q=0.9", "User-Agent": "Mozilla/5.0"},
        ).text
    except requests.RequestException:
        return None
    match = _VIDEO_ID.search(html)
    return f"https://www.youtube.com/watch?v={match.group(1)}" if match else None


def play_youtube(query: str) -> Outcome:
    if not query:
        open_url("https://www.youtube.com")
        return Outcome(True, "opened_site", {"site": "YouTube"})
    open_url(first_youtube_video(query) or youtube_results_url(query))
    return Outcome(True, "playing_on", {"query": query, "app": "YouTube"})


def play_spotify(query: str, catalog) -> Outcome:
    from bolos.executor import spotify

    before = media.now_playing("spotify")
    try:
        if not query:  # "Spotify pe gaane chalao": pick up where the user left off
            spotify.ensure_running()
            media.wait_for_session("spotify")  # a just-started Spotify registers it a moment later
            media.control("resume", "spotify")
        else:
            spotify.play_top_result(query)
    except spotify.SpotifyUnavailable as e:
        log.warning("Spotify: %s", e)
        if query:  # at least show the results
            open_url(f"https://open.spotify.com/search/{quote(query, safe='')}")
        return Outcome(False, "play_failed", {"app": "Spotify", "query": query or "music"})
    now = media.wait_until_playing("spotify", before if query else None)
    if now is None:
        return Outcome(False, "play_failed", {"app": "Spotify", "query": query or "music"})
    if not query:
        return Outcome(True, "playing_app", {"app": "Spotify"})
    playing = f"{now.title}, {now.artist}" if now.artist else now.title
    return Outcome(True, "playing_on", {"query": playing, "app": "Spotify"})
