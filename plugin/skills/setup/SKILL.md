---
description: Switch devmem memory on for this project (creates .devmemory, scans, notes the project in the plugin's data folder; writes no hooks). "/devmem:setup off" switches recording off and keeps the store.
disable-model-invocation: true
argument-hint: "[off]"
allowed-tools: Bash(sh '${CLAUDE_PLUGIN_ROOT}/bin/devmem' setup --plugin-data '${CLAUDE_PLUGIN_DATA}'), Bash(sh '${CLAUDE_PLUGIN_ROOT}/bin/devmem' setup --plugin-data '${CLAUDE_PLUGIN_DATA}' --off), PowerShell(& '${CLAUDE_PLUGIN_ROOT}\bin\devmem.cmd' --% setup --plugin-data "${CLAUDE_PLUGIN_DATA}"), PowerShell(& '${CLAUDE_PLUGIN_ROOT}\bin\devmem.cmd' --% setup --plugin-data "${CLAUDE_PLUGIN_DATA}" --off)
---

Arguments: $ARGUMENTS

Run exactly one command, from the project folder, copied as written (Bash tool):

```
sh '${CLAUDE_PLUGIN_ROOT}/bin/devmem' setup --plugin-data '${CLAUDE_PLUGIN_DATA}'
```

If the arguments say `off`, add ` --off` at the end instead (recording stops, the store is kept). Where `sh` is
missing (Windows without Git Bash), use the PowerShell tool:

```
& '${CLAUDE_PLUGIN_ROOT}\bin\devmem.cmd' --% setup --plugin-data "${CLAUDE_PLUGIN_DATA}"
```

Report the result in two or three lines. If it says the project already records through `devmem init` hooks,
pass that on and change nothing. If it says the repository ships a memory store, show the user that message and
stop: add `--adopt-store` only if the user then explicitly asks for it. Never edit a settings file for this.
