import pytest

from bolos import actions as a
from bolos.file_intent import parse_file_request


@pytest.mark.parametrize("text, name, opens", [
    ("Sahur, open the file called Annual Report", "Annual Report", True),
    ("open the file named Client Documents", "Client Documents", True),
    ('Please open “Client Report.PDF”.', "Client Report.PDF", True),
    ('open the file "Review on Google.pdf"', "Review on Google.pdf", True),
    ("find the file called rent agreement", "rent agreement", False),
    ("show me my annual budget file", "annual budget", False),
    ("where is my tax return PDF", "tax return.pdf", False),
    ("Budget dot x l s x kholo", "Budget.xlsx", True),
    ("Meri fees wali file dhoondho", "fees", False),
    ("Report dot PDF ko dhundo", "Report.pdf", False),
    ("Budget नाम की फ़ाइल खोलो।", "Budget", True),
    ("बजट पीडीएफ खोजो", "बजट.pdf", False),
    ("ವರದಿ.pdf ತೆರೆ", "ವರದಿ.pdf", True),
    ("அறிக்கை.pdf தேடு", "அறிக்கை.pdf", False),
    ("open setup.exe", "setup.exe", True),
    (r"open file ..\Secrets\report.pdf", r"..\Secrets\report.pdf", True),
    (r"open C:\Users\Other\report.pdf", r"C:\Users\Other\report.pdf", True),
])
def test_named_file_requests(text, name, opens):
    assert parse_file_request(text) == a.FindFile(query=name, open=opens)


@pytest.mark.parametrize("text", [
    "maine ek file download ki h use kholo",
    "maine ek file download ki hai, use kholo",
    "main ne ek fail download kari h ussey khol do",
    "जो फाइल मैंने अभी डाउनलोड की है उसे खोलो।",
    "मैंने एक फ़ाइल डाउनलोड की है, उसको खोलो।",
    "jo abhi download kiya tha usko open karo",
    "abhi wali download kholo",
    "open my last download",
    "open the most recently downloaded file",
    "open the file I just downloaded",
    "I just downloaded a file. Open it",
])
def test_recent_download_requests(text):
    assert parse_file_request(text) == a.OpenRecentDownload()


@pytest.mark.parametrize("text", [
    "open Chrome",
    "file explorer kholo",
    "open the file manager",
    "open the PDF viewer",
    "open example.com",
    "open https://example.com/budget.pdf",
    "find budget PDF online",
    "Google pe budget PDF dhundo",
    "download the PDF",
    "maine ek file download karni h",
    "open it",
    "open a file",
    "show me how to open a file",
    "show me how to open taxes.pdf",
    "file kaise kholo",
    "type open budget.pdf",
    "Raju ko bolo ki budget.pdf kholo",
    "budget.pdf mat kholo",
    "open file Budget then open Settings",
    "this form kaise bharen",
    "play sunrise video",
])
def test_unrelated_commands_are_left_to_their_normal_routes(text):
    assert parse_file_request(text) is None
