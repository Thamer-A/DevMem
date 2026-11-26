"""redact.scrub_secret_parts: nothing withheld by the judgement before the file-text exception is stored, except a
file's multi-line text under a file-text key (Claude Code's Edit/Write result `originalFile`, and `original_file`,
`fileContent`, `file_content`, `fileContents`, `oldString`, `newString`), which is that file's text and not a path.

The problem: an Edit of an ordinary `.gitignore` holding the line `.env`, or of a README over 4 KB ending in
`config/.env.example`, was stored with every string replaced by the secret-file marker (about 0.24% of real results).
A first fix also stopped judging path values with a line break or over 4,096 characters, and one-line paths under the
file-text keys, and so stored a secret file's content the earlier judgement withheld. Every value under an ordinary
path key is again judged as the earlier judgement did; only multi-line text under a file-text key is not."""
from __future__ import annotations

import json
import random

import pytest
from hooks_helpers import make_project

from devmem import capture as C
from devmem import consent, hooks, store
from devmem import redact as R
from devmem.redact import OMITTED_SECRET_FILE, scrub_secret_parts

MARK = "ORDINARYQZ5"
NEEDLE = "DBHOSTQZ9"
ENV_TEXT = f"{NEEDLE}=internal-db\n"
SID = "34343434-0000-0000-0000-000000000040"


def _edit_result(path: str, original: str) -> dict:
    return {"filePath": path, "oldString": "node_modules/", "newString": "node_modules/\ndist/",
            "originalFile": original, "userModified": False,
            "structuredPatch": [{"oldStart": 1, "oldLines": 2, "newStart": 1, "newLines": 3,
                                 "lines": [" node_modules/", f"+dist/ # {MARK}", " .env"]}]}


def _long(p: str) -> str:
    return p + "/" + "/".join(["d" * 200] * 21)                    # over 4,096 characters


# ---------------------------------------------------------------- the reference: the judgement before the file-text exception, frozen here

def _names_secret_file_before(d: dict, base, patterns) -> bool:
    """devmem.redact._names_secret_file as it was before the file-text exception (copied; the helpers it calls are today's)."""
    for k, v in d.items():
        if not isinstance(k, str):
            continue
        if any(c in k for c in "./\\") and R.is_secret_file(R._named_path(k, base), R._specific(patterns)):
            return True
        key = k[:R.MAX_KEY_CHECK]
        if not R._PATH_KEY.search(key) or R._NOT_PATH_KEY.search(key):
            continue
        vals = [v] if isinstance(v, str) else [w for w in v[:R.MAX_PATH_VALUES] if isinstance(w, str)] \
            if isinstance(v, list) else []
        if any(w and not w[:7].lower().startswith(("http://", "https:/")) and
               R.is_secret_file(R._named_path(w, base), patterns) for w in vals):
            return True
    return False


def _multi_line_file_text(d: dict) -> bool:
    """The one allowed difference: a value under a file-text key with more than one line (one trailing break aside)."""
    for k, v in d.items():
        if isinstance(k, str) and R._FILE_TEXT_KEY.match(k[:R.MAX_KEY_CHECK]):
            for w in ([v] if isinstance(v, str) else v if isinstance(v, list) else []):
                if isinstance(w, str):
                    for end in ("\r\n", "\n", "\r"):
                        if w.endswith(end):
                            w = w[:-len(end)]
                            break
                    if "\n" in w or "\r" in w:
                        return True
    return False


# ---------------------------------------------------------------- ordinary content is kept

ORDINARY = {
    "gitignore_with_env_line": lambda p: _edit_result(f"{p}/.gitignore", "node_modules/\n.env\n"),
    "py_loading_dotenv": lambda p: _edit_result(f"{p}/app.py", f"from dotenv import load_dotenv\n# {MARK}\n"
                                                               "load_dotenv('config/.env')"),
    "readme_over_4kb": lambda p: _edit_result(f"{p}/README.md", "lorem ipsum " * 400 + "\nCopy config/.env.example"),
    "crlf_file_text": lambda p: _edit_result(f"{p}/.gitignore", "node_modules/\r\n.env\r\n"),
}


@pytest.mark.parametrize("case", sorted(ORDINARY))
def test_a_files_multi_line_text_under_a_file_text_key_is_not_taken_for_a_path(tmp_path, case):
    p = tmp_path.as_posix()
    obj = ORDINARY[case](p)
    assert scrub_secret_parts(obj, tmp_path) is obj                    # unchanged, not a copy with markers


def test_a_one_line_file_whose_only_line_is_env_is_scrubbed_as_before(tmp_path):
    """Accepted: a one-line value under a file-text key is judged as a path, as before the file-text exception."""
    out = scrub_secret_parts(_edit_result(f"{tmp_path.as_posix()}/notes.txt", ".env"), tmp_path)
    assert MARK not in json.dumps(out)


# ---------------------------------------------------------------- what the earlier judgement withheld stays withheld

def _withheld(p: str) -> dict:
    longd = _long(p)
    return {
        "newline_joined_list": {"results": [{"files": "a.py\n.env", "snippet": ENV_TEXT}]},
        "path_trailing_lf": {"results": [{"path": p + "/.env\n", "snippet": ENV_TEXT}]},
        "path_trailing_cr": {"results": [{"path": p + "/.env\r", "snippet": ENV_TEXT}]},
        "path_trailing_crlf": {"results": [{"path": p + "/.env\r\n", "snippet": ENV_TEXT}]},
        "original_file_one_line_path": {"original_file": p + "/.env", "text": ENV_TEXT},
        "originalFile_one_line_path": {"originalFile": p + "/.env", "body2": ENV_TEXT},
        "fileContent_one_line_path": {"fileContent": p + "/.env", "body2": ENV_TEXT},
        "long_path_mcp_item": {"results": [{"snippet": ENV_TEXT, "path": longd + "/.env"},
                                           {"path": p + "/a.py", "snippet": "ok"}]},
        "long_unc_path": {"results": [{"path": "\\\\?\\C:\\p\\" + "\\".join(["d" * 200] * 160) + "\\.env",
                                       "snippet": ENV_TEXT}]},
        "bash_edit_diff_long_path": {"stdout": "done", "stderr": "", "interrupted": False,
                                     "bashEditDiff": {"files": [{"filePath": longd + "/.env",
                                                                 "hunks": [{"lines": ["+" + ENV_TEXT.strip()]}]}]}},
        "edit_of_env_itself": _edit_result(f"{p}/.env", ENV_TEXT),
        "listing_names_env": {"filenames": [f"{p}/.env", f"{p}/src/a.py"], "note": ENV_TEXT},
    }


@pytest.mark.parametrize("case", sorted(_withheld("p")))
def test_what_the_earlier_judgement_withheld_is_withheld(tmp_path, case):
    out = scrub_secret_parts(_withheld(tmp_path.as_posix())[case], tmp_path)
    assert NEEDLE not in json.dumps(out) and OMITTED_SECRET_FILE in json.dumps(out), out


def _store_hits(project) -> list[str]:
    return sorted(str(f.relative_to(project)) for f in (project / ".devmemory").rglob("*")
                  if f.is_file() and NEEDLE.encode() in f.read_bytes())


E2E = ["newline_joined_list", "path_trailing_lf", "path_trailing_crlf", "original_file_one_line_path",
       "originalFile_one_line_path", "long_path_mcp_item", "bash_edit_diff_long_path"]


@pytest.mark.parametrize("case", E2E)
def test_no_route_stores_what_the_earlier_judgement_withheld(tmp_path, monkeypatch, case):
    """The withheld shapes, through the hooks and the stream-json import: the needle is in no store file."""
    tool = "Bash" if case.startswith("bash") else "mcp__search__find"
    inp = {"command": "python scripts/setup.py"} if tool == "Bash" else {"query": "db"}
    # hooks
    project = make_project(tmp_path / "h")
    (project / ".env").write_text(ENV_TEXT, encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    resp = _withheld(project.as_posix())[case]
    for event, extra in (("UserPromptSubmit", {"prompt": "go"}),
                         ("PostToolUse", {"tool_name": tool, "tool_input": inp, "tool_response": resp,
                                          "tool_use_id": "t1"}),
                         ("Stop", {"last_assistant_message": "done"})):
        store._FILTERS.clear()
        consent.forget_decisions()
        assert hooks.run([event], json.dumps({"session_id": SID, "cwd": str(project), "hook_event_name": event,
                                              **extra}).encode())[0] == 0
    assert _store_hits(project) == []
    # stream-json import (`devmem import` of `claude -p` output, and `devmem run`)
    project = make_project(tmp_path / "s")
    (project / ".env").write_text(ENV_TEXT, encoding="utf-8")
    resp = _withheld(project.as_posix())[case]
    lines = [{"type": "system", "subtype": "init", "session_id": SID, "model": "m"},
             {"type": "assistant", "message": {"role": "assistant", "content": [
                 {"type": "tool_use", "id": "C", "name": tool, "input": inp}]}},
             {"type": "user", "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "C", "content": "done"}]}, "tool_use_result": resp},
             {"type": "result", "result": "ok", "session_id": SID}]
    path = tmp_path / "s.jsonl"
    path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    C.record_session(project, path, "x", "2026-11-16T00:00:00Z", store=project / ".devmemory")
    assert _store_hits(project) == []


# ---------------------------------------------------------------- the rule, against the earlier judgement

PATH_KEYS = ["path", "file_path", "filePath", "files", "filenames", "uri", "target", "source", "dir", "relative_path",
             "originalFile", "original_file", "fileContent", "file_content", "fileContents", "oldString", "newString"]
NAMES = [".env", ".env.local", "config/.env", "secrets.json", "id_rsa", "a.py", "README.md", "src/app.ts", ".gitignore",
         ".env.example", "credentials", "notes.txt"]


def _value(rng: random.Random, p: str):
    name = rng.choice(NAMES)
    prefix = rng.choice([p + "/", p.replace("/", "\\") + "\\", "/home/u/proj/", "", "./", "~/", "\\\\?\\C:\\p\\",
                         _long(p) + "/", "\\\\?\\C:\\" + "\\".join(["d" * 200] * 160) + "\\"])
    v = prefix + name
    form = rng.randrange(9)
    if form == 1:
        v += rng.choice(["\n", "\r", "\r\n"])
    elif form == 2:
        v = v[: len(v) // 2] + rng.choice(["\n", "\r", "\r\n"]) + v[len(v) // 2:]
    elif form == 3:
        v = "\n".join([v, prefix + rng.choice(NAMES)])
    elif form == 4:
        v = ", ".join([v, prefix + rng.choice(NAMES)])
    elif form == 5:
        v = "line one\n" + v + "\nline three\n"
    elif form == 6:
        return [v, prefix + rng.choice(NAMES)]
    elif form == 7:
        return rng.choice(["", "https://x/.env", "http://h/.env"]) or v
    return v


def _object(rng: random.Random, p: str, depth: int = 0) -> dict:
    d: dict = {}
    for _ in range(rng.randrange(1, 4)):
        k = rng.choice(PATH_KEYS + ["snippet", "text", "note"])
        d[k] = _value(rng, p) if k in PATH_KEYS else ENV_TEXT
        if depth < 2 and rng.random() < 0.2:
            d["nested"] = [_object(rng, p, depth + 1)] if rng.random() < 0.5 else _object(rng, p, depth + 1)
    return d


def _walk_dicts(x):
    if isinstance(x, dict):
        yield x
        for v in x.values():
            yield from _walk_dicts(v)
    elif isinstance(x, list):
        for v in x:
            yield from _walk_dicts(v)


def test_the_judgement_equals_the_earlier_one_except_multi_line_file_text(tmp_path):
    rng = random.Random(4646)
    p = tmp_path.as_posix()
    base = p.lower()
    differ = 0
    for _ in range(3000):
        obj = _object(rng, p)
        for d in _walk_dicts(obj):
            now, then = R._names_secret_file(d, base, None), _names_secret_file_before(d, base, None)
            if now != then:
                differ += 1
                assert then and not now and _multi_line_file_text(d), d     # only that, and only toward keeping
    assert differ > 0          # the generator does reach the one allowed difference


def test_the_hooks_keep_an_ordinary_gitignore_edit(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    P = project.as_posix()

    def hook(event, **payload):
        store._FILTERS.clear()
        consent.forget_decisions()
        assert hooks.run([event], json.dumps({"session_id": SID, "cwd": str(project), "hook_event_name": event,
                                              **payload}).encode())[0] == 0
    hook("UserPromptSubmit", prompt="ignore dist")
    hook("PostToolUse", tool_name="Edit", tool_use_id="e1", tool_response=_edit_result(f"{P}/.gitignore",
                                                                                         "node_modules/\n.env\n"),
         tool_input={"file_path": f"{P}/.gitignore", "old_string": "node_modules/", "new_string": "node_modules/\ndist/"})
    log = (project / ".devmemory" / "live" / f"{SID}.jsonl").read_text(encoding="utf-8")
    assert MARK in log and "content of a secret file" not in log
