import pytest
from pydantic import ValidationError

from bolos import actions as a
from bolos.contacts import Contact, ContactBook
from bolos.executor.apps import App, Catalog
from bolos.executor.play import service_key, youtube_results_url
from bolos.executor.sites import as_address, search_url, url_for
from bolos.responses import is_yes, say


def test_parse_action_validates_fields():
    assert a.parse_action({"action": "send_message", "contact": "Raju", "text": "hi"}) == \
        a.SendMessage(contact="Raju", text="hi")
    with pytest.raises(ValidationError):
        a.parse_action({"action": "send_message", "contact": "", "text": "hi"})
    with pytest.raises(ValidationError):
        a.parse_action({"action": "format_disk"})


def test_slot_actions_exist_in_schema():
    for name, fields in a.SLOT_FIELDS.items():
        assert name in a.ACTION_DESCRIPTIONS
        assert a.parse_action({"action": name, **{f: "x" for f in fields}}).action == name


BOOK = ContactBook([Contact("Raju", "919800000001", ("राजू", "ರಾಜು", "Raju bhai"))])


@pytest.mark.parametrize("spoken", ["Raju", "raju", "राजू", "ರಾಜು", "Raju bhai", "Rajuu"])
def test_contact_lookup_across_scripts(spoken):
    assert BOOK.find(spoken).phone == "919800000001"


def test_unknown_contact():
    assert BOOK.find("Suresh") is None


@pytest.mark.parametrize("reply,expected", [
    ("haan", True), ("haan bhejo", True), ("Haan ji, bhej do.", True), ("हाँ भेज दो", True), ("theek hai", True),
    ("yes", True), ("yes send it", True), ("ಹೌದು", True), ("Houdu kalsu.", True), ("ஆமா", True), ("Sari anuppu.", True),
    ("nahi", False), ("haan nahi", False), ("haan nahi ruko", False), ("मत भेजो", False), ("ಬೇಡ", False),
    ("வேண்டாம்", False), ("", False), ("hmm", False), ("pata nahi", False), ("ji", False), ("do", False),
    # Refusals and corrections that contain a yes-word must not send.
    ("don't send", False), ("do not send it", False), ("no don't send", False), ("send it later", False),
    ("Priya ko bhejo", False), ("haan but change the message", False), ("wait", False),
])
def test_is_yes(reply, expected):
    assert is_yes(reply) is expected


def test_every_reply_exists_in_every_language():
    from bolos.responses import TEMPLATES

    keys = {lang: set(t) for lang, t in TEMPLATES.items()}
    assert all(k == keys["en"] for k in keys.values()), keys


@pytest.mark.parametrize("action", [a.TypeText(text="hello"), a.Click(target="OK")])
def test_typing_and_clicking_refused_after_window_switch(monkeypatch, action):
    import bolos.executor as ex

    windows = iter([111, 222])  # heard in window 111, about to act in window 222
    monkeypatch.setattr(ex, "foreground_window", lambda: next(windows))
    executor = ex.Executor(BOOK, Catalog(apps=[App("Notepad", "notepad.exe")]))
    executor.begin()
    assert executor.execute(action).key == "focus_changed"


def test_say_falls_back_to_hindi_and_formats():
    assert say("sent", "kn-IN", contact="Raju") == "Raju ಅವರಿಗೆ ಮೆಸೇಜ್ ಕಳುಹಿಸಿದ್ದೇನೆ."
    assert say("sent", "xx-YY", contact="Raju") == "Raju को मैसेज भेज दिया।"
    assert say("sent", None, contact="Raju") == "Raju को मैसेज भेज दिया।"


def test_urls_are_encoded():
    assert search_url(None, "Bangalore weather") == "https://www.google.com/search?q=Bangalore+weather"
    assert youtube_results_url("अरिजीत").startswith("https://www.youtube.com/results?search_query=%E0")


@pytest.mark.parametrize("site,query,expected", [
    ("Amazon", "running shoes", "https://www.amazon.in/s?k=running+shoes"),
    ("YouTube", "cooking videos", "https://www.youtube.com/results?search_query=cooking+videos"),
    ("Flipkart ki", "phone", "https://www.flipkart.com/search?q=phone"),
    ("Some Blog", "recipes", "https://www.google.com/search?q=recipes+Some+Blog"),
])
def test_search_inside_a_site(site, query, expected):
    assert search_url(site, query) == expected


@pytest.mark.parametrize("spoken,expected", [
    ("github dot com", "https://www.github.com"), ("irctc.co.in", "https://irctc.co.in"),
    ("Amazon dot in", "https://www.amazon.in"), ("www.example.org/page", "https://example.org/page"),
    ("Radiohead", None), ("dot", None),
])
def test_spoken_addresses(spoken, expected):
    assert as_address(spoken) == expected


@pytest.mark.parametrize("spoken,expected", [
    ("Flipkart", "https://www.flipkart.com"), ("Amazon ki website", "https://www.amazon.in"),
    ("you tube", "https://www.youtube.com"), ("State Bank", "https://www.onlinesbi.sbi"),
])
def test_known_sites(spoken, expected):
    assert url_for(spoken) == expected


def test_unknown_site_goes_to_the_first_result():
    assert url_for("Mysore Zoo").startswith("https://duckduckgo.com/?q=%5CMysore+Zoo")


CATALOG = Catalog(apps=[App(n, f"id.{n}") for n in [
    "Google Chrome", "Visual Studio Code", "Visual Studio Code - Insiders", "Visual Studio Installer", "WhatsApp",
    "PowerPoint", "Microsoft Teams", "Microsoft Edge", "File Explorer", "Zoom Workplace", "Spotify",
    "Uninstall Spotify", "Calculator", "Media Player", "Sticky Notes", "Windows PowerShell", "Obsidian",
    "Developer PowerShell for VS 2022",
]])


@pytest.mark.parametrize("spoken,expected", [
    ("chrome", "Google Chrome"), ("Google Chrome", "Google Chrome"), ("vs code", "Visual Studio Code"),
    ("visual studio", "Visual Studio Code"), ("whats app", "WhatsApp"), ("what's app", "WhatsApp"),
    ("power point", "PowerPoint"), ("teams", "Microsoft Teams"), ("edge", "Microsoft Edge"),
    ("files", "File Explorer"), ("zoom", "Zoom Workplace"), ("Zoom wala app", "Zoom Workplace"),
    ("spotify", "Spotify"), ("calculater", "Calculator"), ("क्रोम", "Google Chrome"), ("कैलकुलेटर", "Calculator"),
    ("Spotfy", "Spotify"), ("powershell", "Windows PowerShell"), ("what is app", "WhatsApp"),
])
def test_catalog_finds_apps_by_spoken_name(spoken, expected):
    assert CATALOG.find(spoken).app.name == expected


@pytest.mark.parametrize("spoken", ["Photoshop", "Radiohead", "", "app", "obs", "steam", "vs"])
def test_catalog_does_not_invent_apps(spoken):
    assert CATALOG.find(spoken) is None


def test_catalog_uses_learned_names():
    assert CATALOG.find("mera editor") is None
    assert CATALOG.find("mera editor", {"mera editor": "Visual Studio Code"}).app.name == "Visual Studio Code"


@pytest.mark.parametrize("app,expected", [(None, "youtube"), ("Spotify", "spotify"), ("YouTube Music", "youtubemusic"),
                                          ("jio saavn", "jiosaavn"), ("You Tube", "youtube"), ("Gaana", "gaana")])
def test_service_names(app, expected):
    assert service_key(app) == expected


@pytest.mark.parametrize("target,labels,expected", [
    ("Next", ["Next >", "Back"], "next"),
    ("Sumbit", ["Submit", "Cancel"], "submit"),
    ("ok", ["OK", "Cancel"], "ok"),
    ("Delete", ["Delete all", "Cancel"], None),
    ("Sign in", ["Sign in with Google", "Sign up"], None),
    ("send", ["Resend code", "Back"], None),
])
def test_click_matching_is_strict(target, labels, expected):
    from bolos.executor.uia import _label, match_label

    assert match_label(target, [_label(x) for x in labels]) == expected


def test_whatsapp_url():
    from bolos.executor.whatsapp import send_url

    assert send_url("+91 98000-00001", "5 minute lagenge") == \
        "whatsapp://send?phone=919800000001&text=5%20minute%20lagenge"


@pytest.mark.parametrize("spoken,expected", [
    ("Photoshop", None), ("office", None), ("WhatsApp Web", None), ("My Computer", "File Explorer"),
    ("What's App", "WhatsApp"), ("Get Help", "Get Help"), ("computer", None),
])
def test_loose_matches_never_open_the_wrong_app(spoken, expected):
    catalog = Catalog(apps=[App(n, f"id.{n}") for n in [
        "Photos", "Office Language Preferences", "WhatsApp", "File Explorer", "Computer Management", "Get Help",
        "Microsoft Teams", "Spotify", "Product Help.chm"]])
    match = catalog.find(spoken)
    assert (match.app.name if match else None) == expected


def test_an_exact_name_beats_a_learned_guess():
    catalog = Catalog(apps=[App("Photos", "a"), App("Photoshop", "b")])
    assert catalog.find("photoshop", {"photoshop": "Photos"}).app.name == "Photoshop"


@pytest.mark.parametrize("spoken", ["github ಡಾಟ್ com", "github டாட் com", "github డాట్ com", "github डॉट com"])
def test_spoken_dot_in_every_script(spoken):
    assert as_address(spoken) == "https://www.github.com"


def test_slashes_in_queries_stay_in_the_query():
    assert search_url("spotify", "AC/DC").endswith("/search/AC%2FDC")


def test_video_services_search_their_own_site():
    assert service_key("Prime Video") == "primevideo" and service_key("Amazon") == "primevideo"
    assert search_url("Prime Video", "Panchayat").startswith("https://www.primevideo.com/search")
