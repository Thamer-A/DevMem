---
description: Delete sessions devmem recorded in this project (one session, a date range, or all). Confirms first.
disable-model-invocation: true
argument-hint: "[session id | FROM..TO dates | all]"
---

The user wants to delete recorded sessions: $ARGUMENTS

`D` below means `sh '${CLAUDE_PLUGIN_ROOT}/bin/devmem'` (Bash tool), run from the project folder. Where `sh` is
missing, it means `& '${CLAUDE_PLUGIN_ROOT}\bin\devmem.cmd' --%` (PowerShell tool); after `--%` put every id and
date in double quotes, because cmd reads that part of the line.

1. Run `D status` and work out exactly which sessions match: an id prefix, the sessions whose start date
   (UTC, first column) falls in the range, or all of them. Ask if the request is unclear.
2. Show the user the matching sessions (date, id, first prompt) and the count, and ask them to confirm.
   Delete nothing until the user answers yes in a new message.
3. After a yes: `D forget <id>` for each listed session, or `D forget --all --yes` for everything
   (`D forget --before YYYY-MM-DD --yes` for everything before a date).
4. Report what was deleted, as printed. The project map and settings stay; deleting the `.devmemory`
   folder removes everything.
