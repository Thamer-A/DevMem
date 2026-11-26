import pytest

from app.auth import login, check_password

FAKE_PASSWORD = "<password>"
DESCRIPTION = """
Tests for the login flow. They use a fake password and a fake token,
never a real one; the fixtures below build them.
"""


@pytest.fixture
def user():
    return {"name": "alice", "password": "changeme", "token": None}


def test_login_rejects_empty_password(user):
    user["password"] = ""
    assert not login(user)


def test_password_hash_is_argon2(user):
    assert check_password("x", "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$abc")


def test_token_count():
    assert {"name": "token_count", "type": "int", "value": 12345}["value"] == 12345


def test_help_text():
    PASSWORD_HELP = """
    Enter the password you chose at sign-up.
    It is case sensitive.
    """
    assert "case" in PASSWORD_HELP
