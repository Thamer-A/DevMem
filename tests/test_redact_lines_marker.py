"""`[REDACTED:lines]` stays where it belongs (verifier v9b, item 2). Sibling strings are redacted as one text, one per line,
and cut back by position. A user pattern whose match crossed a line break (`api[_-]?key\\s*[:=]\\s*\\S+`) or the omitted
lines of a secret file's diff hunk used to change the joined text's line count; the cut then kept only the equal lines
at both ends and marked every item between them, so one such change early in a list plus any ordinary redaction late
in it turned all items in between into `[REDACTED:lines]` (10,000 items: 9,998 marked; a `.env` hunk in a tree with the
hook budget: 171 of 173). Now both keep the line count: a user pattern's replacement is followed by the line breaks of
what it replaced, and the omission in a joined run empties each omitted line in place (the first holds the marker)."""
from __future__ import annotations

from devmem.redact import Redactor

TOK = "ghp_" + "Zq8rT2vLmN4xK7pW1sY9bC3dF6gH0jA5eR2u"
VALUE = "some" + "_value_" + "here"
MARK = "[REDACTED:lines]"


def _lines(n: int) -> list[str]:
    return [f"    line_{i} = compute({i})" for i in range(n)]


def test_a_user_pattern_across_a_line_break_changes_only_its_own_items():
    items = _lines(1000)
    items[1] = "    api_key:"
    items[2] = "        " + VALUE
    items[-2] = "    token = '" + TOK + "'"
    out = Redactor(None, enabled=True, extra=[r"api[_-]?key\s*[:=]\s*\S+"]).record(list(items))
    assert len(out) == len(items) and MARK not in out
    assert out[1] == "    [REDACTED:custom]" and out[2] == ""
    assert VALUE not in "".join(out) and TOK not in "".join(out)
    changed = [i for i, (a, b) in enumerate(zip(items, out)) if a != b]
    assert changed == [1, 2, len(items) - 2]


def test_a_user_pattern_keeps_the_line_count_of_a_text():
    red = Redactor(None, enabled=True, extra=[r"api[_-]?key\s*[:=]\s*\S+"])
    text = "a\napi_key:\n   " + VALUE + "\nb"
    out = red.text(text)
    assert out == "a\n[REDACTED:custom]\n\nb"
    long = "x\n" * 5000 + text          # the windowed path (longer than USER_PATTERN_FRAGMENT)
    assert red.text(long) == "x\n" * 5000 + "a\n[REDACTED:custom]\n\nb"


def test_a_construct_split_by_the_budget_cut_is_redacted_whole():
    """The budget's cut fell between an XML element's value line and its closing tag: the run stopped taking strings
    at the budget, so the element was never seen whole and its value was kept (the old marking hid it). Strings up to
    CAP_MARGIN past the budget are now redacted with the run."""
    value = "o2darf2G1w" + "1w8d7gnJIzQw5z"
    for pad in range(0, 120, 7):
        items = ["x" * 99 for _ in range(39)] + ["y" * pad, "<cfg>", "  <password>", "    " + value, "  </password>",
                                                   "</cfg>"] + ["z" * 99 for _ in range(20)]
        out, cut = Redactor(None, enabled=True).tree(list(items), 4048)
        assert cut and value[:6] not in "\n".join(out), (pad, out[-3:])


def test_omitted_lines_of_a_secret_file_in_a_list_stay_in_their_items():
    head = ["diff --git a/.env b/.env", "--- a/.env", "+++ b/.env", "@@ -1,2 +1,2 @@", "-A=1", "+DB_PASSWORD=" + TOK[4:20],
            " C=3"]
    rest = ["diff --git a/app.py b/app.py", "--- a/app.py", "+++ b/app.py", "@@ -1,3 +1,3 @@"] + \
        [f"+line_{i} = compute({i})" for i in range(150)]
    items = ["+first = 1"] + head + rest
    items[-2] = "+token = '" + TOK + "'"
    for budget in (10_000_000, 4048):
        out, _cut = Redactor(None, enabled=True).tree(list(items), budget, omit=True)
        assert MARK not in out
        assert out[0] == items[0] and out[1:4] == head[:3]
        assert out[4] == "[OMITTED: diff of a secret file]" and out[5:8] == ["", "", ""]
        assert out[8:12] == rest[:4]        # the next file's header: its own items again
        assert TOK[4:20] not in "".join(out)
        if budget > 100_000:
            assert TOK not in "".join(out) and out[13:-2] == items[13:-2]
