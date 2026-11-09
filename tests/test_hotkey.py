import pytest

from bolos import hotkey


def test_parse_ctrl_space():
    flags, modifier_vks, key = hotkey.parse("Ctrl+Space")
    assert flags == hotkey.MOD_CONTROL and modifier_vks == [0x11] and key == 0x20


def test_parse_letters_and_function_keys():
    assert hotkey.parse("ctrl+alt+k") == (hotkey.MOD_CONTROL | hotkey.MOD_ALT, [0x11, 0x12], ord("K"))
    assert hotkey.parse("win+f9")[2] == 0x78


@pytest.mark.parametrize("bad", ["ctrl", "ctrl+a+b", "ctrl+nonsense", ""])
def test_parse_rejects_unusable_hotkeys(bad):
    with pytest.raises(hotkey.HotkeyError):
        hotkey.parse(bad)
