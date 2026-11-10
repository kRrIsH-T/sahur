"""Replies in all 10 languages, and how short replies (yes, no, "that was wrong, do this") are understood."""

import string
import unicodedata
from pathlib import Path

import pytest

from bolos import responses as r
from bolos.responses import (
    LANGUAGES,
    TEMPLATES,
    is_correction,
    is_no,
    is_yes,
    join_replies,
    lang_key,
    say,
    script_language,
    split_correction,
)

SCRIPT_RANGES = {"hi": (0x0900, 0x097F), "mr": (0x0900, 0x097F), "bn": (0x0980, 0x09FF), "pa": (0x0A00, 0x0A7F),
                 "gu": (0x0A80, 0x0AFF), "ta": (0x0B80, 0x0BFF), "te": (0x0C00, 0x0C7F), "kn": (0x0C80, 0x0CFF),
                 "ml": (0x0D00, 0x0D7F)}
NEW_KEYS = {"one_moment", "still_thinking", "learned", "steps_stopped", "no_speech"}
LATIN_FIELDS = {"app": "Spotify", "contact": "Raju", "query": "Arijit Singh", "site": "YouTube", "target": "OK",
                "text": "main 5 minute mein aa raha hoon", "hotkey": "Ctrl+Space"}


def fields_of(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


# --- Templates -------------------------------------------------------------------------------------------

def test_all_ten_languages_have_every_reply_with_the_same_fields():
    assert set(TEMPLATES) == set(LANGUAGES) and len(LANGUAGES) == 10
    for lang, templates in TEMPLATES.items():
        assert set(templates) == set(TEMPLATES["en"]), lang
        for key, template in templates.items():
            assert fields_of(template) == fields_of(TEMPLATES["en"][key]), (lang, key)
    assert NEW_KEYS <= set(TEMPLATES["en"])


@pytest.mark.parametrize("lang", [lang for lang in LANGUAGES if lang != "en"])
def test_replies_are_written_in_the_languages_own_script(lang):
    low, high = SCRIPT_RANGES[lang]
    for key, template in TEMPLATES[lang].items():
        if key == "dry_run":  # a developer message, English everywhere
            continue
        letters = [c for c in template.format(**{f: "" for f in fields_of(template)})
                   if unicodedata.category(c)[0] in "LM"]
        assert letters and all(low <= ord(c) <= high for c in letters), (lang, key, template)


@pytest.mark.parametrize("lang", LANGUAGES)
def test_every_reply_formats_as_one_clean_spoken_sentence(lang):
    for key, template in TEMPLATES[lang].items():
        spoken = say(key, lang, **{f: LATIN_FIELDS[f] for f in fields_of(template)})
        assert "{" not in spoken and "}" not in spoken, (lang, key)
        assert spoken[-1] in ".?\u0964", (lang, key, spoken)
        assert "  " not in spoken and spoken == spoken.strip(), (lang, key, spoken)


def test_no_em_dash_anywhere():
    for path in (Path(r.__file__), Path(__file__)):
        assert "\u2014" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("lang,key,feminine,masculine", [
    ("hi", "playing", "चला रही हूँ", "रहा हूँ"),
    ("hi", "forgot", "भूल गई", "भूल गया"),
    ("hi", "play_failed", "चला पाई", "चला पाया"),
    ("hi", "one_moment", "देखती हूँ", "देखता"),
    ("hi", "learned", "रखूँगी", "रखूँगा"),
    ("mr", "playing", "लावतेय", "लावतोय"),
    ("mr", "forgot", "मी विसरले", "विसरलो"),
    ("mr", "one_moment", "बघते", "बघतो"),
    ("gu", "playing", "વગાડી રહી છું", "રહ્યો છું"),
    ("gu", "forgot", "ભૂલી ગઈ", "ભૂલી ગયો"),
    ("gu", "play_failed", "ન શકી", "ન શક્યો"),
    ("pa", "playing", "ਚਲਾ ਰਹੀ ਹਾਂ", "ਰਿਹਾ ਹਾਂ"),
    ("pa", "forgot", "ਭੁੱਲ ਗਈ", "ਭੁੱਲ ਗਿਆ"),
    ("pa", "play_failed", "ਚਲਾ ਸਕੀ", "ਚਲਾ ਸਕਿਆ"),
    ("pa", "one_moment", "ਦੇਖਦੀ ਹਾਂ", "ਦੇਖਦਾ"),
    ("pa", "learned", "ਰੱਖਾਂਗੀ", "ਰੱਖਾਂਗਾ"),
    ("hi", "guide_stopped", "रोक देती हूँ", "देता हूँ"),
    ("hi", "guide_timeout", "रुक रही हूँ", "रुक रहा"),
    ("hi", "guide_cannot", "दिखा सकती", "दिखा सकता"),
    ("hi", "guide_unavailable", "पा रही", "पा रहा"),
    ("mr", "guide_stopped", "थांबते", "थांबतो"),
    ("gu", "guide_cannot", "બતાવી શકતી", "શકતો"),
    ("pa", "guide_stopped", "ਰੁਕਦੀ ਹਾਂ", "ਰੁਕਦਾ"),
    ("pa", "guide_cannot", "ਦਿਖਾ ਸਕਦੀ", "ਦਿਖਾ ਸਕਦਾ"),
])
def test_the_female_voice_speaks_of_herself_as_female(lang, key, feminine, masculine):
    assert feminine in TEMPLATES[lang][key] and masculine not in TEMPLATES[lang][key]


@pytest.mark.parametrize("lang,expected", [
    ("hi", "Raju को मैसेज भेज दिया।"),
    ("en", "Message sent to Raju."),
    ("kn", "Raju ಅವರಿಗೆ ಮೆಸೇಜ್ ಕಳುಹಿಸಿದ್ದೇನೆ."),
    ("ta", "Raju க்கு மெசேஜ் அனுப்பிவிட்டேன்."),
    ("te", "Raju కి మెసేజ్ పంపాను."),
    ("mr", "Raju ला मेसेज पाठवला."),
    ("ml", "Raju-ന് മെസേജ് അയച്ചു."),
    ("bn", "Raju-কে মেসেজ পাঠিয়ে দিয়েছি।"),
    ("gu", "Raju ને મેસેજ મોકલી દીધો."),
    ("pa", "Raju ਨੂੰ ਮੈਸੇਜ ਭੇਜ ਦਿੱਤਾ।"),
])
def test_a_latin_name_sits_in_every_script_as_its_own_word(lang, expected):
    assert say("sent", f"{lang}-IN", contact="Raju") == expected


@pytest.mark.parametrize("lang,app,query", [
    ("hi", "YouTube", "अरिजीत सिंह"), ("ta", "Spotify", "இளையராஜா"), ("kn", "YouTube", "ಪುನೀತ್ ಹಾಡುಗಳು"),
    ("bn", "Spotify", "রবীন্দ্রসঙ্গীত"), ("en", "YouTube", "अरिजीत"), ("ml", "ஸ்பாட்டிஃபை", "Arijit"),
])
def test_fields_in_any_script_go_in_unchanged(lang, app, query):
    spoken = say("playing_on", lang, app=app, query=query)
    assert app in spoken and query in spoken


def test_field_values_are_cleaned_for_speech():
    assert say("confirm_send", "en", contact=" Raju ", text="are you coming?") == \
        "Open WhatsApp with this message for Raju: are you coming? Say yes or no."
    assert say("confirm_send", "hi", contact="Raju", text="kal milte hain.\n  bye") == \
        "Raju के लिए यह मैसेज व्हाट्सऐप में खोलूँ: kal milte hain. bye? हाँ या ना बोलिए।"
    assert say("opened", "ta", app="Notepad.") == "Notepad திறந்துவிட்டேன்."
    assert say("searched", "bn", query="আবহাওয়া।") == "আবহাওয়া সার্চ করে দিয়েছি।"
    assert say("playing", "en", query=42) == "Playing 42."


@pytest.mark.parametrize("code,expected", [
    ("ta-IN", "ta"), ("TA", "ta"), ("pa-IN", "pa"), ("ml", "ml"), ("en-IN", "en"), ("xx-YY", "hi"), (None, "hi"),
    ("", "hi"),
])
def test_lang_key(code, expected):
    assert lang_key(code) == expected


@pytest.mark.parametrize("lang", LANGUAGES)
def test_fillers_have_no_fields_so_they_can_be_cached_and_said_instantly(lang):
    for key in NEW_KEYS:
        assert not fields_of(TEMPLATES[lang][key]), (lang, key)
        assert say(key, lang) == TEMPLATES[lang][key]
    assert TEMPLATES[lang]["one_moment"] != TEMPLATES[lang]["still_thinking"]


def test_say_falls_back_to_hindi():
    assert say("one_moment", "xx") == say("one_moment", None) == TEMPLATES["hi"]["one_moment"]


# --- Joining step replies ----------------------------------------------------------------------------------

@pytest.mark.parametrize("lang,expected", [
    ("hi", "Chrome खोल दिया और Bangalore weather सर्च कर दिया।"),
    ("en", "Opened Chrome and searched for Bangalore weather."),
    ("kn", "Chrome ತೆರೆದಿದ್ದೇನೆ ಮತ್ತು Bangalore weather ಹುಡುಕಿದ್ದೇನೆ."),
    ("ta", "Chrome திறந்துவிட்டேன், அப்புறம் Bangalore weather தேடிவிட்டேன்."),
    ("te", "Chrome ఓపెన్ చేశాను, తర్వాత Bangalore weather కోసం వెతికాను."),
    ("mr", "Chrome उघडलं आणि Bangalore weather सर्च केलं."),
    ("ml", "Chrome തുറന്നു, പിന്നെ Bangalore weather സെർച്ച് ചെയ്തു."),
    ("bn", "Chrome খুলে দিয়েছি আর Bangalore weather সার্চ করে দিয়েছি।"),
    ("gu", "Chrome ખોલી દીધું અને Bangalore weather સર્ચ કરી દીધું."),
    ("pa", "Chrome ਖੋਲ੍ਹ ਦਿੱਤਾ ਅਤੇ Bangalore weather ਸਰਚ ਕਰ ਦਿੱਤਾ।"),
])
def test_two_step_replies_join_with_the_languages_and(lang, expected):
    parts = [say("opened", lang, app="Chrome"), say("searched", lang, query="Bangalore weather")]
    assert join_replies(parts, lang) == expected


@pytest.mark.parametrize("lang", LANGUAGES)
def test_three_steps_and_a_refusal(lang):
    parts = [say("opened", lang, app="Chrome"), say("playing", lang, query="lo-fi"), say("not_sent", lang)]
    joined = join_replies(parts, lang)
    assert joined.count(r._JOINERS[lang].strip(", ")) >= 1
    assert r._OPENERS[lang] not in joined  # "okay," only fits a first clause
    assert joined[-1] in ".\u0964" and joined.count(".") + joined.count("\u0964") == 1


def test_english_joins_read_as_one_sentence():
    assert join_replies(["Opened Chrome.", "Playing lo-fi on YouTube.", "Okay, I didn't send it."], "en") == \
        "Opened Chrome, playing lo-fi on YouTube and I didn't send it."
    # A name or "I" at the start of a later clause keeps its capital.
    assert join_replies(["Opened Chrome.", "Raju is not in your contacts."], "en") == \
        "Opened Chrome and Raju is not in your contacts."
    assert join_replies(["Opened Chrome.", "I couldn't find Notepad."], "en") == \
        "Opened Chrome and I couldn't find Notepad."


@pytest.mark.parametrize("lang", LANGUAGES)
def test_stopped_steps_are_said_as_their_own_sentence(lang):
    failed = say("app_not_found", lang, app="Chrome")
    stopped = say("steps_stopped", lang)
    assert join_replies([failed, stopped], lang) == f"{failed} {stopped}"
    done = say("opened", lang, app="Notepad")
    assert join_replies([done, failed, stopped], lang) == f"{join_replies([done, failed], lang)} {stopped}"


def test_join_edge_cases():
    assert join_replies([], "hi") == ""
    assert join_replies(["", "  "], "hi") == ""
    assert join_replies(["Opened Chrome."], "en") == "Opened Chrome."
    assert join_replies(["Volume up.", "Volume up."], "en") == "Volume up."
    assert join_replies(["Chrome खोल दिया।", "ठीक है, नहीं भेजा।"], "hi-IN") == "Chrome खोल दिया और नहीं भेजा।"
    assert join_replies(["Chrome खोल दिया।", "नोटपैड खोल दिया।"], None) == "Chrome खोल दिया और नोटपैड खोल दिया।"


# --- Language from script ----------------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("नोटपैड खोलो", "hi"), ("यूट्यूब पर अरिजीत के गाने चलाओ", "hi"), ("गाना रोक दो", "hi"),
    ("मम्मी को मैसेज भेजो कि मैं नहीं आ रहा", "hi"),
    ("नोटपैड उघडा", "mr"), ("स्पॉटिफायवर गाणी लाव", "mr"), ("आईला मेसेज पाठव", "mr"), ("आवाज कमी करा", "mr"),
    ("मुंबईच्या हवामानाची माहिती", "mr"), ("गूगलमध्ये शोधा", "mr"), ("ब्लूटूथ चालू कर, मला गाणी ऐकायची आहेत", "mr"),
    ("ಕ್ಯಾಲ್ಕುಲೇಟರ್ ತೆರೆ", "kn"), ("Notepad ತೆರೆ ಮತ್ತೆ meeting kal hai anta bari", "kn"),
    ("யூடியூப்ல பாட்டு போடு", "ta"), ("Notepad திற", "ta"),
    ("నోట్\u200cప్యాడ్ తెరువు", "te"), ("యూట్యూబ్ లో పాటలు పెట్టు", "te"),
    ("നോട്ട്പാഡ് തുറക്ക്", "ml"), ("യൂട്യൂബിൽ പാട്ട് വെക്ക്", "ml"),
    ("নোটপ্যাড খোলো", "bn"), ("ইউটিউবে গান চালাও", "bn"),
    ("નોટપેડ ખોલો", "gu"), ("યુટ્યુબ પર ગીત વગાડો", "gu"),
    ("ਨੋਟਪੈਡ ਖੋਲ੍ਹੋ", "pa"), ("ਯੂਟਿਊਬ ਤੇ ਗਾਣੇ ਚਲਾਓ", "pa"),
])
def test_script_language(text, expected):
    assert script_language(text) == expected


@pytest.mark.parametrize("text", ["", "Spotify", "Radiohead chalao", "open github dot com", "123 ?!", "\u0964\u0965",
                                  "०१"])
def test_script_language_says_nothing_without_native_letters(text):
    assert script_language(text) is None


def test_script_language_goes_by_the_main_script():
    assert script_language("ಕನ್ನಡ ಹಾಡು हाँ") == "kn"
    assert script_language("ਪੰਜਾਬੀ ਗਾਣਾ ਚਲਾਓ ok") == "pa"


# --- Yes and no --------------------------------------------------------------------------------------------

YES = {
    "hi": ["haan", "Haan ji, bhej do.", "हाँ भेज दो", "theek hai", "bilkul"],
    "en": ["yes", "Yeah, send it.", "okay", "sure, go ahead"],
    "kn": ["houdu", "Houdu kalsu.", "ಹೌದು ಕಳುಹಿಸು", "sari ri"],
    "ta": ["aama", "Sari anuppu.", "ஆமா அனுப்பு", "seri anuppunga"],
    "te": ["avunu", "sare andi", "అవును పంపండి", "pampu"],
    "mr": ["ho", "ho pathva", "हो पाठवा", "chalel"],
    "ml": ["athe", "athey", "sheri, ayakku", "അതെ അയക്കൂ", "ശരി"],
    "bn": ["hyan", "thik ache", "হ্যাঁ পাঠাও", "হ্যাঁ"],
    "gu": ["ha", "ha moklo", "હા મોકલો", "barabar che"],
    "pa": ["haan ji", "ਹਾਂ ਭੇਜੋ", "theek hai", "ਹਾਂਜੀ"],
}
NO = {
    "hi": ["nahi", "mat bhejo", "नहीं", "rehne do", "मत भेजो"],
    "en": ["no", "don't send it", "do not send it", "cancel", "send it later"],
    "kn": ["beda", "ಬೇಡ", "illa", "kalsu beda"],
    "ta": ["vendam", "வேண்டாம்", "illai", "anuppa vendam"],
    "te": ["vaddu", "వద్దు", "ledu", "pampu vaddu"],
    "mr": ["nako", "नको", "nahi", "pathvu nako"],
    "ml": ["venda", "വേണ്ട", "alla", "ayakkenda venda"],
    "bn": ["na", "না", "thak", "pathio na"],
    "gu": ["na", "ના", "nathi moklvu", "nahi"],
    "pa": ["nahi", "ਨਹੀਂ", "ਨਾ", "na bhejo"],
}


@pytest.mark.parametrize("lang,reply", [(lang, reply) for lang, replies in YES.items() for reply in replies])
def test_yes_in_every_language(lang, reply):
    assert is_yes(reply) and not is_no(reply)


@pytest.mark.parametrize("lang,reply", [(lang, reply) for lang, replies in NO.items() for reply in replies])
def test_no_in_every_language(lang, reply):
    assert is_no(reply) and not is_yes(reply)


@pytest.mark.parametrize("reply", ["haan nahi", "haan nahi ruko", "ho nako", "houdu beda", "aama vendam",
                                   "yes no", "ਹਾਂ ਨਹੀਂ"])
def test_a_mixed_reply_is_a_no(reply):
    assert is_no(reply) and not is_yes(reply)


@pytest.mark.parametrize("reply", ["", "hmm", "ji", "do", "Priya ko bhejo", "haan but change the message",
                                   "send it to Priya", "Raju ko message karo ki main nahi aa raha hoon kal subah"])
def test_neither_yes_nor_no(reply):
    assert not is_yes(reply)
    assert not is_no(reply) or len(reply.split()) <= 6


def test_long_replies_are_neither():
    long_reply = "haan haan haan haan haan haan haan"
    assert not is_yes(long_reply) and not is_no("nahi " * 7)


def test_native_spellings_match_however_they_are_encoded():
    nukta = "\u0917\u093c\u0932\u0924"  # ग + nukta, typed as two characters
    assert is_correction(nukta) and is_correction("\u095a\u0932\u0924")  # and the precomposed letter
    assert is_no("\u0928\u0939\u0940\u0902\u0964")  # "नहीं" and a danda
    assert is_yes("\u0cb9\u0ccc\u0ca6\u0cc1\u200c")  # a stray zero-width non-joiner


# --- Corrections -------------------------------------------------------------------------------------------

BARE = {
    "hi": ["galat tha", "Galat hai.", "ye galat hai", "गलत था", "ग़लत हो गया", "nahi, galat tha", "galat tha yaar"],
    "en": ["wrong", "Wrong.", "that was wrong", "it's wrong"],
    "kn": ["tappu", "ತಪ್ಪು", "tappu aaytu", "ತಪ್ಪಾಯ್ತು"],
    "ta": ["thappu", "தப்பு", "thappu aachu", "தப்பா போச்சு"],
    "te": ["tappu", "తప్పు", "tappu ayyindi", "అది తప్పు"],
    "mr": ["chuk", "chuk zala", "चूक झालं", "te chukicha hota"],
    "ml": ["thettu", "തെറ്റ്", "thettanu", "അത് തെറ്റാണ്"],
    "bn": ["bhul", "ভুল", "bhul hoyeche", "ভুল হয়েছে"],
    "gu": ["khotu", "ખોટું", "khotu che", "આ ખોટું છે"],
    "pa": ["galat si", "ਗਲਤ ਸੀ", "ਇਹ ਗਲਤ ਹੈ", "galat ho gaya"],
}
NEGATED = {
    "hi": ["galat nahi tha", "ye galat nahi hai", "गलत नहीं था"],
    "en": ["that was not wrong", "not wrong"],
    "kn": ["tappu alla", "ತಪ್ಪು ಅಲ್ಲ", "tappu illa"],
    "ta": ["thappu illa", "தப்பு இல்லை"],
    "te": ["tappu kaadu", "తప్పు కాదు"],
    "mr": ["chuk nahi", "चूक नाही"],
    "ml": ["thettu alla", "തെറ്റ് അല്ല"],
    "bn": ["bhul noy", "ভুল নয়", "bhul hoyni"],
    "gu": ["khotu nathi", "ખોટું નથી"],
    "pa": ["galat nahi si", "ਗਲਤ ਨਹੀਂ"],
}


@pytest.mark.parametrize("lang,reply", [(lang, reply) for lang, replies in BARE.items() for reply in replies])
def test_bare_correction_in_every_language(lang, reply):
    assert is_correction(reply)
    assert split_correction(reply) == ""


@pytest.mark.parametrize("lang,reply", [(lang, reply) for lang, replies in NEGATED.items() for reply in replies])
def test_a_negated_correction_is_none(lang, reply):
    assert not is_correction(reply)
    assert split_correction(reply) is None


CORRECTIONS = [
    # Hindi, romanised and native, with and without the pause Prisma writes as a comma.
    ("galat tha, Spotify pe chalao", "Spotify pe chalao"),
    ("galat tha Spotify pe chalao", "Spotify pe chalao"),
    ("nahi, YouTube pe", "YouTube pe"),
    ("Nahi nahi, YouTube pe chalao.", "YouTube pe chalao."),
    ("ye galat hai, Notepad kholo", "Notepad kholo"),
    ("galat tha, mera matlab YouTube pe", "YouTube pe"),
    ("nahi, mera matlab Notepad", "Notepad"),
    ("mera matlab Spotify pe", "Spotify pe"),
    ("galat tha na, YouTube pe", "YouTube pe"),
    ("galat, nahi, YouTube pe", "YouTube pe"),
    ("गलत था, Spotify पर चलाओ", "Spotify पर चलाओ"),
    ("नहीं, यूट्यूब पर", "यूट्यूब पर"),
    # English
    ("wrong, I meant Notepad", "Notepad"),
    ("Wrong I meant Notepad", "Notepad"),
    ("No, open Notepad", "open Notepad"),
    ("That was wrong. Play it on YouTube.", "Play it on YouTube."),
    ("I meant Notepad", "Notepad"),
    ("no, I said Spotify", "Spotify"),
    # Kannada
    ("ತಪ್ಪು, Spotify alli haaku", "Spotify alli haaku"),
    ("tappu aaytu, Notepad tere", "Notepad tere"),
    ("illa, YouTube alli", "YouTube alli"),
    ("ಬೇಡ, YouTube ಅಲ್ಲಿ ಹಾಕು", "YouTube ಅಲ್ಲಿ ಹಾಕು"),
    ("tappu, naanu heliddu Notepad", "Notepad"),
    # Tamil
    ("தப்பு, Notepad திற", "Notepad திற"),
    ("thappu, YouTube la podu", "YouTube la podu"),
    ("illa, Spotify la", "Spotify la"),
    ("வேண்டாம், Spotify ல போடு", "Spotify ல போடு"),
    # Telugu
    ("tappu, Spotify lo pettu", "Spotify lo pettu"),
    ("తప్పు, Notepad తెరువు", "Notepad తెరువు"),
    ("kaadu, YouTube lo", "YouTube lo"),
    ("tappu ayyindi Spotify lo pettu", "Spotify lo pettu"),
    # Marathi
    ("chuk aahe, Spotify var lav", "Spotify var lav"),
    ("चूक झालं, Notepad उघड", "Notepad उघड"),
    ("nahi, YouTube var", "YouTube var"),
    ("नको, Spotify वर लाव", "Spotify वर लाव"),
    # Malayalam
    ("thettu, Spotify il idu", "Spotify il idu"),
    ("തെറ്റ്, Notepad തുറക്ക്", "Notepad തുറക്ക്"),
    ("alla, YouTube il", "YouTube il"),
    ("venda, njan paranjathu Spotify", "Spotify"),
    # Bengali
    ("bhul, Spotify te chalao", "Spotify te chalao"),
    ("ভুল হয়েছে, Notepad খোলো", "Notepad খোলো"),
    ("na, YouTube e", "YouTube e"),
    ("না, ইউটিউবে চালাও", "ইউটিউবে চালাও"),
    # Gujarati
    ("khotu, Spotify par vagado", "Spotify par vagado"),
    ("ખોટું છે, Notepad ખોલો", "Notepad ખોલો"),
    ("na, YouTube par", "YouTube par"),
    ("ના, મારો મતલબ YouTube પર", "YouTube પર"),
    # Punjabi
    ("galat si, Spotify te chalao", "Spotify te chalao"),
    ("ਗਲਤ ਸੀ, Notepad ਖੋਲ੍ਹੋ", "Notepad ਖੋਲ੍ਹੋ"),
    ("nahi, YouTube te", "YouTube te"),
    ("ਨਹੀਂ, ਮੇਰਾ ਮਤਲਬ Spotify ਤੇ", "Spotify ਤੇ"),
]


@pytest.mark.parametrize("text,remainder", CORRECTIONS)
def test_split_correction_returns_the_command_to_do_instead(text, remainder):
    assert split_correction(text) == remainder
    assert remainder in text  # copied exactly, never rewritten
    assert not is_correction(text)  # a correction with content is not a bare undo


@pytest.mark.parametrize("text", [
    # A bare no is just a no, and so is a no with only politeness or another refusal after it.
    "nahi", "No.", "nahi nahi", "ਨਹੀਂ", "beda", "vendam", "na", "No, thanks.", "nahi, rehne do", "illa, beda",
    # A no-word without a pause opens many ordinary replies.
    "nahi pata", "no thanks", "na bhejo",
    # A wrong-word inside an ordinary command.
    "galat number pe message karo", "Raju ko bolo galat hai", "Wrong Turn movie chalao", "wrong number",
    "search wrong turn movie", "galat spelling theek karo", "bhul bhulaiyaa movie chalao",
    # Not corrections at all.
    "", "Spotify pe chalao", "Notepad kholo", "Notepad", "no-fly list search karo", "not now",
    "not wrong, play it on YouTube",
])
def test_split_correction_ignores_what_is_not_a_correction(text):
    assert split_correction(text) is None


@pytest.mark.parametrize("text", ["galat tha, rehne do", "Wrong, thanks.", "galat tha, mera matlab", "tappu, beda"])
def test_a_correction_with_nothing_to_do_is_bare(text):
    assert split_correction(text) == ""


def test_existing_correction_behaviour_is_kept():
    for reply, expected in [("galat tha", True), ("Wrong.", True), ("ye galat hai", True), ("tappu", True),
                            ("galat number pe message karo", False), ("Raju ko bolo galat hai", False), ("", False),
                            ("galat nahi tha", False), ("ye galat nahi hai", False), ("nahi, galat tha", True),
                            ("tappu illa", False), ("nahi", False)]:
        assert is_correction(reply) is expected, reply


@pytest.mark.parametrize("text,remainder", [
    ("galti ho gayi, Spotify pe chalao", "Spotify pe chalao"),
    ("arre nahi, YouTube pe", "YouTube pe"),
    ("nahi yaar, YouTube pe", "YouTube pe"),
    ("No, not that, YouTube pe", "YouTube pe"),
    ("no I meant YouTube", "YouTube"),
    ("nahi mera matlab Notepad", "Notepad"),
    ("wrong one, play it on Spotify", "play it on Spotify"),
    ("wrong, wrong, Notepad", "Notepad"),
    ("galat galat, Spotify pe", "Spotify pe"),
    ("tappu tappu, Spotify alli", "Spotify alli"),
    ("tappu, adu alla, Spotify alli haaku", "Spotify alli haaku"),
    ("athu illa, Spotify la podu", "Spotify la podu"),
    ("porapatu, Spotify lo pettu", "Spotify lo pettu"),
    ("thettu patti, YouTube il idu", "YouTube il idu"),
    ("bhul thai gai, Spotify par vagado", "Spotify par vagado"),
    ("ભૂલ થઈ ગઈ, YouTube પર", "YouTube પર"),
    ("ਗਲਤੀ ਹੋ ਗਈ, Spotify ਤੇ ਚਲਾਓ", "Spotify ਤੇ ਚਲਾਓ"),
    ("Wrong! Spotify.", "Spotify."),
    ("nahi, 5 minute baad bhejna", "5 minute baad bhejna"),
    # Naming what was wrong, then a pause.
    ("wrong song, play Believer", "play Believer"),
    ("Wrong app. Open Notepad.", "Open Notepad."),
    ("galat gaana, YouTube pe Kesariya chalao", "YouTube pe Kesariya chalao"),
    ("गलत गाना था, Spotify पर चलाओ", "Spotify पर चलाओ"),
    ("tappu haadu, Spotify alli haaku", "Spotify alli haaku"),
    ("ತಪ್ಪು ಹಾಡು, YouTube ಅಲ್ಲಿ ಹಾಕು", "YouTube ಅಲ್ಲಿ ಹಾಕು"),
    ("thappu paattu, Spotify la podu", "Spotify la podu"),
    ("tappu paata, YouTube lo pettu", "YouTube lo pettu"),
    ("bhul gaan, Spotify te chalao", "Spotify te chalao"),
    ("wrong song, I meant Believer", "Believer"),
    # How people address each other: "nahi re", "kaadu ra".
    ("nahi re, Spotify pe", "Spotify pe"),
    ("kaadu ra, YouTube lo", "YouTube lo"),
    ("illa kano, Spotify alli", "Spotify alli"),
])
def test_more_ways_people_correct(text, remainder):
    assert split_correction(text) == remainder
    assert not is_correction(text)


@pytest.mark.parametrize("text", ["wrong song", "galat gaana", "galat gaana tha", "ತಪ್ಪು ಹಾಡು", "wrong app",
                                  "thappu paattu", "galat re"])
def test_naming_what_was_wrong_is_still_a_bare_correction(text):
    assert is_correction(text) and split_correction(text) == ""


@pytest.mark.parametrize("text", ["galat app khul gaya", "galat gaana chal raha hai", "wrong song playing",
                                  "wrong video on screen", "galat gaana tha YouTube pe"])
def test_naming_what_was_wrong_without_a_pause_is_not_split(text):
    assert split_correction(text) is None


@pytest.mark.parametrize("text", ["galat hai kya", "galat hai kya?", "chuk aahe kay", "क्या ये गलत है"])
def test_asking_whether_it_was_wrong_is_not_a_correction(text):
    assert split_correction(text) is None and not is_correction(text)


@pytest.mark.parametrize("text", ["galti ho gayi", "galat tha na", "wrong one", "ભૂલ થઈ ગઈ", "porapatu jarigindi",
                                  "ತಪ್ಪಾಗೋಯ್ತು", "tappu agoytu", "galat galat"])
def test_more_bare_corrections(text):
    assert is_correction(text) and split_correction(text) == ""


def test_a_bare_correction_always_splits_to_nothing():
    samples = [t for replies in (*BARE.values(), *NEGATED.values()) for t in replies]
    samples += [t for t, _ in CORRECTIONS] + ["nahi", "galat number pe message karo", "haan", "wrong"]
    for text in samples:
        if is_correction(text):
            assert split_correction(text) == "", text


# --- The confirmation question names words the reply check understands ---------------------------------

CONFIRM_WORDS = {"hi": ("हाँ", "ना"), "en": ("yes", "no"), "kn": ("ಹೌದು", "ಬೇಡ"), "ta": ("ஆமா", "வேண்டாம்"),
                 "te": ("అవును", "వద్దు"), "mr": ("हो", "नाही"), "ml": ("അതെ", "വേണ്ട"), "bn": ("হ্যাঁ", "না"),
                 "gu": ("હા", "ના"), "pa": ("ਹਾਂ", "ਨਾ")}


@pytest.mark.parametrize("lang", LANGUAGES)
def test_the_confirmation_asks_for_words_that_are_understood(lang):
    yes, no = CONFIRM_WORDS[lang]
    question = say("confirm_send", lang, contact="Raju", text="5 minute late")
    assert yes in question and no in question
    assert is_yes(yes) and not is_no(yes)
    assert is_no(no) and not is_yes(no)


MORE_YES = ["hmm haan", "ha moklo ne", "chalo bhej do", "aaytu", "ಆಯ್ತು ಕಳುಹಿಸು", "chalse", "ok send", "ji haan"]
MORE_NO = ["baad mein", "baad mein bhejna", "bedaa", "vaddhu", "venaa", "veenda", "haan bhej do na", "ruk jao"]


@pytest.mark.parametrize("reply", MORE_YES)
def test_more_yes(reply):
    assert is_yes(reply) and not is_no(reply)


@pytest.mark.parametrize("reply", MORE_NO)
def test_more_no(reply):
    assert is_no(reply) and not is_yes(reply)


@pytest.mark.parametrize("reply", ["", "hmm", "ji", "do", "chalo", "Priya ko bhejo", "haan but change the message",
                                   "send it to Priya", "Raju ko message karo ki main nahi aa raha hoon kal subah"])
def test_not_a_yes(reply):
    assert not is_yes(reply)


@pytest.mark.parametrize("reply", ["", "hmm", "ji", "haan", "Spotify pe chalao", "kalsu"])
def test_not_a_no(reply):
    assert not is_no(reply)


@pytest.mark.parametrize("text,expected", [
    ("मी येतोय", "mr"), ("तुम्ही कुठे आहात", "mr"), ("आपण जाऊया", "mr"), ("पाणी हवं", "mr"), ("दार उघडलं", "mr"),
    ("मुझे पानी चाहिए", "hi"), ("मेरा नाम राम है", "hi"), ("ईश्वर भजन चलाओ", "hi"), ("बोलो", "hi"),
])
def test_marathi_is_told_from_hindi(text, expected):
    assert script_language(text) == expected


def test_field_values_are_never_read_as_templates():
    assert say("playing", "ta", query="{app} {0}") == "{app} {0} ப்ளே செய்கிறேன்."


@pytest.mark.parametrize("lang", LANGUAGES)
def test_a_correction_reply_joins_with_learned(lang):
    joined = join_replies([say("playing_on", lang, app="Spotify", query="Arijit"), say("learned", lang)], lang)
    assert joined.startswith(say("playing_on", lang, app="Spotify", query="Arijit").rstrip(".।"))
    assert r._OPENERS[lang] not in joined
