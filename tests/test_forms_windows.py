from types import SimpleNamespace

import pytest

from bolos.forms.fields import Field, Snapshot
from bolos.forms.windows import FormChanged, WindowsFormBackend, metadata, public_host


class NoValue:
    CurrentIsPassword = False
    CurrentIsRequiredForForm = True

    @property
    def CurrentValue(self):
        raise AssertionError("The snapshot must never collect field values")


class Control:
    def __init__(self, name="Full name", control_type="Edit", element=None):
        self.element_info = SimpleNamespace(name=name, control_type=control_type, runtime_id=[4, 12],
                                            element=element or NoValue())
        self.iface_value = NoValue()

    def is_visible(self):
        return True

    def is_enabled(self):
        return True


def test_metadata_extracts_label_and_required_without_reading_values():
    field = metadata(Control())
    assert field == Field("4:12", "Full name", required=True)


def test_password_flag_overrides_an_innocent_accessible_name():
    field = metadata(Control("Customer code", element=SimpleNamespace(CurrentIsPassword=True)))
    assert field.kind == "secret" and field.protected


def test_unlabelled_fields_are_manual_and_nonfield_controls_ignored():
    field = metadata(Control(""))
    assert field.kind == "manual" and field.protected
    assert metadata(Control("Submit", "Button")) is None


def test_file_picker_buttons_count_as_manual_fields_and_block_submission():
    field = metadata(Control("Choose file", "Button"))
    assert field.kind == "file" and field.protected


def test_captcha_automation_id_protects_an_unclear_visible_label():
    ctrl = Control("Enter the image text")
    ctrl.element_info.automation_id = "captcha"
    assert metadata(ctrl).protected
    assert metadata(ctrl).kind == "secret"


def test_bare_radio_names_are_manual_until_group_labels_can_be_verified():
    field = metadata(Control("Yes", "RadioButton"))
    assert field.kind == "manual" and field.protected


def test_offscreen_fields_are_counted_and_prevent_automated_submission():
    ctrl = Control("Address")
    ctrl.is_visible = lambda: False
    field = metadata(ctrl)
    assert field.label == "Address" and field.protected and field.kind == "manual"


def test_missing_stable_identity_is_rejected():
    ctrl = Control()
    ctrl.element_info.runtime_id = None
    with pytest.raises(RuntimeError):
        metadata(ctrl)


def test_every_write_rechecks_snapshot_before_obtaining_control():
    backend = WindowsFormBackend()
    original = Snapshot((1, "doc"), (Field("key", "Name"),))
    backend.snapshot = lambda: Snapshot((2, "doc"), original.fields)
    with pytest.raises(FormChanged):
        backend.set_value(original, original.fields[0], "private")
    with pytest.raises(FormChanged):
        backend.submit(original)


def test_public_host_discards_all_personal_url_components():
    assert public_host("https://user:secret@example.org/customer/Asha?pan=private#private") == "example.org"
    assert public_host("javascript:alert(1)") is None
    assert public_host("https://[malformed") is None
