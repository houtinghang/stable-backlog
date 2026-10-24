# stable-backlog

**This is not an official kernel.org or stable team tool.** It is an
independent project that reads public data: the
[lore stable archive](https://lore.kernel.org/stable/) and
[stable-queue](https://git.kernel.org/pub/scm/linux/kernel/git/stable/stable-queue.git).

stable-backlog lists the Linux stable "FAILED: patch ... failed to apply to
X.Y-stable tree" requests for which no backport has been found yet, and shows
where each request stands:

```
FAILED --> BACKPORT_POSTED --> QUEUED --> RELEASED
```

**Status: early development.** The data layer (parsing the archive and
stable-queue, deriving the state of each request) exists and is tested; there
is no command-line interface, web page or release yet.

## Why

When a commit tagged for stable does not apply to a stable tree, the stable
maintainers mail a FAILED report asking for a backport. Many are picked up,
some are not, and there is no list of the ones still open. Matthew Wilcox
[asked for a dashboard](https://lwn.net/Articles/958860/) of exactly this in
2024.

## How it works

- It reads the public-inbox git mirror of the stable list
  (`https://lore.kernel.org/stable/git/<N>.git`) and stable-queue from
  git.kernel.org. It never scrapes web pages, and it fetches at most once a
  day.
- Requests and answers are matched by **upstream commit hash**: the FAILED
  report's `git cherry-pick -x <hash>` line against a backport's
  `[ Upstream commit <hash> ]`, `commit <hash> upstream.` or
  `(cherry picked from commit <hash>)` line, for the same stable tree.
  Matching by title is only a fallback and is marked as low confidence.
- stable-queue decides QUEUED (`queue-X.Y`, `staging-X.Y`) and RELEASED
  (`releases/X.Y.Z`); fixes the stable maintainers picked up without any mail
  are found this way too.
- An open request is shown as **"no backport found"** with a link to its lore
  thread, never as "nobody is working on it": the tool can only report what is
  in the archive.

Design details: [docs/DESIGN.md](docs/DESIGN.md).

## Security

Anyone can mail the stable list, so every message is treated as written by an
attacker. The tool never executes or applies message content, runs only fixed
`git` commands without a shell, talks only to the two fixed HTTPS sources
above, opens no network port, and has no third-party runtime dependencies.
See "Security model" in [docs/DESIGN.md](docs/DESIGN.md).

Please report security problems privately through GitHub's
"Report a vulnerability" (private security advisory), not in a public issue.

## Development

Linux (or WSL) with Python 3.10 or newer and `git`.

```bash
python3 -m venv ~/.venvs/stable-backlog
~/.venvs/stable-backlog/bin/pip install --require-hashes -r requirements-dev.txt
~/.venvs/stable-backlog/bin/python -m pytest
~/.venvs/stable-backlog/bin/ruff check .
~/.venvs/stable-backlog/bin/ruff format --check .
~/.venvs/stable-backlog/bin/mypy
```

The development tools are pinned with hashes in `requirements-dev.txt`
(generated from `requirements-dev.in`; see the comment there). Tests never use
the network. The golden tests use real stable list messages and stable-queue
files, trimmed to what the parser needs; where each one came from is listed in
`tests/fixtures/golden/MANIFEST.tsv`, and
`scripts/make_golden_fixtures.py` regenerates them from local mirrors.

## AI assistance

This project is developed with the help of
[Claude Code](https://www.anthropic.com/claude-code), Anthropic's AI coding
assistant. So far, most of the code, tests and documentation were drafted by
Claude Code. The maintainer, Ting-Han Hou, decided the scope and the key
choices (name, license, data sources, security rules), reviews every change
before it is committed, and is responsible for it.

How it is tested: golden tests built from real stable list messages whose
outcomes were checked by hand against lore and stable-queue, tests with
malicious input for each security rule, and runs on Python 3.10 and the
latest Python. Before the first release, the results will be checked by hand
against lore on a random sample, and the measured error rate will be
published here.

## License

GPL-2.0-or-later. See [LICENSE](LICENSE).
