"""Common Indian names, states and cities: what the form session listens for and how it writes them.

Speech recognition is not retrained here. This module helps it in two safe ways:

* `keyterms(concept)` gives Prisma a short list of words to favour for one answer (a name field gets common
  first names and surnames, a city field gets cities and states), so "Sunita" is not heard as "sun eta".
* `polish(text, concept)` writes the answer the way forms expect: proper capitals ("asha kumari" -> "Asha
  Kumari", "12 mg road" -> "12 MG Road"), a name spelled letter by letter joined up ("r a v i" -> "Ravi"),
  stretched vowels of a known name closed up ("raamesh" -> "Ramesh"), and colloquial city names in their
  written form ("dilli" -> "Delhi").

It never swaps one valid spelling for another: "Dipak" stays "Dipak" even though "Deepak" is on the list,
because an ID card's spelling is the person's own. `state_option(options, text)` picks a state from a
dropdown whatever script it is in ("karnataka" chooses "कर्नाटक" on a Hindi page).
"""

import re
import unicodedata

from bolos.speech import MAX_KEYTERMS

# Common first names across regions, the most common first (the first ones become recognition hints).
MALE_NAMES = """
Ramesh Suresh Rajesh Mahesh Mukesh Dinesh Ganesh Rakesh Naresh Sunil Anil Ashok Vijay Sanjay Ajay Amit Sumit
Rahul Rohit Mohit Vikas Vivek Manoj Santosh Deepak Pradeep Sandeep Ravi Raju Ramu Krishna Gopal Mohan Shyam
Hari Shankar Shiva Arjun Karan Kiran Akash Prakash Praveen Naveen Nitin Sachin Pankaj Pawan Vinod Vinay Vishal
Vikram Rajendra Ravindra Satish Harish Girish Manish Ashish Ankit Abhishek Aditya Anand Arun Varun Tarun
Gaurav Saurabh Sourav Subhash Sudhir Umesh Yogesh Nilesh Hitesh Jignesh Paresh Bhavesh Kalpesh Mangesh Sagar
Tushar Omkar Swapnil Srinivas Venkatesh Venkat Balaji Murali Karthik Karthikeyan Senthil Murugan Saravanan
Selvam Anbu Manjunath Nagaraj Basavaraj Mallikarjun Prabhu Raghav Raghu Biju Shaji Suraj Arvind Imran Irfan
Salman Faisal Asif Rizwan Abdul Mohammed Mohammad Gurpreet Harpreet Manpreet Jaspreet Amarjeet Sukhwinder
Baljit Debashish Partha Arnab Subhojit Amitabh Dhruv Aarav Kunal Siddharth Uday Jagdish Jayant Chandan Hemant
Bharat Dev Dharmendra Surendra Mahendra Narendra Jitendra Upendra Devendra Yashwant Kishore
""".split()

FEMALE_NAMES = """
Asha Sunita Anita Kavita Savita Sarita Geeta Seema Neha Pooja Priya Priyanka Anjali Archana Aarti Deepa
Deepika Divya Jyoti Kavya Lakshmi Laxmi Lata Meena Meera Rekha Radha Rani Renu Ritu Sangeeta Shanti Shilpa
Shobha Shweta Sneha Sonia Swati Usha Vandana Varsha Vidya Kamala Saraswati Parvathi Padma Revathi Malathi
Sumathi Bhagya Shruti Sowmya Ramya Tanvi Anusha Aparna Aishwarya Bhavana Durga Gayatri Hema Indira Jaya Komal
Madhu Manisha Nisha Poonam Fatima Ayesha Shabana Nazia Gurleen Simran Harleen Mousumi Moumita Sharmila Payal
Hetal Riya Ananya Diya Isha Ishita Kritika Nandini Pallavi Rashmi Rupa Shalini Suman Sushma Uma Vaishali
Yamini
""".split()

FIRST_NAMES = MALE_NAMES + FEMALE_NAMES

SURNAMES = """
Kumar Devi Kumari Singh Sharma Verma Gupta Yadav Patel Shah Reddy Rao Naidu Nair Menon Pillai Iyer Shetty Gowda Hegde
Bhat Joshi Kulkarni Deshpande Patil Pawar Jadhav Shinde Chavan Gaikwad More Mishra Pandey Tiwari Dubey
Shukla Tripathi Srivastava Chauhan Rathore Thakur Chaudhary Choudhary Agarwal Agrawal Jain Mehta Desai Khan
Sheikh Ansari Qureshi Siddiqui Malik Das Dutta Ghosh Bose Sen Roy Saha Mondal Paul Sarkar Banerjee
Mukherjee Chatterjee Chakraborty Bhattacharya Gill Sandhu Dhillon Sidhu Grewal Kaur Arora Kapoor Khanna
Malhotra Chopra Mehra Bhatia Sethi Ahuja Saxena Mathur Bhatt Trivedi Vyas Parmar Solanki Rana Negi Rawat
Bisht Mahato Murmu Soren Tudu Barman Bora Baruah Gogoi Kalita Swain Mohanty Behera Sahoo Panda Nayak
Pradhan Jena Patnaik Prasad Paswan Lal Nath Goud Chowdary Kurup Iyengar Krishnan Subramanian Natarajan
Kamath Pai Shenoy Naik Thomas George Joseph Mathew Varghese Kurian Abraham Fernandes Pinto Gomes Begum
Bano Khatun Ram Rajput Goswami Bansal Goyal Mittal Singhal Garg Tyagi Chaturvedi Upadhyay
""".split()

# canonical English name: (spoken forms in Latin letters, names in Indian scripts)
STATES = {
    "Andhra Pradesh": ("andhra pradesh|andhra|ap", "आंध्र प्रदेश|ఆంధ్ర ప్రదేశ్|ఆంధ్రప్రదేశ్"),
    "Arunachal Pradesh": ("arunachal pradesh|arunachal", "अरुणाचल प्रदेश"),
    "Assam": ("assam|asam|axom", "असम|অসম"),
    "Bihar": ("bihar", "बिहार"),
    "Chhattisgarh": ("chhattisgarh|chattisgarh|chhatisgarh|chattisgad|cg", "छत्तीसगढ़"),
    "Goa": ("goa", "गोवा|गोंय"),
    "Gujarat": ("gujarat|gujrat", "गुजरात|ગુજરાત"),
    "Haryana": ("haryana|hariyana", "हरियाणा"),
    "Himachal Pradesh": ("himachal pradesh|himachal|hp", "हिमाचल प्रदेश"),
    "Jharkhand": ("jharkhand|jharkhnad", "झारखंड|झारखण्ड"),
    "Karnataka": ("karnataka|karnatak|karnatakaa", "कर्नाटक|ಕರ್ನಾಟಕ"),
    "Kerala": ("kerala|keralam|kerela", "केरल|കേരളം"),
    "Madhya Pradesh": ("madhya pradesh|madhyapradesh|mp", "मध्य प्रदेश|मध्यप्रदेश"),
    "Maharashtra": ("maharashtra|maharastra|maharashtr", "महाराष्ट्र"),
    "Manipur": ("manipur", "मणिपुर"),
    "Meghalaya": ("meghalaya", "मेघालय"),
    "Mizoram": ("mizoram", "मिज़ोरम|मिजोरम"),
    "Nagaland": ("nagaland", "नागालैंड"),
    "Odisha": ("odisha|orissa|odisa", "ओडिशा|उड़ीसा|ଓଡ଼ିଶା"),
    "Punjab": ("punjab|panjab", "पंजाब|ਪੰਜਾਬ"),
    "Rajasthan": ("rajasthan|rajastan", "राजस्थान"),
    "Sikkim": ("sikkim", "सिक्किम"),
    "Tamil Nadu": ("tamil nadu|tamilnadu|tamizh nadu|tamizhnadu|tn", "तमिलनाडु|तमिल नाडु|தமிழ்நாடு|தமிழ் நாடு"),
    "Telangana": ("telangana|telengana|telangaana", "तेलंगाना|తెలంగాణ"),
    "Tripura": ("tripura", "त्रिपुरा"),
    "Uttar Pradesh": ("uttar pradesh|uttarpradesh|up", "उत्तर प्रदेश|उत्तरप्रदेश"),
    "Uttarakhand": ("uttarakhand|uttaranchal|uttrakhand", "उत्तराखंड|उत्तराखण्ड"),
    "West Bengal": ("west bengal|bengal|paschim bangal|paschim banga|wb", "पश्चिम बंगाल|পশ্চিমবঙ্গ"),
    "Andaman and Nicobar Islands": ("andaman and nicobar|andaman nicobar|andaman", "अंडमान और निकोबार"),
    "Chandigarh": ("chandigarh|chandigad", "चंडीगढ़"),
    "Dadra and Nagar Haveli and Daman and Diu": ("dadra and nagar haveli|dadra nagar haveli|daman and diu|"
                                                 "daman diu|daman|dadra", "दादरा और नगर हवेली|दमन और दीव"),
    "Delhi": ("delhi|dilli|new delhi|nai dilli", "दिल्ली|नई दिल्ली"),
    "Jammu and Kashmir": ("jammu and kashmir|jammu kashmir|jammu & kashmir|j and k|jk|kashmir", "जम्मू और कश्मीर|"
                          "जम्मू कश्मीर"),
    "Ladakh": ("ladakh|ladak", "लद्दाख"),
    "Lakshadweep": ("lakshadweep|lakshdweep", "लक्षद्वीप"),
    "Puducherry": ("puducherry|pondicherry|pondy|puduchery", "पुडुचेरी|புதுச்சேரி"),
}

# canonical written name: spoken forms that differ from it (colloquial or misspelled), "" when none
CITIES = {
    "Mumbai": "bambai|bombay", "Delhi": "dilli", "New Delhi": "nai dilli", "Bengaluru": "bengalooru|bengluru",
    "Bangalore": "banglore|bangalor", "Hyderabad": "hydrabad|haidarabad", "Ahmedabad": "amdavad|ahmadabad",
    "Chennai": "chenai", "Kolkata": "kolkatta|kalkatta|kolkota", "Surat": "", "Pune": "puna",
    "Jaipur": "", "Lucknow": "lakhnau|lucknau", "Kanpur": "", "Nagpur": "", "Indore": "", "Thane": "thana",
    "Bhopal": "", "Visakhapatnam": "vishakhapatnam|vizag", "Patna": "", "Vadodara": "baroda", "Ghaziabad": "",
    "Ludhiana": "", "Agra": "", "Nashik": "nasik", "Faridabad": "", "Meerut": "merath", "Rajkot": "",
    "Varanasi": "banaras|benaras", "Srinagar": "", "Aurangabad": "", "Dhanbad": "", "Amritsar": "",
    "Navi Mumbai": "", "Prayagraj": "", "Allahabad": "", "Ranchi": "", "Howrah": "haora", "Coimbatore": "kovai",
    "Jabalpur": "", "Gwalior": "", "Vijayawada": "bezawada", "Jodhpur": "", "Madurai": "", "Raipur": "",
    "Kota": "", "Guwahati": "gauhati", "Chandigarh": "chandigad", "Solapur": "sholapur", "Hubballi": "hubli",
    "Dharwad": "", "Mysuru": "mysooru", "Mysore": "", "Tiruchirappalli": "trichy|tiruchi", "Bareilly": "bareli",
    "Aligarh": "", "Tiruppur": "tirupur", "Gurugram": "gurgaon", "Moradabad": "", "Jalandhar": "jalandar",
    "Bhubaneswar": "bhubaneshwar", "Salem": "", "Warangal": "", "Thiruvananthapuram": "trivandrum",
    "Bhiwandi": "", "Saharanpur": "", "Gorakhpur": "", "Guntur": "", "Bikaner": "", "Amravati": "", "Noida": "",
    "Jamshedpur": "", "Bhilai": "", "Cuttack": "katak", "Kochi": "cochin", "Nellore": "", "Bhavnagar": "",
    "Dehradun": "dehradoon", "Durgapur": "", "Asansol": "", "Nanded": "", "Kolhapur": "", "Ajmer": "",
    "Kalaburagi": "gulbarga", "Jamnagar": "", "Ujjain": "", "Siliguri": "", "Jhansi": "", "Jammu": "",
    "Mangaluru": "mangalore", "Erode": "", "Belagavi": "belgaum", "Tirunelveli": "", "Gaya": "",
    "Udaipur": "", "Kozhikode": "calicut", "Thrissur": "trichur", "Kurnool": "", "Rajahmundry": "",
    "Davanagere": "davangere", "Ballari": "bellary", "Shivamogga": "shimoga", "Tumakuru": "tumkur",
    "Hassan": "", "Mandya": "", "Udupi": "", "Bidar": "", "Vijayapura": "bijapur", "Raichur": "", "Hosur": "",
    "Vellore": "", "Thanjavur": "tanjore", "Kanchipuram": "kanchi", "Puducherry": "pondicherry", "Panaji": "panjim",
    "Margao": "madgaon", "Shimla": "simla", "Haridwar": "", "Rishikesh": "", "Mathura": "", "Ayodhya": "",
    "Bhagalpur": "", "Muzaffarpur": "", "Darbhanga": "", "Purnia": "", "Bokaro": "", "Rourkela": "",
    "Sambalpur": "", "Puri": "", "Silchar": "", "Dibrugarh": "", "Jorhat": "", "Shillong": "", "Imphal": "",
    "Aizawl": "", "Kohima": "", "Agartala": "", "Gangtok": "", "Itanagar": "", "Port Blair": "", "Leh": "",
    "Pathankot": "", "Bathinda": "bhatinda", "Patiala": "", "Mohali": "", "Panipat": "", "Karnal": "",
    "Rohtak": "", "Hisar": "hissar", "Sonipat": "", "Ambala": "", "Alwar": "", "Bharatpur": "", "Sikar": "",
    "Bhilwara": "", "Satna": "", "Rewa": "", "Sagar": "", "Ratlam": "", "Dewas": "", "Bilaspur": "",
    "Korba": "", "Durg": "", "Akola": "", "Latur": "", "Ahmednagar": "", "Satara": "", "Sangli": "",
    "Jalgaon": "", "Ratnagiri": "", "Anand": "", "Nadiad": "", "Gandhinagar": "", "Junagadh": "", "Bhuj": "",
    "Mehsana": "mahesana", "Valsad": "", "Vapi": "", "Navsari": "", "Porbandar": "", "Kakinada": "",
    "Tirupati": "", "Anantapur": "", "Kadapa": "cuddapah", "Ongole": "", "Eluru": "", "Karimnagar": "",
    "Nizamabad": "", "Khammam": "", "Mahbubnagar": "", "Nalgonda": "", "Alappuzha": "alleppey",
    "Kollam": "quilon", "Kannur": "cannanore", "Palakkad": "palghat", "Kottayam": "", "Malappuram": "",
    "Kasaragod": "", "Nagercoil": "", "Dindigul": "", "Karur": "", "Kumbakonam": "", "Cuddalore": "",
    "Hosapete": "hospet", "Chikkamagaluru": "chikmagalur", "Kolar": "", "Chitradurga": "", "Karwar": "",
    "Gadag": "", "Bagalkot": "", "Haveri": "", "Koppal": "", "Chamarajanagar": "", "Ramanagara": "",
    "Madikeri": "", "Malda": "", "Bardhaman": "burdwan", "Kharagpur": "", "Haldia": "", "Berhampur": "",
    "Balasore": "", "Hapur": "", "Mirzapur": "", "Bulandshahr": "", "Rampur": "", "Shahjahanpur": "",
    "Etawah": "", "Sitapur": "", "Gonda": "", "Basti": "", "Azamgarh": "", "Jaunpur": "", "Ballia": "",
    "Deoria": "", "Begusarai": "", "Arrah": "", "Hajipur": "", "Siwan": "", "Chhapra": "", "Hazaribagh": "",
    "Giridih": "", "Deoghar": "",
}

# Words that appear in Indian addresses. Short ones that are abbreviations are written in capitals.
_ADDRESS_WORDS = ("nagar colony layout road street lane cross main sector phase block ward gali mohalla marg "
                  "chowk bazaar bazar puram pet palya halli wadi village post taluk tehsil district near "
                  "opposite behind house flat floor apartment society enclave extension stage plot").split()
_CAPITALS = {"mg", "jp", "hsr", "btm", "rt", "sv", "rr", "dlf", "ip", "gt", "nh", "sh", "po", "ps", "hal", "bhel",
             "up", "mp", "ap", "hp", "tn", "wb", "jk", "cg"}  # the last ones are states said by their letters


def _ascii_key(word: str) -> str:
    return re.sub(r"[^a-z]", "", word.casefold())


def _close_vowels(word: str) -> str:
    """'raamesh' -> 'ramesh', 'sunitaa' -> 'sunita': speech written with stretched vowels."""
    return re.sub(r"([aiou])\1+", r"\1", word).replace("ee", "i")


_NAMES = {n.casefold(): n for n in FIRST_NAMES + SURNAMES}
_NAMES_CLOSED: dict[str, set[str]] = {}
for _name in _NAMES.values():
    _NAMES_CLOSED.setdefault(_close_vowels(_name.casefold()), set()).add(_name)

_STATE_FORMS: dict[str, str] = {}  # every spoken or written form (casefolded) -> canonical English name
for _state, (_spoken, _native) in STATES.items():
    for _form in [_state, *_spoken.split("|"), *_native.split("|")]:
        _STATE_FORMS[unicodedata.normalize("NFC", _form).casefold()] = _state

_PLACES: dict[str, str] = {}  # spoken words (casefolded, single spaces) -> written name
for _city, _aliases in CITIES.items():
    _PLACES[_city.casefold()] = _city
    for _alias in filter(None, _aliases.split("|")):
        _PLACES.setdefault(_alias, _city)
for _state, (_spoken, _) in STATES.items():
    _PLACES.setdefault(_state.casefold(), _state)
    for _form in _spoken.split("|"):
        if len(_form) > 3:  # "up", "mp" and "tn" are ordinary words inside an address
            _PLACES.setdefault(_form, _state)
_PLACE_SPAN = max(len(k.split()) for k in _PLACES)


def keyterms(concept: str, label: str = "") -> list[str]:
    """Words for Prisma to favour while it hears the answer to one field (at most MAX_KEYTERMS)."""
    if concept in {"name", "relative_name", "nominee"}:
        # men's and women's names alike (the lists differ in length; the extra ones come later anyway)
        mixed = [n for pair in zip(MALE_NAMES, FEMALE_NAMES, strict=False) for n in pair]
        terms = mixed[:130] + SURNAMES[:70]
    elif concept == "place" and is_state_field(label):
        terms = list(STATES)
    elif concept in {"place", "address"}:
        terms = list(STATES) + list(CITIES)
    else:
        return []
    return list(dict.fromkeys(terms))[:MAX_KEYTERMS]


def is_state_field(label: str) -> bool:
    return bool(re.search(r"\bstate\b|\bu\.?t\.?\b|राज्य|ರಾಜ್ಯ|மாநிலம்|రాష్ట్రం|സംസ്ഥാനം|রাজ্য|રાજ્ય|ਰਾਜ",
                          (label or "").casefold()))


def _fold(text: str) -> str:
    """Lower-case words with single spaces. Letters, digits and Indic vowel signs stay inside a word."""
    text = unicodedata.normalize("NFC", text or "").casefold()
    return " ".join("".join(c if unicodedata.category(c)[0] in "LNM" or c == "&" else " " for c in text).split())


_STATE_FORMS = {_fold(k): v for k, v in _STATE_FORMS.items()}
_FILLER = re.compile(r"\b(state|rajya|ka|ki|hai|mein|me|se|from|is)\b|राज्य")


def state_of(text: str) -> str | None:
    """The state or union territory named in `text` ("karnatak", "UP", "कर्नाटक"), else None."""
    said = " ".join(_FILLER.sub(" ", _fold(text)).split())
    if said in _STATE_FORMS:
        return _STATE_FORMS[said]
    closed = _close_vowels(said)
    found = {v for k, v in _STATE_FORMS.items() if k.isascii() and _close_vowels(k) == closed}
    return found.pop() if len(found) == 1 else None


def state_option(options, text: str) -> str | None:
    """The dropdown option for the state named in `text`, matching the option in any script."""
    state = state_of(text)
    if state is None:
        return None
    matches = [o for o in options if _STATE_FORMS.get(_fold(o)) == state]
    return matches[0] if len(matches) == 1 else None


def _capital(word: str) -> str:
    """'asha' -> 'Asha', "d'souza" -> "D'Souza", 'ram-prasad' -> 'Ram-Prasad'."""
    return re.sub(r"[a-z]+", lambda m: m.group().capitalize(), word.lower())


def _join_spelled(words: list[str]) -> list[str]:
    """Three or more single letters in a row are one spelled-out word: 'r a v i' -> 'ravi'."""
    out, run = [], []
    for word in [*words, ""]:
        if len(word) == 1 and word.isalpha():
            run.append(word)
            continue
        if len(run) >= 3:
            out.append("".join(run))
        else:
            out.extend(run)
        run = []
        if word:
            out.append(word)
    return out


def _known_name(word: str) -> str | None:
    key = word.casefold()
    if key in _NAMES:
        return _NAMES[key]
    if re.search(r"([aiou])\1|ee", key) is None:
        return None  # only stretched vowels are closed up; other spellings are the person's own
    found = _NAMES_CLOSED.get(_close_vowels(key), set())
    return next(iter(found)) if len(found) == 1 else None


def polish_name(text: str) -> str:
    words = _join_spelled(text.split())
    if not all(w.isascii() for w in words):
        return " ".join(words)  # written in an Indian script: leave it exactly as heard
    out = []
    for word in words:
        known = _known_name(word)
        if known is not None:
            out.append(known)
        elif not word.islower() and not word.isupper():
            out.append(word)  # written with its own capitals ("DSouza", "McLeod"): the person's own
        else:
            out.append(_capital(word))
    return " ".join(out)


def _split_punctuation(word: str) -> tuple[str, str]:
    """'Road,' -> ('Road', ',')."""
    match = re.search(r"[^\w']+$", word)
    return (word[:match.start()], match.group()) if match else (word, "")


def _places(words: list[str]) -> list[str]:
    """Replace the longest spoken place names with their written form, left to right."""
    out, i = [], 0
    while i < len(words):
        for span in range(min(_PLACE_SPAN, len(words) - i), 0, -1):
            key = " ".join(_ascii_key(w) for w in words[i:i + span])
            if key in _PLACES:
                out.append(_PLACES[key] + _split_punctuation(words[i + span - 1])[1])
                i += span
                break
        else:
            out.append(words[i])
            i += 1
    return out


def polish_place(text: str) -> str:
    """A city, village or address written the way forms show it."""
    words = text.split()
    if not words or not all(w.isascii() for w in words):
        return text.strip()
    placed = set(_PLACES.values())
    out = []
    for word in _places(words):
        bare, mark = _split_punctuation(word)
        if bare in placed or not bare.islower():
            out.append(word)  # a place's written name, or a word already written with capitals
        elif bare in _CAPITALS:
            out.append(bare.upper() + mark)
        elif bare[:1].isdigit():
            out.append((bare.upper() if re.fullmatch(r"\d+[a-z]", bare) else bare) + mark)  # '12b' -> '12B'
        else:
            out.append(_capital(bare) + mark)
    return " ".join(out)


# ---------------------------------------------------------------------------------------------------------
# Indian scripts in Latin letters, to compare a dropdown's "किसान" with the "kisaan" Prisma writes. The ten
# Indic blocks share one layout (ISCII), so one table of offsets from each block's start serves them all.

_INDIC_BLOCKS = (0x0900, 0x0980, 0x0A00, 0x0A80, 0x0B00, 0x0B80, 0x0C00, 0x0C80, 0x0D00)
_VOWELS = {0x05: "a", 0x06: "aa", 0x07: "i", 0x08: "ii", 0x09: "u", 0x0A: "uu", 0x0B: "ri", 0x0E: "e", 0x0F: "e",
           0x10: "ai", 0x12: "o", 0x13: "o", 0x14: "au"}
_SIGNS = {0x3E: "aa", 0x3F: "i", 0x40: "ii", 0x41: "u", 0x42: "uu", 0x43: "ri", 0x46: "e", 0x47: "e", 0x48: "ai",
          0x4A: "o", 0x4B: "o", 0x4C: "au"}
_CONSONANTS = dict(zip(range(0x15, 0x3A), (
    "k kh g gh ng ch chh j jh ny t th d dh n t th d dh n n p ph b bh m y r r l l zh v sh sh s h").split(), strict=True))
_MARKS = {0x01: "n", 0x02: "n", 0x03: "h"}  # chandrabindu, anusvara, visarga
_VIRAMA, _NUKTA = 0x4D, 0x3C


def _indic(c: str) -> int | None:
    code = ord(c)
    block = next((b for b in _INDIC_BLOCKS if b <= code < b + 0x80), None)
    return None if block is None else code - block


def romanize(text: str) -> str:
    """'किसान' -> 'kisaan', '1 से 5 लाख' -> '1 se 5 laakh', 'ಕರ್ನಾಟಕ' -> 'karnaataka'. Latin text is unchanged."""
    out: list[str] = []
    for word in unicodedata.normalize("NFC", text).split():
        letters, pending = [], False  # pending: a consonant still carrying its inherent 'a'
        for c in word:
            offset = _indic(c)
            if offset is None:
                letters.append(("a" if pending else "") + c)
                pending = False
                continue
            if offset in _CONSONANTS:
                letters.append(("a" if pending else "") + _CONSONANTS[offset])
                pending = True
            elif offset in _SIGNS:
                letters.append(_SIGNS[offset])
                pending = False
            elif offset == _VIRAMA or offset == _NUKTA:
                pending = pending and offset == _NUKTA
            elif offset in _VOWELS:
                letters.append(("a" if pending else "") + _VOWELS[offset])
                pending = False
            elif offset in _MARKS:
                letters.append(("a" if pending else "") + _MARKS[offset])
                pending = False
            elif 0x66 <= offset <= 0x6F:
                letters.append(("a" if pending else "") + str(offset - 0x66))
                pending = False
        out.append("".join(letters))  # a final inherent 'a' is not said in Hindi: 'लाख' is 'laakh'
    return " ".join(out)


def loose(text: str) -> str:
    """A spelling-tolerant key for Latin words: 'kisaan', 'kisan' and 'kissan' all become 'kisan'."""
    text = re.sub(r"[^a-z0-9 ]+", " ", romanize(text).casefold())
    text = text.replace("ee", "i").replace("oo", "u").replace("w", "v").replace("z", "j").replace("q", "k")
    text = re.sub(r"([a-z])\1+", r"\1", text)
    return " ".join(re.sub(r"(?<=[aeiou])h$", "", word) for word in text.split())  # 'pandrah' = 'pandra'


# Hindi numbers as Prisma writes them, 1 to 99: Hindi has a word for each (unlike English, no pattern).
_HINDI_NUMBERS = (
    "ek do teen chaar paanch chhah saat aath nau das gyaarah baarah terah chaudah pandrah solah satrah athaarah "
    "unnees bees ikkees baaees teyees chaubees pachchees chhabbees sattaaees atthaaees untees tees ikattees "
    "battees taintees chauntees paintees chhattees saintees adtees untaalees chaalees iktaalees bayaalees "
    "taintaalees chauvaalees paintaalees chhiyaalees saintaalees adtaalees unchaas pachaas ikyaavan baavan tirpan "
    "chauvan pachpan chhappan sattaavan atthaavan unsath saath iksath baasath tirsath chausath painsath chhiyaasath "
    "sadsath adsath unhattar sattar ikhattar bahattar tihattar chauhattar pachhattar chhihattar sathattar "
    "athhattar unaasi assi ikyaasi bayaasi tiraasi chauraasi pachaasi chhiyaasi sattaasi athaasi navaasi nabbe "
    "ikyaanave baanave tiraanave chauraanave pachaanave chhiyaanave sattaanave atthaanave ninyaanave").split()
_HINDI_NUMBER = {loose(word): n for n, word in enumerate(_HINDI_NUMBERS, 1)}
_HINDI_NUMBER.update({loose(w): n for w, n in (("chhe", 6), ("chah", 6), ("unnis", 19), ("unis", 19), ("bis", 20),
                                               ("pachis", 25), ("pachas", 50), ("pachasi", 85), ("nabe", 90))})
_HUNDRED, _THOUSAND = {"sau", "so"}, {"hajar", "hajaar", "hazar", "hazaar"}


def hindi_number(words: list[str]) -> int | None:
    """'pandrah' -> 15, 'unnis sau pachasi' -> 1985, 'do hazaar teen' -> 2003, else None."""
    keys = [loose(w) for w in words]
    if not keys or any(not k for k in keys):
        return None
    if any(k in _THOUSAND for k in keys):
        at = next(i for i, k in enumerate(keys) if k in _THOUSAND)
        head = hindi_number(words[:at]) if at else 1
        tail = hindi_number(words[at + 1:]) if at + 1 < len(words) else 0
        return head * 1000 + tail if head is not None and tail is not None and head < 100 else None
    if any(k in _HUNDRED for k in keys):
        at = next(i for i, k in enumerate(keys) if k in _HUNDRED)
        head = hindi_number(words[:at]) if at else 1
        tail = hindi_number(words[at + 1:]) if at + 1 < len(words) else 0
        return head * 100 + tail if head is not None and tail is not None and tail < 100 else None
    return _HINDI_NUMBER.get(keys[0]) if len(keys) == 1 else None


def option_for(options, said: str) -> str | None:
    """The option a spoken answer names when its script or spelling differs: 'kisaan' -> 'किसान',
    'ek se paanch lakh' -> '1 से 5 लाख'. Exact first, then ignoring short a's (Hindi leaves many unsaid:
    'तमिलनाडु' is 'tamilnadu'). Two candidates are never a guess."""
    words = []
    for word in said.split():
        number = hindi_number([word])
        words.append(str(number) if number is not None else word)
    key = loose(" ".join(words))
    if not key:
        return None
    # "विधवा / विधुर" is named by either half
    names = {o: [loose(o), *(loose(part) for part in o.split("/") if "/" in o)] for o in options}
    for fold in (lambda k: k, lambda k: k.replace("a", "")):
        matches = [o for o in options if fold(key) in {fold(n) for n in names[o] if n}]
        if len(matches) == 1:
            return matches[0]
        if matches:
            return None
    return None


def polish(text: str, concept: str) -> str:
    if concept in {"name", "relative_name", "nominee"}:
        return polish_name(text)
    if concept in {"place", "address"}:
        return polish_place(text)
    return text


__all__ = ["CITIES", "FEMALE_NAMES", "FIRST_NAMES", "MALE_NAMES", "STATES", "SURNAMES", "hindi_number",
           "is_state_field", "keyterms", "loose", "option_for", "polish", "polish_name", "polish_place", "romanize",
           "state_of", "state_option"]
