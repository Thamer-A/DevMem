"""DevMemory (working name): local-first project memory and context packs for AI coding sessions."""
import os as _os
import sys as _sys

__version__ = "0.4.0"

# `python -m devmem ...` (an MCP server entry written by `devmem connect --command python`, `devmem codex`) puts the
# current folder first on sys.path, and a client starts the server in the project: a `json.py` the project ships would
# then be imported in place of the standard library's. Drop that entry while the package is being located (sys.argv[0]
# is "-m" only then); devmem's own modules load from its package folder whatever sys.path holds.
if _sys.argv[:1] == ["-m"] and _sys.path and _sys.path[0] in ("", _os.getcwd()):
    del _sys.path[0]
