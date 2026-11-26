"""Helpers for the retrieval tests: a small project with captured sessions, built in tmp_path."""
from __future__ import annotations

import json
from pathlib import Path

from devmem import sessions as S
from devmem.scan import scan

FILES = {
    "src/shop.py": "# The upgrade shop between waves.\n"
                   "from src.config import REROLL_COST\n\n"
                   "def open_shop(player):\n    offers = roll_offers(player)\n    return offers\n\n"
                   "def reroll_offers(player):\n    player.coins -= REROLL_COST\n    return roll_offers(player)\n",
    "src/config.py": "# Tunable numbers.\nREROLL_COST = 10\nWAVE_SIZE = 8\nBLADE_COUNT = 2\n",
    "src/waves.py": "def run_waves(zombies):\n    for z in zombies:\n        if z.dead:\n            continue\n"
                    "        ok = z.step()\n        if not ok:\n            continue\n",
    "src/blades.py": "def orbit(blades):\n    for b in blades:\n        ok = b.spin()\n        if not ok:\n"
                     "            continue\n",
    "src/audio.py": "def play_sound(name):\n    return name\n",
    "README.md": "# Demo game\nRun it with rojo serve.\n",
}


def make_project(root: Path, files: dict[str, str] | None = None) -> tuple[Path, Path]:
    project = root / "proj"
    for rel, text in (files or FILES).items():
        p = project / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    store = project / ".devmemory"
    (store / "sessions").mkdir(parents=True)
    from devmem.consent import claim_store
    claim_store(store)       # the user's own store, as devmem notes one it creates (devmem.consent.store_refusal)
    return project, store


def turn(prompt: str, result: str = "", calls: list[dict] | None = None, index: int = 0,
         started_at: str = "2026-11-01T10:00:00Z") -> dict:
    t = S.new_turn(index, prompt, started_at)
    t["result_text"] = result or None
    t["result_source"] = "assistant-visible (test)" if result else None
    t["tool_calls"] = list(calls or [])
    return t


def write_session(store: Path, sid: str, started_at: str, turns: list[dict]) -> dict:
    for i, t in enumerate(turns):
        t["index"] = i
    rec = {"schema": 2, "session_id": sid, "provider": "claude-code", "capture_method": "hooks",
           "started_at": started_at, "turns": turns}
    S.derive_aggregates(rec, None, hash_now=False)
    meta = S.write_record(store, rec)
    rec["_meta"] = meta.name
    return rec


def write_live(store: Path, sid: str, prompts: list[str]) -> Path:
    log = store / "live" / f"{sid}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    lines = [{"event": "SessionStart", "ts": "2026-11-02T09:00:00.000Z", "session_id": sid, "source": "startup"}]
    for i, p in enumerate(prompts):
        lines.append({"event": "UserPromptSubmit", "ts": f"2026-11-02T09:0{i + 1}:00.000Z", "session_id": sid,
                      "prompt": p})
    log.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    return log


def standard_sessions(store: Path) -> dict[str, dict]:
    """Three sessions: the shop (with a stated reroll price), blades, and a two-turn session whose second
    turn is the one about sound."""
    shop = write_session(store, "aaaa1111-0000-0000-0000-000000000001", "2026-11-01T10:00:00Z", [
        turn("Add an upgrade shop between waves. Rerolling the offers costs 10 coins. All purchase logic must "
             "be server-side.", "I added the shop in src/shop.py.",
             [{"tool": "Write", "file": "src/shop.py", "new": FILES["src/shop.py"], "truncated": False}])])
    blades = write_session(store, "bbbb2222-0000-0000-0000-000000000002", "2026-11-01T11:00:00Z", [
        turn("Make the blades orbit the player: start with 2 blades, orbit radius 7 studs.",
             "Blades now orbit.", [{"tool": "Write", "file": "src/blades.py", "new": FILES["src/blades.py"],
                                    "truncated": False}])])
    two = write_session(store, "cccc3333-0000-0000-0000-000000000003", "2026-11-01T12:00:00Z", [
        turn("Tidy the README wording.", "Done.", started_at="2026-11-01T12:00:00Z"),
        turn("Now add a sound effect when a zombie dies, volume 0.4 exactly.", "Added play_sound.",
             [{"tool": "Edit", "file": "src/audio.py", "old": "", "new": "def play_sound(name):",
               "truncated": False}], started_at="2026-11-01T12:05:00Z")])
    return {"shop": shop, "blades": blades, "two": two}


def setup(tmp_path: Path, with_sessions: bool = True):
    project, store = make_project(tmp_path)
    recs = standard_sessions(store) if with_sessions else {}
    scan(project, store)
    return project, store, recs
