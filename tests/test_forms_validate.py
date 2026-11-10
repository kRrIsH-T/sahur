"""Spoken answers in 10 languages -> field values, and validation with plain reasons."""

from datetime import date

import pytest

from bolos.forms.fields import Field
from bolos.forms.normalize import InvalidValue, amount, choose, digits, email, normalize, parse_date, spelled
from bolos.forms.prompts import PROMPTS
from bolos.forms.validate import REASONS, check, reason_text


def test_pan_application_choice_can_be_named_without_its_full_legal_label():
    options = ("New PAN - Form No. 93 (Indian Citizen)", "New PAN - Form No. 94 (Indian Entity)",
               "New PAN - Form No. 95 (Foreign Citizen)")
    assert choose(options, "Indian citizen") == options[0]
    with pytest.raises(InvalidValue):
        choose(options, "New PAN")

VALID = next(v for v in ("23456789012" + str(i) for i in range(10)) if check("aadhaar", v)[0])


@pytest.mark.parametrize("text,want", [
    ("double five six seven", "5567"), ("triple zero one", "0001"), ("dabal nau", "99"),
    ("ondu eradu mooru", "123"), ("onnu rendu moonu", "123"), ("okati rendu moodu", "123"),
    ("ek don teen", "123"), ("ek dui tin", "123"), ("ek be tran", "123"), ("ikk do tinn", "123"),
    ("98765 43210", "9876543210"), ("९८७", "987"),
])
def test_spoken_digits_in_every_language(text, want):
    assert digits(text) == want


def test_trailing_double_is_rejected():
    with pytest.raises(InvalidValue):
        digits("five double")


@pytest.mark.parametrize("text,want", [
    ("a b c p e 1 2 3 4 f", "ABCPE1234F"), ("A for apple B for ball C for cat P E one two three four F", "ABCPE1234F"),
    ("double a b", "AAB"), ("ess bee eye en", "SBIN"), ("एस बी आई एन", "SBIN"), ("double u", "W"),
])
def test_spelled_letters(text, want):
    assert spelled(text) == want


@pytest.mark.parametrize("kind,text,want", [
    ("pan", "a b c p e one two three four f", "ABCPE1234F"),
    ("ifsc", "s b i n oh zero zero one two three four", "SBIN0001234"),
    ("ifsc", "sbin 0 001234", "SBIN0001234"),
    ("mobile", "+91 98765 43210", "9876543210"), ("mobile", "098765 43210", "9876543210"),
    ("email", "ramesh dot kumar at the rate gmail dot com", "ramesh.kumar@gmail.com"),
    ("email", "r a m e s h one two three at gmail dot com", "ramesh123@gmail.com"),
    ("email", "asha underscore k at yahoo dot co dot in", "asha_k@yahoo.co.in"),
    ("email", "रमेश एट जीमेल डॉट कॉम", None),  # native-script names cannot be an email address
])
def test_codes_and_emails(kind, text, want):
    if want is None:
        with pytest.raises(InvalidValue):
            normalize(Field("a", "A", kind), text)
    else:
        assert normalize(Field("a", "A", kind), text) == want


@pytest.mark.parametrize("text,want", [
    ("15 August 1985", date(1985, 8, 15)), ("august 15 1985", date(1985, 8, 15)),
    ("15th of august 1985", date(1985, 8, 15)), ("15 agast 1985", date(1985, 8, 15)),
    ("15 अगस्त 1985", date(1985, 8, 15)), ("15 ಆಗಸ್ಟ್ 1985", date(1985, 8, 15)), ("15 ஆகஸ்ட் 1985", date(1985, 8, 15)),
    ("one five august nineteen eighty five", date(1985, 8, 15)), ("3 march two thousand three", date(2003, 3, 3)),
    ("5/11/1990", date(1990, 11, 5)), ("05-11-1990", date(1990, 11, 5)), ("1990-11-05", date(1990, 11, 5)),
    ("2 जानेवारी 2000", date(2000, 1, 2)), ("2 জানুয়ারি 2000", date(2000, 1, 2)),
])
def test_spoken_dates(text, want):
    assert parse_date(text) == want


@pytest.mark.parametrize("text", ["15 august 85", "august", "31 february 2000", "kal", "15 8 85"])
def test_unclear_dates_are_rejected_not_guessed(text):
    with pytest.raises(InvalidValue):
        parse_date(text)


def test_birth_dates_must_be_plausible():
    field = Field("d", "Date of birth", "date")
    for text in ("15 august 2090", "1 jan 1890"):
        with pytest.raises(InvalidValue) as error:
            normalize(field, text)
        assert error.value.reason in {"date_range"}


def test_date_format_follows_the_label_hint():
    assert normalize(Field("d", "Date of birth (DD/MM/YYYY)", "date"), "15 august 1985") == "15/08/1985"
    assert normalize(Field("d", "DOB", "date"), "15 ஆகஸ்ட் 1985") == "1985-08-15"


@pytest.mark.parametrize("text,want", [
    ("my name is ramesh kumar", "Ramesh Kumar"), ("mera naam sunita devi hai", "Sunita Devi"),
    ("RAMESH", "Ramesh"), ("Ramesh McKay", "Ramesh McKay"), ("रमेश कुमार", "रमेश कुमार"),
])
def test_names_are_cleaned_and_capitalised(text, want):
    assert normalize(Field("n", "Full name"), text, "name") == want


def test_names_with_digits_are_rejected():
    with pytest.raises(InvalidValue) as error:
        normalize(Field("n", "Name"), "flat 12", "name")
    assert error.value.reason == "name"


@pytest.mark.parametrize("text,want", [("do lakh", "200000"), ("2 lakh 50 hazaar", "250000"),
                                       ("150000 rupees", "150000"), ("ek crore", "10000000")])
def test_amounts(text, want):
    assert amount(text) == want


@pytest.mark.parametrize("text,want", [
    ("first", "Karnataka"), ("pehla wala", "Karnataka"), ("doosra", "Kerala"), ("kerala", "Kerala"),
    ("KERALA.", "Kerala"),
])
def test_choices_skip_placeholders_and_accept_ordinals(text, want):
    assert choose(("-- Select --", "Karnataka", "Kerala"), text) == want


@pytest.mark.parametrize("text,want", [("purush", "Male"), ("ಹೆಣ್ಣು", "Female"), ("aan", "Male"),
                                       ("பெண்", "Female"), ("anya", "Other")])
def test_gender_words(text, want):
    assert choose(("Male", "Female", "Other"), text) == want
    assert normalize(Field("g", "Gender"), text, "gender") == want


def test_gender_in_native_options():
    assert choose(("पुरुष", "महिला", "अन्य"), "mahila") == "महिला"


@pytest.mark.parametrize("kind,text,reason", [
    ("aadhaar", "12345", "aadhaar_length"), ("aadhaar", "234567890120", "aadhaar_checksum"),
    ("mobile", "12345", "mobile_length"), ("mobile", "1234567890", "mobile_start"),
    ("pan", "ABCDE1234F", "pan"), ("ifsc", "SBIN1001234", "ifsc"), ("pin_code", "012345", "postal"),
    ("email", "asha at gmail", "email"), ("date", "sometime", "date"), ("account_number", "123", "account"),
    ("yes_no", "maybe", "yes_no"), ("person_name", "12", "name"), ("choice", "x", "choice"),
])
def test_check_returns_reason_keys(kind, text, reason):
    value, why = check(kind, text, options=("A", "B") if kind == "choice" else ())
    assert value is None and why == reason and text not in why


def test_check_accepts_valid_values():
    assert check("aadhaar", VALID) == (VALID, None)
    assert check("pin_code", "५६०००१") == ("560001", None)
    assert check("yes_no", "haan") == ("true", None)


@pytest.mark.parametrize("language", PROMPTS)
def test_every_reason_is_explained_in_every_language(language):
    assert set(REASONS[language]) == set(REASONS["en"])
    assert reason_text("unknown_reason", language) == REASONS[language]["format"]


def test_email_helper_rejects_double_dots():
    with pytest.raises(InvalidValue):
        email("a dot dot b at c dot com")
