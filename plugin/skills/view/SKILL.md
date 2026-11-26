---
description: Open devmem's local viewer for this project (sessions turn by turn, decisions, map).
disable-model-invocation: true
allowed-tools: Bash(sh '${CLAUDE_PLUGIN_ROOT}/bin/devmem' app), PowerShell(& '${CLAUDE_PLUGIN_ROOT}\bin\devmem.cmd' --% app)
---

Start the viewer from the project folder, in the background (Bash tool, run in background: it serves until its
window is closed), copied as written:

```
sh '${CLAUDE_PLUGIN_ROOT}/bin/devmem' app
```

Where `sh` is missing (Windows without Git Bash), use the PowerShell tool:

```
& '${CLAUDE_PLUGIN_ROOT}\bin\devmem.cmd' --% app
```

Tell the user the window opened; if it printed an address instead (no Chrome or Edge found), the viewer opened in
their default browser. It listens on 127.0.0.1 only. A printed address carries a launch code that works once, within
a minute, so it cannot be used to open the viewer again; run the command again for a new window.
