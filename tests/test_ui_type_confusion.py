"""A store holding type-confused records or a garbled project map never makes a viewer route fail (security review V-X2).

Before: one record with `"usage": {"input_tokens": "x"}` made /api/overview raise a TypeError (HTTP 500), and the
whole project failed to load in the viewer. Every number read from a record is now coerced, the project map is
brought to the expected shape when it is loaded, and a route that still meets data it cannot use answers with a JSON
error (4xx) rather than a 500.
"""
from __future__ import annotations

import json
import random

import pytest
from ui_helpers import make_project, request, running_server, with_token

ROUTES = ["/api/home", "/api/state", "/api/vault", "/api/vault/file?path=PROJECT.md", "/api/overview", "/api/files",
          "/api/file?path=src/main.py", "/api/sessions", "/api/session?id=aaaa", "/api/session?id=",
          "/api/decisions", "/api/graph", "/api/pack?task=hello%20main", "/api/nope"]

GARBAGE = ["x", "", 1, -1, 0.5, 10 ** 30, -(10 ** 30), 1e308, True, None, [], [1, "a"], {}, {"a": {"b": [None]}},
           [[[[["deep"]]]]], "‮", "1e999"]


def _confused(rnd: random.Random, depth: int = 0):
    v = rnd.choice(GARBAGE)
    if depth < 2 and rnd.random() < 0.3:
        return {rnd.choice(["input_tokens", "x", "files", "turns"]): _confused(rnd, depth + 1)}
    return v


def _records(rnd: random.Random) -> list[dict]:
    fields = ["session_id", "started_at", "ended_at", "prompt", "result_text", "provider", "usage", "turns",
              "files_edited", "files_read", "tool_calls", "assistant_text", "errors", "file_hashes_at_capture",
              "raw_record", "prompt_source", "in_progress", "git", "model"]
    recs = [{"session_id": "aaaa0000-0000-0000-0000-000000000001", "started_at": "2026-11-01T00:00:00Z",
             "prompt": "We decided to always use tabs.",
             "usage": {"input_tokens": "x", "output_tokens": [1], "cache_read_tokens": {"a": 1},
                       "cache_creation_tokens": None, "cost_usd_list": "1.5"}}]
    for i in range(40):
        r = {f: _confused(rnd) for f in fields if rnd.random() < 0.7}
        if rnd.random() < 0.5:
            r["session_id"] = f"bbbb{i:04d}-0000-0000-0000-000000000000"
        if rnd.random() < 0.5:
            r["turns"] = [{k: _confused(rnd) for k in ("prompt", "tool_calls", "files_edited", "started_at",
                                                       "result_text", "prompt_source")} for _ in range(2)]
        if rnd.random() < 0.3:
            r["usage"] = {k: _confused(rnd) for k in ("input_tokens", "output_tokens", "cache_read_tokens",
                                                       "cache_creation_tokens", "cost_usd_list")}
        recs.append(r)
    recs.append({"session_id": "cccc0000-0000-0000-0000-000000000001",
                 "usage": {"input_tokens": 10 ** 30, "output_tokens": float("inf"), "cost_usd_list": -1e308}})
    return recs


def _garbled_map(store, rnd: random.Random, variant: int) -> None:
    p = store / "index" / "map.json"
    m = json.loads(p.read_text(encoding="utf-8"))
    if variant == 1:
        m["files"] = [1, 2]
        m["project"], m["engine"], m["conventions"], m["used_by"], m["rojo"] = 5, None, [], "x", 3
    elif variant == 2:
        for i in m["files"].values():
            i.update(module=["list"], deps="notalist", lang={"a": 1}, lines="many", symbols=7, remotes_used=[1])
        m["files"]["weird.py"] = "not a dict"
        m["conventions"] = {"exclusive_sites": [{"op": 1}], "hub_apis": "x", "config_modules": [], "tests": 5}
    elif variant == 3:
        p.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        return
    p.write_text(json.dumps(m), encoding="utf-8")


@pytest.mark.parametrize("variant", [0, 1, 2, 3])
def test_every_route_answers_cleanly_on_a_type_confused_store(tmp_path, variant):
    project, store = make_project(tmp_path)
    rnd = random.Random(variant)
    (store / "sessions").mkdir(exist_ok=True)
    for n, rec in enumerate(_records(rnd)):
        (store / "sessions" / f"20261101T0000{n:02d}_x{n:07d}.meta.json").write_text(json.dumps(rec),
                                                                                      encoding="utf-8")
    if variant:
        _garbled_map(store, rnd, variant)
    with running_server(project, store) as (port, st):
        for route in ROUTES:
            status, _h, body = request(port, "GET", route, headers=with_token(st))
            assert status < 500, (route, status, body)
            assert isinstance(body, dict), (route, body)
            assert "Traceback" not in json.dumps(body)
            if status >= 400:
                assert "error" in body, (route, body)
        status, _h, body = request(port, "GET", "/api/overview", headers=with_token(st))
        assert status == 200, body
        assert isinstance(body["tokens"]["input"], (int, float)) and isinstance(body["cost_usd"], (int, float))
