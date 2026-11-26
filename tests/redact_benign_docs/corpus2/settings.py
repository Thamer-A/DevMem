"""Settings for the app."""
import os

PASSWORD_HELP = """Enter the password for your account."""
API_KEY_DESCRIPTION = """
The API key is shown once in the console. Copy it into API_KEY_FILE.
"""
TOKEN_USAGE = """
usage: app token [--ttl SECONDS]
  prints a new token
"""
SECRET_KEY = os.environ.get("SECRET_KEY")
password_prompt = """
Password:
"""
token_template = """{token}"""
api_key_example = """sk-example"""


def get_password():
    """Return the password from the environment.

    The password is never logged.
    """
    return os.environ["DB_PASSWORD"]


SQL = """
SELECT name, password_hash, token_count FROM users WHERE key = %s
"""
