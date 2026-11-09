"""Common Indian names, states and cities: recognition hints and how answers are written into forms."""

import pytest

from bolos.forms import indian
from bolos.forms.fields import Field
from bolos.forms.normalize import normalize
from bolos.speech import MAX_KEYTERMS


@pytest.mark.parametrize("said,written", [
    ("asha kumari", "Asha Kumari"),
    ("ASHA KUMARI", "Asha Kumari"),
    ("raamesh kumaar", "Ramesh Kumar"),  # stretched vowels of a common name close up
    ("sunitaa devi", "Sunita Devi"),
    ("r a v i kumar", "Ravi Kumar"),  # spelled letter by letter
    ("r k laxman", "R K Laxman"),  # two initials stay initials
    ("preeti d'souza", "Preeti D'Souza"),
    ("ram-prasad", "Ram-Prasad"),
    ("Ramesh McKay", "Ramesh McKay"),  # the person's own capitals
    ("रमेश कुमार", "रमेश कुमार"),  # an Indian script is left exactly as heard
])
def test_names_are_written_as_on_an_id_card(said, written):
    assert indian.polish_name(said) == written


def test_a_valid_spelling_is_never_swapped_for_a_more_common_one():
    assert indian.polish_name("dipak das") == "Dipak Das"  # "Deepak" is on the list; "Dipak" is a name too
    assert indian.polish_name("laxmi") == "Laxmi" and indian.polish_name("lakshmi") == "Lakshmi"


@pytest.mark.parametrize("said,written", [
    ("12 mg road, gandhi nagar dilli", "12 MG Road, Gandhi Nagar Delhi"),
    ("flat 4b, 2nd cross, hsr layout, bangalore 560102", "Flat 4B, 2nd Cross, HSR Layout, Bangalore 560102"),
    ("gram post rampur, tehsil sadar, up", "Gram Post Rampur, Tehsil Sadar, UP"),
    ("near shiv mandir, nai dilli", "Near Shiv Mandir, New Delhi"),
    ("mysuru", "Mysuru"), ("bambai", "Mumbai"), ("gurgaon", "Gurugram"),
    ("12 MG Road", "12 MG Road"),
])
def test_places_and_addresses_are_written_the_way_forms_show_them(said, written):
    assert indian.polish_place(said) == written


@pytest.mark.parametrize("said,state", [
    ("karnataka", "Karnataka"), ("karnatak", "Karnataka"), ("UP", "Uttar Pradesh"), ("up", "Uttar Pradesh"),
    ("tamil nadu state", "Tamil Nadu"), ("tamilnadu", "Tamil Nadu"), ("dilli", "Delhi"), ("orissa", "Odisha"),
    ("karnataka hai", "Karnataka"), ("कर्नाटक", "Karnataka"), ("தமிழ்நாடு", "Tamil Nadu"), ("hello", None),
])
def test_states_are_recognised_however_they_are_said(said, state):
    assert indian.state_of(said) == state


def test_every_state_and_union_territory_is_listed_once():
    assert len(indian.STATES) == 36
    assert all(indian.state_of(name) == name for name in indian.STATES)


def test_a_state_is_chosen_from_a_dropdown_in_any_script():
    hindi = ["राज्य चुनें", "कर्नाटक", "केरल", "तमिलनाडु"]
    assert indian.state_option(hindi, "karnataka") == "कर्नाटक"
    assert indian.state_option(["Select", "Karnataka", "Kerala"], "karnatak") == "Karnataka"
    assert indian.state_option(["Select", "Tamil Nadu"], "தமிழ்நாடு") == "Tamil Nadu"
    assert indian.state_option(hindi, "goa") is None  # not offered: never a guess


def test_normalize_uses_the_lists_for_names_places_and_state_dropdowns():
    assert normalize(Field("n", "Full name"), "mera naam raamesh kumaar hai", "name") == "Ramesh Kumar"
    assert normalize(Field("a", "Address"), "12 mg road dilli", "address") == "12 MG Road Delhi"
    state = Field("s", "राज्य", "choice", options=("राज्य चुनें", "कर्नाटक", "केरल"), control_type="ComboBox")
    assert normalize(state, "karnataka", "place") == "कर्नाटक"


def test_recognition_hints_fit_in_one_request_and_suit_the_field():
    names = indian.keyterms("name")
    assert len(names) <= MAX_KEYTERMS and len(set(names)) == len(names)
    assert {"Ramesh", "Sunita", "Kumar", "Devi"} <= set(names)  # men's and women's names, and surnames
    assert indian.keyterms("place", "State") == list(indian.STATES)
    city = indian.keyterms("place", "City / Town / Village")
    assert "Mysuru" in city and "Karnataka" in city and len(city) <= MAX_KEYTERMS
    assert indian.keyterms("mobile") == [] and indian.keyterms("relative_name") == names


@pytest.mark.parametrize("written,latin", [
    ("किसान", "kisaan"), ("1 से 5 लाख", "1 se 5 laakh"), ("कर्नाटक", "karnaatak"), ("ಕರ್ನಾಟಕ", "karnaatak"),
    ("Karnataka", "Karnataka"),
])
def test_indian_scripts_are_romanized_the_way_they_are_said(written, latin):
    assert indian.romanize(written) == latin


HINDI_JOBS = ("चुनें", "किसान", "मज़दूर", "गृहिणी", "छात्र", "नौकरी")
HINDI_MARITAL = ("चुनें", "अविवाहित", "विवाहित", "विधवा / विधुर", "तलाकशुदा")
HINDI_INCOME = ("चुनें", "1 लाख से कम", "1 से 5 लाख", "5 से 10 लाख", "10 लाख से ज़्यादा")


@pytest.mark.parametrize("options,said,chosen", [
    (HINDI_JOBS, "kisaan", "किसान"), (HINDI_JOBS, "mazdoor", "मज़दूर"), (HINDI_JOBS, "chhatra", "छात्र"),
    (HINDI_MARITAL, "shaadishuda", "विवाहित"), (HINDI_MARITAL, "kunwara", "अविवाहित"),
    (HINDI_MARITAL, "talaqshuda", "तलाकशुदा"), (HINDI_MARITAL, "vidhur", "विधवा / विधुर"),
    (HINDI_INCOME, "ek se paanch lakh", "1 से 5 लाख"), (HINDI_INCOME, "paanch se das lakh", "5 से 10 लाख"),
    (HINDI_INCOME, "das lakh se zyada", "10 लाख से ज़्यादा"),
    (("Select", "Tamil Nadu", "Kerala"), "tamilnadu", "Tamil Nadu"),
])
def test_a_spoken_choice_finds_its_option_in_another_script(options, said, chosen):
    assert normalize(Field("x", "X", "choice", options=options, control_type="ComboBox"), said, "choice") == chosen


def test_an_unclear_choice_is_never_a_guess():
    assert indian.option_for(["1 लाख से कम", "10 लाख से ज़्यादा"], "lakh") is None
    assert indian.option_for(["किसान", "किसान"], "kisan") is None


@pytest.mark.parametrize("words,number", [
    ("pandrah", 15), ("unnis sau pachasi", 1985), ("unnees sau pachaasi", 1985), ("do hazaar teen", 2003),
    ("saath", 60), ("saat", 7), ("chhe", 6), ("bees", 20), ("hello", None),
])
def test_hindi_numbers(words, number):
    assert indian.hindi_number(words.split()) == number


def test_a_date_said_in_hindi_numbers_is_understood():
    assert normalize(Field("d", "जन्म तिथि", "date"), "pandrah august unnis sau pachasi") == "1985-08-15"
