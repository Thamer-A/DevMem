from __future__ import annotations

import pytest

from ui_helpers import make_project, request, running_server, with_token

from devmem import ui_server


@pytest.fixture()
def served(tmp_path):
    project, store = make_project(tmp_path)
    with running_server(project, store) as (port, st):
        yield port, st, project, tmp_path


def test_api_without_token_is_rejected(served):
    port, st, *_ = served
    status, _, body = request(port, "GET", "/api/state")
    assert status == 401 and "token" in body["error"]


def test_api_with_wrong_token_is_rejected(served):
    port, st, *_ = served
    status, _, _ = request(port, "GET", "/api/state", {"X-DevMemory-Token": "nope"})
    assert status == 401


def test_token_in_query_string_is_not_accepted(served):
    port, st, *_ = served
    status, _, _ = request(port, "GET", f"/api/state?token={st.token}")
    assert status == 401


def test_wrong_host_is_rejected_even_with_token(served):
    port, st, *_ = served
    for host in ("evil.example", f"evil.example:{port}", f"127.0.0.1:{port + 1}", ""):
        status, _, _ = request(port, "GET", "/api/state", with_token(st), host=host or "x")
        assert status == 403, host
    status, _, _ = request(port, "GET", "/", host=f"rebind.example:{port}")
    assert status == 403


def test_localhost_host_is_accepted(served):
    port, st, *_ = served
    status, _, body = request(port, "GET", "/api/state", with_token(st), host=f"localhost:{port}")
    assert status == 200 and body["name"] == "proj"


def test_cross_origin_post_is_rejected_and_has_no_side_effect(served):
    port, st, project, tmp_path = served
    victim = tmp_path / "victim"
    victim.mkdir()
    q = f"/api/open?project={victim}&scan=1"
    for origin in ("http://evil.example", "null", f"http://127.0.0.1:{port + 1}", "https://127.0.0.1:%d" % port):
        status, _, _ = request(port, "POST", q, with_token(st, {"Origin": origin}))
        assert status == 403, origin
    status, _, _ = request(port, "POST", q)  # no token, no origin
    assert status == 401
    status, _, _ = request(port, "POST", q, {"Sec-Fetch-Site": "cross-site"})
    assert status == 403
    assert not (victim / ".devmemory").exists()
    assert st.project == project.resolve()


def test_happy_path_get_and_post(served):
    port, st, project, tmp_path = served
    own = {"Origin": f"http://127.0.0.1:{port}", "Sec-Fetch-Site": "same-origin"}
    status, _, body = request(port, "GET", "/api/state", with_token(st, own))
    assert status == 200 and body["project"] == str(project.resolve())
    status, _, body = request(port, "GET", "/api/files", with_token(st))
    assert status == 200 and any(f["path"] == "src/main.py" for f in body["files"])
    status, _, body = request(port, "GET", "/api/file?path=src/main.py", with_token(st))
    assert status == 200 and "def hello" in body["content"]
    # POST: a folder without a store asks before scanning, and scans only when told to
    other = tmp_path / "other"
    (other / "a").mkdir(parents=True)
    (other / "a" / "x.py").write_text("x = 1\n", encoding="utf-8")
    status, _, body = request(port, "POST", f"/api/open?project={other}", with_token(st, own))
    assert status == 200 and body["needs_scan"] is True
    assert not (other / ".devmemory").exists()
    status, _, body = request(port, "POST", f"/api/open?project={other}&scan=1", with_token(st, own))
    assert status == 200 and body["ok"] is True
    assert (other / ".devmemory" / "index" / "map.json").is_file()


def test_static_page_needs_no_token_and_carries_security_headers(served):
    port, st, *_ = served
    status, headers, body = request(port, "GET", "/")
    assert status == 200 and b"<title>DevMem</title>" in body
    low = {k.lower(): v for k, v in headers.items()}
    assert "default-src 'self'" in low["content-security-policy"]
    assert low["x-frame-options"] == "DENY"
    assert low["x-content-type-options"] == "nosniff"
    assert "token" not in body.decode("utf-8").lower().replace("x-devmemory-token", "")  # page never embeds the token


def test_static_traversal_fails(served):
    port, st, *_ = served
    for p in ("/../ui_server.py", "/..%2fui_server.py", "/%2e%2e/ui_server.py", "/app.js/../../cli.py"):
        status, _, _ = request(port, "GET", p)
        assert status in (403, 404), p
        assert status != 200


def test_api_file_traversal_fails(served):
    port, st, project, tmp_path = served
    (tmp_path / "secret.txt").write_text("top secret", encoding="utf-8")
    for p in ("../secret.txt", "..\\secret.txt", "src/../../secret.txt", str(tmp_path / "secret.txt"), "%2e%2e/secret.txt",
              "src/main.py\x00.txt"):
        status, _, body = request(port, "GET", "/api/file?path=" + p.replace("\\", "%5C").replace("\x00", "%00"), with_token(st))
        assert status == 404, p
        assert b"top secret" not in (body if isinstance(body, bytes) else str(body).encode())


def test_api_vault_file_traversal_fails(served):
    port, st, project, tmp_path = served
    for p in ("../PROJECT.md", "../../src/main.py", "..%5C..%5Csrc%5Cmain.py", str(project / "src" / "main.py")):
        status, _, _ = request(port, "GET", "/api/vault/file?path=" + p, with_token(st))
        assert status == 404, p
    status, _, body = request(port, "GET", "/api/vault/file?path=PROJECT.md", with_token(st))
    assert status == 200


def test_reveal_refuses_paths_outside_project_and_store(served):
    port, st, project, tmp_path = served
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    for p in (str(outside), "../outside.txt"):
        status, _, _ = request(port, "POST", "/api/reveal?dry=1&path=" + p, with_token(st))
        assert status == 400, p
    status, _, body = request(port, "POST", "/api/reveal?dry=1&path=src/main.py", with_token(st))
    assert status == 200 and body["path"].endswith("main.py")


def test_token_is_random_per_server(tmp_path):
    project, store = make_project(tmp_path)
    with running_server(project, store) as (_, a), running_server(project, store) as (_, b):
        assert a.token != b.token and len(a.token) >= 32


def test_examples_only_listed_when_present_and_no_coinrush(tmp_path, monkeypatch):
    assert not any("coinrush" in e["name"].lower() for e in ui_server.EXAMPLES)
    monkeypatch.setattr(ui_server, "EXAMPLES", [{"name": "Gone", "desc": "", "project": tmp_path / "nope", "store": tmp_path / "nope" / ".devmemory"}])
    st = ui_server.State(None, None)
    assert ui_server.home_info(st)["examples"] == []
    here = tmp_path / "here"
    (here / ".devmemory").mkdir(parents=True)
    monkeypatch.setattr(ui_server, "EXAMPLES", [{"name": "Here", "desc": "", "project": here, "store": here / ".devmemory"}])
    assert [e["name"] for e in ui_server.home_info(st)["examples"]] == ["Here"]


def test_cross_site_top_level_page_navigation_is_served_but_never_the_api(served):
    """Clicking the printed link in a chat or document is a cross-site navigation; it must load the page."""
    port, st, *_ = served
    nav = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
    status, _, _ = request(port, "GET", "/", nav)
    assert status == 200
    status, _, _ = request(port, "GET", "/api/state", with_token(st, nav))
    assert status == 403
    status, _, _ = request(port, "POST", "/api/open?project=x", with_token(st, nav))
    assert status == 403
    status, _, _ = request(port, "GET", "/", {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors",
                                              "Sec-Fetch-Dest": "script"})
    assert status == 403
