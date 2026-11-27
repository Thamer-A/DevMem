# Gate task format

What `evals/gate/tasks/` holds, so that the task writer, the graders' validation, the freeze and the runner agree.
`PREREGISTRATION.md` says why; this file says where things are.

```
evals/gate/
  tasks/<repo>/                       scored material for one repository (<repo> = md2conf or bonsai-js)
    repo.json
    history/
      history.json                    the scripted sessions, in order
      NN.patch                        reference patch of session NN (git diff, applied in order)
    <task id>/                        one folder per scored task (H01..H08 history-dependent, R01..R04 ordinary)
      task.json
      grade.py
      reference.patch                 a correct solution, on top of the starting code
      guess.patch                     a plausible solution that the grader must reject
      hidden/                         optional: test files copied in only at grading time
  tuning/<repo>/<task id>/            the 3 tuning tasks per repository, same layout (T01..T03); never scored
  MANIFEST.sha256                     every file under tasks/ and tuning/, with its SHA-256 (verify_freeze)
```

**repo.json**: `{"name", "url", "commit" (full SHA), "language", "license", "deps", "test": [argv],
"test_timeout_s"}`. Dependencies are installed once per repository, outside every working copy
(`evals/gate/workspace.py`):
- `"deps": {"kind": "python", "requirements": ["pkg==1.2.3", ...]}`: a virtual environment with exactly these
  (pinned) packages; the repository's own package is not installed, it is imported from the working copy.
- `"deps": {"kind": "node"}`: `npm ci` from the repository's own `package.json` and lock file; each working copy
  gets `node_modules` as a link (a junction on Windows) to that install.

`test` runs the repository's own suite offline from the working copy, with the tool environment on `PATH` (for
example `["python", "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"]` or `["npx", "vitest", "run"]`).

**history.json**: `{"sessions": [{"n": 1, "prompt": "...", "patch": "01.patch", "requirements": [{"id": "R1",
"text": "..."}]}]}`. `prompt` is the user's whole message for that session, exactly as it will be sent. Each
requirement's `text` is a sentence copied verbatim from that prompt. Five or six sessions; eight to ten
requirements in all, each with an exact value (a number, a name, a format, an order).

**The starting code** of every task and arm: the repository at `commit`, then every `history/NN.patch` in order.

**task.json**: `{"id", "repo", "kind": "history" | "ordinary" | "tuning", "prompt", "requirement": "R3" or null,
"oracle": "<the requirement's text, verbatim>" or null, "max_turns", "timeout_s"}`. `oracle` is set for history
and tuning tasks that depend on a requirement, and null for ordinary ones.

**grade.py**: defines `grade(work: Path, env: dict) -> dict` returning at least `{"passed": bool, "checks": {name:
bool}}`. `env` is the process environment with the repository's tools first on `PATH` (pass it to `subprocess.run`).
It may run the repository's tests (with files from `hidden/` copied in first and removed after; the folder of
grade.py is `Path(__file__).parent`) and read files. Standard library only. It must not use the network, must finish
within the task's `timeout_s`, and must not modify anything outside `work`. A grader that raises counts as a failed
run.

**Validation** (`python -m evals.gate.validate`): for every task, the starting code fails the grader, the starting
code plus `reference.patch` passes it, and the starting code plus `guess.patch` fails it. A history task's guess
follows the code and the prompt but not its requirement.
