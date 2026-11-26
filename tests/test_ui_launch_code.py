"""The launch URL carries a single-use launch code, never the session token.

The token used to travel in the browser's command line (`--app=http://127.0.0.1:PORT/?token=...`), which other local
users can read on Linux and macOS (/proc/<pid>/cmdline, ps), and `devmem ui` put it in the normal browser's history.
Now the URL carries a code that the page exchanges once, within LAUNCH_SECONDS, for the token (POST /api/launch with
the code in a header); the code is dead after that, and a second attempt is refused and reported.
"""
from __future__ import annotations

import pytest
from ui_helpers import make_project, request, running_server, with_token

from devmem import app, ui_server
from devmem.ui_server import LAUNCH_HEADER, LAUNCH_PATH


@pytest.fixture()
def served(tmp_path):
    project, store = make_project(tmp_path)
    with running_server(project, store) as (port, st):
        yield port, st


def _code(url: str) -> str:
    return url.split("?launch=", 1)[1]


def _exchange(port, code, extra=None):
    return request(port, "POST", LAUNCH_PATH, {LAUNCH_HEADER: code, **(extra or {})})


def test_the_launch_url_carries_a_code_and_never_the_token(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_server, "recents_file", lambda *a, **k: tmp_path / "recents.json")
    project, store = make_project(tmp_path)
    srv, st = ui_server.make_server(project, store, port=0)
    try:
        url = ui_server.launch_url(srv, st)
        assert url.startswith(f"http://127.0.0.1:{srv.server_address[1]}/?launch=")
        assert st.token not in url and "token" not in url
        assert ui_server.launch_url(srv, st) != url          # each link has its own code
        cmd = app.browser_command(url, exe="/x/chrome", platform="linux", env={})
        assert cmd is not None and not any(st.token in c for c in cmd)
    finally:
        srv.server_close()


def test_a_code_is_exchanged_for_the_token_once(served, capsys):
    port, st = served
    code = _code(ui_server.launch_url_for_port(port, st))
    own = {"Origin": f"http://127.0.0.1:{port}", "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors"}
    status, headers, body = _exchange(port, code, own)
    assert status == 200 and body == {"token": st.token}
    assert headers.get("Cache-Control") == "no-store"
    assert request(port, "GET", "/api/state", with_token(st))[0] == 200
    status, _, body = _exchange(port, code, own)                       # the same code again
    assert status == 403 and "already used" in body["error"]
    assert st.refused_launches == 1
    assert "used a second time" in capsys.readouterr().err


def test_an_expired_or_unknown_code_is_refused(served, monkeypatch):
    port, st = served
    monkeypatch.setattr(ui_server, "LAUNCH_SECONDS", -1.0)
    code = _code(ui_server.launch_url_for_port(port, st))
    status, _, body = _exchange(port, code)
    assert status == 403 and "expired" in body["error"]
    status, _, body = _exchange(port, "not-a-code")
    assert status == 403 and "token" not in body


def test_the_exchange_is_not_usable_cross_site_or_without_the_header(served):
    port, st = served
    code = _code(ui_server.launch_url_for_port(port, st))
    for extra in ({"Origin": "http://evil.example"}, {"Origin": "null"},
                  {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors"},
                  {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "no-cors"},
                  {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}):
        status, _, body = _exchange(port, code, extra)
        assert status == 403 and "token" not in body, extra
    status, _, body = request(port, "POST", LAUNCH_PATH, host=f"rebind.example:{port}", headers={LAUNCH_HEADER: code})
    assert status == 403
    status, _, body = request(port, "POST", f"{LAUNCH_PATH}?launch={code}")      # in the URL instead of the header
    assert status == 401 and "token" not in body
    status, _, body = request(port, "GET", LAUNCH_PATH, {LAUNCH_HEADER: code})
    assert status == 405
    status, _, _ = request(port, "GET", f"/?launch={code}")                          # loading the page spends nothing
    assert status == 200
    status, _, body = _exchange(port, code)                                         # none of the above spent it
    assert status == 200 and body["token"] == st.token


def test_every_other_api_call_still_needs_the_token(served):
    port, st = served
    code = _code(ui_server.launch_url_for_port(port, st))
    for path in ("/api/state", "/api/open?project=x"):
        status, _, _ = request(port, "POST" if "open" in path else "GET", path, {LAUNCH_HEADER: code})
        assert status == 401, path


def test_the_page_exchanges_the_code_and_keeps_only_the_token():
    js = (ui_server.STATIC / "app.js").read_text(encoding="utf-8")
    assert 'searchParams.get("launch")' in js and LAUNCH_PATH in js and LAUNCH_HEADER in js
    assert 'searchParams.get("token")' not in js        # a token in a URL is no longer read


def test_enter_prints_a_new_link_that_works(served, capsys):
    import io
    port, st = served

    class Srv:
        server_address = ("127.0.0.1", port)
    ui_server.new_links_on_enter(Srv(), st, stdin=io.StringIO("\n\n"))
    out_lines = [ln for ln in capsys.readouterr().out.splitlines() if "?launch=" in ln]
    assert len(out_lines) == 2
    codes = [_code(ln) for ln in out_lines]
    assert codes[0] != codes[1]
    assert _exchange(port, codes[1])[0] == 200


def test_app_dry_run_prints_a_launch_link(capsys):
    assert app.run_app(None, None, dry_run=True, port=0) == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert first.startswith("http://127.0.0.1:") and "/?launch=" in first and "token" not in first
