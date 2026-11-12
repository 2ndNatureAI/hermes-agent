import argparse
from unittest.mock import Mock

import pytest

from plugins.platforms.photon import auth, cli


@pytest.mark.parametrize("phone", ["5551234567", "+15551234567\n", "+1١٢٣٤٥٦٧٨٩٠"])
def test_invalid_explicit_phone_stops_before_login(monkeypatch, phone):
    monkeypatch.setattr(auth, "load_photon_token", Mock(side_effect=AssertionError("unexpected login")))
    assert cli._cmd_setup(argparse.Namespace(phone=phone)) == 1


def test_formatted_phone_matches_existing_user(monkeypatch):
    lookup = Mock(return_value={"id": "existing"})
    monkeypatch.setattr(auth, "find_user_by_phone", lookup)
    assert auth.register_user_if_absent("project", "secret", phone_number="+1 (555) 123-4567") == (
        {"id": "existing"}, False
    )
    lookup.assert_called_once_with("project", "secret", "+15551234567")


def test_invalid_number_stops_before_lookup_or_creation(monkeypatch):
    lookup = Mock(side_effect=AssertionError("unexpected lookup"))
    monkeypatch.setattr(auth, "find_user_by_phone", lookup)
    with pytest.raises(ValueError, match="E.164"):
        auth.register_user_if_absent("project", "secret", phone_number="+15551234567\n")
    lookup.assert_not_called()


def test_formatted_unknown_phone_does_not_create_user(monkeypatch):
    monkeypatch.setattr(auth, "find_user_by_phone", Mock(return_value=None))
    with pytest.raises(ValueError, match="E.164"):
        auth.register_user_if_absent("project", "secret", phone_number="+1 (555) 123-4567")
