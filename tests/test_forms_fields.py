"""bolos.forms.fields scan/fill API: the pure tree-to-form logic with fake UIA nodes, plus an opt-in live test.

The live test (BOLOS_LIVE_BROWSER=1) starts its own Chrome/Edge with a temporary profile on the local demo page,
scans and fills it with invented values, verifies by re-scanning and closes that browser. It never touches the
user's own browser windows.
"""

import os
import re
import sys
from pathlib import Path

import pytest

from bolos.forms import fields, windows
from bolos.forms.fields import Field, Form, field_kind
from bolos.forms.windows import Node, form_from_tree, match_option

DEMO = Path(__file__).resolve().parents[1] / "docs" / "demo"


def text(name, **kw):
    return Node("Text", name=name, **kw)


def group(*children, name="", **kw):
    return Node("Group", name=name, children=list(children), **kw)


def edit(name, rid, **kw):
    return Node("Edit", name=name, runtime_id=(42, rid), **kw)


def page(*children):
    return Node("Document", name="Re-KYC Update", value="https://bank.example/kyc", children=list(children))


def test_fields_keep_reading_order_labels_required_and_values():
    tree = page(
        group(text("Full name"), text("*")),
        edit("Full name*", 1, required=True, value="Asha"),
        edit("Mobile number", 2, value="", help="10-digit mobile number"),
        Node("ComboBox", name="State*", runtime_id=(42, 3), value="Select state", required=True),
        Node("CheckBox", name="I agree", runtime_id=(42, 4), toggle=1),
    )
    form = form_from_tree(tree, hwnd=7, url="https://bank.example/kyc")
    assert [f.label for f in form.fields] == ["Full name", "Mobile number", "State", "I agree"]
    assert [f.control_type for f in form.fields] == ["Edit", "Edit", "ComboBox", "CheckBox"]
    assert [f.required for f in form.fields] == [True, False, True, False]
    assert form.fields[0].value == "Asha" and form.fields[1].value == ""
    assert form.fields[1].kind == "mobile" and form.fields[1].help == "10-digit mobile number"
    assert form.fields[3].value == "true" and form.fields[3].kind == "checkbox"
    assert form.title == "Re-KYC Update" and form.url == "https://bank.example/kyc" and form.hwnd == 7
    assert form.fields[0].key == "42:1" and form.fields[0].id == "42:1" and form.fields[0].kind_hint == "text"


def test_values_never_appear_in_repr():
    field = Field("1", "Mobile number", "mobile", value="9876543210", element=object())
    assert "9876543210" not in repr(field)


def test_secret_and_password_values_are_never_read():
    tree = page(edit("Enter OTP", 1, value="123456"), edit("Customer code", 2, value="hunter2", password=True),
                edit("Enter the characters shown in the image", 3, aid="captcha", value="7kQ2p"))
    form = form_from_tree(tree)
    assert all(f.value is None and f.protected and f.kind == "secret" for f in form.fields)
    assert form.fields[1].is_password


def test_unlabelled_field_falls_back_to_placeholder_then_preceding_text():
    tree = page(text("Nominee name"), edit("", 1), edit("", 2, help="Village"))
    labels = [f.label for f in form_from_tree(tree).fields]
    assert labels == ["Nominee name", "Village"]


def test_radio_group_becomes_one_field_with_legend_options_and_choice():
    radios = [Node("RadioButton", name=n, runtime_id=(5, i), required=True, selected=(n == "Female"))
              for i, n in enumerate(("Male", "Female", "Transgender"))]
    tree = page(group(group(text("Gender"), text("*")), *radios, name="Gender*", runtime_id=(5, 99)))
    (field,) = form_from_tree(tree).fields
    assert field.label == "Gender" and field.control_type == "RadioButton" and field.kind == "choice"
    assert field.options == ("Male", "Female", "Transgender") and field.value == "Female" and field.required
    assert [n for n, _ in field.parts] == ["Male", "Female", "Transgender"] and not field.protected


def test_radio_group_without_a_question_is_left_to_the_user():
    tree = page(group(Node("RadioButton", name="Yes", runtime_id=(1,)), Node("RadioButton", name="No",
                                                                             runtime_id=(2,))))
    (field,) = form_from_tree(tree).fields
    assert field.protected and field.kind == "manual"


def test_date_input_reads_its_spin_buttons_and_time_inputs_stay_manual():
    def spin(top, value):
        return Node("Spinner", range_max=top, range_value=value)

    date = edit("Date of birth*", 1, value="", children=[group(spin(31, 15), spin(12, 8), spin(2008, 1985))])
    empty = edit("Date of birth", 2, value="", children=[group(spin(31, 0), spin(12, 0), spin(2008, 0))])
    clock = edit("Appointment time", 3, value="", children=[group(spin(12, 1), spin(59, 0))])
    form = form_from_tree(page(date, empty, clock))
    assert form.fields[0].control_type == "Date" and form.fields[0].kind == "date"
    assert form.fields[0].value == "1985-08-15" and [r for r, _ in form.fields[0].parts] == ["day", "month", "year"]
    assert form.fields[1].value == ""
    assert form.fields[2].kind == "manual" and form.fields[2].protected


def test_file_input_is_a_protected_field_not_a_button():
    tree = page(Node("Button", name="Upload Aadhaar (front side): No file chosen", value="No file chosen",
                     runtime_id=(9,)), Node("Button", name="Submit", runtime_id=(10,)))
    form = form_from_tree(tree)
    (field,) = form.fields
    assert field.label == "Upload Aadhaar (front side)" and field.kind == "file" and field.protected
    assert field.control_type == "File" and form.buttons == ["Submit"]


def test_field_error_drops_the_help_text_and_page_alerts_come_first():
    tree = page(
        Node("Text", name="Please correct the 2 errors below.", role="alert"),
        edit("Aadhaar number*", 1, valid=False, aria="invalid=true",
             desc="12-digit number on your card. Aadhaar number must have 12 digits."),
        group(text("12-digit number on your card."), aid="aadhaar-help"),
        group(text("Aadhaar number must have 12 digits."), aid="aadhaar-error"),
        edit("PAN*", 2, aria="required=true;invalid=true", desc="Invalid PAN format."),
        edit("City", 3, desc="Town or village name"),  # valid: its description is help, not an error
    )
    form = form_from_tree(tree)
    assert form.fields[0].error == "Aadhaar number must have 12 digits."
    assert form.fields[1].error == "Invalid PAN format."
    assert form.fields[2].error is None
    assert form.errors == ["Please correct the 2 errors below.", "Aadhaar number: Aadhaar number must have 12 digits.",
                           "PAN: Invalid PAN format."]


def test_help_only_description_of_an_invalid_field_is_not_an_error():
    tree = page(edit("Full name*", 1, aria="invalid=true", desc="As printed on Aadhaar."),
                group(text("As printed on Aadhaar."), aid="full_name-help"))
    assert form_from_tree(tree).fields[0].error is None


def test_headings_and_buttons():
    tree = page(Node("Text", name="Update your KYC details", role="heading",
                     children=[text("Update your KYC details")]),
                Node("Button", name="Send OTP"), Node("Button", name="Submit"), Node("Button", name=""))
    form = form_from_tree(tree)
    assert form.headings == ["Update your KYC details"] and form.buttons == ["Send OTP", "Submit"]
    assert form.fields == []


def test_dropdown_placeholder_is_not_an_option_or_a_value():
    combo = Field("1", "State", "choice", control_type="ComboBox", value="Select state")
    items = [(n, None) for n in ("Select state", "Karnataka", "Kerala")]
    got = windows._with_options(combo, items)
    assert got.options == ("Karnataka", "Kerala") and got.value == ""
    hindi = windows._with_options(Field("2", "राज्य", "choice", control_type="ComboBox", value="कर्नाटक"),
                                  [(n, None) for n in ("राज्य चुनें", "कर्नाटक")])
    assert hindi.options == ("कर्नाटक",) and hindi.value == "कर्नाटक"


def test_option_matching_is_exact_or_unique_never_a_guess():
    states = ["Andhra Pradesh", "Madhya Pradesh", "Uttar Pradesh", "Karnataka", "Kerala", "Tamil Nadu"]
    assert match_option(states, "karnataka") == "Karnataka"
    assert match_option(states, "tamil nadu.") == "Tamil Nadu"
    assert match_option(states, "Karnatak") == "Karnataka"
    assert match_option(states, "Pradesh") is None  # three states: ambiguous
    assert match_option(states, "Ker") == "Kerala"
    assert match_option(states, "Ka") is None
    assert match_option(["Yes", "No"], "no") == "No"
    assert match_option(["1 to 5 lakh", "5 to 10 lakh"], "1 to 5 lakh") == "1 to 5 lakh"


def test_dates_parse_iso_and_day_first():
    assert windows._parse_date("1985-08-15") == (1985, 8, 15)
    assert windows._parse_date("15/08/1985") == (1985, 8, 15)
    assert windows._parse_date("31-02-1985") is None and windows._parse_date("8/15") is None


def test_protected_fields_are_refused_before_any_ui_call():
    element = object()  # any UIA call on it would raise
    for field in (Field("1", "OTP", "secret", protected=True, element=element),
                  Field("2", "Password", "text", is_password=True, element=element),
                  Field("3", "Upload", "file", protected=True, control_type="File", element=element),
                  Field("4", "Name", "text")):  # no element: not from a scan
        assert fields.fill(field, "123456") is False
        assert fields.choose(field, "x") is False and fields.check_box(field, True) is False


def test_press_refuses_missing_or_ambiguous_buttons():
    form = Form("t", None, [], ["Submit", "Submit"], [], button_elements={"Submit": [object(), object()]})
    assert fields.press(form, "Submit") is False
    assert fields.press(form, "Pay now") is False


def test_card_data_and_banking_logins_are_secret_but_id_cards_are_not():
    for label in ("Debit card number", "Card expiry (MM/YY)", "Net banking user ID", "CVV", "ATM PIN",
                  "Net banking password", "डेबिट कार्ड नंबर"):
        assert field_kind(label) == "secret", label
    assert field_kind("Aadhaar card number") == "aadhaar"
    assert field_kind("PAN card number") == "pan"
    assert field_kind("Ration card number") == "text"


def _inputs(html: str) -> list[str]:
    return re.findall(r'<(?:input|select|textarea)[^>]*\bid="([^"]+)"', html)


def test_hindi_page_is_a_complete_translation_of_the_english_form():
    english = (DEMO / "kyc_bank.html").read_text(encoding="utf-8")
    hindi = (DEMO / "kyc_hindi.html").read_text(encoding="utf-8")
    assert _inputs(hindi) == _inputs(english) and len(_inputs(english)) >= 20
    assert english.count("<option") == hindi.count("<option")
    assert 'lang="hi"' in hindi and "जमा करें" in hindi and 'src="form.js"' in hindi


def test_scam_page_asks_for_what_banks_never_ask():
    scam = (DEMO / "fake_kyc_scam.html").read_text(encoding="utf-8").casefold()
    for word in ("otp", "atm pin", "cvv", "password", "card number"):
        assert word in scam
    assert "fake" in scam


def test_demo_files_never_use_the_em_dash():
    for path in DEMO.iterdir():
        if path.suffix in {".html", ".js", ".css", ".py", ".md", ".svg"}:
            assert "—" not in path.read_text(encoding="utf-8"), path.name


def test_submitted_page_matches_the_form_language_and_warns_after_the_scam():
    from docs.demo.serve import received_page

    assert "Reference number: DGB-RK-" in received_page("/kyc_bank.html")
    assert "संदर्भ संख्या" in received_page("/kyc_hindi.html")
    assert "1930" in received_page("/fake_kyc_scam.html")


LIVE = os.environ.get("BOLOS_LIVE_BROWSER") == "1" and sys.platform == "win32"


@pytest.mark.skipif(not LIVE, reason="set BOLOS_LIVE_BROWSER=1 to drive a private Chrome/Edge window")
@pytest.mark.parametrize("which", ["chrome", "edge"])
def test_live_scan_fill_verify_in_a_private_browser(which):
    from docs.demo.browser import find_browser, private_browser
    from docs.demo.serve import start

    if find_browser(which) is None:
        pytest.skip(f"{which} not installed")
    values = {"Full name (as on Aadhaar)": "Asha Example", "Date of birth": "1985-08-15", "Gender": "Female",
              "Mobile number": "9876543210", "State": "Karnataka", "PIN code": "570001",
              "I declare that the information given above is true": "true"}
    server, base = start()
    try:
        with private_browser(base + "kyc_bank.html", which) as (hwnd, _):
            form = fields.scan(hwnd)
            assert form.url == base + "kyc_bank.html" == fields.browser_url(hwnd)
            assert form.buttons[-1] == "Submit" and len(form.fields) == 25
            assert not fields.fill(form.field("Enter OTP sent to your mobile"), "123456")
            for label, value in values.items():
                assert fields.fill(form.field(label), value), label
            again = fields.scan(hwnd)
            assert {label: again.field(label).value for label in values} == values
            assert fields.press(again, "Submit")
            assert any("required" in e for e in fields.errors(hwnd))  # father's name etc. still empty
    finally:
        server.shutdown()
        server.server_close()


def test_the_pan_application_page_is_labelled_the_way_a_person_reads_it():
    """The Online PAN application form (onlineservices.proteantech.in, scanned live on 8 Nov 2026): unlabelled
    dropdowns report their selected option as their name, the consent box's sentence comes after it and is
    split by a link, and reCAPTCHA is a checkbox."""
    from bolos.forms import kinds

    def combo(value, rid):
        return Node("ComboBox", name=value, value=value, runtime_id=(42, rid))

    tree = page(
        text("Select PAN Application Type*"), text("(New or Change Request)"), combo("----Please Select------", 1),
        text("Select Applicant Category*"), text("(Individual,Trust,HUF,...)"), combo("INDIVIDUAL", 2),
        text("Mobile Number"), text("*"), edit("Mobile Number", 3),  # the star in its own span, as live
        Node("CheckBox", runtime_id=(42, 4), toggle=0),
        text("By submitting data to us and/or using our Protean e-Gov TIN web site"),
        Node("Hyperlink", name="https://onlineservices.proteantech.in/paam/endUserRegisterContact.html",
             children=[text("https://onlineservices.proteantech.in/paam/endUserRegisterContact.html")]),
        text("you give your consent that all personal data/information that you submit may be processed"),
        Node("CheckBox", name="I'm not a robot", runtime_id=(42, 5), toggle=0),
    )
    form = form_from_tree(tree)
    application, category, mobile, consent, robot = form.fields
    assert application.label == "Select PAN Application Type (New or Change Request)" and application.required
    assert category.label == "Select Applicant Category (Individual,Trust,HUF,...)"  # not the chosen "INDIVIDUAL"
    assert mobile.label == "Mobile Number" and mobile.required and category.required
    assert consent.label.startswith("By submitting data") and "http" not in consent.label
    assert kinds.classify(consent) == "manual"  # a legal consent: the person reads and ticks it
    assert kinds.classify(robot) == "captcha" and kinds.sensitivity("captcha") == kinds.NEVER_FILL
