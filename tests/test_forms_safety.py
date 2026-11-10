import pytest

from bolos.forms import safety
from bolos.forms.fields import Field, Form
from bolos.forms.prompts import PROMPTS


def form(*fields, title="Update details", headings=()):
    return Form(title, None, list(fields), ["Submit"], [], headings=list(headings))


@pytest.mark.parametrize("url,org", [
    ("https://uidai.gov.in/en/my-aadhaar.html", "UIDAI (Aadhaar)"), ("https://www.incometax.gov.in/iec", None),
    ("https://onlinesbi.sbi/", "SBI"), ("https://retail.onlinesbi.sbi/retail/login.htm", "SBI"),
    ("https://www.hdfcbank.com", "HDFC Bank"), ("pmkisan.gov.in/", "the Government of India"),
    ("https://sbi.bank.in", "SBI"), ("https://example.bank.in", "a bank"), ("https://pay.google.com", "Google Pay"),
    ("https://onlineservices.proteantech.in/paam/endUserRegisterContact.html", "Protean (PAN services)"),
])
def test_official_domains_and_subdomains(url, org):
    assert safety.is_official(url)
    if org:
        assert safety.official_org(safety.host_of(url)) == org


@pytest.mark.parametrize("url,claim", [
    ("https://sbi-kyc-update.xyz/login", "SBI"), ("https://onlinesbi.co/", "SBI"),
    ("https://uidai-gov.in/aadhaar", "UIDAI (Aadhaar)"), ("http://hdfcbank-rekyc.com", "HDFC Bank"),
    ("https://onlinesbi.sbi.secure-login.top", "SBI"), ("https://gov.in.kyc-check.net", "the Government of India"),
    ("https://paytm-kyc.in/upi", "Paytm"),
    ("https://onlineservices.proteantech.in.example.org/paam/endUserRegisterContact.html", "Protean (PAN services)"),
])
def test_lookalikes_are_not_official_and_name_the_brand(url, claim):
    assert not safety.is_official(url)
    warnings = safety.assess(url, form())
    assert warnings[0].key == "site_lookalike" and warnings[0].detail["org"] == claim
    assert safety.dangerous(warnings)


def test_page_claiming_a_bank_on_an_unknown_domain_is_a_lookalike():
    warnings = safety.assess("https://secure-update.top/x", form(title="State Bank of India - KYC"))
    assert warnings[0].key == "site_lookalike" and warnings[0].detail["org"] == "SBI"


def test_unknown_but_unclaimed_site_is_not_listed_not_dangerous():
    warnings = safety.assess("https://example.org/feedback", form(title="Feedback"))
    assert [w.key for w in warnings] == ["site_not_listed"] and not safety.dangerous(warnings)


def test_http_ip_and_punycode_are_flagged():
    assert "site_http" in {w.key for w in safety.assess("http://example.org", form(title="x"))}
    assert safety.assess("http://203.0.113.5/pay", form(title="x"))[0].key == "site_ip"
    assert "site_punycode" in {w.key for w in safety.assess("https://xn--sbi-xyz.com", form(title="x"))}


def test_money_secrets_on_an_insecure_page_are_dangerous():
    page = form(Field("otp", "Enter OTP", "secret"), Field("c", "ATM PIN", "secret"))
    warnings = safety.assess("http://example.org/verify", page)
    assert {"site_http", "site_money_fields"} <= {w.key for w in warnings} and safety.dangerous(warnings)
    assert warnings[-1].key != "site_official"


def test_official_page_still_mentions_money_secrets_and_is_never_called_safe():
    page = form(Field("otp", "OTP", "secret"))
    warnings = safety.assess("https://onlinesbi.sbi/otp", page)
    assert {w.key for w in warnings} == {"site_money_fields", "site_official"} and not safety.dangerous(warnings)
    text = " ".join(safety.messages(warnings, "en"))
    assert "safe" not in text.casefold() and "never" in text


def test_captcha_and_password_are_secrets_but_not_money():
    page = form(Field("p", "Password", "secret"), Field("c", "Enter captcha", "secret"))
    assert safety.risky_fields(page) == {"password": ["Password"], "captcha": ["Enter captcha"]}
    assert "secrets" in {w.key for w in safety.assess("https://incometax.gov.in", page)}


def test_card_number_fields_count_as_money_secrets():
    page = form(Field("n", "Debit card number"), Field("e", "Expiry (MM/YY)"))
    assert "card_number" in safety.risky_fields(page)


def test_local_demo_pages_and_unknown_urls():
    assert safety.assess("http://127.0.0.1:8765/kyc_bank.html", form())[0].key == "site_local"
    assert safety.assess(None, form())[0].key == "site_unknown"
    assert safety.host_of("onlinesbi.sbi/retail") == "onlinesbi.sbi"


@pytest.mark.parametrize("language", PROMPTS)
def test_every_warning_has_a_message_in_every_language(language):
    keys = ["site_lookalike", "site_official", "site_not_listed", "site_http", "site_ip", "site_punycode",
            "site_money_fields", "secrets", "site_unknown", "site_local"]
    warnings = [safety.SiteWarning(k, {"host": "a.example", "org": "SBI"}) for k in keys]
    texts = safety.messages(warnings, language)
    assert len(texts) == len(keys) and all(texts)
    assert "a.example" in texts[0] and "1930" in PROMPTS[language]["fraud"]
