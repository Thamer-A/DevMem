# Retrieval eval (2026-11-13, commit 5c7979d59b, dirty tree, budget 1500, mode full)

| group | cases | session recall | file recall | verbatim recall | noise | empty ok | tokens mean (max) |
|---|---|---|---|---|---|---|---|
| **overall** | 60 | 71% (45) | 67% (52) | 72% (40) | 40% | 100% (8) | 482.2 (1465) |
| kind: direct | 7 | 100% (4) | 95% (7) | 100% (3) | 45% | - (0) | 860.4 (1252) |
| kind: followup_early_req | 16 | 100% (16) | 86% (16) | 100% (16) | 36% | - (0) | 788.9 (1465) |
| kind: non_english | 10 | 0% (10) | 0% (10) | 0% (9) | - | - (0) | 0 (0) |
| kind: paraphrase | 5 | 40% (5) | 30% (5) | 50% (4) | 56% | - (0) | 328.8 (912) |
| kind: stale_memory | 2 | 100% (2) | 100% (2) | - (0) | 38% | - (0) | 537.5 (656) |
| kind: terse_bug | 8 | 100% (8) | 88% (8) | 100% (8) | 38% | - (0) | 773.1 (1141) |
| kind: trivial | 8 | - (0) | - (0) | - (0) | - | 100% (8) | 0 (0) |
| kind: untouched_file | 4 | - (0) | 100% (4) | - (0) | 60% | - (0) | 345.2 (649) |
| project: blade-swarm | 24 | 67% (18) | 72% (20) | 65% (17) | 44% | 100% (4) | 573.3 (1465) |
| project: coinrush | 18 | 83% (12) | 69% (16) | 89% (9) | 41% | 100% (2) | 463.3 (1252) |
| project: taskboard | 18 | 67% (15) | 59% (16) | 71% (14) | 33% | 100% (2) | 379.5 (1210) |

Numbers in parentheses are the count of cases that carry that label type. Noise = share of retrieved sessions+files that no label asked for. Empty ok = trivial prompts that got no session or file.

## Cases

| id | kind | sess | file | verb | items/off-label | tokens | missing |
|---|---|---|---|---|---|---|---|
| bs-01 | followup_early_req | 1/1 | 2/2 | 1/1 | 9/3 | 1353 |  |
| bs-02 | terse_bug | 1/1 | 1/1 | 2/2 | 8/4 | 1004 |  |
| bs-03 | followup_early_req | 2/2 | 1/1 | 2/2 | 9/5 | 1465 |  |
| bs-04 | followup_early_req | 2/2 | 1/1 | 2/2 | 6/1 | 1084 |  |
| bs-05 | direct | 1/1 | 1/1 | 1/1 | 5/1 | 796 |  |
| bs-06 | paraphrase | 0/1 | 1/2 | 0/1 | 3/2 | 377 | d9c19cbb, src/shared/Config.lua, "each wave adds 4 more" |
| bs-07 | paraphrase | 1/1 | 1/1 | 1/1 | 4/2 | 912 |  |
| bs-08 | terse_bug | 1/1 | 2/2 | 1/1 | 7/1 | 1114 |  |
| bs-09 | terse_bug | 1/1 | 1/1 | 1/1 | 4/1 | 565 |  |
| bs-10 | followup_early_req | 1/1 | 1/1 | 1/1 | 9/5 | 1140 |  |
| bs-11 | followup_early_req | 1/1 | 1/1 | 1/1 | 6/3 | 977 |  |
| bs-12 | untouched_file | - | 1/1 | - | 3/2 | 561 |  |
| bs-13 | untouched_file | - | 1/1 | - | 5/4 | 649 |  |
| bs-14 | trivial (empty expected) | - | - | - | 0/0 | 0 |  |
| bs-15 | trivial (empty expected) | - | - | - | 0/0 | 0 |  |
| bs-16 | trivial (empty expected) | - | - | - | 0/0 | 0 |  |
| bs-17 | non_english | 0/1 | 0/1 | 0/1 | 0/0 | 0 | d9c19cbb, src/shared/Config.lua, "each wave adds 4 more" |
| bs-18 | non_english | 0/1 | 0/1 | 0/1 | 0/0 | 0 | dbf18bb2, src/shared/Config.lua, "Rerolling the offers costs 10 coins" |
| bs-19 | non_english | 0/1 | 0/1 | 0/1 | 0/0 | 0 | 3e2bb0e2, src/shared/Config.lua, "Common = gray, Rare = blue, Epic = purple" |
| bs-20 | non_english | 0/1 | 0/1 | 0/1 | 0/0 | 0 | dbf18bb2, src/shared/Config.lua, "Rerolling the offers costs 10 coins" |
| bs-21 | non_english | 0/1 | 0/1 | 0/1 | 0/0 | 0 | d9c19cbb, src/shared/Config.lua, "20 second break between waves" |
| bs-22 | direct | 1/1 | 1/1 | 1/1 | 8/4 | 1087 |  |
| bs-23 | direct | 1/1 | 1/1 | - | 5/2 | 676 |  |
| bs-24 | trivial (empty expected) | - | - | - | 0/0 | 0 |  |
| cr-A1 | direct | - | 2/3 | - | 8/4 | 1252 | src/client/controllers/InputController.luau |
| cr-A2 | direct | - | 1/1 | - | 8/5 | 1248 |  |
| cr-B1 | followup_early_req | 1/1 | 1/3 | 5/5 | 2/0 | 470 | src/server/services/CurrencyService.luau, src/shared/Config.luau |
| cr-B2 | followup_early_req | 1/1 | 1/2 | 1/1 | 5/2 | 925 | src/shared/ItemCatalog.luau |
| cr-C1 | stale_memory | 1/1 | 2/2 | - | 3/0 | 419 |  |
| cr-C2 | stale_memory | 1/1 | 1/1 | - | 5/3 | 656 |  |
| cr-D1 | untouched_file | - | 1/1 | - | 1/0 | 109 |  |
| cr-D2 | untouched_file | - | 1/1 | - | 1/0 | 62 |  |
| cr-11 | terse_bug | 1/1 | 1/1 | 1/1 | 8/5 | 1141 |  |
| cr-12 | followup_early_req | 1/1 | 1/1 | 1/1 | 3/0 | 386 |  |
| cr-13 | paraphrase | 1/1 | 0/2 | 1/1 | 2/1 | 355 | src/server/services/DataService.luau, src/shared/Config.luau |
| cr-14 | followup_early_req | 1/1 | 1/1 | 2/2 | 4/2 | 606 |  |
| cr-15 | non_english | 0/1 | 0/1 | 0/1 | 0/0 | 0 | 17c7cb15, src/shared/Config.luau, "daily_reward" |
| cr-16 | non_english | 0/1 | 0/1 | - | 0/0 | 0 | f4fde737, src/client/controllers/ShopController.luau |
| cr-17 | followup_early_req | 1/1 | 1/1 | 1/1 | 2/0 | 340 |  |
| cr-18 | trivial (empty expected) | - | - | - | 0/0 | 0 |  |
| cr-19 | trivial (empty expected) | - | - | - | 0/0 | 0 |  |
| cr-20 | terse_bug | 1/1 | 1/2 | 1/1 | 2/0 | 370 | src/server/services/ShopService.luau |
| tb-01 | followup_early_req | 1/1 | 2/2 | 3/3 | 7/2 | 862 |  |
| tb-02 | direct | 1/1 | 1/1 | 2/2 | 3/1 | 573 |  |
| tb-03 | terse_bug | 1/1 | 1/2 | 1/1 | 3/1 | 565 | server.js |
| tb-04 | followup_early_req | 1/1 | 0/2 | 1/1 | 4/2 | 742 | public/app.js, server.js |
| tb-05 | followup_early_req | 1/1 | 1/1 | 1/1 | 3/0 | 367 |  |
| tb-06 | followup_early_req | 1/1 | 1/1 | 2/2 | 2/0 | 327 |  |
| tb-07 | followup_early_req | 1/1 | 1/1 | 1/1 | 5/2 | 368 |  |
| tb-08 | paraphrase | 0/1 | 0/1 | 0/1 | 0/0 | 0 | c2fabc15, server.js, "at most 3 cards" |
| tb-09 | paraphrase | 0/1 | 0/1 | - | 0/0 | 0 | fdb761dd, public/app.js |
| tb-10 | terse_bug | 1/1 | 1/1 | 1/1 | 6/3 | 740 |  |
| tb-11 | non_english | 0/1 | 0/2 | 0/3 | 0/0 | 0 | 4644d1f4, public/app.js, public/style.css, "#e5484d", "#3e63dd", "#8b8d98" |
| tb-12 | non_english | 0/1 | 0/1 | 0/1 | 0/0 | 0 | c2fabc15, server.js, "at most 3 cards" |
| tb-13 | non_english | 0/1 | 0/1 | 0/1 | 0/0 | 0 | 7edb923c, public/style.css, "Overdue cards get a red border" |
| tb-14 | direct | - | 1/1 | - | 3/1 | 391 |  |
| tb-15 | trivial (empty expected) | - | - | - | 0/0 | 0 |  |
| tb-16 | trivial (empty expected) | - | - | - | 0/0 | 0 |  |
| tb-17 | followup_early_req | 1/1 | 2/2 | 1/1 | 8/3 | 1210 |  |
| tb-18 | terse_bug | 1/1 | 1/1 | 1/1 | 4/1 | 686 |  |

Pack latency (varies run to run): mean 12.2 ms, p50 6.8 ms, p95 16.4 ms, max 118.4 ms.
