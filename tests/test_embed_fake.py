"""Optional embeddings: off by default, guarded imports, vector cache, combination with keywords (fake model)."""
from __future__ import annotations

import importlib.util
import json

import pytest

from devmem import embed
from devmem.index import Index
from devmem.pack import build_pack, sync_session_index

from pack_helpers import setup


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(embed.ENV, raising=False)
    embed._EMBEDDERS.clear()


def test_off_by_default(tmp_path):
    _project, store, _ = setup(tmp_path, with_sessions=False)
    assert embed.settings(store) is None
    assert embed.get_embedder(store) is None


def test_settings_from_env_and_config(tmp_path, monkeypatch):
    _project, store, _ = setup(tmp_path, with_sessions=False)
    (store / "config.json").write_text(json.dumps({"embeddings": {"backend": "fake"}}), encoding="utf-8")
    assert embed.settings(store)["backend"] == "fake"
    monkeypatch.setenv(embed.ENV, "off")
    assert embed.settings(store) is None
    monkeypatch.setenv(embed.ENV, "fastembed:some/model")
    assert embed.settings(store) == {"backend": "fastembed", "model": "some/model"}


@pytest.mark.skipif(importlib.util.find_spec("fastembed") is not None, reason="fastembed is installed here")
def test_missing_package_means_off():
    assert embed.get_embedder(None, {"backend": "fastembed", "model": None}) is None


def test_fake_is_deterministic_and_normalized():
    e = embed.FakeEmbedder()
    a, b = e.embed(["shop reroll price", "shop reroll price"])
    assert a == b and abs(sum(x * x for x in a) - 1) < 1e-6


class Counting(embed.FakeEmbedder):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def embed(self, texts, kind="passage"):
        self.calls += len(texts)
        return super().embed(texts, kind)


def test_cache_embeds_each_document_once(tmp_path):
    _project, store, _ = setup(tmp_path, with_sessions=False)
    e = Counting()
    c = embed.VectorCache(store, e)
    docs = {"a": ("alpha text", {}), "b": ("beta text", {})}
    assert c.ensure("file", docs) == 2
    assert c.ensure("file", docs) == 0
    assert c.ensure("file", {"a": ("alpha changed", {}), "b": ("beta text", {})}) == 1
    assert e.calls == 3
    c.close()


def test_pack_with_fake_embeddings_and_forget(tmp_path, monkeypatch):
    monkeypatch.setenv(embed.ENV, "fake")
    project, store, recs = setup(tmp_path)
    text, info = build_pack(project, store, "make rerolling the shop offers cost more")
    assert "src/shop.py" in info["files"]
    conn = embed._connect(store)
    turn_keys = {r[0] for r in conn.execute("SELECT key FROM vec WHERE kind='turn'")}
    conn.close()
    blades = recs["blades"]["_meta"]
    assert f"{blades}#0" in turn_keys
    # forgetting a session drops its vectors too
    (store / "sessions" / blades).unlink()
    sync_session_index(store)
    conn = embed._connect(store)
    left = {r[0] for r in conn.execute("SELECT key FROM vec WHERE kind='turn'")}
    conn.close()
    assert f"{blades}#0" not in left
    with Index(store) as idx:
        assert blades not in idx.keys("session")


def test_contentless_prompt_stays_empty_with_embeddings(tmp_path, monkeypatch):
    monkeypatch.setenv(embed.ENV, "fake")
    project, store, _ = setup(tmp_path)
    assert build_pack(project, store, "thanks, that works")[0] == ""


def test_embeddings_never_drop_keyword_picks(tmp_path, monkeypatch):
    project, store, _ = setup(tmp_path)
    task = "make rerolling the shop offers cost more"
    _, off = build_pack(project, store, task)
    monkeypatch.setenv(embed.ENV, "fake")
    _, on = build_pack(project, store, task)
    assert {s["session"] for s in off["sessions"]} <= {s["session"] for s in on["sessions"]}
    assert set(off["files"]) <= set(on["files"])
