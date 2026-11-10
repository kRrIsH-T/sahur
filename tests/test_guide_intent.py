"""Which commands start a "show me how" guide, and how the user steers one by voice."""

import pytest

from bolos.guide.intent import (
    control,
    is_guide_request,
    is_screen_question,
    language_hint,
    new_pan_request,
    settings_toggle_lesson,
    takeover_goal,
    wallpaper_lesson,
    wants_form,
)


@pytest.mark.parametrize("command", [
    "WhatsApp pe photo kaise bhejte hain, dikhao",
    "mujhe dikhao Bluetooth kaise on karte hain",
    "wallpaper kaise badalte hain dikha do",
    "Chrome mein bookmark karna sikhao",
    "mujhe screenshot lena sikha do",
    "show me how to change the wallpaper",
    "show me where to click",
    "where should I click?",
    "show me how to send a photo in WhatsApp",
    "walk me through turning on Wi-Fi",
    "teach me to make a folder",
    "step by step batao printer kaise lagate hain",
    "वॉलपेपर कैसे बदलते हैं दिखाओ",
    "मुझे फ़ोल्डर बनाना सिखाओ",
    "ವಾಲ್‌ಪೇಪರ್ ಹೇಗೆ ಬದಲಿಸೋದು ತೋರಿಸು",
    "வால்பேப்பர் எப்படி மாற்றுவது காட்டு",
    "వాల్‌పేపర్ ఎలా మార్చాలో చూపించు",
    "वॉलपेपर कसा बदलायचा दाखव",
    "ওয়ালপেপার কীভাবে বদলাতে হয় দেখাও",
    "વોલપેપર કેવી રીતે બદલવું બતાવો",
    "വാൾപേപ്പർ എങ്ങനെ മാറ്റാം കാണിക്കൂ",
    "ਵਾਲਪੇਪਰ ਕਿਵੇਂ ਬਦਲਣਾ ਹੈ ਦਿਖਾਓ",
    "Bluetooth hege on maadodu torisu",
    "wifi eppadi on pannuvadhu kaattu",
    # What Prisma really wrote down on 8 Nov (bolos history): the show/teach verb arrives misspelt.
    "folder banana shikha.",
    "folder kaise banaate hain, bikhau?",
    "folder kaise banaye dikaho",
    "Dekhako, folder kaise banate hain",
    # A how-question about a computer task is a lesson even without "dikhao".
    "Bluetooth kaise on karein?",
    "Settings mein Bluetooth kaise hota hai?",
    "wifi ka password kaise badlein",
    "screenshot kaise lete hain",
    "how do I change my wallpaper",
    # Applying for a document online.
    "PAN card ke liye apply karna hai",
    "mujhe PAN card banwana hai, madad karo",
    "PAN card online apply kaise karte hain",
    "passport ke liye online apply karna hai",
    "help me apply for a PAN card",
])
def test_guide_requests(command):
    assert is_guide_request(command)


@pytest.mark.parametrize("command", [
    "photos dikhao",  # show my photos: open them, not a lesson
    "Bangalore ka mausam dikhao",
    "biryani kaise banate hain",  # how-to without "show me": a web search
    "how to make tea",
    "kaise ho",
    "Spotify pe Radiohead chalao",
    "Raju ko message bhejo ki mujhe dikhao kaise karte hain",  # dictated text stays a message
    "Raju ko WhatsApp karo ki folder kaise banate hain",
    "likho ki sikhao",
    "type show me how to send a photo",
    "form bharne mein guide karo",  # the form assistant's request
    "",
    "Shikha ko call karo",  # a name, not "teach"
    "Lata Mangeshkar ke baare mein batao",
    "chai kaise banate hain",
    "Bluetooth on karo",  # do it, don't teach it
    "Settings kholo",
    "PAN card ka status kya hai",  # a question, not an application
    "Mausam kaisa hai",
])
def test_not_guide_requests(command):
    assert not is_guide_request(command)


@pytest.mark.parametrize("command,topic,enabled", [
    ("wifi hotspot kaise chalu karein", "hotspot", True),
    ("hot spot kesay chalu karun", "hotspot", True),
    ("hotspot kivein chalu karna", "hotspot", True),
    ("मोबाइल हॉटस्पॉट कैसे चालू करें", "hotspot", True),
    ("Wi Fi kaise on karein", "wifi", True),
    ("wifi kaise on kare", "wifi", True),
    ("वाइफाइ कैसे चालू करें", "wifi", True),
    ("Wi-Fi band kaise karein", "wifi", False),
    ("how do I turn Wi-Fi off on Windows?", "wifi", False),
    ("how do I turn Wi‑Fi on?", "wifi", True),
    ("teach me how to turn on Bluetooth", "bluetooth", True),
    ("bluetooth kaise on kare", "bluetooth", True),
    ("ब्लूटूथ ऑफ कैसे करें", "bluetooth", False),
    ("night light kaise off karun", "night_light", False),
    ("how do I turn night light back on?", "night_light", True),
])
def test_windows_toggle_lessons_recognize_the_requested_setting_and_direction(command, topic, enabled):
    assert is_guide_request(command)
    assert settings_toggle_lesson(command) == (topic, enabled)


@pytest.mark.parametrize("command", [
    "hotspot chalu karo",
    "message bhejo ki wifi hotspot kaise chalu karein",
    "how do I check whether Wi-Fi is on?",
    "how do I find mobile hotspot on Windows?",
    "how do I turn Wi-Fi on or off?",
    "how do I turn hotspot off and then back on?",
    "how do I turn on Wi-Fi and Bluetooth?",
    "how do I turn on night light automatically?",
    "how do I turn on Wi-Fi on my Android phone?",
    "mobile mein hotspot kaise chalu karein",
    "फ़ोन पर हॉटस्पॉट कैसे चालू करें",
    "hotspot ka password kaise badlein",
    "how do I turn on my router's Wi-Fi?",
    "how do I connect to a hotspot?",
    "how to turn on Wi-Fi and connect to a hotspot",
    "how do I change Bluetooth settings?",
    "Bluetooth speaker kaise pair karein",
    "biryani kaise banate hain",
])
def test_windows_toggle_lesson_does_not_guess_a_device_action_or_other_goal(command):
    assert settings_toggle_lesson(command) is None


@pytest.mark.parametrize("command", [
    "wallpaper kaise badle",
    "wall paper kaise lagau",
    "desktop background change karna sikhao",
    "वॉलपेपर कैसे बदलें",
    "வால்பேப்பர் எப்படி மாற்றுவது காட்டு",
    "show me how to change the Windows background",
    "how do I set this picture as my wallpaper?",
])
def test_windows_wallpaper_lesson_recognizes_the_desktop_background_goal(command):
    assert is_guide_request(command)
    assert wallpaper_lesson(command)


@pytest.mark.parametrize("command", [
    "change my wallpaper",
    "type wallpaper kaise badle",
    "phone ka wallpaper kaise badle",
    "how do I change my Zoom wallpaper?",
    "how do I change my lock screen wallpaper?",
    "how do I download a wallpaper?",
    "wallpaper kaise badle aur wifi kaise on kare",
])
def test_windows_wallpaper_lesson_leaves_other_goals_on_their_own_route(command):
    assert not wallpaper_lesson(command)


@pytest.mark.parametrize("command", ["how do I install an application", "Windows application kaise open karein",
                                     "how to apply dark mode", "how do I register this app"])
def test_app_lessons_are_not_requests_for_forms(command):
    assert not wants_form(command)


@pytest.mark.parametrize("command", ["show me how to apply for a PAN card", "passport ke liye apply karna hai",
                                     "show me how to fill out an application", "show me how to fill this form",
                                     "show me how to apply for college admission"])
def test_document_and_paperwork_lessons_still_allow_form_handoff(command):
    assert wants_form(command)


@pytest.mark.parametrize("command", ["open the PAN card application form", "help me fill the PAN form",
                                     "PAN card ke liye apply karna hai", "pan ka form bharna h",
                                     "पैन कार्ड के लिए आवेदन करना है"])
def test_regular_pan_application_requests_use_the_direct_route(command):
    assert new_pan_request(command) and is_guide_request(command)


@pytest.mark.parametrize("command", ["PAN card online apply kaise karte hain", "Show me how to apply for a PAN card",
                                     "apply for instant e PAN", "apply for a PAN card correction",
                                     "apply for a duplicate PAN card", "what documents for a PAN card application",
                                     "PAN card application status", "type apply for a PAN card",
                                     "don't apply for a PAN card", "pan card apply mat karo"])
def test_pan_lessons_and_other_requests_keep_their_own_route(command):
    assert not new_pan_request(command)


@pytest.mark.parametrize("question", ["Where is the export button?", "What does that button do?",
                                       "ye button kya karta hai?", "What should I do next?",
                                       "theriyila enna irukkudhu?", "theriyala enna irukkudhu?",
                                       "thiraayil enna irukkudhu?", "theraiyil enna irukkudhu?",
                                       "aduthu enna pannanum?", "Screen meedhu emundi?"])
def test_contextual_screen_questions(question):
    assert is_screen_question(question)


@pytest.mark.parametrize("romanized,native,language", [
    ("screen par kya hai", "स्क्रीन पर क्या है?", "hi"),
    ("screen alli enide", "ಪರದೆಯ ಮೇಲೆ ಏನು ಇದೆ?", "kn"),
    ("screen la enna irukku", "திரையில் என்ன இருக்கிறது?", "ta"),
    ("screen meeda emundi", "స్క్రీన్‌లో ఏముంది?", "te"),
    ("screen var kay aahe", "स्क्रीनवर काय आहे?", "mr"),
    ("screenil entha kaanunnathu", "സ്ക്രീനിൽ എന്താണ് കാണുന്നത്?", "ml"),
    ("screen e ki ache", "স্ক্রিনে কী আছে?", "bn"),
    ("screen par shu chhe", "સ્ક્રીન પર શું છે?", "gu"),
    ("screen te ki hai", "ਸਕ੍ਰੀਨ ਤੇ ਕੀ ਹੈ?", "pa"),
    ("what is on my screen", "What does this button do?", "en"),
])
def test_screen_questions_match_and_select_the_reply_language(romanized, native, language):
    for question in (romanized, native):
        assert is_screen_question(question)
        assert language_hint(question) == language


@pytest.mark.parametrize("command,language", [
    ("calculator scientific mode ku eppadi maathuradhu", "ta"),
    ("calculator na scientific mode ge hege badalayisodu", "kn"),
    ("calculator lo scientific mode ki ela marchali", "te"),
    ("calculator madhye scientific mode kasa lavaycha", "mr"),
    ("WhatsApp mein message kaise send karun", "hi"),
    ("PDF eppadi upload pannuvadhu", "ta"),
    ("Bluetooth engane on cheyyum", "ml"),
    ("Excel e file kibhabe open korbo", "bn"),
    ("WiFi kevi rite chalu karu", "gu"),
    ("hotspot kivein chalu karna", "pa"),
    ("hot spot kesay chalu karun", "hi"),
    ("Chrome vich bookmark kiven banauna", "pa"),
])
def test_romanized_lessons_survive_the_app_name_and_mixed_english(command, language):
    assert is_guide_request(command)
    assert language_hint(command) == language


def test_takeover_requires_an_explicit_prefix_and_goal():
    assert takeover_goal("Take over and switch Calculator to Scientific mode") == \
        "switch Calculator to Scientific mode"
    assert takeover_goal("mere liye kar do Calculator scientific mode mein") == "Calculator scientific mode mein"
    assert takeover_goal("type Take over and switch Calculator") is None
    assert takeover_goal("take over and") is None
    assert not is_screen_question("where is Bangalore")


@pytest.mark.parametrize("prefix,language", [
    ("ನನಗಾಗಿ ಮಾಡಿ", "kn"), ("enakkaga senju kudu", "ta"), ("నా కోసం చేసి పెట్టు", "te"),
])
def test_localized_takeover_prefix_preserves_the_goal(prefix, language):
    command = f"{prefix}: Calculator Scientific mode"
    assert takeover_goal(command) == "Calculator Scientific mode"
    assert language_hint(command) == language
    assert takeover_goal(f"type {command}") is None


@pytest.mark.parametrize("reply,expected", [
    ("ruko", "stop"), ("bas", "stop"), ("band karo", "stop"), ("stop", "stop"), ("rehne do", "stop"),
    ("रुको", "stop"), ("ಬೇಡ", "stop"), ("நிறுத்து", "stop"), ("ఆపు", "stop"),
    ("ho gaya", "next"), ("aage", "next"), ("next", "next"), ("kar diya", "next"), ("done", "next"),
    ("हो गया", "next"), ("ಆಯ್ತು", "next"), ("முடிஞ்சது", "next"), ("అయిపోయింది", "next"),
    ("phir se bolo", "repeat"), ("dobara", "repeat"), ("repeat", "repeat"), ("kya bola", "repeat"),
    ("फिर से बोलो", "repeat"), ("samajh nahi aaya", "repeat"),
    ("matte helu", "repeat"), ("tirumba sollunga", "repeat"), ("malli cheppandi", "repeat"),
    ("mudinjiduchu", "next"), ("aythu", "next"), ("niruthunga", "stop"),
    ("Spotify pe gaana chalao", None), ("WhatsApp kholo", None), ("", None),
])
def test_control_words(reply, expected):
    assert control(reply) == expected
