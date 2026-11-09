from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from docs.demo.serve import PAGES, start


@pytest.fixture
def demo():
    server, base = start()
    try:
        yield base
    finally:
        server.shutdown()
        server.server_close()


def test_all_advertised_demo_pages_exist_and_use_local_submission(demo):
    for page in PAGES:
        with urlopen(demo + page, timeout=5) as response:
            html = response.read().decode("utf-8")
            assert response.status == 200
            assert '<form' in html and 'action="/submit"' in html
            assert response.headers["Cache-Control"] == "no-store"


def test_submission_does_not_echo_values_or_attacker_controlled_referer(demo):
    personal = b"name=FAKE_PERSONAL_SECRET&mobile=9000000000"
    request = Request(demo + "submit", personal, {"Referer": 'https://evil.invalid/\"><script>private</script>'})
    with urlopen(request, timeout=5) as response:
        html = response.read().decode()
    assert "FAKE_PERSONAL_SECRET" not in html and "9000000000" not in html
    assert "evil.invalid" not in html and "<script>" not in html
    assert 'href="/form_smoke.html"' in html


def test_only_demo_submit_endpoint_accepts_post(demo):
    with pytest.raises(HTTPError) as error:
        urlopen(Request(demo + "unrelated", b"test"), timeout=5)
    assert error.value.code == 404
