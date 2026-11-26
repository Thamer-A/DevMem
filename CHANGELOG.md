# Changelog

## 0.4.0 — unreleased

The first public version. What the tool does at this version:

- **Capture.** Claude Code hooks (eight events, from `SessionStart` to `SessionEnd`) record prompts, visible replies,
  tool calls and edits into the project's `.devmemory/` folder, through the Claude Code plugin (`/devmem:setup`) or
  standalone hooks (`devmem init`). Nothing records until a project is switched on, and the switch lives outside the
  project. Other tools: `devmem run`/`import` (stream-json), `wrap`, `codex`, `begin`/`end` snapshots, and the
  `devmemory` MCP server; `devmem connect <tool>` sets up Cursor, VS Code, Codex, Gemini, Devin and others (written
  from each tool's documentation; not yet tried with those tools).
- **Redaction.** Recorded text is redacted with built-in patterns before it is written to the store (a store's
  config can turn this off only with the user's `--allow-unredacted`), and the content of files with secret-like
  names (`.env`, `*.pem`, `id_rsa*`, ...) is dropped. Redaction is pattern-based: a secret no rule recognises is
  stored as written. See SECURITY.md.
- **Packs and retrieval.** `devmem pack` and the prompt hook build a context pack from a local SQLite keyword index:
  earlier sessions found turn by turn, earlier requirements quoted verbatim, relevant files and lines, and a note when
  a remembered change is gone. A prompt with no evidence gets no pack. Optional local embeddings (`[embeddings]`
  extra, fastembed) are off by default.
- **App.** `devmem ui` / `devmem app`: a local viewer on 127.0.0.1 for the project map, sessions turn by turn,
  decisions and trial packs, guarded by a per-launch key and a one-time launch code.
- **Installer.** A per-user Windows installer with its own embeddable Python 3.13, no administrator rights; not
  code-signed yet.
- **Commands.** `status`, `doctor`, `sessions`, `show`, `forget`, `adopt`, `scan`, `setup`, `uninstall`, `embed`.
