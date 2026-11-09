"""What the assistant says back, per language, and how short spoken replies are understood: yes, no, and
"that was wrong" (with or without the corrected command after it).

Replies are written in each language's own script because Timbre speaks them. Field values (app, contact,
song, site...) usually arrive in Latin letters from Prisma and go in unchanged, so every template keeps its
fields as separate words ("Raju ಅವರಿಗೆ", "Spotify-ൽ") instead of joining a case ending a name in another script
couldn't take. The voice is female: where a language marks the speaker's gender on the verb (Hindi, Marathi,
Gujarati, Punjabi) the templates use the feminine form ("चला रही हूँ", "भूल गई").

Spoken replies come from Prisma in Latin letters (it does not report the language), typed ones may be in
native script, so every word list below has both the romanised spellings Prisma writes and the native ones.
"""

import re
import unicodedata
from collections.abc import Callable

LANGUAGE_NAMES = {"hi": "Hindi", "en": "English", "kn": "Kannada", "ta": "Tamil", "te": "Telugu", "mr": "Marathi",
                  "ml": "Malayalam", "bn": "Bengali", "gu": "Gujarati", "pa": "Punjabi"}
LANGUAGES = tuple(LANGUAGE_NAMES)

TEMPLATES: dict[str, dict[str, str]] = {
    "hi": {
        "opened": "{app} खोल दिया।",
        "app_not_found": "{app} नहीं मिला।",
        "contact_not_found": "{contact} आपकी कॉन्टैक्ट लिस्ट में नहीं है।",
        "sent": "{contact} को मैसेज भेज दिया।",
        "searched": "{query} सर्च कर दिया।",
        "playing": "{query} चला रही हूँ।",
        "typed": "लिख दिया।",
        "clicked": "{target} दबा दिया।",
        "control_not_found": "स्क्रीन पर {target} नहीं मिला।",
        "cancelled": "ठीक है, रहने देते हैं।",
        "not_understood": "माफ़ कीजिए, समझ नहीं आया। फिर से बोलिए।",
        "confirm_send": "{contact} को भेजूँ: {text}? हाँ या ना बोलिए।",
        "not_sent": "ठीक है, नहीं भेजा।",
        "opened_site": "{site} खोल दी।",
        "searched_on": "{site} पर {query} सर्च कर दिया।",
        "playing_on": "{app} पर {query} चला रही हूँ।",
        "playing_app": "{app} पर गाने चला दिए।",
        "play_failed": "{app} पर {query} नहीं चला पाई, खोलकर छोड़ दिया है।",
        "control_pause": "रोक दिया।",
        "control_resume": "फिर से चला दिया।",
        "control_next": "अगला चला दिया।",
        "control_previous": "पिछला चला दिया।",
        "control_volume_up": "आवाज़ बढ़ा दी।",
        "control_volume_down": "आवाज़ कम कर दी।",
        "control_mute": "आवाज़ बंद कर दी।",
        "nothing_playing": "अभी कुछ नहीं चल रहा।",
        "forgot": "ठीक है, वो भूल गई।",
        "nothing_to_forget": "भूलने के लिए कुछ नहीं है।",
        "dry_run": "Dry run: nothing was executed.",
        "focus_changed": "स्क्रीन बदल गई, इसलिए कुछ नहीं किया। फिर से बोलिए।",
        "error": "कुछ गड़बड़ हो गई, फिर से कोशिश कीजिए।",
        "one_moment": "एक सेकंड, देखती हूँ।",
        "still_thinking": "बस, एक पल और।",
        "learned": "ठीक है, अगली बार याद रखूँगी।",
        "steps_stopped": "इसलिए बाकी काम नहीं किया।",
        "no_speech": "कुछ सुनाई नहीं दिया।",
    },
    "en": {
        "opened": "Opened {app}.",
        "app_not_found": "I couldn't find {app}.",
        "contact_not_found": "{contact} is not in your contacts.",
        "sent": "Message sent to {contact}.",
        "searched": "Searched for {query}.",
        "playing": "Playing {query}.",
        "typed": "Done typing.",
        "clicked": "Clicked {target}.",
        "control_not_found": "I couldn't find {target} on the screen.",
        "cancelled": "Okay, cancelled.",
        "not_understood": "Sorry, I didn't get that. Please say it again.",
        "confirm_send": "Send to {contact}: {text}? Say yes or no.",
        "not_sent": "Okay, I didn't send it.",
        "opened_site": "Opened {site}.",
        "searched_on": "Searched {site} for {query}.",
        "playing_on": "Playing {query} on {app}.",
        "playing_app": "Playing music on {app}.",
        "play_failed": "I opened {app} but couldn't start {query}.",
        "control_pause": "Paused.",
        "control_resume": "Playing again.",
        "control_next": "Next one.",
        "control_previous": "Previous one.",
        "control_volume_up": "Volume up.",
        "control_volume_down": "Volume down.",
        "control_mute": "Muted.",
        "nothing_playing": "Nothing is playing right now.",
        "forgot": "Okay, I've forgotten that.",
        "nothing_to_forget": "There's nothing to forget.",
        "dry_run": "Dry run: nothing was executed.",
        "focus_changed": "The window changed, so I did nothing. Please say it again.",
        "error": "Something went wrong, please try again.",
        "one_moment": "One moment, let me check.",
        "still_thinking": "Just a little longer.",
        "learned": "Okay, I'll remember that next time.",
        "steps_stopped": "So I didn't do the rest.",
        "no_speech": "I didn't hear anything.",
    },
    "kn": {
        "opened": "{app} ತೆರೆದಿದ್ದೇನೆ.",
        "app_not_found": "{app} ಸಿಗಲಿಲ್ಲ.",
        "contact_not_found": "{contact} ನಿಮ್ಮ ಕಾಂಟ್ಯಾಕ್ಟ್ ಪಟ್ಟಿಯಲ್ಲಿ ಇಲ್ಲ.",
        "sent": "{contact} ಅವರಿಗೆ ಮೆಸೇಜ್ ಕಳುಹಿಸಿದ್ದೇನೆ.",
        "searched": "{query} ಹುಡುಕಿದ್ದೇನೆ.",
        "playing": "{query} ಪ್ಲೇ ಮಾಡುತ್ತಿದ್ದೇನೆ.",
        "typed": "ಬರೆದಿದ್ದೇನೆ.",
        "clicked": "{target} ಒತ್ತಿದ್ದೇನೆ.",
        "control_not_found": "ಪರದೆಯ ಮೇಲೆ {target} ಸಿಗಲಿಲ್ಲ.",
        "cancelled": "ಸರಿ, ಬಿಟ್ಟುಬಿಡೋಣ.",
        "not_understood": "ಕ್ಷಮಿಸಿ, ಅರ್ಥವಾಗಲಿಲ್ಲ. ಮತ್ತೊಮ್ಮೆ ಹೇಳಿ.",
        "confirm_send": "{contact} ಅವರಿಗೆ ಕಳುಹಿಸಲೇ: {text}? ಹೌದು ಅಥವಾ ಬೇಡ ಎಂದು ಹೇಳಿ.",
        "not_sent": "ಸರಿ, ಕಳುಹಿಸಲಿಲ್ಲ.",
        "opened_site": "{site} ತೆರೆದಿದ್ದೇನೆ.",
        "searched_on": "{site} ನಲ್ಲಿ {query} ಹುಡುಕಿದ್ದೇನೆ.",
        "playing_on": "{app} ನಲ್ಲಿ {query} ಪ್ಲೇ ಮಾಡುತ್ತಿದ್ದೇನೆ.",
        "playing_app": "{app} ನಲ್ಲಿ ಹಾಡುಗಳನ್ನು ಹಾಕಿದ್ದೇನೆ.",
        "play_failed": "{app} ತೆರೆದಿದ್ದೇನೆ, ಆದರೆ {query} ಪ್ಲೇ ಆಗಲಿಲ್ಲ.",
        "control_pause": "ನಿಲ್ಲಿಸಿದ್ದೇನೆ.",
        "control_resume": "ಮತ್ತೆ ಹಾಕಿದ್ದೇನೆ.",
        "control_next": "ಮುಂದಿನದು ಹಾಕಿದ್ದೇನೆ.",
        "control_previous": "ಹಿಂದಿನದು ಹಾಕಿದ್ದೇನೆ.",
        "control_volume_up": "ಧ್ವನಿ ಹೆಚ್ಚಿಸಿದ್ದೇನೆ.",
        "control_volume_down": "ಧ್ವನಿ ಕಡಿಮೆ ಮಾಡಿದ್ದೇನೆ.",
        "control_mute": "ಧ್ವನಿ ಆಫ್ ಮಾಡಿದ್ದೇನೆ.",
        "nothing_playing": "ಈಗ ಏನೂ ಪ್ಲೇ ಆಗುತ್ತಿಲ್ಲ.",
        "forgot": "ಸರಿ, ಅದನ್ನು ಮರೆತಿದ್ದೇನೆ.",
        "nothing_to_forget": "ಮರೆಯಲು ಏನೂ ಇಲ್ಲ.",
        "dry_run": "Dry run: nothing was executed.",
        "focus_changed": "ವಿಂಡೋ ಬದಲಾಗಿದೆ, ಹಾಗಾಗಿ ಏನೂ ಮಾಡಲಿಲ್ಲ. ಮತ್ತೊಮ್ಮೆ ಹೇಳಿ.",
        "error": "ಏನೋ ತೊಂದರೆ ಆಯಿತು, ಮತ್ತೊಮ್ಮೆ ಪ್ರಯತ್ನಿಸಿ.",
        "one_moment": "ಒಂದು ನಿಮಿಷ, ನೋಡುತ್ತೇನೆ.",
        "still_thinking": "ಇನ್ನು ಸ್ವಲ್ಪ ಹೊತ್ತು.",
        "learned": "ಸರಿ, ಮುಂದಿನ ಸಲ ನೆನಪಿಟ್ಟುಕೊಳ್ಳುತ್ತೇನೆ.",
        "steps_stopped": "ಹಾಗಾಗಿ ಉಳಿದದ್ದನ್ನು ಮಾಡಲಿಲ್ಲ.",
        "no_speech": "ಏನೂ ಕೇಳಿಸಲಿಲ್ಲ.",
    },
    "ta": {
        "opened": "{app} திறந்துவிட்டேன்.",
        "app_not_found": "{app} கிடைக்கவில்லை.",
        "contact_not_found": "{contact} உங்கள் தொடர்பு பட்டியலில் இல்லை.",
        "sent": "{contact} க்கு மெசேஜ் அனுப்பிவிட்டேன்.",
        "searched": "{query} தேடிவிட்டேன்.",
        "playing": "{query} ப்ளே செய்கிறேன்.",
        "typed": "எழுதிவிட்டேன்.",
        "clicked": "{target} அழுத்திவிட்டேன்.",
        "control_not_found": "திரையில் {target} கிடைக்கவில்லை.",
        "cancelled": "சரி, விட்டுவிடலாம்.",
        "not_understood": "மன்னிக்கவும், புரியவில்லை. மீண்டும் சொல்லுங்கள்.",
        "confirm_send": "{contact} க்கு அனுப்பட்டுமா: {text}? ஆமா அல்லது வேண்டாம் என்று சொல்லுங்கள்.",
        "not_sent": "சரி, அனுப்பவில்லை.",
        "opened_site": "{site} திறந்துவிட்டேன்.",
        "searched_on": "{site} இல் {query} தேடிவிட்டேன்.",
        "playing_on": "{app} இல் {query} ப்ளே செய்கிறேன்.",
        "playing_app": "{app} இல் பாட்டு போட்டுவிட்டேன்.",
        "play_failed": "{app} திறந்தேன், ஆனால் {query} ப்ளே ஆகவில்லை.",
        "control_pause": "நிறுத்திவிட்டேன்.",
        "control_resume": "மீண்டும் போட்டுவிட்டேன்.",
        "control_next": "அடுத்தது போட்டுவிட்டேன்.",
        "control_previous": "முந்தையது போட்டுவிட்டேன்.",
        "control_volume_up": "சத்தம் கூட்டிவிட்டேன்.",
        "control_volume_down": "சத்தம் குறைத்துவிட்டேன்.",
        "control_mute": "மியூட் செய்துவிட்டேன்.",
        "nothing_playing": "இப்போது எதுவும் ப்ளே ஆகவில்லை.",
        "forgot": "சரி, அதை மறந்துவிட்டேன்.",
        "nothing_to_forget": "மறக்க எதுவும் இல்லை.",
        "dry_run": "Dry run: nothing was executed.",
        "focus_changed": "விண்டோ மாறிவிட்டது, அதனால் எதுவும் செய்யவில்லை. மீண்டும் சொல்லுங்கள்.",
        "error": "ஏதோ பிரச்சனை, மீண்டும் முயற்சிக்கவும்.",
        "one_moment": "ஒரு நிமிஷம், பார்க்கிறேன்.",
        "still_thinking": "இன்னும் கொஞ்சம் நேரம்.",
        "learned": "சரி, அடுத்த முறை நினைவில் வைத்துக்கொள்கிறேன்.",
        "steps_stopped": "அதனால் மீதியைச் செய்யவில்லை.",
        "no_speech": "எதுவும் கேட்கவில்லை.",
    },
    "te": {
        "opened": "{app} ఓపెన్ చేశాను.",
        "app_not_found": "{app} దొరకలేదు.",
        "contact_not_found": "{contact} మీ కాంటాక్ట్ లిస్ట్ లో లేరు.",
        "sent": "{contact} కి మెసేజ్ పంపాను.",
        "searched": "{query} కోసం వెతికాను.",
        "playing": "{query} ప్లే చేస్తున్నాను.",
        "typed": "టైప్ చేశాను.",
        "clicked": "{target} నొక్కాను.",
        "control_not_found": "స్క్రీన్ మీద {target} కనిపించలేదు.",
        "cancelled": "సరే, వదిలేద్దాం.",
        "not_understood": "క్షమించండి, అర్థం కాలేదు. మళ్ళీ చెప్పండి.",
        "confirm_send": "{contact} కి పంపనా: {text}? అవును లేదా వద్దు అని చెప్పండి.",
        "not_sent": "సరే, పంపలేదు.",
        "opened_site": "{site} ఓపెన్ చేశాను.",
        "searched_on": "{site} లో {query} కోసం వెతికాను.",
        "playing_on": "{app} లో {query} ప్లే చేస్తున్నాను.",
        "playing_app": "{app} లో పాటలు పెట్టాను.",
        "play_failed": "{app} ఓపెన్ చేశాను, కానీ {query} ప్లే కాలేదు.",
        "control_pause": "ఆపేశాను.",
        "control_resume": "మళ్ళీ ప్లే చేశాను.",
        "control_next": "తర్వాతది పెట్టాను.",
        "control_previous": "ఇంతకుముందుది పెట్టాను.",
        "control_volume_up": "సౌండ్ పెంచాను.",
        "control_volume_down": "సౌండ్ తగ్గించాను.",
        "control_mute": "మ్యూట్ చేశాను.",
        "nothing_playing": "ఇప్పుడు ఏదీ ప్లే అవ్వట్లేదు.",
        "forgot": "సరే, అది మర్చిపోయాను.",
        "nothing_to_forget": "మర్చిపోవడానికి ఏమీ లేదు.",
        "dry_run": "Dry run: nothing was executed.",
        "focus_changed": "విండో మారింది, అందుకే ఏమీ చేయలేదు. మళ్ళీ చెప్పండి.",
        "error": "ఏదో తప్పు జరిగింది, మళ్ళీ ప్రయత్నించండి.",
        "one_moment": "ఒక్క నిమిషం, చూస్తాను.",
        "still_thinking": "ఇంకొంచెం సేపు.",
        "learned": "సరే, ఇకపై గుర్తుపెట్టుకుంటాను.",
        "steps_stopped": "అందుకే మిగతావి చేయలేదు.",
        "no_speech": "ఏమీ వినిపించలేదు.",
    },
    "mr": {
        "opened": "{app} उघडलं.",
        "app_not_found": "{app} सापडलं नाही.",
        "contact_not_found": "{contact} तुमच्या कॉन्टॅक्ट लिस्टमध्ये नाही.",
        "sent": "{contact} ला मेसेज पाठवला.",
        "searched": "{query} सर्च केलं.",
        "playing": "{query} लावतेय.",
        "typed": "लिहिलं.",
        "clicked": "{target} दाबलं.",
        "control_not_found": "स्क्रीनवर {target} सापडलं नाही.",
        "cancelled": "ठीक आहे, रद्द केलं.",
        "not_understood": "माफ करा, समजलं नाही. पुन्हा सांगा.",
        "confirm_send": "{contact} ला पाठवू: {text}? हो किंवा नाही सांगा.",
        "not_sent": "ठीक आहे, पाठवला नाही.",
        "opened_site": "{site} उघडली.",
        "searched_on": "{site} वर {query} सर्च केलं.",
        "playing_on": "{app} वर {query} लावतेय.",
        "playing_app": "{app} वर गाणी लावली.",
        "play_failed": "{app} उघडलं, पण {query} लावता आलं नाही.",
        "control_pause": "थांबवलं.",
        "control_resume": "पुन्हा सुरू केलं.",
        "control_next": "पुढचं लावलं.",
        "control_previous": "मागचं लावलं.",
        "control_volume_up": "आवाज वाढवला.",
        "control_volume_down": "आवाज कमी केला.",
        "control_mute": "आवाज बंद केला.",
        "nothing_playing": "आत्ता काहीच चालू नाही.",
        "forgot": "ठीक आहे, ते मी विसरले.",
        "nothing_to_forget": "विसरण्यासारखं काही नाही.",
        "dry_run": "Dry run: nothing was executed.",
        "focus_changed": "विंडो बदलली, म्हणून काही केलं नाही. पुन्हा सांगा.",
        "error": "काहीतरी चुकलं, पुन्हा प्रयत्न करा.",
        "one_moment": "एक सेकंद, बघते.",
        "still_thinking": "अजून थोडा वेळ.",
        "learned": "ठीक आहे, पुढच्या वेळी लक्षात ठेवेन.",
        "steps_stopped": "म्हणून बाकीचं केलं नाही.",
        "no_speech": "काहीच ऐकू आलं नाही.",
    },
    "ml": {
        "opened": "{app} തുറന്നു.",
        "app_not_found": "{app} കിട്ടിയില്ല.",
        "contact_not_found": "{contact} നിങ്ങളുടെ കോൺടാക്റ്റ് ലിസ്റ്റിൽ ഇല്ല.",
        "sent": "{contact}-ന് മെസേജ് അയച്ചു.",
        "searched": "{query} സെർച്ച് ചെയ്തു.",
        "playing": "{query} പ്ലേ ചെയ്യുന്നു.",
        "typed": "ടൈപ്പ് ചെയ്തു.",
        "clicked": "{target} അമർത്തി.",
        "control_not_found": "സ്ക്രീനിൽ {target} കണ്ടില്ല.",
        "cancelled": "ശരി, വേണ്ടെന്നു വച്ചു.",
        "not_understood": "ക്ഷമിക്കണം, മനസ്സിലായില്ല. ഒന്നുകൂടി പറയൂ.",
        "confirm_send": "{contact}-ന് അയക്കട്ടെ: {text}? അതെ അല്ലെങ്കിൽ വേണ്ട എന്ന് പറയൂ.",
        "not_sent": "ശരി, അയച്ചില്ല.",
        "opened_site": "{site} തുറന്നു.",
        "searched_on": "{site}-ൽ {query} സെർച്ച് ചെയ്തു.",
        "playing_on": "{app}-ൽ {query} പ്ലേ ചെയ്യുന്നു.",
        "playing_app": "{app}-ൽ പാട്ട് വച്ചു.",
        "play_failed": "{app} തുറന്നു, പക്ഷേ {query} പ്ലേ ആയില്ല.",
        "control_pause": "നിർത്തി.",
        "control_resume": "വീണ്ടും പ്ലേ ചെയ്തു.",
        "control_next": "അടുത്തത് വച്ചു.",
        "control_previous": "മുമ്പത്തേത് വച്ചു.",
        "control_volume_up": "ശബ്ദം കൂട്ടി.",
        "control_volume_down": "ശബ്ദം കുറച്ചു.",
        "control_mute": "മ്യൂട്ട് ചെയ്തു.",
        "nothing_playing": "ഇപ്പോൾ ഒന്നും പ്ലേ ചെയ്യുന്നില്ല.",
        "forgot": "ശരി, അത് മറന്നു.",
        "nothing_to_forget": "മറക്കാൻ ഒന്നുമില്ല.",
        "dry_run": "Dry run: nothing was executed.",
        "focus_changed": "വിൻഡോ മാറി, അതുകൊണ്ട് ഒന്നും ചെയ്തില്ല. ഒന്നുകൂടി പറയൂ.",
        "error": "എന്തോ പ്രശ്നം പറ്റി, വീണ്ടും ശ്രമിക്കൂ.",
        "one_moment": "ഒരു നിമിഷം, നോക്കട്ടെ.",
        "still_thinking": "ഇനി കുറച്ചു കൂടി.",
        "learned": "ശരി, അടുത്ത തവണ ഓർത്തുവയ്ക്കാം.",
        "steps_stopped": "അതുകൊണ്ട് ബാക്കി ചെയ്തില്ല.",
        "no_speech": "ഒന്നും കേട്ടില്ല.",
    },
    "bn": {
        "opened": "{app} খুলে দিয়েছি।",
        "app_not_found": "{app} খুঁজে পাইনি।",
        "contact_not_found": "{contact} আপনার কন্টাক্ট লিস্টে নেই।",
        "sent": "{contact}-কে মেসেজ পাঠিয়ে দিয়েছি।",
        "searched": "{query} সার্চ করে দিয়েছি।",
        "playing": "{query} চালাচ্ছি।",
        "typed": "লিখে দিয়েছি।",
        "clicked": "{target} টিপে দিয়েছি।",
        "control_not_found": "স্ক্রিনে {target} পাইনি।",
        "cancelled": "ঠিক আছে, থাক।",
        "not_understood": "দুঃখিত, বুঝতে পারিনি। আবার বলুন।",
        "confirm_send": "{contact}-কে পাঠাব: {text}? হ্যাঁ বা না বলুন।",
        "not_sent": "ঠিক আছে, পাঠাইনি।",
        "opened_site": "{site} খুলে দিয়েছি।",
        "searched_on": "{site}-এ {query} সার্চ করে দিয়েছি।",
        "playing_on": "{app}-এ {query} চালাচ্ছি।",
        "playing_app": "{app}-এ গান চালিয়ে দিয়েছি।",
        "play_failed": "{app} খুলেছি, কিন্তু {query} চালাতে পারিনি।",
        "control_pause": "থামিয়ে দিয়েছি।",
        "control_resume": "আবার চালিয়ে দিয়েছি।",
        "control_next": "পরেরটা চালিয়েছি।",
        "control_previous": "আগেরটা চালিয়েছি।",
        "control_volume_up": "আওয়াজ বাড়িয়ে দিয়েছি।",
        "control_volume_down": "আওয়াজ কমিয়ে দিয়েছি।",
        "control_mute": "আওয়াজ বন্ধ করে দিয়েছি।",
        "nothing_playing": "এখন কিছু চলছে না।",
        "forgot": "ঠিক আছে, ওটা ভুলে গেছি।",
        "nothing_to_forget": "ভোলার মতো কিছু নেই।",
        "dry_run": "Dry run: nothing was executed.",
        "focus_changed": "উইন্ডো বদলে গেছে, তাই কিছু করিনি। আবার বলুন।",
        "error": "কিছু একটা গোলমাল হয়েছে, আবার চেষ্টা করুন।",
        "one_moment": "এক সেকেন্ড, দেখছি।",
        "still_thinking": "আর একটু।",
        "learned": "ঠিক আছে, পরের বার মনে রাখব।",
        "steps_stopped": "তাই বাকিগুলো করিনি।",
        "no_speech": "কিছু শুনতে পাইনি।",
    },
    "gu": {
        "opened": "{app} ખોલી દીધું.",
        "app_not_found": "{app} મળ્યું નહીં.",
        "contact_not_found": "{contact} તમારા કોન્ટેક્ટ લિસ્ટમાં નથી.",
        "sent": "{contact} ને મેસેજ મોકલી દીધો.",
        "searched": "{query} સર્ચ કરી દીધું.",
        "playing": "{query} વગાડી રહી છું.",
        "typed": "લખી દીધું.",
        "clicked": "{target} દબાવી દીધું.",
        "control_not_found": "સ્ક્રીન પર {target} મળ્યું નહીં.",
        "cancelled": "ઠીક છે, રહેવા દઈએ.",
        "not_understood": "માફ કરજો, સમજાયું નહીં. ફરીથી બોલો.",
        "confirm_send": "{contact} ને મોકલું: {text}? હા કે ના બોલો.",
        "not_sent": "ઠીક છે, મોકલ્યો નથી.",
        "opened_site": "{site} ખોલી દીધી.",
        "searched_on": "{site} પર {query} સર્ચ કરી દીધું.",
        "playing_on": "{app} પર {query} વગાડી રહી છું.",
        "playing_app": "{app} પર ગીતો વગાડ્યાં.",
        "play_failed": "{app} ખોલ્યું, પણ {query} વગાડી ન શકી.",
        "control_pause": "રોકી દીધું.",
        "control_resume": "ફરી ચાલુ કર્યું.",
        "control_next": "પછીનું ચાલુ કર્યું.",
        "control_previous": "પાછલું ચાલુ કર્યું.",
        "control_volume_up": "અવાજ વધારી દીધો.",
        "control_volume_down": "અવાજ ઓછો કરી દીધો.",
        "control_mute": "અવાજ બંધ કરી દીધો.",
        "nothing_playing": "અત્યારે કંઈ વાગતું નથી.",
        "forgot": "ઠીક છે, એ ભૂલી ગઈ.",
        "nothing_to_forget": "ભૂલવા જેવું કંઈ નથી.",
        "dry_run": "Dry run: nothing was executed.",
        "focus_changed": "વિન્ડો બદલાઈ ગઈ, એટલે કંઈ કર્યું નહીં. ફરીથી બોલો.",
        "error": "કંઈક ગડબડ થઈ ગઈ, ફરીથી પ્રયત્ન કરો.",
        "one_moment": "એક સેકન્ડ, જોઉં છું.",
        "still_thinking": "બસ, થોડી વાર.",
        "learned": "ઠીક છે, આવતી વખતે યાદ રાખીશ.",
        "steps_stopped": "એટલે બાકીનું કર્યું નથી.",
        "no_speech": "કંઈ સંભળાયું નહીં.",
    },
    "pa": {
        "opened": "{app} ਖੋਲ੍ਹ ਦਿੱਤਾ।",
        "app_not_found": "{app} ਨਹੀਂ ਮਿਲਿਆ।",
        "contact_not_found": "{contact} ਤੁਹਾਡੀ ਕਾਂਟੈਕਟ ਲਿਸਟ ਵਿੱਚ ਨਹੀਂ ਹੈ।",
        "sent": "{contact} ਨੂੰ ਮੈਸੇਜ ਭੇਜ ਦਿੱਤਾ।",
        "searched": "{query} ਸਰਚ ਕਰ ਦਿੱਤਾ।",
        "playing": "{query} ਚਲਾ ਰਹੀ ਹਾਂ।",
        "typed": "ਲਿਖ ਦਿੱਤਾ।",
        "clicked": "{target} ਦਬਾ ਦਿੱਤਾ।",
        "control_not_found": "ਸਕਰੀਨ ਤੇ {target} ਨਹੀਂ ਮਿਲਿਆ।",
        "cancelled": "ਠੀਕ ਹੈ, ਰਹਿਣ ਦਿੰਦੇ ਹਾਂ।",
        "not_understood": "ਮਾਫ਼ ਕਰਨਾ, ਸਮਝ ਨਹੀਂ ਆਇਆ। ਦੁਬਾਰਾ ਬੋਲੋ।",
        "confirm_send": "{contact} ਨੂੰ ਭੇਜਾਂ: {text}? ਹਾਂ ਜਾਂ ਨਾ ਬੋਲੋ।",
        "not_sent": "ਠੀਕ ਹੈ, ਨਹੀਂ ਭੇਜਿਆ।",
        "opened_site": "{site} ਖੋਲ੍ਹ ਦਿੱਤੀ।",
        "searched_on": "{site} ਤੇ {query} ਸਰਚ ਕਰ ਦਿੱਤਾ।",
        "playing_on": "{app} ਤੇ {query} ਚਲਾ ਰਹੀ ਹਾਂ।",
        "playing_app": "{app} ਤੇ ਗਾਣੇ ਚਲਾ ਦਿੱਤੇ।",
        "play_failed": "{app} ਤੇ {query} ਨਹੀਂ ਚਲਾ ਸਕੀ, ਖੋਲ੍ਹ ਕੇ ਛੱਡ ਦਿੱਤਾ ਹੈ।",
        "control_pause": "ਰੋਕ ਦਿੱਤਾ।",
        "control_resume": "ਫਿਰ ਤੋਂ ਚਲਾ ਦਿੱਤਾ।",
        "control_next": "ਅਗਲਾ ਚਲਾ ਦਿੱਤਾ।",
        "control_previous": "ਪਿਛਲਾ ਚਲਾ ਦਿੱਤਾ।",
        "control_volume_up": "ਆਵਾਜ਼ ਵਧਾ ਦਿੱਤੀ।",
        "control_volume_down": "ਆਵਾਜ਼ ਘਟਾ ਦਿੱਤੀ।",
        "control_mute": "ਆਵਾਜ਼ ਬੰਦ ਕਰ ਦਿੱਤੀ।",
        "nothing_playing": "ਹੁਣ ਕੁਝ ਨਹੀਂ ਚੱਲ ਰਿਹਾ।",
        "forgot": "ਠੀਕ ਹੈ, ਉਹ ਭੁੱਲ ਗਈ।",
        "nothing_to_forget": "ਭੁੱਲਣ ਲਈ ਕੁਝ ਨਹੀਂ ਹੈ।",
        "dry_run": "Dry run: nothing was executed.",
        "focus_changed": "ਵਿੰਡੋ ਬਦਲ ਗਈ, ਇਸ ਲਈ ਕੁਝ ਨਹੀਂ ਕੀਤਾ। ਦੁਬਾਰਾ ਬੋਲੋ।",
        "error": "ਕੁਝ ਗੜਬੜ ਹੋ ਗਈ, ਦੁਬਾਰਾ ਕੋਸ਼ਿਸ਼ ਕਰੋ।",
        "one_moment": "ਇੱਕ ਸਕਿੰਟ, ਦੇਖਦੀ ਹਾਂ।",
        "still_thinking": "ਬਸ, ਥੋੜ੍ਹਾ ਜਿਹਾ ਹੋਰ।",
        "learned": "ਠੀਕ ਹੈ, ਅਗਲੀ ਵਾਰ ਯਾਦ ਰੱਖਾਂਗੀ।",
        "steps_stopped": "ਇਸ ਲਈ ਬਾਕੀ ਕੰਮ ਨਹੀਂ ਕੀਤਾ।",
        "no_speech": "ਕੁਝ ਸੁਣਾਈ ਨਹੀਂ ਦਿੱਤਾ।",
    },
}
DEFAULT_LANG = "hi"


_CONFIRM_CLICK = {
    "hi": "{target} दबाऊँ? हाँ या ना कहिए।",
    "en": "Press {target}? Say yes or no.",
    "kn": "{target} ಒತ್ತಲೇ? ಹೌದು ಅಥವಾ ಇಲ್ಲ ಹೇಳಿ.",
    "ta": "{target} அழுத்தவா? ஆம் அல்லது இல்லை சொல்லுங்கள்.",
    "te": "{target} నొక్కనా? అవును లేదా కాదు చెప్పండి.",
    "mr": "{target} दाबू का? हो किंवा नाही सांगा.",
    "ml": "{target} അമർത്തട്ടെ? അതെ അല്ലെങ്കിൽ ഇല്ല എന്നു പറയൂ.",
    "bn": "{target} চাপব? হ্যাঁ বা না বলুন।",
    "gu": "{target} દબાવું? હા કે ના કહો.",
    "pa": "{target} ਦਬਾਵਾਂ? ਹਾਂ ਜਾਂ ਨਾਂ ਕਹੋ।",
}
_PROTECTED_FIELD = {
    "hi": "इस खाने को खुद भरें। मैं सुरक्षित या अज्ञात खाने में नहीं लिखूँगी।",
    "en": "Please enter this yourself. I cannot type into a protected or unverified field.",
    "kn": "ಈ ಜಾಗದಲ್ಲಿ ನೀವೇ ಬರೆಯಿರಿ. ಸುರಕ್ಷಿತ ಅಥವಾ ಪರಿಶೀಲಿಸದ ಜಾಗದಲ್ಲಿ ನಾನು ಬರೆಯುವುದಿಲ್ಲ.",
    "ta": "இதை நீங்களே நிரப்புங்கள். பாதுகாக்கப்பட்ட அல்லது சரிபார்க்காத இடத்தில் எழுத மாட்டேன்.",
    "te": "దీన్ని మీరే నింపండి. రక్షిత లేదా నిర్ధారించని చోట నేను రాయను.",
    "mr": "हा रकाना स्वतः भरा. सुरक्षित किंवा पडताळणी न झालेल्या रकान्यात मी लिहिणार नाही.",
    "ml": "ഇത് സ്വയം നൽകുക. സംരക്ഷിതമോ പരിശോധിക്കാത്തതോ ആയ ഇടത്തിൽ ഞാൻ എഴുതില്ല.",
    "bn": "এটি নিজে লিখুন। সুরক্ষিত বা যাচাই না করা ঘরে আমি লিখব না।",
    "gu": "આ જાતે ભરો. સુરક્ષિત અથવા ચકાસ્યા વગરના ખાનામાં હું લખીશ નહીં.",
    "pa": "ਇਹ ਖਾਨਾ ਆਪ ਭਰੋ। ਸੁਰੱਖਿਅਤ ਜਾਂ ਬਿਨਾਂ ਜਾਂਚੇ ਖਾਨੇ ਵਿੱਚ ਮੈਂ ਨਹੀਂ ਲਿਖਾਂਗੀ।",
}
# Guide mode ("dikhao"). The steps themselves are written by System 2 in the user's language; these are the
# fixed endings, each without fields so they can be cached.
_SCREEN_GUIDANCE = {
    "hi": {
        "guide_private_step": "यह कदम खुद पूरा करें, फिर हो गया कहें।",
        "screen_unavailable": "मैं अभी स्क्रीन पढ़ नहीं पा रही हूँ।",
        "guide_start": "ठीक है, मैं स्क्रीन पर दिखाती हूँ।",
        "guide_to_form": "फॉर्म आ गया है। अब मैं हर खाने के लिए आपसे पूछूँगी।",
        "guide_unavailable": "अभी मैं स्क्रीन पर दिखा नहीं पा रही, थोड़ी देर बाद कोशिश कीजिए।",
        "guide_lost": "मुझे अगला कदम नहीं मिल रहा। फिर से दिखाने को कहिए।",
        "guide_done": "बहुत बढ़िया, हो गया।",
        "guide_stopped": "ठीक है, यहीं रोक देती हूँ।",
        "guide_timeout": "मैं यहीं रुक रही हूँ। जब चाहें, फिर से दिखाने को कहिए।",
        "guide_cannot": "माफ़ कीजिए, यह मैं स्क्रीन पर नहीं दिखा सकती।",
        "guide_form": "फॉर्म के लिए कहिए: फॉर्म भरने में मदद करो।",
    },
    "en": {
        "guide_private_step": "Complete this step on the page yourself, then say done.",
        "screen_unavailable": "I can't read the screen right now.",
        "guide_start": "Okay, I'll show you on the screen.",
        "guide_to_form": "The form is here. Now I'll ask you for each box.",
        "guide_unavailable": "I can't show you on the screen right now. Please try again in a little while.",
        "guide_lost": "I can't find the next step. Please ask me to show you again.",
        "guide_done": "Well done, that's it.",
        "guide_stopped": "Okay, I'll stop here.",
        "guide_timeout": "I'll stop here for now. Ask me to show you again whenever you like.",
        "guide_cannot": "Sorry, I can't show that on the screen.",
        "guide_form": "For forms, say: help me fill this form.",
    },
    "kn": {
        "guide_private_step": "ಈ ಹಂತವನ್ನು ನೀವೇ ಮುಗಿಸಿ, ನಂತರ ಆಯಿತು ಎಂದು ಹೇಳಿ.",
        "screen_unavailable": "ಈಗ ನನಗೆ ಪರದೆಯನ್ನು ಓದಲು ಆಗುತ್ತಿಲ್ಲ.",
        "guide_start": "ಸರಿ, ಪರದೆಯ ಮೇಲೆ ತೋರಿಸುತ್ತೇನೆ.",
        "guide_to_form": "ಫಾರ್ಮ್ ಬಂದಿದೆ. ಈಗ ಪ್ರತಿ ಜಾಗಕ್ಕೂ ನಿಮ್ಮನ್ನು ಕೇಳುತ್ತೇನೆ.",
        "guide_unavailable": "ಈಗ ಪರದೆಯ ಮೇಲೆ ತೋರಿಸಲು ಆಗುತ್ತಿಲ್ಲ. ಸ್ವಲ್ಪ ಸಮಯದ ನಂತರ ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಿ.",
        "guide_lost": "ಮುಂದಿನ ಹಂತ ಸಿಗುತ್ತಿಲ್ಲ. ಮತ್ತೆ ತೋರಿಸಲು ಹೇಳಿ.",
        "guide_done": "ಚೆನ್ನಾಗಿದೆ, ಆಯ್ತು.",
        "guide_stopped": "ಸರಿ, ಇಲ್ಲಿಗೇ ನಿಲ್ಲಿಸುತ್ತೇನೆ.",
        "guide_timeout": "ಇಲ್ಲಿಗೇ ನಿಲ್ಲಿಸುತ್ತೇನೆ. ಬೇಕಾದಾಗ ಮತ್ತೆ ತೋರಿಸಲು ಹೇಳಿ.",
        "guide_cannot": "ಕ್ಷಮಿಸಿ, ಇದನ್ನು ಪರದೆಯ ಮೇಲೆ ತೋರಿಸಲು ಆಗುವುದಿಲ್ಲ.",
        "guide_form": "ಫಾರ್ಮ್‌ಗಾಗಿ ಹೀಗೆ ಹೇಳಿ: ಫಾರ್ಮ್ ತುಂಬಲು ಸಹಾಯ ಮಾಡಿ.",
    },
    "ta": {
        "guide_private_step": "இந்தப் படியை நீங்களே முடித்து, முடிந்தது என்று சொல்லுங்கள்.",
        "screen_unavailable": "இப்போது என்னால் திரையைப் படிக்க முடியவில்லை.",
        "guide_start": "சரி, திரையில் காட்டுகிறேன்.",
        "guide_to_form": "படிவம் வந்துவிட்டது. இப்போது ஒவ்வொரு கட்டத்துக்கும் உங்களிடம் கேட்கிறேன்.",
        "guide_unavailable": "இப்போது திரையில் காட்ட முடியவில்லை. சிறிது நேரம் கழித்து மீண்டும் முயற்சி செய்யுங்கள்.",
        "guide_lost": "அடுத்த படி கிடைக்கவில்லை. மீண்டும் காட்டச் சொல்லுங்கள்.",
        "guide_done": "அருமை, முடிந்தது.",
        "guide_stopped": "சரி, இங்கேயே நிறுத்துகிறேன்.",
        "guide_timeout": "இங்கேயே நிறுத்துகிறேன். வேண்டும்போது மீண்டும் காட்டச் சொல்லுங்கள்.",
        "guide_cannot": "மன்னிக்கவும், இதைத் திரையில் காட்ட முடியாது.",
        "guide_form": "படிவத்துக்கு இப்படிச் சொல்லுங்கள்: படிவம் நிரப்ப உதவி செய்.",
    },
    "te": {
        "guide_private_step": "ఈ దశను మీరే పూర్తి చేసి, అయిపోయింది అని చెప్పండి.",
        "screen_unavailable": "ప్రస్తుతం నేను తెరను చదవలేకపోతున్నాను.",
        "guide_start": "సరే, తెరపై చూపిస్తాను.",
        "guide_to_form": "ఫారం వచ్చింది. ఇప్పుడు ప్రతి ఖాళీ కోసం మిమ్మల్ని అడుగుతాను.",
        "guide_unavailable": "ఇప్పుడు తెరపై చూపించలేకపోతున్నాను. కాసేపటి తర్వాత మళ్ళీ ప్రయత్నించండి.",
        "guide_lost": "తర్వాతి అడుగు కనిపించడం లేదు. మళ్ళీ చూపించమని అడగండి.",
        "guide_done": "బాగుంది, అయిపోయింది.",
        "guide_stopped": "సరే, ఇక్కడే ఆపుతాను.",
        "guide_timeout": "ఇక్కడే ఆపుతాను. కావాలంటే మళ్ళీ చూపించమని అడగండి.",
        "guide_cannot": "క్షమించండి, దీన్ని తెరపై చూపించలేను.",
        "guide_form": "ఫారం కోసం ఇలా చెప్పండి: ఫారం నింపడంలో సహాయం చేయి.",
    },
    "mr": {
        "guide_private_step": "ही पायरी स्वतः पूर्ण करा, मग झालं असं सांगा.",
        "screen_unavailable": "मला सध्या स्क्रीन वाचता येत नाही.",
        "guide_start": "ठीक आहे, मी स्क्रीनवर दाखवते.",
        "guide_to_form": "फॉर्म आला आहे. आता प्रत्येक रकान्यासाठी मी तुम्हाला विचारेन.",
        "guide_unavailable": "आत्ता मी स्क्रीनवर दाखवू शकत नाही. थोड्या वेळाने पुन्हा प्रयत्न करा.",
        "guide_lost": "पुढची पायरी सापडत नाहीये. पुन्हा दाखवायला सांगा.",
        "guide_done": "छान, झालं.",
        "guide_stopped": "ठीक आहे, इथेच थांबते.",
        "guide_timeout": "मी इथेच थांबते. हवं तेव्हा पुन्हा दाखवायला सांगा.",
        "guide_cannot": "माफ करा, हे मी स्क्रीनवर दाखवू शकत नाही.",
        "guide_form": "फॉर्मसाठी असं सांगा: फॉर्म भरायला मदत कर.",
    },
    "ml": {
        "guide_private_step": "ഈ ഘട്ടം സ്വയം പൂർത്തിയാക്കി കഴിഞ്ഞു എന്ന് പറയൂ.",
        "screen_unavailable": "ഇപ്പോൾ എനിക്ക് സ്ക്രീൻ വായിക്കാൻ കഴിയുന്നില്ല.",
        "guide_start": "ശരി, സ്ക്രീനിൽ കാണിക്കാം.",
        "guide_to_form": "ഫോം വന്നു. ഇനി ഓരോ കള്ളിക്കും ഞാൻ നിങ്ങളോട് ചോദിക്കും.",
        "guide_unavailable": "ഇപ്പോൾ സ്ക്രീനിൽ കാണിക്കാൻ കഴിയുന്നില്ല. കുറച്ചു കഴിഞ്ഞ് വീണ്ടും ശ്രമിക്കൂ.",
        "guide_lost": "അടുത്ത ഘട്ടം കണ്ടെത്താനാകുന്നില്ല. വീണ്ടും കാണിക്കാൻ പറയൂ.",
        "guide_done": "കൊള്ളാം, കഴിഞ്ഞു.",
        "guide_stopped": "ശരി, ഇവിടെ നിർത്തുന്നു.",
        "guide_timeout": "ഞാൻ ഇവിടെ നിർത്തുന്നു. വേണ്ടപ്പോൾ വീണ്ടും കാണിക്കാൻ പറയൂ.",
        "guide_cannot": "ക്ഷമിക്കണം, ഇത് സ്ക്രീനിൽ കാണിക്കാൻ കഴിയില്ല.",
        "guide_form": "ഫോമിനായി ഇങ്ങനെ പറയൂ: ഫോം പൂരിപ്പിക്കാൻ സഹായിക്കൂ.",
    },
    "bn": {
        "guide_private_step": "এই ধাপটি নিজে শেষ করে বলুন হয়ে গেছে।",
        "screen_unavailable": "আমি এখন স্ক্রিন পড়তে পারছি না।",
        "guide_start": "ঠিক আছে, স্ক্রিনে দেখাচ্ছি।",
        "guide_to_form": "ফর্ম এসে গেছে। এবার প্রতিটি ঘরের জন্য আপনাকে জিজ্ঞেস করব।",
        "guide_unavailable": "এখন স্ক্রিনে দেখাতে পারছি না। একটু পরে আবার চেষ্টা করুন।",
        "guide_lost": "পরের ধাপ খুঁজে পাচ্ছি না। আবার দেখাতে বলুন।",
        "guide_done": "দারুণ, হয়ে গেছে।",
        "guide_stopped": "ঠিক আছে, এখানেই থামছি।",
        "guide_timeout": "আমি এখানেই থামছি। যখন চান, আবার দেখাতে বলুন।",
        "guide_cannot": "দুঃখিত, এটা আমি স্ক্রিনে দেখাতে পারব না।",
        "guide_form": "ফর্মের জন্য বলুন: ফর্ম পূরণে সাহায্য করো।",
    },
    "gu": {
        "guide_private_step": "આ પગલું જાતે પૂરું કરો, પછી થઈ ગયું કહો.",
        "screen_unavailable": "હું અત્યારે સ્ક્રીન વાંચી શકતી નથી.",
        "guide_start": "ઠીક છે, હું સ્ક્રીન પર બતાવું છું.",
        "guide_to_form": "ફોર્મ આવી ગયું છે. હવે દરેક ખાના માટે હું તમને પૂછીશ.",
        "guide_unavailable": "અત્યારે હું સ્ક્રીન પર બતાવી શકતી નથી. થોડી વાર પછી ફરી પ્રયત્ન કરો.",
        "guide_lost": "આગળનું પગલું મળતું નથી. ફરી બતાવવા કહો.",
        "guide_done": "સરસ, થઈ ગયું.",
        "guide_stopped": "ઠીક છે, અહીં જ અટકું છું.",
        "guide_timeout": "હું અહીં જ અટકું છું. જ્યારે ઇચ્છો, ફરી બતાવવા કહો.",
        "guide_cannot": "માફ કરશો, આ હું સ્ક્રીન પર બતાવી શકતી નથી.",
        "guide_form": "ફોર્મ માટે કહો: ફોર્મ ભરવામાં મદદ કરો.",
    },
    "pa": {
        "guide_private_step": "ਇਹ ਕਦਮ ਆਪ ਪੂਰਾ ਕਰੋ, ਫਿਰ ਹੋ ਗਿਆ ਕਹੋ।",
        "screen_unavailable": "ਮੈਂ ਇਸ ਵੇਲੇ ਸਕਰੀਨ ਪੜ੍ਹ ਨਹੀਂ ਸਕਦੀ।",
        "guide_start": "ਠੀਕ ਹੈ, ਮੈਂ ਸਕਰੀਨ ਉੱਤੇ ਦਿਖਾਉਂਦੀ ਹਾਂ।",
        "guide_to_form": "ਫਾਰਮ ਆ ਗਿਆ ਹੈ। ਹੁਣ ਮੈਂ ਹਰ ਖਾਨੇ ਲਈ ਤੁਹਾਨੂੰ ਪੁੱਛਾਂਗੀ।",
        "guide_unavailable": "ਹੁਣ ਮੈਂ ਸਕਰੀਨ ਉੱਤੇ ਦਿਖਾ ਨਹੀਂ ਸਕਦੀ। ਥੋੜ੍ਹੀ ਦੇਰ ਬਾਅਦ ਫਿਰ ਕੋਸ਼ਿਸ਼ ਕਰੋ।",
        "guide_lost": "ਅਗਲਾ ਕਦਮ ਨਹੀਂ ਮਿਲ ਰਿਹਾ। ਫਿਰ ਤੋਂ ਦਿਖਾਉਣ ਲਈ ਕਹੋ।",
        "guide_done": "ਸ਼ਾਬਾਸ਼, ਹੋ ਗਿਆ।",
        "guide_stopped": "ਠੀਕ ਹੈ, ਇੱਥੇ ਹੀ ਰੁਕਦੀ ਹਾਂ।",
        "guide_timeout": "ਮੈਂ ਇੱਥੇ ਹੀ ਰੁਕਦੀ ਹਾਂ। ਜਦੋਂ ਚਾਹੋ, ਫਿਰ ਤੋਂ ਦਿਖਾਉਣ ਲਈ ਕਹੋ।",
        "guide_cannot": "ਮਾਫ਼ ਕਰਨਾ, ਇਹ ਮੈਂ ਸਕਰੀਨ ਉੱਤੇ ਨਹੀਂ ਦਿਖਾ ਸਕਦੀ।",
        "guide_form": "ਫਾਰਮ ਲਈ ਕਹੋ: ਫਾਰਮ ਭਰਨ ਵਿੱਚ ਮਦਦ ਕਰੋ।",
    },
}
# The dashboard's speaker check, in the Timbre voice.
_SOUND_TESTS = {
    "hi": "नमस्ते, मैं बोल ओएस हूँ। क्या आपको मेरी आवाज़ साफ़ सुनाई दे रही है?",
    "en": "Hello, I'm Bol OS. Can you hear me clearly?",
    "kn": "ನಮಸ್ಕಾರ, ನಾನು ಬೋಲ್ ಓಎಸ್. ನನ್ನ ಧ್ವನಿ ಸ್ಪಷ್ಟವಾಗಿ ಕೇಳಿಸುತ್ತಿದೆಯೇ?",
    "ta": "வணக்கம், நான் போல் ஓஎஸ். என் குரல் தெளிவாகக் கேட்கிறதா?",
    "te": "నమస్కారం, నేను బోల్ ఓఎస్. నా గొంతు స్పష్టంగా వినిపిస్తోందా?",
    "mr": "नमस्कार, मी बोल ओएस. माझा आवाज स्पष्ट ऐकू येतोय का?",
    "ml": "നമസ്കാരം, ഞാൻ ബോൾ ഒഎസ് ആണ്. എന്റെ ശബ്ദം വ്യക്തമായി കേൾക്കുന്നുണ്ടോ?",
    "bn": "নমস্কার, আমি বোল ওএস। আমার গলা কি পরিষ্কার শুনতে পাচ্ছেন?",
    "gu": "નમસ્તે, હું બોલ ઓએસ છું. મારો અવાજ સ્પષ્ટ સંભળાય છે?",
    "pa": "ਸਤ ਸ੍ਰੀ ਅਕਾਲ, ਮੈਂ ਬੋਲ ਓਐਸ ਹਾਂ। ਕੀ ਮੇਰੀ ਆਵਾਜ਼ ਸਾਫ਼ ਸੁਣਾਈ ਦੇ ਰਹੀ ਹੈ?",
}
# The pointer's hello beside the cursor (first run, and "Show the hello again" on the dashboard): short, it
# types itself out next to the mouse.
_BUDDY_HELLO = {
    "hi": ("नमस्ते, मैं बोल ओएस हूँ।", "{hotkey} दबाकर बोलिए।"),
    "en": ("Hi, I'm Bol OS.", "Press {hotkey} and talk to me."),
    "kn": ("ನಮಸ್ಕಾರ, ನಾನು ಬೋಲ್ ಓಎಸ್.", "{hotkey} ಒತ್ತಿ ಮಾತನಾಡಿ."),
    "ta": ("வணக்கம், நான் போல் ஓஎஸ்.", "{hotkey} அழுத்திப் பேசுங்கள்."),
    "te": ("నమస్తే, నేను బోల్ ఓఎస్.", "{hotkey} నొక్కి మాట్లాడండి."),
    "mr": ("नमस्कार, मी बोल ओएस.", "{hotkey} दाबून बोला."),
    "ml": ("നമസ്കാരം, ഞാൻ ബോൾ ഒഎസ്.", "{hotkey} അമർത്തി സംസാരിക്കൂ."),
    "bn": ("নমস্কার, আমি বোল ওএস।", "{hotkey} চেপে কথা বলুন।"),
    "gu": ("નમસ્તે, હું બોલ ઓએસ છું.", "{hotkey} દબાવીને બોલો."),
    "pa": ("ਸਤ ਸ੍ਰੀ ਅਕਾਲ, ਮੈਂ ਬੋਲ ਓਐਸ ਹਾਂ।", "{hotkey} ਦਬਾ ਕੇ ਬੋਲੋ।"),
}
for _lang in LANGUAGES:
    TEMPLATES[_lang]["speaker_test"] = _SOUND_TESTS[_lang]
    TEMPLATES[_lang]["buddy_hello"], TEMPLATES[_lang]["buddy_hint"] = _BUDDY_HELLO[_lang]
    TEMPLATES[_lang]["confirm_click"] = _CONFIRM_CLICK[_lang]
    TEMPLATES[_lang]["protected_field"] = _PROTECTED_FIELD[_lang]
    TEMPLATES[_lang].update(_SCREEN_GUIDANCE[_lang])


def lang_key(language_code: str | None) -> str:
    key = (language_code or "").split("-")[0].lower()
    return key if key in TEMPLATES else DEFAULT_LANG


# A field is spoken inside a sentence, so its own trailing full stop or question mark would double the
# template's ("hain.?"), and a line break would be read as a pause.
_FIELD_END = ".,;:!?\u0964\u0965 "


def _field(value: object) -> str:
    return re.sub(r"\s+", " ", str(value)).strip().rstrip(_FIELD_END)


def say(key: str, language_code: str | None, **fields: object) -> str:
    return TEMPLATES[lang_key(language_code)][key].format(**{k: _field(v) for k, v in fields.items()})


# --- Combining the replies of a multi-step command --------------------------------------------------------

# "Chrome खोल दिया और Bangalore weather सर्च कर दिया।" Tamil, Telugu and Malayalam join two finished actions
# with "then" after a pause ("..., அப்புறம் ..."); their written "and" (மற்றும், మరియు) only joins nouns.
_JOINERS = {"hi": " और ", "en": " and ", "kn": " ಮತ್ತು ", "ta": ", அப்புறம் ", "te": ", తర్వాత ", "mr": " आणि ",
            "ml": ", പിന്നെ ", "bn": " আর ", "gu": " અને ", "pa": " ਅਤੇ "}
# The "okay," a reply may open with only fits the first clause: "Chrome खोल दिया और नहीं भेजा।"
_OPENERS = {"hi": "ठीक है,", "en": "Okay,", "kn": "ಸರಿ,", "ta": "சரி,", "te": "సరే,", "mr": "ठीक आहे,", "ml": "ശരി,",
            "bn": "ঠিক আছে,", "gu": "ઠીક છે,", "pa": "ਠੀਕ ਹੈ,"}
_SENTENCE_END = ".!?\u0964\u0965"
# English clauses after the first start in lower case ("Opened Chrome and searched for ..."), but only when
# they start with a template's own word: a name ("Raju is not in your contacts") or "I" keeps its capital.
_EN_STARTS = frozenset(t.split()[0] for t in TEMPLATES["en"].values()
                       if not t.startswith("{") and not re.match(r"I\b", t))


def _later_clause(part: str, key: str) -> str:
    opener = _OPENERS[key]
    if part.startswith(opener) and part[len(opener):].strip():
        part = part[len(opener):].strip()
    if key == "en" and part.split()[0] in _EN_STARTS:
        part = part[0].lower() + part[1:]
    return part


def join_replies(parts: list[str], lang: str | None) -> str:
    """One spoken reply for the steps of a command, joined with the language's "and": "A and B", "A, B and C"."""
    key = lang_key(lang)
    parts = list(dict.fromkeys(p.strip() for p in parts if p and p.strip()))  # "volume up and volume up"
    # "So I didn't do the rest" is a sentence of its own: "Chrome नहीं मिला। इसलिए बाकी काम नहीं किया।"
    stopped = TEMPLATES[key]["steps_stopped"]
    if len(parts) > 1 and parts[-1] == stopped:
        return f"{join_replies(parts[:-1], key)} {stopped}"
    if len(parts) <= 1:
        return parts[0] if parts else ""
    clauses = [parts[0]] + [_later_clause(p, key) for p in parts[1:]]
    heads = [c.rstrip(_SENTENCE_END + " ") for c in clauses[:-1]]
    joiner = _JOINERS[key]
    if joiner.startswith(","):
        return joiner.join([*heads, clauses[-1]])
    return ", ".join(heads) + joiner + clauses[-1]


# --- Which language a native-script command is in ---------------------------------------------------------

_SCRIPTS = ((0x0900, 0x097F, "hi"), (0x0980, 0x09FF, "bn"), (0x0A00, 0x0A7F, "pa"), (0x0A80, 0x0AFF, "gu"),
            (0x0B80, 0x0BFF, "ta"), (0x0C00, 0x0C7F, "te"), (0x0C80, 0x0CFF, "kn"), (0x0D00, 0x0D7F, "ml"))
# Devanagari is shared by Hindi and Marathi. Words only Marathi uses (and the letter ळ, which Hindi doesn't
# have) decide Marathi; anything else is Hindi, the far more common of the two.
_MARATHI_WORDS = frozenset(unicodedata.normalize("NFC", w) for w in (
    "आहे आहेत नाही नाहीये नको आणि मला तुला तुम्ही माझा माझी माझे माझ्या तुझा काय करा उघड उघडा लाव लावा पाठव "
    "पाठवा सांग सांगा बघ बघा द्या आत्ता किती होय शोधा वाजव वाजवा थांबव थांबवा वाढव वाढवा जास्त म्हणून "
    "गाणं गाणी गाणे पुढचं पुढचा मागचं मागचा मी आम्ही आपण कुठे इथे तिथे जाऊया पाणी बरं चालेल होतं झालं केलं "
    "ऐकायचं").split())
# Marathi endings Hindi never writes: case endings joined to the word ("मुंबईच्या", "स्पॉटिफायमध्ये"; Hindi
# writes its postpositions apart), the neuter past "-लं" ("उघडलं") and the present "-तोय"/"-तेय" ("येतोय").
_MARATHI_ENDINGS = ("च्या", "मध्ये", "लं", "तोय", "तेय", "ायचं", "ायची")


def _script_of(char: str) -> str | None:
    code = ord(char)
    for low, high, lang in _SCRIPTS:
        if low <= code <= high:
            return lang if unicodedata.category(char)[0] in "LM" else None
    return None


def _looks_marathi(text: str) -> bool:
    if "\u0933" in text:  # ळ, a letter Hindi does not use
        return True
    words = re.findall(r"[\u0900-\u0963\u0966-\u097f]+", unicodedata.normalize("NFC", text))
    return any(w in _MARATHI_WORDS or w.endswith(_MARATHI_ENDINGS) for w in words)


def script_language(text: str) -> str | None:
    """The language a command's native script shows: Kannada letters are Kannada, Devanagari is Hindi unless
    Marathi-only words appear. None for Latin-only text (Prisma's transcripts), which says nothing."""
    counts: dict[str, int] = {}
    for char in text:
        lang = _script_of(char)
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    if not counts:
        return None
    lang = max(counts, key=lambda k: counts[k])
    return "mr" if lang == "hi" and _looks_marathi(text) else lang


# --- Understanding short replies ---------------------------------------------------------------------------

# Letters and digits, Indic letters with their vowel signs and viramas (not the danda, which ends a
# sentence), and the zero-width joiners some keyboards put inside Malayalam and Kannada words.
_TOKEN = re.compile(r"[\w\u0900-\u0963\u0966-\u0dff\u200c\u200d]+")
# A pause: a comma or a sentence end, or a full stop or dash with a space beside it ("Wrong. Open ...",
# "galat - YouTube pe"), but not the dot in "amazon.in" or the hyphen in "no-fly".
_PAUSE = re.compile(r"[,;:!?\u0964\u0965]|[.\u2013\u2014-]\s|\s[.\u2013\u2014-]")


def _norm(word: str) -> str:
    """Comparable form: NFC (so a precomposed and a decomposed nukta match), joiners dropped, case folded."""
    return unicodedata.normalize("NFC", word).replace("\u200c", "").replace("\u200d", "").casefold()


def _words(text: str) -> list[str]:
    return [_norm(m.group()) for m in _TOKEN.finditer(text)]


def _set(*groups: str) -> frozenset[str]:
    return frozenset(_norm(w) for group in groups for w in group.split())


# A message is only sent on a clear, pure yes: every word must be a yes-word or a harmless filler. So
# "haan bhejo" and "theek hai" send, but "don't send", "haan nahi ruko" and a correction like
# "Priya ko bhejo" do not.
_YES = _set(
    "haan han haa ha haanji hanji theek thik bilkul sahi chalega accha acha achha bhejo bhej bhejiye bhejdo",
    "हाँ हां ठीक बिल्कुल बिलकुल सही चलेगा अच्छा भेजो भेज भेजिए",
    "yes yeah yep yup ok okay sure correct right send",
    "houdu haudu howdu hudu hoon hu aaytu aytu kalsu kalisu kalsi kalisi",
    "ಹೌದು ಹೂಂ ಹೂ ಸರಿ ಆಯ್ತು ಕಳುಹಿಸು ಕಳಿಸು ಕಳ್ಸು",
    "aama aamaa aam aamam amam sari seri anuppu anuppunga",
    "ஆமா ஆம் ஆமாம் சரி அனுப்பு அனுப்புங்க",
    "avunu avnu aunu sare pampu pampandi pampinchu",
    "అవును సరే సరి పంపు పంపండి",
    "ho hoy barobar chalel pathav pathva",
    "हो होय बरोबर चालेल पाठव पाठवा",
    "athe aathe athey sheri shari seri ayakku ayakkoo ayacho ayachoo",
    "അതെ ശരി അയക്കൂ അയയ്ക്കൂ അയക്ക് അയച്ചോ",
    "hyan hya hyaan pathao pathan pathiye",
    "হ্যাঁ হাঁ হ্যা ঠিক পাঠাও পাঠান পাঠিয়ে",
    "barabar moklo mokli mokal saru chalse",
    "હા હાં બરાબર ઠીક મોકલો મોકલી મોકલ ચાલશે",
    "ਹਾਂ ਹਾਂਜੀ ਠੀਕ ਸਹੀ ਬਿਲਕੁਲ ਭੇਜੋ ਭੇਜ",
)
_FILLERS = _set(
    "ji please hai do de kar karo kardo dijiye it go ahead now abhi jaldi hmm hm chalo yaar",
    "जी है दो दे कर करो दीजिए अभी चलो",
    "maadi maadu ri ಮಾಡಿ ಮಾಡು ರೀ",
    "pannu pannunga da pa பண்ணு பண்ணுங்க",
    "andi cheyyi cheyandi అండి చేయి చేయండి",
    "aahe ahe kara dya आहे करा द्या",
    "cheyyu ചെയ്യൂ",
    "ache dao আছে দাও",
    "che chhe ne છે કરો ને",
    "deo ਜੀ ਹੈ ਦਿਓ ਕਰੋ ਕਰ ਦੋ",
)
# A clear refusal. Any of these in a short reply means "don't": "mat bhejo", "beda", "don't send it".
_NO = _set(
    "no nope nah not don dont never cancel stop wait later",
    "nahi nahin nai na naa mat ruko ruk rehne baad",
    "नहीं नही ना मत रुको रुकिए रहने बाद",
    "nako nahiye thamba thamb नको नाही नाहीये थांबा थांब",
    "beda bedaa bedi illa alla ಬೇಡ ಬೇಡಿ ಇಲ್ಲ ಅಲ್ಲ",
    "vendam vendaam venam venaam venaa venda illai வேண்டாம் வேணாம் இல்லை இல்ல",
    "vaddu vaddhu ledu kaadu aapu వద్దు లేదు కాదు ఆపు",
    "vendaa veenda വേണ്ട അല്ല ഇല്ല",
    "noy thak না নয় থাক",
    "nathi ના નહીં નથી",
    "ਨਹੀਂ ਨਾ ਨਾਂ",
)
_MAX_REPLY_WORDS = 6


def is_yes(reply: str) -> bool:
    words = _words(reply)
    return (0 < len(words) <= _MAX_REPLY_WORDS
            and all(w in _YES or w in _FILLERS for w in words)
            and any(w in _YES for w in words))


def is_no(reply: str) -> bool:
    """A short reply that refuses ("nahi", "mat bhejo", "ಬೇಡ", "don't send"). Mixed replies ("haan nahi") count
    as no: when unsure, not doing it is the safe side."""
    words = _words(reply)
    return 0 < len(words) <= _MAX_REPLY_WORDS and any(w in _NO for w in words) and not is_yes(reply)


# "That was wrong" right after a command: Bol OS forgets what it learned from it. Same strictness as yes:
# a short reply made only of a wrong-word and fillers, so "galat number pe message karo" is not one.
_WRONG = _set(
    "galat ghalat galath galt glt galti galati गलत ग़लत गलती ग़लती ਗਲਤ ਗ਼ਲਤ ਗਲਤੀ ਗ਼ਲਤੀ",
    "wrong incorrect mistake",
    "tappu thappu tapp thappa tappa tappaytu tappagide tappagoytu ತಪ್ಪು ತಪ್ಪಾಯ್ತು ತಪ್ಪಾಗಿದೆ ತಪ್ಪಾಗೋಯ್ತು",
    "thavaru தப்பு தப்பா தவறு",
    "tappayindi tappaindi porapatu porapaatu తప్పు తప్పయింది తప్పైంది పొరపాటు",
    "chuk chook chukla chukle chukicha chukiche चूक चुकलं चुकले चुकला चुकीचं चुकीचे",
    "thettu tettu thett thetti thettanu thettayi തെറ്റ് തെറ്റി തെറ്റാണ് തെറ്റായി",
    "bhul bhool ভুল",
    "khotu khotun khoto ખોટું ખોટુ ભૂલ",
)
# A negation after the wrong-word turns it around: "galat nahi tha" means it was right. One before it
# ("nahi, galat tha") is still a correction. Hindi's tag "na" ("galat tha na?") is not a negation here.
_NEGATIONS = _set(
    "no not nahi nahin nai नहीं नही",
    "illa illai alla ಇಲ್ಲ ಅಲ್ಲ இல்லை இல்ல",
    "ledu kaadu లేదు కాదు",
    "nako nahiye नको नाही नाहीये",
    "അല്ല ഇല്ല",
    "noy hoyni nei নয় হয়নি নেই",
    "nathi નથી નહીં",
    "ਨਹੀਂ",
)
_WRONG_FILLERS = _NEGATIONS | _set(
    "tha thi hai ho gaya gayi hogaya ye yeh woh vo kiya kiye kar diya bilkul sab bahut yaar s na naa arre are",
    "है था थी ये यह वो वह हो गया गई गयी किया बिल्कुल सब ना अरे",
    "that this was it is very totally one the",
    "aaytu aytu ide agide aagide agoytu aagoytu idu adu ಆಯ್ತು ಆಗಿದೆ ಆಗೋಯ್ತು ಇದೆ ಇದು ಅದು",
    "pa da aachu aayiduchu pochu idhu adhu ithu athu ஆச்சு ஆயிடுச்சு போச்சு இது அது",
    "ayyindi ayindi jarigindi idi adi undi అయింది అయ్యింది జరిగింది ఇది అది ఉంది",
    "aahe ahe hota hote zala jhala jhale he te kela आहे होता होतं होते झालं झाले हे ते केलं",
    "aanu ayi aayi poyi patti pattiyi ആണ് ആയി പോയി പറ്റി ഇത് അത്",
    "hoyeche hoye geche eta ota chilo holo হয়েছে হয়ে গেছে এটা ওটা ছিল হল",
    "che chhe hatu thayu thai gai gayu aa છે હતું થયું થઈ ગઈ ગયું આ એ",
    "si eh oh ਸੀ ਹੈ ਹੋ ਗਿਆ ਗਈ ਇਹ ਉਹ",
    "re ra kano ರೀ ಕಣೋ",  # "nahi re, ...", "kaadu ra, ...": how people address each other, not words of the command
)
# What was wrong, said right after the wrong-word: "wrong song, play Believer", "galat gaana, YouTube pe". Only
# with a pause after it: without one the words are the command itself ("galat app khul gaya").
_WRONG_WHAT = _set(
    "song songs app video site website contact person",
    "gaana gana gaane gane gaanaa गाना गाने ऐप",
    "haadu ಹಾಡು paattu paatu பாட்டு paata పాట gaanam गाणं गाणे paatt പാട്ട് gaan গান geet ગીત ਗਾਣਾ ਗਾਣੇ",
)


def is_correction(reply: str) -> bool:
    """A bare "that was wrong" ("galat tha", "ತಪ್ಪು", "wrong song"), with nothing else to do."""
    words = _words(reply)
    allowed = _WRONG | _WRONG_FILLERS | _WRONG_WHAT
    if not (0 < len(words) <= _MAX_REPLY_WORDS and all(w in allowed for w in words)):
        return False
    first_wrong = next((i for i, w in enumerate(words) if w in _WRONG), None)
    return first_wrong is not None and not _negated(words, first_wrong)


def _negated(words: list[str], wrong: int) -> bool:
    """English negates before the word ("that was not wrong"), the Indian languages after it ("galat nahi
    tha", "tappu alla")."""
    return (wrong > 0 and words[wrong - 1] == "not") or any(w in _NEGATIONS for w in words[wrong + 1:])


# "No, on YouTube": a no-word can open a correction, but only followed by a pause, since "nahi" alone is just
# a no and "nahi pata" is not a command.
_CORRECTION_LEADS = _NEGATIONS | _set(
    "nope nah na naa beda vendam venda vaddu vendaa",
    "ना ಬೇಡ வேண்டாம் వద్దు വേണ്ട না ના ਨਾ",
)
# "I meant ...", "mera matlab ...": dropped from the start of the corrected command. The two-word ones can
# start a correction on their own ("I meant Notepad").
_START_MARKERS = tuple(tuple(_words(m)) for m in (
    "i meant", "i said", "mera matlab", "मेरा मतलब", "maine bola", "मैंने बोला", "maine kaha", "मैंने कहा",
    "mala mhanaycha", "मला म्हणायचं", "naanu heliddu", "ನಾನು ಹೇಳಿದ್ದು", "naan sonnadhu", "naan sonnathu",
    "நான் சொன்னது", "nenu cheppindi", "నేను చెప్పింది", "njan paranjathu", "ഞാൻ പറഞ്ഞത്", "ami bolechi",
    "আমি বলেছি", "maro matlab", "મારો મતલબ", "ਮੇਰਾ ਮਤਲਬ", "main keha", "ਮੈਂ ਕਿਹਾ",
))
_MARKERS = _START_MARKERS + tuple((w,) for w in _words(
    "meant instead rather actually matlab balki मतलब बल्कि mhanje म्हणजे mane মানে etle એટલે ਮਤਲਬ"))
# A remainder made only of these is no command: "nahi, thanks" is a no, "galat tha, rehne do" a bare
# correction.
_NOT_A_COMMAND = _YES | _FILLERS | _NO | _WRONG_FILLERS | _set(
    "thanks thank you shukriya dhanyavaad dhanyavad dhanyavadagalu nandri dhanyavadalu dhonnobad")
# "galat hai kya?" asks whether it was wrong; it is not a correction.
_QUESTIONS = _set("kya kyaa क्या kay काय")
# What may stand between the wrong-word and the corrected command when a pause closes it: "tappu, adu alla,
# Spotify alli", "wrong, wrong, Notepad", "galat, nahi, YouTube pe".
_BETWEEN = _WRONG | _WRONG_FILLERS | _CORRECTION_LEADS
_MAX_LEAD = 4


def _marker_at(words: list[str], i: int, markers: tuple[tuple[str, ...], ...] = _MARKERS) -> int:
    return max((len(m) for m in markers if tuple(words[i:i + len(m)]) == m), default=0)


def split_correction(text: str) -> str | None:
    """Split "that was wrong, do this instead" into the command to do instead.

    None if `text` is not a correction ("nahi", "galat nahi tha", "galat number pe message karo"); "" for a
    bare one ("galat tha"); else the corrected command exactly as written: "galat tha, Spotify pe chalao" ->
    "Spotify pe chalao", "nahi, YouTube pe" -> "YouTube pe", "wrong, I meant Notepad" -> "Notepad".
    """
    found = list(_TOKEN.finditer(text))
    if not found:
        return None
    if is_correction(text):
        return ""
    words = [_norm(m.group()) for m in found]
    n = len(words)

    def pause_after(i: int) -> bool:
        gap = text[found[i].end():found[i + 1].start()] if i + 1 < n else text[found[i].end():] + " "
        return _PAUSE.search(gap) is not None

    lead = 0  # "ye galat hai", "nahi, galat tha", "nahi, YouTube pe"
    while (lead < min(n, _MAX_LEAD) and words[lead] not in _WRONG
           and (words[lead] in _WRONG_FILLERS or words[lead] in _CORRECTION_LEADS)):
        lead += 1
    if lead < n and words[lead] in _WRONG:
        if lead and words[lead - 1] == "not":
            return None  # "not wrong, ..."
        # The wrong-word, then its own words ("tha", "aaytu") up to a pause. Without a pause or one of those,
        # it is part of the command: "galat number pe message karo", "Wrong Turn movie chalao".
        start, paused, named = lead + 1, pause_after(lead), False
        while not paused and start < n and (words[start] in _WRONG_FILLERS or words[start] in _WRONG
                                            or words[start] in _WRONG_WHAT):
            if words[start] in _NEGATIONS:
                return None  # "galat nahi tha, ..."
            named = named or words[start] in _WRONG_WHAT
            paused = pause_after(start)
            start += 1
        if not paused and (start == lead + 1 or named) and not _marker_at(words, start):
            return None
        bare = ""
    elif lead and any(w in _CORRECTION_LEADS for w in words[:lead]) and (
            pause_after(lead - 1) or _marker_at(words, lead)):  # "nahi, YouTube pe", "no I meant YouTube"
        start, bare = lead, None
    elif _marker_at(words, 0, _START_MARKERS):
        start, bare = 0, None
    else:
        return None
    # Drop "I meant" and a run like "no," or "not that," before the command: "galat, nahi, mera matlab
    # YouTube pe".
    while start < n:
        if skip := _marker_at(words, start):
            start += skip
        elif skip := _run_to_pause(words, start, pause_after):
            start += skip
        else:
            break
    rest = words[start:]
    if any(w in _QUESTIONS for w in rest) and all(w in _NOT_A_COMMAND or w in _QUESTIONS for w in rest):
        return None
    if all(w in _NOT_A_COMMAND for w in rest):
        return bare
    return text[found[start].start():].strip()


def _run_to_pause(words: list[str], start: int, pause_after: Callable[[int], bool]) -> int:
    """How many words from `start` are only "no", "that" and wrong-words, up to a pause; 0 if none are."""
    run = 0
    for i in range(start, min(len(words), start + _MAX_LEAD)):
        if words[i] not in _BETWEEN:
            break
        if pause_after(i):
            run = i + 1 - start
    return run
