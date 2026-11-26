"""Property check of joining sibling strings and cutting them back (Redactor._run, Redactor.joined, _json_strings), after
the verifier's f1_join_fuzz.py (round v8b): random lists of benign strings (empty, newline, NUL, CR, ANSI, combining
marks, CJK, JSON text, 5000-character items, backslashes, quotes) with one or two multi-line secrets planted across
items (one line per item, with a newline, in groups, in one item, starting or ending inside an item; with "", "+",
" " or "-" decoration), in eight shapes: record, tree, joined, capture._bounded, dictionary values, content blocks,
JSON text and JSON inside JSON. Counts leaks, benign items that changed, lists whose length changed, output that does
not parse and redactions that are not deterministic. All secrets are made up, generated from the seed at run time."""
from __future__ import annotations

import json
import random

from devmem import capture
from devmem.redact import Redactor

AL = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
KEY = "PRIVATE " + "KEY-----"


def rnd(rng, n, al=AL):
    return "".join(rng.choice(al) for _ in range(n))


def b64line(rng, n=64):
    return rnd(rng, n, AL + "+/")


def pw(rng):
    return rnd(rng, 4) + rng.choice("!#%*-_.") + rnd(rng, 6) + str(rng.randint(10, 99))


def construct(rng, kind):
    """(lines, secret fragments) of one multi-line secret."""
    if kind == "pem":
        body = [b64line(rng) for _ in range(rng.randint(2, 6))] + [b64line(rng, 20) + "=="]
        return ["-----BEGIN RSA " + KEY] + body + ["-----END RSA " + KEY], body
    if kind == "pem_hdr":
        body = [b64line(rng) for _ in range(3)]
        return (["-----BEGIN RSA " + KEY, "Proc-Type: 4,ENCRYPTED", "DEK-Info: AES-128-CBC,"
                 + rnd(rng, 32, "0123456789ABCDEF"), ""] + body + ["-----END RSA " + KEY]), body
    if kind == "openssh":
        body = [b64line(rng, 70) for _ in range(4)]
        return ["-----BEGIN OPENSSH " + KEY] + body + ["-----END OPENSSH " + KEY], body
    if kind == "pgp":
        body = [b64line(rng) for _ in range(3)] + ["=" + rnd(rng, 4)]
        return (["-----BEGIN PGP PRIVATE " + "KEY BLOCK-----", ""] + body + ["-----END PGP PRIVATE " + "KEY BLOCK-----"]), \
            body[:3]
    if kind == "ppk":
        pub = [b64line(rng) for _ in range(2)]
        priv = [b64line(rng) for _ in range(3)]
        return (["PuTTY-User-Key-File-3: ssh-rsa", "Encryption: none", "Comment: rsa-key", "Public-Lines: 2"] + pub +
                ["Private-Lines: 3"] + priv + ["Private-MAC: " + rnd(rng, 64, "0123456789abcdef")]), priv
    if kind == "yaml_block":
        p = pw(rng)
        return ["db:", "  password: |", "    " + p, "  host: db"], [p]
    if kind == "yaml_folded":
        p = pw(rng)
        return ["db:", "  secret_key: >-", "    " + p, "  port: 5432"], [p]
    if kind == "toml_ml":
        p = pw(rng)
        return ["[db]", 'password = """', p, '"""'], [p]
    if kind == "xml_ml":
        p = pw(rng)
        return ["<config>", "  <password>", "    " + p, "  </password>", "</config>"], [p]
    if kind == "k8s":
        p = pw(rng)
        return ["env:", "  - name: DB_PASSWORD", "    value: " + p], [p]
    if kind == "json_split":
        p = pw(rng)
        return ['{', '  "password":', '    "' + p + '"', '}'], [p]
    raise KeyError(kind)


KINDS = ["pem", "pem_hdr", "openssh", "pgp", "ppk", "yaml_block", "yaml_folded", "toml_ml", "xml_ml", "k8s", "json_split"]

BENIGN = ["", "\n", "\n\n", "}", "{", "+", " ", "  ", "-", "foo()", "x = 1", "return x", "\tindent", "é",
          "é", "日本語", '{"a": 1, "b": [1, 2]}', '["x", "y"]', "\r", "line\r", "a\x00b", "\x00",
          "\x07bell", "\x1b[0m", "# comment", "// note", "import os", "def f(a, b):", "    pass", "SELECT 1;", "a\nb",
          "c\nd\n", "\\", '"', "'", "\\n", "x" * 5000, "lorem ipsum " * 300, "　", "﻿bom", "tab\tsep",
          "a,b,c", "k: v", "k = v", "name: web", "image: nginx:1.25", "port = 8080", "{{ .Values.x }}", "${HOME}"]


def benign(rng):
    if rng.random() < 0.7:
        return rng.choice(BENIGN)
    return "word" + str(rng.randint(0, 999)) + " " + rng.choice(["foo", "bar(1)", "baz[2]", "q.r"])


def plant(rng, n_items):
    """A list of strings with 1-2 constructs placed in it, items grouping 1..k lines, optional decoration; the indexes
    of the items that hold part of a construct, and the secret fragments."""
    items, owned, secrets = [], set(), []
    for _ in range(rng.randint(0, n_items)):
        items.append(benign(rng))
    for _c in range(rng.randint(1, 2)):
        lines, sec = construct(rng, rng.choice(KINDS))
        secrets += sec
        deco = rng.choice(["", "", "+", " ", "-"])
        lines = [deco + ln for ln in lines]
        mode = rng.choice(["perline", "perline_nl", "groups", "one", "midstart", "midend"])
        if mode == "perline":
            group = list(lines)
        elif mode == "perline_nl":
            group = [ln + "\n" for ln in lines]
        elif mode == "groups":
            group, k = [], 0
            while k < len(lines):
                s = rng.randint(1, 3)
                group.append("\n".join(lines[k:k + s]) + ("\n" if rng.random() < 0.3 else ""))
                k += s
        elif mode == "one":
            group = ["\n".join(lines)]
        elif mode == "midstart":
            group = ["prefix text\n" + lines[0]] + lines[1:]
        else:
            group = lines[:-1] + [lines[-1] + "\ntrailing text"]
        free = [k for k in range(len(items) + 1) if not (k in owned and (k - 1) in owned)]
        pos = rng.choice(free)
        items[pos:pos] = group
        owned = {i + (len(group) if i >= pos else 0) for i in owned} | set(range(pos, pos + len(group)))
    for _ in range(rng.randint(0, n_items)):
        items.append(benign(rng))
    return items, owned, secrets


def leaks(text, secrets):
    out = []
    for s in secrets:
        for frag in (s, s[:16], s[-16:]) if len(s) >= 16 else (s,):
            if frag in text or json.dumps(frag)[1:-1] in text:
                out.append(frag)
                break
    return out


def _check(name, items, owned, secrets, out, stats, examples):
    def bump(what, *ex):
        stats[f"{name}:{what}"] = stats.get(f"{name}:{what}", 0) + 1
        if len(examples) < 20:
            examples.append((name, what, *ex))
    if leaks(json.dumps(out, ensure_ascii=False), secrets):
        bump("leak", items, out)
    if len(out) != len(items):
        bump("len", items, out)
    for i, (a, b) in enumerate(zip(items, out)):
        if i not in owned and a != b:
            bump("benign_changed", i, a, b, items, out)
            break


def run(n_cases: int, seed: int) -> tuple[dict, list]:
    rng = random.Random(seed)
    stats: dict = {}
    examples: list = []
    for _case in range(n_cases):
        items, owned, secrets = plant(rng, 6)
        alone = Redactor(None, enabled=True)
        owned = owned | {i for i, t in enumerate(items) if alone.text(t) != t}   # look-alikes changed on their own
        if leaks(Redactor(None, enabled=True).text("\n".join(t[:-1] if t.endswith("\n") else t for t in items)),
                 secrets):
            stats["plain:leak"] = stats.get("plain:leak", 0) + 1
            continue
        o1 = Redactor(None, enabled=True).record(list(items))
        _check("record", items, owned, secrets, o1, stats, examples)
        if Redactor(None, enabled=True).record(list(items)) != o1:
            stats["record:nondeterministic"] = stats.get("record:nondeterministic", 0) + 1
        o3, _cut = Redactor(None, enabled=True).tree({"lines": list(items)}, 10_000_000)
        _check("tree", items, owned, secrets, o3["lines"], stats, examples)
        _check("joined", items, owned, secrets, Redactor(None, enabled=True).joined(list(items)), stats, examples)
        _check("capture", items, owned, secrets, capture._bounded(list(items), Redactor(None, enabled=True)), stats,
               examples)
        d = {f"k{i}": t for i, t in enumerate(items)}
        o5 = Redactor(None, enabled=True).record(dict(d))
        if list(o5) != list(d):
            stats["dict:keyorder"] = stats.get("dict:keyorder", 0) + 1
        _check("dict", items, owned, secrets, list(o5.values()), stats, examples)
        o6 = Redactor(None, enabled=True).record([{"type": "text", "text": t} for t in items])
        _check("blocks", items, owned, secrets, [b.get("text") for b in o6], stats, examples)
        txt = json.dumps({"structuredPatch": [{"lines": items}]})
        for name, text, loads in (("jsontext", txt, json.loads),
                                  ("jsonjson", json.dumps(txt), lambda s: json.loads(json.loads(s)))):
            red = Redactor(None, enabled=True).text(text)
            try:
                back = loads(red)["structuredPatch"][0]["lines"]
            except ValueError:
                stats[f"{name}:unparseable"] = stats.get(f"{name}:unparseable", 0) + 1
                continue
            _check(name, items, owned, secrets, back, stats, examples)
    return stats, examples


if __name__ == "__main__":
    import sys
    st, ex = run(int(sys.argv[1]) if len(sys.argv) > 1 else 3000, int(sys.argv[2]) if len(sys.argv) > 2 else 12)
    print(json.dumps(st, indent=1))
    for e in ex[:5]:
        print(repr(e)[:2000])
