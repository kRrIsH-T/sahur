import logging
import sys

import pytest

from bolos.forms.profile import Profile, ProfileError, profile_path


def fake_crypto():
    """Reversible stand-in for DPAPI that marks blobs, so tests can see nothing is stored in plain text."""
    def protect(data):
        return b"ENC" + bytes(b ^ 0x5A for b in data)

    def unprotect(blob):
        assert blob.startswith(b"ENC")
        return bytes(b ^ 0x5A for b in blob[3:])
    return protect, unprotect


def test_values_round_trip_encrypted_and_never_in_repr(tmp_path):
    path = tmp_path / "form_profile.bin"
    profile = Profile(path, *fake_crypto())
    profile.update({"name": "Asha Example", "mobile": "9000000000", "otp": "123456"})
    raw = path.read_bytes()
    assert b"Asha" not in raw and b"9000000000" not in raw
    again = Profile(path, *fake_crypto())
    assert again.get("name") == "Asha Example" and again.get("mobile") == "9000000000"
    assert again.get("otp") is None  # secrets are never stored
    assert "Asha" not in repr(again) and "2 saved" in repr(again)
    assert again.kinds() == ["mobile", "name"]


def test_forget_one_and_all(tmp_path):
    path = tmp_path / "p.bin"
    profile = Profile(path, *fake_crypto())
    profile.update({"name": "A", "pan": "ABCPE1234F"})
    profile.forget("pan")
    assert Profile(path, *fake_crypto()).kinds() == ["name"]
    profile.forget("name")
    assert not path.exists()
    profile.set("name", "B")
    profile.forget()
    assert not path.exists() and profile.get("name") is None


def test_unreadable_profile_is_treated_as_empty_and_can_be_deleted(tmp_path):
    path = tmp_path / "p.bin"
    path.write_bytes(b"garbage")
    profile = Profile(path, *fake_crypto())
    assert profile.get("name") is None and profile.kinds() == []
    with pytest.raises(ProfileError) as error:
        profile.update({"name": "X"})
    assert "X" not in str(error.value)
    profile.forget()
    assert not path.exists()


def test_no_values_are_logged(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    profile = Profile(tmp_path / "p.bin", *fake_crypto())
    profile.update({"aadhaar": "234567890124"})
    profile.get("aadhaar")
    assert "234567890124" not in caplog.text


def test_profile_path_sits_next_to_preferences(tmp_path):
    assert profile_path(tmp_path / "preferences.json") == tmp_path / "form_profile.bin"


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows only")
def test_real_dpapi_round_trip(tmp_path):
    pytest.importorskip("win32crypt")
    path = tmp_path / "p.bin"
    Profile(path).update({"name": "Test Person Fake"})
    assert b"Test Person" not in path.read_bytes()
    assert Profile(path).get("name") == "Test Person Fake"
