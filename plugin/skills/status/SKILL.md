---
description: Show what devmem has recorded in this project and check its setup.
disable-model-invocation: true
allowed-tools: Bash(sh '${CLAUDE_PLUGIN_ROOT}/bin/devmem' status --plugin-data '${CLAUDE_PLUGIN_DATA}'), Bash(sh '${CLAUDE_PLUGIN_ROOT}/bin/devmem' doctor --plugin-data '${CLAUDE_PLUGIN_DATA}'), PowerShell(& '${CLAUDE_PLUGIN_ROOT}\bin\devmem.cmd' --% status --plugin-data "${CLAUDE_PLUGIN_DATA}"), PowerShell(& '${CLAUDE_PLUGIN_ROOT}\bin\devmem.cmd' --% doctor --plugin-data "${CLAUDE_PLUGIN_DATA}")
---

Run both commands from the project folder, copied as written (Bash tool):

```
sh '${CLAUDE_PLUGIN_ROOT}/bin/devmem' status --plugin-data '${CLAUDE_PLUGIN_DATA}'
sh '${CLAUDE_PLUGIN_ROOT}/bin/devmem' doctor --plugin-data '${CLAUDE_PLUGIN_DATA}'
```

Where `sh` is missing (Windows without Git Bash), use the PowerShell tool:

```
& '${CLAUDE_PLUGIN_ROOT}\bin\devmem.cmd' --% status --plugin-data "${CLAUDE_PLUGIN_DATA}"
& '${CLAUDE_PLUGIN_ROOT}\bin\devmem.cmd' --% doctor --plugin-data "${CLAUDE_PLUGIN_DATA}"
```

Summarise briefly: how many sessions are recorded (the latest few), which route records them, whether the
project is set up for the plugin, and every problem or warning doctor lists with its suggested fix. If there is
no memory store, or the project is not set up, suggest /devmem:setup.
