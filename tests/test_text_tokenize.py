"""devmem.text.tokenize: English/code behaviour unchanged from v1, plus non-English scripts."""
import re
from collections import Counter

import pytest

from devmem.text import count_terms, tokenize


def v1_tokenize(text):
    """The v1 tokenizer from devmem/pack.py (hackathon build), copied here as the reference."""
    stop = set("""a an the and or of to in on for with from by at as is are be been it its this that these those
into onto than then so we our you your i me my us let lets let's please can could should would will just
add make new use using change implement implemented create update set get fix also when after before
same each all any some there here what which who how do does done not no now last next only one two""".split())
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text or "")
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
    out = []
    for w in re.findall(r"[A-Za-z][A-Za-z0-9]+|\d+", text.lower()):
        if w in stop or len(w) < 2:
            continue
        for suf in ("ing", "ed", "es", "s"):
            if len(w) > 4 + len(suf) - 1 and w.endswith(suf):
                w = w[: -len(suf)]
                break
        out.append(w)
    return out


ASCII_SAMPLES = [
    "Add a getUserName helper to the HUDController and fix snake_case_var parsing",
    "HTTPServer x86_64 html5Parser utf8 ABCDef parsedValues 1990s",
    "local function onPurchaseItem(player, itemId) Remotes.get('PurchaseItem'):FireServer(itemId) end",
    "def build_pack(project, store, task, budget=1500):\n    return tokenize(task)",
    "",
    "   ",
    "the and of",
]


# Where the current tokenizer (TOKENIZER_VERSION 3) intentionally differs from v1 on the samples above.
# Both differences are the stemmer now dropping a final -e the way Porter does when the stem before it
# has two or more syllables (purchase -> purchas, tokenize -> tokeniz), so that purchase/purchased/
# purchasing/purchases share one stem; v1 left "purchase" and stemmed "purchased" to "purchas".
INTENDED_VS_V1 = {
    ASCII_SAMPLES[2]: {"purchase": "purchas"},
    ASCII_SAMPLES[3]: {"tokenize": "tokeniz"},
}


def _normalise_v1(text, tokens):
    return [INTENDED_VS_V1.get(text, {}).get(t, t) for t in tokens]


@pytest.mark.parametrize("text", ASCII_SAMPLES)
def test_ascii_matches_v1_except_intended_differences(text):
    assert tokenize(text) == _normalise_v1(text, v1_tokenize(text))


def test_v3_differences_from_v1_word_by_word():
    """Each intended change, with the reason; anything not listed here must equal v1."""
    cases = {
        # plural / past forms now share one stem (v1: zombie->zombie, zombies->zombi)
        "zombie zombies": (["zombie", "zombi"], ["zomby", "zomby"]),
        "inventory inventories": (["inventory", "inventori"], ["inventory", "inventory"]),
        "move moving moved moves": (["move", "moving", "moved", "move"], ["move"] * 4),
        "rename renamed renaming": (["rename", "renam", "renam"], ["renam"] * 3),
        "run running": (["run", "runn"], ["run", "run"]),
        "map maps": (["map", "maps"], ["map", "map"]),            # v1 did not stem 4-letter plurals
        "day days": (["day", "days"], ["day", "day"]),
        "die dies dying died": (["die", "dies", "dying", "died"], ["di"] * 4),
        "class classes": (["clas", "class"], ["class", "class"]),  # v1 cut the s of "class"
        "status": (["statu"], ["status"]),                           # ss/us/is endings keep their s
        # words that must stay apart or unchanged
        "state stats mode mod": (["state", "stats", "mode", "mod"], ["state", "stat", "mode", "mod"]),
        "need speed string thing": (["need", "speed", "string", "thing"], ["need", "speed", "string", "thing"]),
        "x86 utf8 html5": (["x86", "utf8", "html5"], ["x86", "utf8", "html5"]),
    }
    for text, (old, new) in cases.items():
        assert tokenize(text) == new, text
        # the recorded v1 value is what v1 produced (guards the table against typos)
        if text not in ("state stats mode mod", "need speed string thing", "x86 utf8 html5", "die dies dying died"):
            assert v1_tokenize(text) == old, text


def test_function_words_are_stop_words():
    assert tokenize("he said she had been over there but they were doing very well") == ["said", "well"]
    assert tokenize("used uses") == []


def test_camel_snake_and_stemming():
    assert tokenize("getUserName") == ["user", "name"]           # "get" is a stop word
    assert tokenize("snake_case_value") == ["snake", "case", "valu"]   # value/values share a stem
    assert tokenize("HTTPServer") == ["http", "server"]
    assert tokenize("parsing parsed classes items") == ["pars", "pars", "class", "item"]


def test_arabic_prompt_yields_terms():
    toks = tokenize("المستخدمين يسجلون الدخول من صفحة تسجيل الدخول")
    assert toks, "an Arabic prompt must produce terms"
    assert "مستخدم" in toks            # ال prefix and ين suffix removed
    assert "دخول" in toks
    assert "من" not in toks             # stop word


def test_arabic_normalisation_matches_variants():
    # diacritics, tatweel and alef/ta-marbuta variants must not change the term
    assert tokenize("المُسْتَخْدِم") == tokenize("المستخدم")
    assert tokenize("مـــستخدم") == tokenize("مستخدم")
    assert tokenize("إعدادات") == tokenize("اعدادات")
    assert tokenize("صفحة") == tokenize("صفحه")
    assert tokenize("٣٤٥") == ["345"]


def test_mixed_arabic_and_code():
    toks = tokenize("أصلح الخطأ في HUDController عند شراء item")
    assert "hud" in toks and "controller" in toks and "item" in toks
    assert any("؀" <= c <= "ۿ" for t in toks for c in t)
    # the English part tokenizes exactly as it would alone
    assert [t for t in toks if t.isascii()] == tokenize("HUDController item")


def test_cjk_bigrams():
    assert tokenize("数据库") == ["数据", "据库"]
    assert tokenize("数据库connection") == ["数据", "据库", "connection"]
    assert tokenize("東京") == ["東京"]
    assert tokenize("字") == ["字"]                       # a lone character is kept
    # a query word matches a document containing it in running text
    assert set(tokenize("数据库")) <= set(tokenize("请把数据库连接移到配置文件"))
    assert tokenize("한국어") == ["한국", "국어"]


def test_accented_latin_greek_cyrillic():
    assert tokenize("Café naïve") == ["cafe", "naiv"]
    assert tokenize("Café") == tokenize("cafe")
    assert tokenize("naïveÉcole") == ["naiv", "ecol"]     # Unicode camelCase split
    assert tokenize("Привет мир") == ["привет", "мир"]
    assert tokenize("Ελληνικά") == ["ελληνικα"]


def test_indic_marks_kept_inside_words():
    assert tokenize("हिन्दी पाठ") == ["हिन्दी", "पाठ"]


def test_count_terms_equals_counter_of_tokenize():
    for text in ASCII_SAMPLES + ["المستخدمين يسجلون الدخول", "数据库connection Café getValue getValue"]:
        assert count_terms(text) == dict(Counter(tokenize(text)))


def test_non_ascii_text_keeps_ascii_words_identical():
    # one stray non-ASCII character (an em dash) must not change how the rest tokenizes
    a = "Add the parsedValues helper — then update HUDController"
    assert tokenize(a) == v1_tokenize(a.replace("—", " "))
