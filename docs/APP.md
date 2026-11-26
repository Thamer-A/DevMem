# The app

The local viewer (`devmem ui`, `devmem app`): what it shows, what it writes, and how it keeps other programs and web pages out.

Section names in quotes ("Install", "Privacy", "Optional embeddings", ...) are the headings of the pages in this folder; [README.md](README.md) lists which page holds each.

## The app

`devmem ui` serves a local viewer on 127.0.0.1 and `devmem app` opens it in its own browser window. It never
modifies your project files, but it is not read-only: it can scan and rescan, which writes the map and the index in
the store (and a list of recently opened projects in your user profile). It shows the
project map, each captured session turn by turn (the prompt, then the tool calls and edits under it; sessions still
being captured are marked "in progress"), decisions taken from your prompts, a graph, and a box to try the context
pack on a task. Captured text is untrusted and is escaped before it is shown.

How the viewer keeps other programs and web pages out: it listens on 127.0.0.1 only, accepts only its own Host and
Origin, and every request for data needs a key that is created each time it starts. That key is never put in a web
address. The address `devmem app` and `devmem ui` open carries a one-time launch code instead; the page trades it for
the key as it loads, and from then on the code is worthless. A launch code works once and only within 60 seconds, so
an address left in the browser history, in a terminal or in the list of running programs (which other users of the
same computer can read on Linux and macOS) does not open the viewer again. Reloading the page is fine: the tab keeps
its key. With `devmem ui --no-browser`, open the printed address within a minute; press Enter in that terminal to
print a new one. If a launch code is used a second time, the viewer refuses it and prints a warning in its terminal.

Each `devmem app` window runs on its own browser profile (`DevMemory\browser`, `browser-2`, ... under
`%LOCALAPPDATA%`, `~/Library/Application Support` or `~/.cache`), so a second window does not hand itself to the
first one's browser. If a browser still hands its window over and exits at once, the server keeps running until the
page has made no request for 30 minutes. "Open project folder" uses tkinter's dialog, or on Windows the shell's own
folder dialog when tkinter is missing (as in the Windows installer's Python).
