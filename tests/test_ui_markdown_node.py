"""V-X1 / V-X3: the viewer's real markdown and highlighting code, run in Node (skipped when `node` is not installed).

`mdInline`'s bare-URL autolink swallowed the placeholders that stand for links already built, so
`http://a[x](https://b/autofocus/tabindex/onfocus=...//)` put a whole `<a ...>` inside another link's href and the
recorded text chose the attributes (verify3/viewer). app.js is loaded with a minimal stand-in for the DOM, and its
functions are called directly.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parent.parent / "plugin" / "devmem" / "ui" / "app.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="node is not installed")

HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const stub = new Proxy(function () {}, {
  get: (t, k) => (k === Symbol.toPrimitive ? () => "" : k === "then" ? undefined : stub),
  apply: () => stub, construct: () => stub, set: () => true,
});
const g = {
  document: stub, window: stub, history: stub, navigator: stub,
  location: { href: "http://127.0.0.1:1/", search: "", pathname: "/", hash: "" },
  sessionStorage: { getItem: () => null, setItem() {} },
  fetch: () => new Promise(() => {}), requestAnimationFrame: () => 0, ResizeObserver: function () { return stub; },
};
const f = new Function(...Object.keys(g), src + "\n;return {mdInline, renderMd, highlight};");
const api = f(...Object.values(g));
const calls = JSON.parse(fs.readFileSync(0, "utf8"));
const out = calls.map(([fn, arg, lang]) => {
  const t = Date.now();
  const r = fn === "highlight" ? api.highlight(arg, lang).join("\n") : api[fn](arg);
  return [r, Date.now() - t];
});
process.stdout.write(JSON.stringify(out));
"""


def _run(tmp_path, calls):
    h = tmp_path / "harness.js"
    h.write_text(HARNESS, encoding="utf-8")
    r = subprocess.run([NODE, str(h), str(APP_JS)], input=json.dumps(calls).encode("utf-8"), capture_output=True,
                       timeout=180)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[-2000:]
    return json.loads(r.stdout.decode("utf-8"))


ALLOWED_ATTRS = {"href", "target", "rel", "class", "title", "data-open", "data-id"}
ATTACKS = [
    "http://a[x](https://b/autofocus/tabindex/onfocus=alert(1)//)",
    'see http://a[x](https://b/style=position:fixed;inset:0;z-index:9//) now',
    "http://a`code`more", "http://a[[note]]b", "https://x/\x01" + "0\x01", "a\x01" + "0\x01b http://c",
]


def test_recorded_text_cannot_choose_link_attributes(tmp_path):
    out = _run(tmp_path, [["mdInline", s] for s in ATTACKS] + [["renderMd", s] for s in ATTACKS])
    for (html, _ms), src in zip(out, ATTACKS + ATTACKS):
        for tag in re.findall(r"<a\b([^>]*)>", html):
            names = set(re.findall(r'\s([a-zA-Z-]+)=', tag))
            assert names <= ALLOWED_ATTRS, (src, html)
            assert tag.count('"') % 2 == 0 and "<" not in tag, (src, html)


def test_links_still_work(tmp_path):
    (html, _), = _run(tmp_path, [["mdInline", "read https://example.com/a?b=1 and [docs](https://d.example/x)"]])
    assert '<a href="https://example.com/a?b=1"' in html and '<a href="https://d.example/x"' in html


def test_highlighting_an_unclosed_long_string_is_fast(tmp_path):
    shapes = [["highlight", "[[" * 100_000, "luau"], ["highlight", "--[[" * 50_000, "luau"],
              ["highlight", '"""' * 70_000, "python"], ["highlight", "/*" * 100_000, "js"],
              ["highlight", "`" * 200_000, "js"], ["highlight", "[[" * 30_000, "luau"]]     # the last one below 64 KB
    t = time.perf_counter()
    out = _run(tmp_path, shapes)
    assert time.perf_counter() - t < 20
    assert all(ms < 5000 for _html, ms in out), [ms for _h, ms in out]


# G2: from every quote of an unclosed, escape-filled string (`"\"\"\"...`) the old string patterns scanned to the end of
# the line, failed and began again at the next quote: 3-5 s per 64 KB file on the machine that found it (0.1-3 ms now).
BS = chr(92)    # a backslash, kept out of string literals
QUOTE_SHAPES = [(lang, q + BS) for lang in ("js", "python", "luau") for q in ('"', "'")] + [
    ("js", "`" + BS), ("luau", "`" + BS), ("js", '"' + BS + "<b>&"), ("python", "'" + BS + '<i>"')]


def test_highlighting_unclosed_escaped_strings_is_linear(tmp_path):
    calls = [["highlight", motif * (65_000 // len(motif)), lang] for lang, motif in QUOTE_SHAPES]
    out = _run(tmp_path, calls)
    slow = [(lang, motif, ms) for (lang, motif), (_h, ms) in zip(QUOTE_SHAPES, out) if ms >= 1000]
    assert not slow, slow


SPAN = re.compile(r'<span class="tk-[cknsf]">|</span>')


def _unescape(s: str) -> str:
    return s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'").replace("&amp;", "&")


def test_highlighting_unclosed_strings_stays_escaped(tmp_path):
    texts = [("js", 'x = "<img src=x onerror=alert(1)>' + BS + "\nlet y = `<script>${a}" + BS),
             ("python", "s = '<b>" + BS + "'\n" + '"""<i>&amp;'), ("luau", 'local s = "</span><a>' + BS + "\n`<x>" + BS),
             ("js", ('"' + BS + "<b>&") * 500)]
    out = _run(tmp_path, [["highlight", t, lang] for lang, t in texts])
    for (_lang, text), (html, _ms) in zip(texts, out):
        bare = SPAN.sub("", html)
        assert "<" not in bare and ">" not in bare, html[:300]
        assert _unescape(bare) == text
