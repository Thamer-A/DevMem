# Retrieval eval (2026-11-13, commit 58ab826df3, dirty tree, budget 1500, mode full)

| group | cases | session recall | file recall | verbatim recall | noise | empty ok | tokens mean (max) |
|---|---|---|---|---|---|---|---|
| **overall** | 60 | 73% (45) | 69% (52) | 75% (40) | 58% | 38% (8) | 722.4 (1488) |
| kind: direct | 7 | 100% (4) | 76% (7) | 100% (3) | 57% | - (0) | 924.1 (1461) |
| kind: followup_early_req | 16 | 100% (16) | 74% (16) | 100% (16) | 52% | - (0) | 1016.9 (1486) |
| kind: non_english | 10 | 10% (10) | 50% (10) | 11% (9) | 50% | - (0) | 265.6 (554) |
| kind: paraphrase | 5 | 40% (5) | 50% (5) | 50% (4) | 62% | - (0) | 586.4 (1169) |
| kind: stale_memory | 2 | 100% (2) | 100% (2) | - (0) | 44% | - (0) | 886 (1003) |
| kind: terse_bug | 8 | 100% (8) | 62% (8) | 100% (8) | 58% | - (0) | 1002.9 (1488) |
| kind: trivial | 8 | - (0) | - (0) | - (0) | 100% | 38% (8) | 292.8 (596) |
| kind: untouched_file | 4 | - (0) | 100% (4) | - (0) | 71% | - (0) | 719.2 (917) |
| project: blade-swarm | 24 | 72% (18) | 88% (20) | 71% (17) | 64% | 0% (4) | 827.3 (1488) |
| project: coinrush | 18 | 83% (12) | 67% (16) | 89% (9) | 53% | 50% (2) | 866.3 (1461) |
| project: taskboard | 18 | 67% (15) | 47% (16) | 71% (14) | 52% | 100% (2) | 438.5 (1190) |

Numbers in parentheses are the count of cases that carry that label type. Noise = share of retrieved sessions+files that no label asked for. Empty ok = trivial prompts that got no session or file.

## Cases

| id | kind | sess | file | verb | items/off-label | tokens | missing |
|---|---|---|---|---|---|---|---|
| bs-01 | followup_early_req | 1/1 | 2/2 | 1/1 | 12/6 | 1340 |  |
| bs-02 | terse_bug | 1/1 | 1/1 | 2/2 | 12/9 | 1436 |  |
| bs-03 | followup_early_req | 2/2 | 1/1 | 2/2 | 10/6 | 1476 |  |
| bs-04 | followup_early_req | 2/2 | 1/1 | 2/2 | 8/4 | 1455 |  |
| bs-05 | direct | 1/1 | 1/1 | 1/1 | 6/2 | 1126 |  |
| bs-06 | paraphrase | 0/1 | 1/2 | 0/1 | 4/3 | 541 | d9c19cbb, src/server/Zombies.server.lua, "each wave adds 4 more" |
| bs-07 | paraphrase | 1/1 | 1/1 | 1/1 | 5/3 | 1169 |  |
| bs-08 | terse_bug | 1/1 | 0/2 | 1/1 | 8/4 | 1488 | src/client/ShopUi.client.lua, src/server/Shop.lua |
| bs-09 | terse_bug | 1/1 | 1/1 | 1/1 | 6/3 | 856 |  |
| bs-10 | followup_early_req | 1/1 | 0/1 | 1/1 | 10/7 | 1486 | src/client/Hud.client.lua |
| bs-11 | followup_early_req | 1/1 | 1/1 | 1/1 | 7/5 | 1174 |  |
| bs-12 | untouched_file | - | 1/1 | - | 5/4 | 847 |  |
| bs-13 | untouched_file | - | 1/1 | - | 8/6 | 917 |  |
| bs-14 | trivial (empty expected) | - | - | - | 2/2 | 248 |  |
| bs-15 | trivial (empty expected) | - | - | - | 3/3 | 295 |  |
| bs-16 | trivial (empty expected) | - | - | - | 5/5 | 363 |  |
| bs-17 | non_english | 0/1 | 1/1 | 0/1 | 2/1 | 248 | d9c19cbb, "each wave adds 4 more" |
| bs-18 | non_english | 0/1 | 1/1 | 0/1 | 2/1 | 248 | dbf18bb2, "Rerolling the offers costs 10 coins" |
| bs-19 | non_english | 0/1 | 1/1 | 0/1 | 2/1 | 248 | 3e2bb0e2, "Common = gray, Rare = blue, Epic = purple" |
| bs-20 | non_english | 0/1 | 1/1 | 0/1 | 2/1 | 248 | dbf18bb2, "Rerolling the offers costs 10 coins" |
| bs-21 | non_english | 1/1 | 1/1 | 1/1 | 4/2 | 554 |  |
| bs-22 | direct | 1/1 | 1/1 | 1/1 | 8/6 | 1178 |  |
| bs-23 | direct | 1/1 | 1/1 | - | 7/3 | 666 |  |
| bs-24 | trivial (empty expected) | - | - | - | 2/2 | 248 |  |
| cr-A1 | direct | - | 1/3 | - | 7/5 | 1461 | src/client/controllers/InputController.luau, src/shared/Config.luau |
| cr-A2 | direct | - | 1/1 | - | 9/6 | 1452 |  |
| cr-B1 | followup_early_req | 1/1 | 1/3 | 5/5 | 3/1 | 917 | src/server/services/CurrencyService.luau, src/shared/Config.luau |
| cr-B2 | followup_early_req | 1/1 | 1/2 | 1/1 | 4/1 | 1349 | src/shared/ItemCatalog.luau |
| cr-C1 | stale_memory | 1/1 | 2/2 | - | 4/1 | 769 |  |
| cr-C2 | stale_memory | 1/1 | 1/1 | - | 5/3 | 1003 |  |
| cr-D1 | untouched_file | - | 1/1 | - | 3/2 | 652 |  |
| cr-D2 | untouched_file | - | 1/1 | - | 1/0 | 461 |  |
| cr-11 | terse_bug | 1/1 | 1/1 | 1/1 | 7/4 | 1365 |  |
| cr-12 | followup_early_req | 1/1 | 1/1 | 1/1 | 3/1 | 715 |  |
| cr-13 | paraphrase | 1/1 | 0/2 | 1/1 | 4/3 | 947 | src/server/services/DataService.luau, src/shared/Config.luau |
| cr-14 | followup_early_req | 1/1 | 1/1 | 2/2 | 4/2 | 1022 |  |
| cr-15 | non_english | 0/1 | 0/1 | 0/1 | 0/0 | 444 | 17c7cb15, src/shared/Config.luau, "daily_reward" |
| cr-16 | non_english | 0/1 | 0/1 | - | 0/0 | 444 | f4fde737, src/client/controllers/ShopController.luau |
| cr-17 | followup_early_req | 1/1 | 1/1 | 1/1 | 2/0 | 738 |  |
| cr-18 | trivial (empty expected) | - | - | - | 4/4 | 596 |  |
| cr-19 | trivial (empty expected) | - | - | - | 0/0 | 444 |  |
| cr-20 | terse_bug | 1/1 | 1/2 | 1/1 | 2/0 | 815 | src/server/services/ShopService.luau |
| tb-01 | followup_early_req | 1/1 | 1/2 | 3/3 | 4/2 | 860 | public/style.css |
| tb-02 | direct | 1/1 | 1/1 | 2/2 | 3/1 | 423 |  |
| tb-03 | terse_bug | 1/1 | 1/2 | 1/1 | 8/5 | 904 | server.js |
| tb-04 | followup_early_req | 1/1 | 0/2 | 1/1 | 4/2 | 923 | public/app.js, server.js |
| tb-05 | followup_early_req | 1/1 | 1/1 | 1/1 | 10/7 | 684 |  |
| tb-06 | followup_early_req | 1/1 | 1/1 | 2/2 | 2/0 | 391 |  |
| tb-07 | followup_early_req | 1/1 | 1/1 | 1/1 | 7/3 | 551 |  |
| tb-08 | paraphrase | 0/1 | 0/1 | 0/1 | 0/0 | 74 | c2fabc15, server.js, "at most 3 cards" |
| tb-09 | paraphrase | 0/1 | 1/1 | - | 3/1 | 201 | fdb761dd |
| tb-10 | terse_bug | 1/1 | 0/1 | 1/1 | 4/2 | 427 | public/style.css |
| tb-11 | non_english | 0/1 | 0/2 | 0/3 | 0/0 | 74 | 4644d1f4, public/app.js, public/style.css, "#e5484d", "#3e63dd", "#8b8d98" |
| tb-12 | non_english | 0/1 | 0/1 | 0/1 | 0/0 | 74 | c2fabc15, server.js, "at most 3 cards" |
| tb-13 | non_english | 0/1 | 0/1 | 0/1 | 0/0 | 74 | 7edb923c, public/style.css, "Overdue cards get a red border" |
| tb-14 | direct | - | 0/1 | - | 2/1 | 163 | server.js |
| tb-15 | trivial (empty expected) | - | - | - | 0/0 | 74 |  |
| tb-16 | trivial (empty expected) | - | - | - | 0/0 | 74 |  |
| tb-17 | followup_early_req | 1/1 | 1/2 | 1/1 | 8/4 | 1190 | server.js |
| tb-18 | terse_bug | 1/1 | 1/1 | 1/1 | 8/5 | 732 |  |

Pack latency (varies run to run): mean 25.2 ms, p50 24.3 ms, p95 34.0 ms, max 93.6 ms.
