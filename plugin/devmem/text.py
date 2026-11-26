"""The one tokenizer (stdlib only). Everything that indexes or queries text uses `tokenize()`.

What a term is:
  * ASCII / code: camelCase and snake_case are split, text is lower-cased, words are
    `[a-z][a-z0-9]+` or digit runs, English stop words are dropped and a light suffix stemmer
    (-ies, -ing, -ed, -es, -s, trailing -e; see `_stem_en`) is applied. Version 3 widened the stop list
    with English function words and unified word forms (zombie/zombies, move/moving/moved, rename/renamed,
    inventory/inventories, die/dies/dying); `tests/test_text_tokenize.py` lists every intended difference
    from the v1 tokenizer.
  * Other alphabetic scripts (accented Latin, Greek, Cyrillic, Hebrew, Indic, ...): NFKC, lower-case,
    words are runs of letters/digits/combining marks. Diacritics are folded for Latin, Greek,
    Cyrillic, Hebrew and Arabic ("café" -> "cafe"); Indic scripts keep their marks because vowel signs
    are part of the word there.
  * Arabic: harakat and tatweel removed, alef variants -> ا, ى -> ي, ة -> ه, Arabic-Indic digits ->
    ASCII digits, a small stop-word list, and a light prefix/suffix stemmer in the style of Larkey's
    Light10 (و / ال / بال / كال / فال / لل; ها ان ات ون ين يه ه ي). Persian/Urdu share the script
    and get the same treatment, which is approximate for them.
  * Scripts written without spaces between words (Han, Hiragana, Katakana, Hangul, Thai, Lao,
    Khmer, Myanmar): overlapping character bigrams, as Lucene's CJK analyzer does; a run of a single
    character yields that character. No dictionary segmenter is needed and it is symmetric between
    query and document. Hangul is spaced, but bigrams also cope with particles attached to nouns.

Bump TOKENIZER_VERSION whenever the output of tokenize() changes; the index rebuilds on mismatch.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter

TOKENIZER_VERSION = 3

STOP_WORDS = frozenset("""a an the and or of to in on for with from by at as is are be been it its this that these
those into onto than then so we our you your i me my us let lets let's please can could should would will just
add make new use using change implement implemented create update set get fix also when after before
same each all any some there here what which who how do does done not no now last next only one two
used uses myself ours ourselves yours yourself yourselves he him his himself she her hers herself itself
they them their theirs themselves whom am was were being have has had having did doing but if because until
while about against between through during above below up down out off over under again further once where
why both few more most other such nor own too very don""".split())

_ASCII_CAMEL1 = re.compile(r"([a-z0-9])([A-Z])")
# an upper-case letter that starts a capitalised word right after other capitals ("HTTPServer" -> "HTTP Server"):
# `([A-Z]+)([A-Z][a-z])` gave the same splits but rescanned a long run of capitals from every position
_ASCII_CAMEL2 = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_ASCII_WORD = re.compile(r"[a-z][a-z0-9]+|\d+")

# Combining marks that belong inside words (Python's \w does not match category M characters).
_MARKS = ("̀-ͯ҃-҉֑-ׇؐ-ًؚ-ٰٟۖ-ۭ"
          "ऀ-ॣ०-෿฀-໿ༀ-࿿က-႟ក-៿"
          "᪰-᫿᷀-᷿⃐-⃿゙゚︠-︯")
# a word starts with a letter (or mark) and continues with letters/digits/marks; or is a digit run
_WORD = re.compile(rf"(?:[^\W\d_]|[{_MARKS}])(?:[^\W_]|[{_MARKS}])*|\d+")

_DIGITS = {**{0x0660 + i: str(i) for i in range(10)}, **{0x06F0 + i: str(i) for i in range(10)}}

# scripts without spaces between words -> character bigrams
_UNSEG_RANGES = ((0x0E00, 0x0EFF), (0x1000, 0x109F), (0x1780, 0x17FF), (0x1100, 0x11FF),
                 (0x3040, 0x30FF), (0x3130, 0x318F), (0x31F0, 0x31FF), (0x3400, 0x4DBF),
                 (0x4E00, 0x9FFF), (0xA960, 0xA97F), (0xAC00, 0xD7FF), (0xF900, 0xFAFF),
                 (0x20000, 0x3FFFF))
_UNSEG_CHARS = re.compile("[" + "".join(
    f"{chr(a)}-{chr(b)}" if b < 0x10000 else f"\\U{a:08x}-\\U{b:08x}" for a, b in _UNSEG_RANGES) + "]")


def _is_unseg(ch: str) -> bool:
    cp = ord(ch)
    return any(a <= cp <= b for a, b in _UNSEG_RANGES)


# ---- Arabic ---------------------------------------------------------------------------------
_AR_RANGE = re.compile("[؀-ۿݐ-ݿࢠ-ࣿ]")
_AR_DROP = re.compile("[ـً-ٰٟۖ-ۭ]")      # tatweel + harakat
_AR_MAP = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ة": "ه"})
_AR_PREFIXES = ("وال", "بال", "كال", "فال", "لل", "ال")
_AR_SUFFIXES = ("ها", "ان", "ات", "ون", "ين", "يه", "ه", "ي")


def _ar_norm(w: str) -> str:
    return _AR_DROP.sub("", w).translate(_AR_MAP)


_AR_STOP_RAW = """في من على إلى الى عن مع هذا هذه ذلك تلك هناك هنا التي الذي الذين اللذين او أو ثم لا ما لم لن هل
كان كانت يكون قد كل بعد قبل عند عندما هو هي هم هن انا أنا نحن انت أنت انتم أنتم يا ان أن إن اذا إذا حتى لكن بين ايضا
أيضا غير اي أي منذ لقد كما فقط لو فضلك رجاء الآن اريد أريد نريد"""
AR_STOP_WORDS = frozenset(_ar_norm(w) for w in _AR_STOP_RAW.split())


def _ar_stem(w: str) -> str:
    if len(w) > 3 and w.startswith("و"):
        w = w[1:]
    for p in _AR_PREFIXES:
        if w.startswith(p) and len(w) - len(p) >= 2:
            w = w[len(p):]
            break
    for s in _AR_SUFFIXES:
        if w.endswith(s) and len(w) - len(s) >= 2:
            w = w[: -len(s)]
    return w


# ---- per-word normalisation -------------------------------------------------------------------
_IE_FORMS = {"die": "di", "dies": "di", "died": "di", "dying": "di",
             "lie": "li", "lies": "li", "lied": "li", "lying": "li",
             "tie": "ti", "ties": "ti", "tied": "ti", "tying": "ti", "vying": "vi"}
_VOWELS = frozenset("aeiouy")


def _cv(stem: str) -> str:
    """Consonant/vowel pattern of a lower-case word ('y' is a vowel after a consonant)."""
    out = []
    for i, ch in enumerate(stem):
        if ch in "aeiou" or (ch == "y" and i > 0 and out[-1] == "C"):
            out.append("V")
        else:
            out.append("C")
    return "".join(out)


def _measure(stem: str) -> int:
    """Porter's m: the number of vowel-then-consonant groups in the stem."""
    return _cv(stem).count("VC")


def _short_cvc(stem: str) -> bool:
    """Ends consonant-vowel-consonant, the last consonant not w, x or y (Porter's *o)."""
    p = _cv(stem)
    return len(stem) >= 3 and p[-3:] == "CVC" and stem[-1] not in "wxy"


def _past_or_ing_base(b: str) -> str | None:
    """The stem left after removing -ing/-ed, or None when it is too short to be a word stem."""
    if len(b) < 3 or not _VOWELS & set(b):
        return None
    # running -> runn -> run, stopped -> stop; real double letters stay (call, miss, stuff, buzz, add)
    if len(b) >= 4 and b[-1] == b[-2] and b[-1] not in "lsfzaeiou":
        return b[:-1]
    # moving -> mov -> move (short stems lost an e: the e is what makes mov/move, tun/tune one word)
    if _measure(b) == 1 and _short_cvc(b):
        return b + "e"
    return b


def _stem_en(w: str) -> str:
    """Light English suffix stemmer for lower-case words, a small subset of Porter's rules. Inflections
    of one word should share a stem (zombie/zombies, move/moving/moved, rename/renamed, inventory/
    inventories, die/dies/dying) while unrelated words stay apart (state vs stats, mode vs mod, class vs
    clas): a stem needs a vowel and three letters, `ss`/`us`/`is` endings keep their s (class, status,
    analysis), `-eed` words (need, speed) are not read as past tense, and a final e is only dropped
    where Porter drops it (not from short consonant-vowel-consonant stems: mode, state, file, code).
    Words containing digits (x86, utf8) are left alone."""
    if not w.isalpha():
        return w
    if w in _IE_FORMS:
        return _IE_FORMS[w]
    n = len(w)
    if n > 4 and w.endswith(("ies", "ied")):
        return w[:-3] + "y"
    b = None
    if w.endswith("ing"):
        b = _past_or_ing_base(w[:-3])
    elif w.endswith("ed") and not w.endswith("eed"):
        b = _past_or_ing_base(w[:-2])
    elif w.endswith("s") and n >= 4 and not w.endswith(("ss", "us", "is")):
        b = w[:-2] if w.endswith(("sses", "xes", "zes", "ches", "shes")) else w[:-1]
    if b is None:
        b = w
    if len(b) >= 5 and b.endswith("ie"):          # zombie -> zomby, matching zombies -> zomby
        return b[:-2] + "y"
    if len(b) >= 4 and b.endswith("e") and not b.endswith("ee"):
        stem = b[:-1]
        m = _measure(stem)
        if m > 1 or (m == 1 and not _short_cvc(stem)):
            return stem
    return b


_CACHE: dict[str, str | None] = {}


def _ascii_term(w: str) -> str | None:
    """w is a lower-case ASCII word from _ASCII_WORD."""
    t = _CACHE.get(w, "")
    if t != "":
        return t
    t = None if (w in STOP_WORDS or len(w) < 2) else _stem_en(w)
    if len(_CACHE) > 200_000:
        _CACHE.clear()
    _CACHE[w] = t
    return t


def _fold_marks(w: str) -> str:
    d = unicodedata.normalize("NFKD", w)
    return unicodedata.normalize("NFC", "".join(c for c in d if not unicodedata.combining(c)))


def _unicode_term(w: str) -> str | None:
    """w is a lower-case non-ASCII word without unsegmented-script characters."""
    if _AR_RANGE.search(w):
        w = _ar_norm(w)
        if w in AR_STOP_WORDS or len(w) < 2:
            return None
        w = _ar_stem(w)
        return w if len(w) >= 2 else None
    base = next((c for c in w if not unicodedata.combining(c)), w[:1])
    if ord(base) < 0x0600:          # Latin, Greek, Cyrillic, Armenian, Hebrew: fold diacritics
        w = _fold_marks(w)
        if w.isascii():
            if w.isdigit():
                return w if len(w) >= 2 else None
            return _ascii_term(w) if w[:1].isalpha() else None
    return w if len(w) >= 2 else None


def _camel_split(w: str) -> list[str]:
    parts, start = [], 0
    for i in range(1, len(w)):
        a, b = w[i - 1], w[i]
        if b.isupper() and (a.islower() or a.isdigit()):
            parts.append(w[start:i])
            start = i
        elif a.isupper() and b.isupper() and i + 1 < len(w) and w[i + 1].islower():
            parts.append(w[start:i])
            start = i
    parts.append(w[start:])
    return [p for p in parts if p]


def _bigrams(run: str) -> list[str]:
    if len(run) == 1:
        return [run]
    return [run[i:i + 2] for i in range(len(run) - 1)]


def _ascii_tokens(text: str) -> list[str]:
    text = _ASCII_CAMEL2.sub(" ", _ASCII_CAMEL1.sub(r"\1 \2", text))
    out = []
    for w in _ASCII_WORD.findall(text.lower()):
        t = _ascii_term(w)
        if t:
            out.append(t)
    return out


NFKC_PIECE = 4096       # characters normalised at a time
NFKC_SLACK = 4096       # characters NFKC may add to a text before the rest of it is left out


def nfkc(text: str, cap: int | None = None) -> str:
    """NFKC of `text`, at most `cap` characters of it (default: the input's length + NFKC_SLACK). Some characters
    expand a lot (U+FDFA becomes 18), and both normalising and tokenising cost grow with the output: 500 KB of
    U+FDFA took 2.9 s to normalise and 7.3 s to tokenise. Text that is already normal (almost all) is returned as it
    is; otherwise it is normalised in pieces cut after a space or line end, and the output stops at the cap."""
    if unicodedata.is_normalized("NFKC", text):
        return text
    if cap is None:
        cap = len(text) + NFKC_SLACK
    out, n, i, size = [], 0, 0, len(text)
    while i < size and n < cap:
        j = min(size, i + NFKC_PIECE)
        if j < size:
            k = max(text.rfind(" ", i, j), text.rfind("\n", i, j))
            if k > i:
                j = k + 1
        piece = unicodedata.normalize("NFKC", text[i:j])
        out.append(piece)
        n += len(piece)
        i = j
    return "".join(out)[:cap]


def _general_tokens(text: str) -> list[str]:
    text = nfkc(text).translate(_DIGITS)
    text = _ASCII_CAMEL2.sub(" ", _ASCII_CAMEL1.sub(r"\1 \2", text))
    out: list[str] = []
    for w in _WORD.findall(text):
        if w.isascii():
            lw = w.lower()
            if lw.isdigit():
                if len(lw) >= 2:
                    out.append(lw)
                continue
            t = _ascii_term(lw)
            if t:
                out.append(t)
            continue
        pieces = _camel_split(w) if (w != w.lower() and w != w.upper()) else [w]
        for piece in pieces:
            if _UNSEG_CHARS.search(piece):
                run, seg = [], []
                for ch in piece:
                    if _is_unseg(ch):
                        if seg:
                            out.extend(_plain(("".join(seg)).lower()))
                            seg = []
                        run.append(ch)
                    else:
                        if run:
                            out.extend(_bigrams("".join(run)))
                            run = []
                        seg.append(ch)
                if run:
                    out.extend(_bigrams("".join(run)))
                if seg:
                    out.extend(_plain("".join(seg).lower()))
            else:
                out.extend(_plain(piece.lower()))
    return out


def _plain(w: str) -> list[str]:
    """A lower-case word without unsegmented characters -> 0..n terms (a digit/letter boundary
    left inside a mixed word is split the same way the ASCII path does)."""
    if w.isascii():
        return [t for t in (_ascii_term(x) if not x.isdigit() else (x if len(x) >= 2 else None)
                            for x in _ASCII_WORD.findall(w)) if t]
    t = _unicode_term(w)
    return [t] if t else []


def count_terms(text: str | None) -> dict[str, int]:
    """Same result as Counter(tokenize(text)), faster for long ASCII text (each distinct word is
    normalised once instead of once per occurrence). Used for indexing document bodies."""
    if not text:
        return {}
    if not text.isascii():
        return dict(Counter(_general_tokens(text)))
    text = _ASCII_CAMEL2.sub(" ", _ASCII_CAMEL1.sub(r"\1 \2", text))
    out: dict[str, int] = {}
    for w, c in Counter(_ASCII_WORD.findall(text.lower())).items():
        t = _ascii_term(w)
        if t:
            out[t] = out.get(t, 0) + c
    return out


def tokenize(text: str | None) -> list[str]:
    """Text -> list of index terms (see module docstring). Order and duplicates are preserved."""
    if not text:
        return []
    if text.isascii():
        return _ascii_tokens(text)
    return _general_tokens(text)
