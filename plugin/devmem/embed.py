"""Optional local embeddings for retrieval (off by default; the core stays stdlib-only).

Keyword search cannot match "a ceiling on how many tickets can sit in the middle stage" to a session
about a WIP limit, or an Arabic prompt to an English session. A small multilingual sentence-embedding
model running locally on the CPU can. `devmem.pack` combines it with the keyword ranking: the keyword
picks stay, and the embedding ranking adds a few sessions/files the keywords missed. It never replaces it.

Turn it on (nothing is downloaded or imported unless it is on):

    .devmemory/config.json   {"embeddings": {"backend": "fastembed",
                                             "model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"}}
    or the environment       DEVMEM_EMBED=fastembed[:<model>]   (DEVMEM_EMBED=off disables it everywhere)

Backends (each behind an import guard; a missing package means "off", never an error):
  fastembed   `pip install fastembed` (ONNX Runtime on the CPU). Default model
              sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 (384-d, 50+ languages, ~0.22 GB).
  model2vec   `pip install model2vec`, static embeddings (e.g. minishlab/potion-multilingual-128M): much
              faster, lower quality.
  fake        deterministic hashed bag of words, for tests only (no semantic knowledge at all).

The model files are fetched by the backend library from Hugging Face on first use (the only network
access, and only when embeddings are turned on); `cache_dir` in the config chooses where they go. Only
`devmem embed --download` fetches them (devmem.cli.cmd_embed calls allow_download): an explicit act that an agent's
ordinary use of devmem (`devmem pack` through its shell, the MCP tools, the recall skill) does not perform. Everything
else loads a model only from local files: fastembed with local_files_only (a fastembed without that option is not
used), model2vec only from an absolute folder; otherwise embeddings stay off for that call. A model folder or a
cache_dir must be an absolute path outside the project and the store (check_model_path): a repository can ship model
files, and a project's committed settings can set DEVMEM_EMBED for its hooks (a relative `model2vec:<folder>` would
be loaded from the hook's working folder, the project).
Project text never leaves the machine.

Vectors are cached in `<store>/index/vectors.db` (SQLite), one row per (model, kind, key) with a hash of
the embedded text, so each document is embedded once. `forget_keys`/`prune` drop vectors of deleted
sessions; `devmem.pack.sync_session_index` calls them.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
from array import array
from pathlib import Path

ENV = "DEVMEM_EMBED"
DEFAULT_BACKEND = "fastembed"
DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
FILE_TEXT_CHARS = 1200      # path, names and the head of the file are what a file "is about"
TURN_TEXT_CHARS = 2000
NEW_DOCS_SECONDS = 5.0      # at most this long embedding not-yet-cached documents per pack call (the rest
                            # follow on later calls); DEVMEM_EMBED_SECONDS overrides
BATCH = 32
DROP_MAX = 2000             # stale vectors deleted per call (in one transaction; the rest on later calls)
# Similarity thresholds for the default model (calibrated on the evaluation fixtures, see the report):
TURN_MIN_SIM = 0.30         # a past request below this similarity is not considered at all
TURN_MARGIN = 0.10          # only requests within this of the best similarity are candidates
FILE_MIN_SIM = 0.30         # files embed less well (code, not prose): stricter
FILE_MARGIN = 0.05
CONFIDENT_SIM = 0.35        # a past request this similar lets a prompt past the keyword evidence gate
                            # (on the fixtures, contentless prompts reached at most 0.23)
_E5_PREFIX = ("query: ", "passage: ")


# ------------------------------------------------------------------------------------------------
# settings and backends
# ------------------------------------------------------------------------------------------------
def settings(store: Path | None) -> dict | None:
    """The embedding settings in force, or None when embeddings are off (the default)."""
    env = os.environ.get(ENV, "").strip()
    if env.lower() in ("off", "0", "false", "no"):
        return None
    if env:
        backend, _, model = env.partition(":")
        return {"backend": backend.strip().lower(), "model": model.strip() or None}
    if store is None:
        return None
    try:
        from .store import read_store_text
        cfg = json.loads(read_store_text(Path(store) / "config.json", 1024 * 1024))
    except (OSError, ValueError):
        return None
    e = cfg.get("embeddings") if isinstance(cfg, dict) else None
    if isinstance(e, bool):
        e = {"enabled": e}
    if not isinstance(e, dict) or e.get("enabled") is False:
        return None
    # Embeddings mean a model download (network) and a cache folder the config names. A config.json that git tracks
    # can arrive with a `git pull`; it never turns them on (DEVMEM_EMBED, set by the user, still can).
    try:
        from .store import tracked_file
        if tracked_file(Path(store).parent, Path(store), "config.json"):     # any spelling; also when git cannot say
            return None
    except Exception:  # noqa: BLE001 - when in doubt, embeddings stay off
        return None
    return {"backend": str(e.get("backend") or DEFAULT_BACKEND).lower(), "model": e.get("model"),
            "cache_dir": e.get("cache_dir"), **{k: e[k] for k in ("turn_min_sim", "turn_margin", "file_min_sim",
                                                                    "file_margin", "confident_sim") if k in e}}


def _normalize(v) -> list[float]:
    v = [float(x) for x in v]
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class Embedder:
    """Interface: `name` (cache key) and `embed(texts, kind)` -> unit vectors. kind: query | passage."""
    name = "base"

    def embed(self, texts: list[str], kind: str = "passage") -> list[list[float]]:
        raise NotImplementedError


class FakeEmbedder(Embedder):
    """Deterministic and dependency-free: a hashed bag of devmem.text terms. For tests only."""

    def __init__(self, dim: int = 64):
        self.dim = dim
        self.name = f"fake-{dim}"

    def embed(self, texts, kind="passage"):
        from .text import tokenize
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for term in tokenize(t):
                h = int(hashlib.md5(term.encode("utf-8"), usedforsecurity=False).hexdigest(), 16)
                v[h % self.dim] += 1.0 if (h >> 8) & 1 else -1.0
            out.append(_normalize(v) if any(v) else v)
        return out


def default_cache_dir() -> Path:
    """Where fastembed keeps model files when the config names no cache_dir: `<user data>/models/fastembed`, devmem's
    per-user folder (never fastembed's own default, which FASTEMBED_CACHE_PATH in a project's settings could move)."""
    from .consent import user_data_dir
    return user_data_dir() / "models" / "fastembed"


# environment variables model libraries take a cache folder from; a project's committed settings can set them for
# its hooks (`./models`), so one that names a folder inside the project or its store turns embeddings off
CACHE_ENV = ("FASTEMBED_CACHE_PATH", "HF_HOME", "HF_HUB_CACHE", "TRANSFORMERS_CACHE", "XDG_CACHE_HOME",
             "SENTENCE_TRANSFORMERS_HOME")


def check_cache_env(project, store) -> None:
    """Raises ImportError (embeddings off for this call) when a cache variable of CACHE_ENV is relative or resolves
    inside the project or the store."""
    for var in CACHE_ENV:
        value = os.environ.get(var)
        if not value:
            continue
        p = Path(os.path.expanduser(value))
        if not p.is_absolute() or _inside_any(p, (project, store)):
            raise ImportError(f"{var}={value} names a folder inside the project or its store (or a relative one); "
                              "devmem does not use embeddings while it is set")


class FastEmbedder(Embedder):
    def __init__(self, model: str | None = None, cache_dir: str | None = None, download: bool = False,
                 project=None, store=None):
        check_cache_env(project, store)
        cache_dir = cache_dir or str(default_cache_dir())
        check_model_path(cache_dir, project, store, "the model cache folder (cache_dir)")
        from fastembed import TextEmbedding  # pyright: ignore[reportMissingImports]  # optional extra; ImportError when absent
        self.model_name = model or DEFAULT_MODEL
        self.name = f"fastembed:{self.model_name}"
        self.prefix = _E5_PREFIX if "e5" in self.model_name.lower() else ("", "")
        kw: dict = {"cache_dir": os.path.expanduser(cache_dir)}
        if not download:
            import inspect
            try:
                params = inspect.signature(TextEmbedding.__init__).parameters
            except (TypeError, ValueError):
                params = {}
            if "local_files_only" not in params:
                raise ImportError("this fastembed cannot be limited to local files; not used without a download")
            kw["local_files_only"] = True
        self.model = TextEmbedding(model_name=self.model_name, **kw)

    def embed(self, texts, kind="passage"):
        pre = self.prefix[0] if kind == "query" else self.prefix[1]
        return [_normalize(v) for v in self.model.embed([pre + t for t in texts], batch_size=BATCH)]


def _inside_any(path: Path, folders) -> bool:
    real = Path(os.path.normcase(os.path.realpath(path)))
    for f in folders:
        if f is None:
            continue
        top = Path(os.path.normcase(os.path.realpath(f)))
        if real == top or real.is_relative_to(top):
            return True
    return False


def check_model_path(value: str | None, project, store, what: str) -> None:
    """A model folder or a cache folder must be an absolute path outside the project and the store (a repository can
    ship model files there, and a project's settings can set DEVMEM_EMBED for its hooks). Raises ImportError (the
    embedder is then off)."""
    if not value:
        return
    p = Path(os.path.expanduser(str(value)))
    if not p.is_absolute():
        raise ImportError(f"{what} {value!r} is not an absolute path; devmem does not load model files from a relative "
                          "folder")
    if _inside_any(p, (project, store)):
        raise ImportError(f"{what} {value} is inside the project or its store; devmem does not load model files there")


def _looks_like_a_folder(name: str) -> bool:
    return os.path.isabs(os.path.expanduser(name)) or name.startswith((".", "~")) or "\\" in name or \
        name.count("/") > 1 or os.path.lexists(name)


class Model2VecEmbedder(Embedder):
    def __init__(self, model: str | None = None, cache_dir: str | None = None, download: bool = False,
                 project=None, store=None):
        check_cache_env(project, store)
        self.model_name = model or "minishlab/potion-multilingual-128M"
        if os.path.isabs(os.path.expanduser(self.model_name)):
            check_model_path(self.model_name, project, store, "the model folder")
            if not Path(os.path.expanduser(self.model_name)).is_dir():
                raise ImportError("no such model folder")
        elif _looks_like_a_folder(self.model_name):
            # a relative name that is (or could be) a folder: model2vec would load it from the current folder, which is
            # the project in a hook. Only a hub name (`org/model`) or an absolute folder is used.
            raise ImportError("model2vec: give an absolute model folder outside the project, or a model name")
        elif not download:
            raise ImportError("model2vec loads a model from a local folder only here (no download)")
        from model2vec import StaticModel  # pyright: ignore[reportMissingImports]  # optional, not a declared extra
        self.name = f"model2vec:{self.model_name}"
        self.model = StaticModel.from_pretrained(os.path.expanduser(self.model_name))

    def embed(self, texts, kind="passage"):
        return [_normalize(v) for v in self.model.encode(list(texts))]


_EMBEDDERS: dict[str, Embedder | None] = {}
_DOWNLOAD = [False]          # allow_download: a command the user typed may fetch model files


def allow_download(ok: bool = True) -> None:
    """Let this process fetch model files (only `devmem embed --download`). Off by default: every other command, the
    hooks and the MCP server load models from local files only."""
    _DOWNLOAD[0] = bool(ok)


def get_embedder(store: Path | None = None, cfg: dict | None = None, project: Path | None = None) -> Embedder | None:
    """The configured embedder (cached per process), or None if off, its package is missing, or a model or cache
    folder it names is relative or inside the project or the store (check_model_path). `project` defaults to the folder
    that holds the store."""
    cfg = cfg if cfg is not None else settings(store)
    if not cfg:
        return None
    download = _DOWNLOAD[0]
    if project is None and store is not None:
        project = Path(store).parent
    key = json.dumps([cfg, download, str(project), str(store)], sort_keys=True)
    if key not in _EMBEDDERS:
        b = cfg.get("backend")
        try:
            if b == "fake":
                _EMBEDDERS[key] = FakeEmbedder()
            elif b == "fastembed":
                _EMBEDDERS[key] = FastEmbedder(cfg.get("model"), cfg.get("cache_dir"), download=download,
                                               project=project, store=store)
            elif b == "model2vec":
                _EMBEDDERS[key] = Model2VecEmbedder(cfg.get("model"), cfg.get("cache_dir"), download=download,
                                                    project=project, store=store)
            else:
                _EMBEDDERS[key] = None
        except Exception:  # noqa: BLE001 - a missing package, or no local model files: embeddings stay off
            _EMBEDDERS[key] = None
    return _EMBEDDERS[key]


# ------------------------------------------------------------------------------------------------
# vector cache
# ------------------------------------------------------------------------------------------------
def _db_path(store: Path) -> Path:
    return Path(store) / "index" / "vectors.db"


def _connect(store: Path, create: bool = True) -> sqlite3.Connection | None:
    p = _db_path(store)
    if not create and not p.exists():
        return None
    from .index import _not_regular
    linked = any(os.path.islink(x) or _not_regular(str(x)) for x in (p.parent, p, f"{p}-wal", f"{p}-shm", f"{p}-journal"))
    if linked and not create:
        return None          # never let SQLite write through a link
    if not linked:
        from .store import private_mkdir, private_sqlite
        private_mkdir(p.parent)
        private_sqlite(p)
    conn = sqlite3.connect(":memory:" if linked else str(p), timeout=10, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA secure_delete=ON")
    conn.execute("CREATE TABLE IF NOT EXISTS vec(model TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL, "
                 "h TEXT NOT NULL, meta TEXT, v BLOB NOT NULL, PRIMARY KEY(model, kind, key))")
    return conn


def _pack(v: list[float]) -> bytes:
    return array("f", v).tobytes()


def _unpack(b) -> list[float] | None:
    """A stored vector, or None for a blob that is not one (not bytes, or a length that is not a multiple of 4: a
    damaged or foreign row is skipped, never an exception)."""
    if not isinstance(b, (bytes, bytearray, memoryview)) or len(b) % 4:
        return None
    a = array("f")
    a.frombytes(bytes(b))
    return list(a)


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


class VectorCache:
    def __init__(self, store: Path, embedder: Embedder):
        self.store, self.emb = Path(store), embedder
        self._conn = _connect(store)  # create=True: never None, until close()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise AttributeError("the vector cache is closed")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def all(self, kind: str) -> dict[str, tuple[str, dict, list[float]]]:
        rows = self.conn.execute("SELECT key, h, meta, v FROM vec WHERE model=? AND kind=?", (self.emb.name, kind))
        out = {}
        for k, h, m, v in rows:
            vec = _unpack(v)
            if vec is None:
                continue          # a damaged row: skipped (and recomputed by ensure, which sees it missing)
            try:
                meta = json.loads(m) if m else {}
            except ValueError:
                continue
            out[k] = (h, meta, vec)
        return out

    def ensure(self, kind: str, docs: dict[str, tuple[str, dict]], have: dict | None = None,
               deadline: float | None = None) -> int:
        """Embed and store the docs ({key: (text, meta)}) whose text is new or changed. Stops at
        `deadline` (time.monotonic()); the rest is embedded on a later call. Returns how many were added."""
        have = have if have is not None else self.all(kind)
        todo = [(k, t, m) for k, (t, m) in docs.items() if k not in have or have[k][0] != _h(t)]
        done = 0
        for i in range(0, len(todo), BATCH):
            if deadline is not None and time.monotonic() > deadline:
                break
            chunk = todo[i:i + BATCH]
            vecs = self.emb.embed([t for _, t, _ in chunk], "passage")
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.executemany(
                    "INSERT OR REPLACE INTO vec(model, kind, key, h, meta, v) VALUES (?,?,?,?,?,?)",
                    [(self.emb.name, kind, k, _h(t), json.dumps(m, ensure_ascii=False), _pack(v))
                     for (k, t, m), v in zip(chunk, vecs)])
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            done += len(chunk)
        return done

    def drop(self, kind: str, keys) -> int:
        """Delete the vectors of these keys: at most DROP_MAX per call (the rest go on later calls), in one
        transaction. One autocommitted delete per row took 20 s for 5000 stale vectors, inside a prompt."""
        keys = sorted(keys)[:DROP_MAX]
        if not keys:
            return 0
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.executemany("DELETE FROM vec WHERE model=? AND kind=? AND key=?",
                                  [(self.emb.name, kind, k) for k in keys])
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        return len(keys)


def forget_keys(store: Path, items: list[tuple[str, str]]) -> None:
    """Remove cached vectors (every model) for these (kind, key) pairs. For kind "turn", key may be a
    session key: all of that session's turns ("<key>#<n>") go."""
    conn = _connect(store, create=False)
    if conn is None:
        return
    try:
        for kind, key in items:
            if kind == "turn":
                conn.execute("DELETE FROM vec WHERE kind='turn' AND (key=? OR substr(key, 1, ?)=?)",
                             (key, len(key) + 1, key + "#"))
            else:
                conn.execute("DELETE FROM vec WHERE kind=? AND key=?", (kind, key))
    finally:
        conn.close()


def scrub(store: Path, timeout: float = 3.0) -> bool:
    """For `devmem forget` only: VACUUM the vector cache so pages freed before secure_delete was on hold no
    deleted text. True if done (or there is no cache), False if the cache stayed locked."""
    conn = _connect(store, create=False)
    if conn is None:
        return True
    try:
        conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        conn.execute("VACUUM")
        return True
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


# ------------------------------------------------------------------------------------------------
# documents and ranking
# ------------------------------------------------------------------------------------------------
def file_text(rel: str, info: dict, project: Path) -> str:
    names = [rel, str(info.get("module", ""))] + [str(s) for s in info.get("symbols", [])[:30]]
    from .store import MAX_TEXT_BYTES, project_path, read_project_text
    p = project_path(project, rel)       # a plain project path, regular file, no link: like every project read
    head = ((read_project_text(p, MAX_TEXT_BYTES) if p is not None else None) or "")[:FILE_TEXT_CHARS]
    return " ".join(names) + "\n" + head


def _turn_docs(store: Path) -> dict[str, tuple[str, dict]]:
    from .pack import _docs_for
    from .sessions import load_sessions
    out = {}
    for rec in load_sessions(store):
        if not rec.get("_meta"):
            continue
        _s, tdocs = _docs_for(rec)
        for key, _fields, meta in tdocs:
            text = meta.get("p") or ""
            if text.strip():
                out[key] = (text[:TURN_TEXT_CHARS], meta)
    return out


def _select(scored: list[tuple[str, float]], min_sim: float, margin: float, limit: int = 20) -> list[tuple[str, float]]:
    scored = sorted(scored, key=lambda x: (-x[1], x[0]))
    if not scored or scored[0][1] < min_sim:
        return []
    top = scored[0][1]
    return [(k, s) for k, s in scored if s >= min_sim and s >= top - margin][:limit]


def rank_for_pack(project: Path, store: Path, task: str, idx, files_map: dict, with_sessions: bool,
                  session_id: str | None = None) -> dict | None:
    """Embedding rankings for a pack, or None when embeddings are off. Returns
    {"file": [(rel, sim)], "turn": [(turn key, sim)], "turn_meta": {turn key: meta}, "confident": bool,
     "pending": n documents not embedded yet}."""
    cfg = settings(store)
    emb = get_embedder(store, cfg, project=project)
    if cfg is None or emb is None or not (task or "").strip():  # no settings means no embedder
        return None
    t_min, t_margin = float(cfg.get("turn_min_sim", TURN_MIN_SIM)), float(cfg.get("turn_margin", TURN_MARGIN))
    f_min, f_margin = float(cfg.get("file_min_sim", FILE_MIN_SIM)), float(cfg.get("file_margin", FILE_MARGIN))
    confident_sim = float(cfg.get("confident_sim", CONFIDENT_SIM))
    deadline = time.monotonic() + float(os.environ.get("DEVMEM_EMBED_SECONDS", NEW_DOCS_SECONDS))
    cache = VectorCache(store, emb)
    try:
        qv = emb.embed([task], "query")[0]
        out: dict = {"file": [], "turn": [], "turn_meta": {}, "confident": False, "pending": 0}
        # files: (re)embed new or changed ones, keyed by the scan's content hash
        have = cache.all("file")
        if files_map:     # an empty or unreadable map is no evidence that every file is gone
            cache.drop("file", [k for k in have if k not in files_map])
        need = {rel: (info.get("hash", ""), info) for rel, info in files_map.items()
                if rel not in have or have[rel][1].get("hash") != info.get("hash")}
        if need:
            docs = {rel: (file_text(rel, info, project), {"hash": h}) for rel, (h, info) in need.items()}
            cache.ensure("file", docs, have={}, deadline=deadline)
            have = cache.all("file")
        out["pending"] += sum(1 for rel in files_map if rel not in have)
        out["file"] = _select([(k, cosine(qv, v)) for k, (_h0, _m, v) in have.items() if k in files_map],
                              f_min, f_margin)
        if with_sessions:
            known = idx.keys("turn", strict=True)    # None when the index was locked: then nothing is dropped
            tkeys = known if known is not None else set()
            thave = cache.all("turn")
            if known is not None:
                cache.drop("turn", [k for k in thave if k not in tkeys])
            if any(k not in thave for k in tkeys):
                docs = {k: v for k, v in _turn_docs(store).items() if k in tkeys}
                cache.ensure("turn", docs, have=thave, deadline=deadline)
                thave = cache.all("turn")
            scored = []
            for k, (_h0, meta, v) in thave.items():
                if session_id and meta.get("sid") == str(session_id):
                    continue
                scored.append((k, cosine(qv, v)))
                out["turn_meta"][k] = meta
            out["turn"] = _select(scored, t_min, t_margin)
            out["confident"] = bool(out["turn"]) and out["turn"][0][1] >= confident_sim
            out["pending"] += sum(1 for k in tkeys if k not in thave)
        out["turn_meta"] = {k: out["turn_meta"][k] for k, _ in out["turn"]}
        out["best"] = {"file": out["file"][0][1] if out["file"] else None,
                       "turn": out["turn"][0][1] if out["turn"] else None}
        return out
    finally:
        cache.close()
