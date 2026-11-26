"""An embedding model is downloaded only by a command the user types. The hooks, the MCP server and the recall
skill's lookups load one from local files only (fastembed's local_files_only; model2vec from a local folder), so a
DEVMEM_EMBED that reaches a hook process (a project's committed settings can set environment variables for its
hooks) starts no download. The backends are stand-ins here: no library, no network."""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from devmem import cli, embed


class _WithLocalOnly:
    calls: list = []

    def __init__(self, model_name, cache_dir=None, local_files_only=False):
        type(self).calls.append({"model_name": model_name, "local_files_only": local_files_only})

    def embed(self, texts, batch_size=1):
        return [[1.0, 0.0] for _ in texts]


class _WithoutLocalOnly:
    calls: list = []

    def __init__(self, model_name, cache_dir=None):
        type(self).calls.append({"model_name": model_name})


@pytest.fixture()
def fake_fastembed(monkeypatch):
    def use(cls):
        cls.calls = []
        monkeypatch.setitem(sys.modules, "fastembed", types.SimpleNamespace(TextEmbedding=cls))
        monkeypatch.setattr(embed, "_EMBEDDERS", {})
        return cls
    monkeypatch.setattr(embed, "_DOWNLOAD", [False])
    return use


def test_hooks_and_mcp_load_from_local_files_only(fake_fastembed):
    cls = fake_fastembed(_WithLocalOnly)
    assert embed.get_embedder(cfg={"backend": "fastembed", "model": "some/model"}) is not None
    assert cls.calls == [{"model_name": "some/model", "local_files_only": True}]


def test_a_fastembed_that_cannot_stay_local_is_not_used_there(fake_fastembed):
    cls = fake_fastembed(_WithoutLocalOnly)
    assert embed.get_embedder(cfg={"backend": "fastembed", "model": "some/model"}) is None
    assert cls.calls == []                                   # never constructed: it might download


def test_a_command_the_user_types_may_download(fake_fastembed):
    cls = fake_fastembed(_WithLocalOnly)
    embed.allow_download()
    assert embed.get_embedder(cfg={"backend": "fastembed", "model": "some/model"}) is not None
    assert cls.calls == [{"model_name": "some/model", "local_files_only": False}]


def test_model2vec_only_from_a_local_folder_there(monkeypatch, tmp_path):
    monkeypatch.setattr(embed, "_DOWNLOAD", [False])
    monkeypatch.setattr(embed, "_EMBEDDERS", {})
    loaded = []
    fake = types.SimpleNamespace(StaticModel=types.SimpleNamespace(from_pretrained=lambda m: loaded.append(m)))
    monkeypatch.setitem(sys.modules, "model2vec", fake)
    assert embed.get_embedder(cfg={"backend": "model2vec", "model": "minishlab/potion-base-8M"}) is None
    assert loaded == []
    assert embed.get_embedder(cfg={"backend": "model2vec", "model": str(tmp_path)}) is not None
    assert loaded == [str(tmp_path)]


@pytest.mark.parametrize("argv,allowed", [(["status"], False), (["pack", "fix it"], False), (["scan"], False),
                                          (["sessions", "--plugin-data", "x"], False), (["embed"], False),
                                          (["embed", "--download", "--model", "fake"], True)])
def test_which_commands_may_download(monkeypatch, tmp_path, capsys, argv, allowed):
    """Only `devmem embed --download` may fetch model files: `devmem pack` and the rest are what an agent runs through
    its shell in ordinary use, so they load from local files only."""
    monkeypatch.setattr(embed, "_DOWNLOAD", [False])
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.chdir(project)
    cli.main(argv)
    capsys.readouterr()
    assert embed._DOWNLOAD[0] is allowed


def test_embed_download_fetches_the_model(fake_fastembed, tmp_path, monkeypatch, capsys):
    cls = fake_fastembed(_WithLocalOnly)
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.chdir(project)
    assert cli.main(["embed", "--model", "fastembed:some/model"]) == 0
    assert cls.calls[-1]["local_files_only"] is True
    assert cli.main(["embed", "--download", "--model", "fastembed:some/model"]) == 0
    assert cls.calls[-1] == {"model_name": "some/model", "local_files_only": False}
    assert "on this machine now" in capsys.readouterr().out


def _fake_model2vec(monkeypatch):
    loaded = []
    fake = types.SimpleNamespace(StaticModel=types.SimpleNamespace(from_pretrained=lambda m: loaded.append(m)))
    monkeypatch.setitem(sys.modules, "model2vec", fake)
    monkeypatch.setattr(embed, "_EMBEDDERS", {})
    return loaded


@pytest.mark.parametrize("download", [False, True])
def test_a_model_folder_inside_the_project_is_never_loaded(monkeypatch, tmp_path, download):
    """DEVMEM_EMBED=model2vec:<folder> can come from a project's committed settings (`env` for its hooks): a folder in
    the project or its store, given relative (loaded from the hook's working folder, the project) or absolute, is
    refused on every route; an absolute folder outside the project is used."""
    monkeypatch.setattr(embed, "_DOWNLOAD", [download])
    loaded = _fake_model2vec(monkeypatch)
    project = tmp_path / "proj"
    store = project / ".devmemory"
    (project / "models" / "evil").mkdir(parents=True)
    (store / "m").mkdir(parents=True)
    monkeypatch.chdir(project)
    for model in ("models/evil", "./models/evil", str(project / "models" / "evil"), str(store / "m"), "models"):
        monkeypatch.setenv(embed.ENV, f"model2vec:{model}")
        assert embed.get_embedder(store, project=project) is None, model
    assert loaded == []
    outside = tmp_path / "models-outside"
    outside.mkdir()
    monkeypatch.setenv(embed.ENV, f"model2vec:{outside}")
    assert embed.get_embedder(store, project=project) is not None
    assert loaded == [str(outside)]


def test_a_cache_folder_inside_the_project_is_never_used(fake_fastembed, tmp_path):
    cls = fake_fastembed(_WithLocalOnly)
    project = tmp_path / "proj"
    store = project / ".devmemory"
    store.mkdir(parents=True)
    for cache in ("models", str(project / "models"), str(store / "models")):
        assert embed.get_embedder(store, {"backend": "fastembed", "cache_dir": cache}, project=project) is None
    assert cls.calls == []
    assert embed.get_embedder(store, {"backend": "fastembed", "cache_dir": str(tmp_path / "hf")}, project=project)


class _Recording:
    calls: list = []

    def __init__(self, model_name, cache_dir=None, local_files_only=False):
        type(self).calls.append({"model_name": model_name, "cache_dir": cache_dir})

    def embed(self, texts, batch_size=1):
        return [[1.0, 0.0] for _ in texts]


def test_fastembed_always_gets_an_explicit_cache_folder_outside_the_project(fake_fastembed, tmp_path):
    """Without one, fastembed took its cache folder from FASTEMBED_CACHE_PATH, which a project's committed settings
    can set for its hooks (reviewer's v8a/b/t12_embed_env.py)."""
    cls = fake_fastembed(_Recording)
    project = tmp_path / "p"
    store = project / ".devmemory"
    store.mkdir(parents=True)
    assert embed.get_embedder(store, {"backend": "fastembed"}, project=project) is not None
    got = Path(cls.calls[0]["cache_dir"])
    assert got.is_absolute() and got == embed.default_cache_dir() and not got.is_relative_to(project)


@pytest.mark.parametrize("var", embed.CACHE_ENV)
@pytest.mark.parametrize("where", ["relative", "inside"])
def test_a_cache_variable_naming_a_folder_in_the_project_turns_embeddings_off(fake_fastembed, tmp_path, monkeypatch,
                                                                            var, where):
    cls = fake_fastembed(_Recording)
    project = tmp_path / "p"
    store = project / ".devmemory"
    store.mkdir(parents=True)
    monkeypatch.setenv(var, "./models" if where == "relative" else str(project / "models"))
    assert embed.get_embedder(store, {"backend": "fastembed"}, project=project) is None
    assert cls.calls == []
    with pytest.raises(ImportError, match=var):
        embed.check_cache_env(project, store)
    monkeypatch.setenv(var, str(tmp_path / "elsewhere"))
    embed._EMBEDDERS.clear()
    assert embed.get_embedder(store, {"backend": "fastembed"}, project=project) is not None
