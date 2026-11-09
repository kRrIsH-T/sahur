import pytest

from bolos.forms.fields import Field, field_kind
from bolos.forms.normalize import InvalidValue, digits, normalize, valid_aadhaar


@pytest.mark.parametrize("label,kind", [
    ("PAN", "pan"), ("PAN number", "pan"), ("PIN code", "postal"), ("Postal PIN", "postal"),
    ("Bank PIN", "secret"), ("Enter PIN", "secret"), ("UPI PIN", "secret"), ("OTP", "secret"),
    ("Password", "secret"), ("CVV", "secret"), ("CAPTCHA", "secret"), ("Upload Aadhaar", "file"),
    ("Aadhaar number", "aadhaar"), ("आधार नंबर", "aadhaar"), ("Date of birth", "date"),
    ("Enter the characters shown in the image", "secret"), ("Verification code", "secret"),
    ("ओटीपी", "secret"), ("ಒಟಿಪಿ", "secret"), ("ஓடிபி", "secret"), ("ఓటీపీ", "secret"),
    ("ഒടിപി", "secret"), ("ওটিপি", "secret"), ("ઓટીપી", "secret"), ("ਓਟੀਪੀ", "secret"),
    ("ಪಿನ್", "secret"), ("பின்", "secret"), ("పిన్", "secret"), ("പിൻ", "secret"),
    ("পিন", "secret"), ("પિન", "secret"), ("ਪਿੰਨ", "secret"), ("पिन कोड", "postal"),
    ("Full name (as on Aadhaar)*", "text"), ("Name as on PAN", "text"), ("Aadhaar holder name", "text"),
    ("API key", "secret"), ("Access token", "secret"), ("Client secret", "secret"), ("Private key", "secret"),
])
def test_local_field_classification(label, kind):
    assert field_kind(label) == kind


@pytest.mark.parametrize("text,want", [
    ("one two three four five six seven eight nine zero", "1234567890"),
    ("एक दो तीन चार पाँच छह सात आठ नौ शून्य", "1234567890"),
    ("ಒಂದು ಎರಡು ಮೂರು ನಾಲ್ಕು ಐದು ಆರು ಏಳು ಎಂಟು ಒಂಬತ್ತು ಸೊನ್ನೆ", "1234567890"),
    ("ஒன்று இரண்டு மூன்று நான்கு ஐந்து ஆறு ஏழு எட்டு ஒன்பது பூஜ்யம்", "1234567890"),
    ("ఒకటి రెండు మూడు నాలుగు ఐదు ఆరు ఏడు ఎనిమిది తొమ్మిది సున్నా", "1234567890"),
    ("ഒന്ന് രണ്ട് മൂന്ന് നാല് അഞ്ച് ആറ് ഏഴ് എട്ട് ഒമ്പത് പൂജ്യം", "1234567890"),
    ("১২৩৪৫৬৭৮৯০", "1234567890"), ("૧૨૩૪૫૬૭૮૯૦", "1234567890"), ("੧੨੩੪੫੬੭੮੯੦", "1234567890"),
    ("एक दोन तीन चार पाच सहा सात आठ नऊ शून्य", "1234567890"),
])
def test_digit_conversion(text, want):
    assert digits(text) == want


def test_invalid_digit_words_reject_without_exposing_value():
    with pytest.raises(InvalidValue) as error:
        digits("my private number is six")
    assert "private" not in str(error.value)


@pytest.mark.parametrize("kind,text,want", [
    ("mobile", "nine eight seven six five four three two one zero", "9876543210"),
    ("postal", "५६०००१", "560001"), ("pan", "abcpe 1234 f", "ABCPE1234F"),
    ("ifsc", "abcd 0 123456", "ABCD0123456"), ("account", "000123456789", "000123456789"),
    ("email", "asha at example dot com", "asha@example.com"), ("date", "5/11/1990", "1990-11-05"),
    ("date", "1990-11-05", "1990-11-05"), ("checkbox", "yes", "true"), ("checkbox", "no", "false"),
])
def test_formats_preserve_exact_details(kind, text, want):
    assert normalize(Field("a", "A", kind), text) == want


@pytest.mark.parametrize("kind,text", [
    ("mobile", "1234567890"), ("postal", "012345"), ("pan", "ABCD12345F"), ("ifsc", "ABCDE123456"),
    ("date", "31/2/2000"), ("date", "11/12/90"), ("date", "sometime next week"),
    ("email", "not email"), ("checkbox", "yes no"), ("account", "123"),
])
def test_invalid_and_ambiguous_formats_are_not_guessed(kind, text):
    with pytest.raises(InvalidValue):
        normalize(Field("a", "A", kind), text)


def test_aadhaar_checksum_is_enforced_with_synthetic_valid_number():
    candidates = ["23456789012" + str(i) for i in range(10)]
    valid = [v for v in candidates if valid_aadhaar(v)]
    assert len(valid) == 1
    assert normalize(Field("a", "Aadhaar", "aadhaar"), valid[0]) == valid[0]
    for value in set(candidates) - set(valid):
        with pytest.raises(InvalidValue):
            normalize(Field("a", "Aadhaar", "aadhaar"), value)
    assert not valid_aadhaar("123456789012")


def test_unknown_choice_is_never_fuzzy_matched():
    field = Field("a", "Occupation", "choice", options=("Farmer", "Retired"))
    assert normalize(field, "farmer") == "Farmer"
    with pytest.raises(InvalidValue):
        normalize(field, "former")


@pytest.mark.parametrize("kind", ["secret", "file"])
def test_protected_inputs_never_normalize(kind):
    with pytest.raises(InvalidValue):
        normalize(Field("a", "Protected", kind), "123456")
