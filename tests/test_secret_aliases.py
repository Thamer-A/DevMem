"""R1: a secret file named another way is still recognised (verify3/redact/t7_secretfile.py, alias/hookalias.py).

Shell commands naming a secret file by an 8.3 short name, with a trailing dot, or through a glob (`type ENV~1`,
`Get-Content NPMRC~1`, `cat .en?`, `Get-Content server.pem.`), and a Read of an 8.3 name outside the project
(`MY_CRE~1.JSO`), had their output stored.
"""
from __future__ import annotations

import pytest

from devmem.redact import Redactor, is_secret_file, mentions_secret_file

SECRET_COMMANDS = ["type ENV~1", "Get-Content NPMRC~1", "cat .en?", "cat .e*", "type .en?", "cat id_rs?", "cat *.pe?",
                   "cat server.p*", "cat my_secret?.txt", "Get-Content server.pem.", "cat '.env '",
                   "Get-Content (Get-ChildItem -Filter '*.pem')", "cat .[e]nv", "type .env::$DATA"]
PLAIN_COMMANDS = ["cat README.md", "grep -rn foo *", "ls *.py", "cat src/env_setup.py", "type PROGRA~1",
                  "cat main.py.", "pytest tests/"]


@pytest.mark.parametrize("cmd", SECRET_COMMANDS)
def test_other_spellings_of_a_secret_file_are_recognised(cmd):
    assert mentions_secret_file(cmd), cmd


@pytest.mark.parametrize("cmd", PLAIN_COMMANDS)
def test_ordinary_commands_are_not_taken_for_secret_files(cmd):
    assert not mentions_secret_file(cmd), cmd


def test_a_read_of_a_short_name_outside_the_project(tmp_path):
    red = Redactor(None)
    assert red.input_names_secret_file({"file_path": "D:\\elsewhere\\MY_CRE~1.JSO"}, tmp_path)
    assert is_secret_file("C:/x/SERVER.PEM.") and is_secret_file("C:/x/.ENV::$DATA")
