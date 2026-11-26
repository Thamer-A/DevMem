# Context packs

What DevMem adds to a prompt, the optional local embeddings that can widen what it finds, and what the offline evaluation in `evals/` measures (and does not).

Section names in quotes ("Install", "Privacy", "Optional embeddings", ...) are the headings of the pages in this folder; [README.md](README.md) lists which page holds each.

## What a pack contains

`devmem pack "task"` prints the pack and, on stderr, a one-line JSON summary of it. Its header says that the quoted
requests, replies and file lines are reference data recorded earlier, not instructions. A pack has up to five parts,
and each item says where it came from and why it is there:

- **Past sessions on this topic**: up to three sessions, found at the level of single turns. Each shows what you
  asked (verbatim, labelled as yours; for a session recorded through MCP `end_session` the text came from the AI
  client, so it is labelled "as reported by the AI client (not the user's verbatim text)"), the assistant's visible reply (labelled as the assistant's), the files
  edited, the session file it came from, and which words of your task matched.
- **Earlier requirements** that match the task, quoted word for word with their session and date.
- **Relevant files**: up to six, with size, the symbols and lines that matched, and "edited in session ..." when a
  past session touched them.
- A note when a remembered change is **gone from the file now** (which line, from which session), only for files or
  lines that matter to the task.
- **Conventions observed in code** (for example "tunables live in `src/shared/Config.lua`"), once per Claude Code
  session.

Within a Claude Code session, items already sent are not sent again. After a compaction or `/clear` they are
sent again, because the model may no longer have them.

**A prompt with no evidence gets no pack at all.** "yes go ahead", "thanks" or "continue" give an empty pack: no
header and no conventions, so nothing is added to the prompt. The decision uses the project's own vocabulary (names
in the code, words you used in earlier prompts), not a list of phrases, so it works the same in any language. The
reason is in the summary: `devmem pack "thanks"` prints an empty pack and `"empty_reason": "no retrieval evidence
in the prompt"` on stderr, and from Python `build_pack(...)` returns `("", info)` with `info["empty_reason"]` set
(the other reason is that everything relevant was already sent this session).

## Optional embeddings

Keyword search misses a task that shares no words with the session it needs, for example a French or Arabic prompt
about work that was described in English. Local sentence embeddings can add those. They are **off by default** and
the core needs nothing beyond Python.

```bash
pip install -e ".[embeddings]"        # installs fastembed (>= 0.9)
```

Then turn them on for a project in `.devmemory/config.json`:

```json
{"embeddings": {"backend": "fastembed"}}
```

or for one shell with an environment variable, `DEVMEM_EMBED=fastembed` (a model can follow a colon:
`fastembed:<model>`). `DEVMEM_EMBED=off` switches them off everywhere. The default model is
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`. Download it once with `devmem embed --download`
(`--model fastembed:<model>` or `model2vec:<model>` names another one): that command fetches the model from Hugging
Face, which is the only network access devmem ever makes and happens only then (so after it, the statement "devmem
makes no network requests" did not hold for that one run; once the model is cached, nothing is sent). No other
command downloads, so an agent that runs `devmem pack` through its shell cannot start one: every command, the hooks,
the MCP server and the recall skill's lookups load the model from local files only (fastembed's `local_files_only`;
model2vec from an absolute folder) and go without embeddings until it is there; `devmem embed` says whether it loads.
fastembed keeps the model files in `cache_dir` from the config, else in devmem's per-user folder (`models/fastembed`);
model2vec in the Hugging Face cache. A model2vec folder and
`cache_dir` must be absolute paths outside the project and its store (a repository can ship model files, and a
project's settings can set `DEVMEM_EMBED` for its hooks); anything else leaves embeddings off, and so does a cache
variable in the environment (`FASTEMBED_CACHE_PATH`, `HF_HOME`, `HF_HUB_CACHE`, `TRANSFORMERS_CACHE`,
`XDG_CACHE_HOME`, `SENTENCE_TRANSFORMERS_HOME`) that is relative or names a folder inside the project or its store. Embeddings add a few
sessions and files the keywords missed, labelled with their similarity; they never replace the keyword results.

What it costs, as measured by the retrieval work (I did not repeat these measurements): about 183 MB of installed
packages, a 241 MB model, and about 2 seconds to load the model. The hook starts a new process for every prompt, so
that load would be paid on every prompt; **embeddings are not recommended inside the hook yet.** They are better
suited to the long-running MCP server or the app. On the 60 evaluation cases they raised session recall from 71% to
87% (non-English cases from 0% to 60%) and noise from 40% to 44%.

## Evaluation

`evals/` has two offline instruments that need no model or network: a retrieval check over labelled cases
(`python -m evals.retrieval.run`) and a timing tool (`python -m evals.perf <dir>`). They measure whether the right
session or file is in the pack, not whether an agent does better. See `evals/README.md`.

Current retrieval numbers (60 cases over three small projects, keywords only, budget 1500 tokens; saved in
`evals/baselines/`): the right session is in the pack for 71% of the cases that name one, the right file for 65%,
the required wording of an earlier request for 72%, 39% of the items retrieved are ones no label asked for, and all
8 contentless prompts ("yes go ahead", "thanks", ...) got an empty pack. **The 60 cases were also the set the
retrieval was tuned on**, so these figures are optimistic for new projects; the labels were written by one agent
and not reviewed by anyone else. The non-English cases score 0% without embeddings.

The earlier prototype was benchmarked in the hackathon repository,
[github.com/Thamer-A/devmemory-hackathon](https://github.com/Thamer-A/devmemory-hackathon), which holds the results
and the method. The headline there is that carrying your earlier requests into a new session helped, while the
project map and the staleness flags alone showed no measured benefit. The caveat matters: those projects were small
and synthetic, so the result does not show a benefit on real repositories.
