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
stable-queue, deriving the state of each request), `stable-backlog sync`,
`list`, `show` and `export` (JSON and the web page) exist and are tested,
and GitHub Actions publishes the web page once a day. There is no release
yet.

## Why

When a commit tagged for stable does not apply to a stable tree, the stable
maintainers mail a FAILED report asking for a backport. Many are picked up,
some are not, and there is no list of the ones still open. Matthew Wilcox
[asked for a dashboard](https://lwn.net/Articles/958860/) of exactly this in
2024.

## How it works

- It reads the public-inbox git mirror of the stable list
  (`https://lore.kernel.org/stable/git/<N>.git`) and stable-queue from
  git.kernel.org. It never scrapes web pages. After the first clone it fetches
  only new data, and at most once an hour; the web page is updated once a
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

## Web page

<https://houtinghang.github.io/stable-backlog/> lists the requests with no
backport found, for all trees and for each tree, with links to the FAILED
reports and replies on lore and to the upstream commits on git.kernel.org.
[`stable-backlog.json`](https://houtinghang.github.io/stable-backlog/stable-backlog.json)
next to it has every request in every status, in the format of
`export --json`.

A GitHub Actions workflow (`.github/workflows/page.yml`) rebuilds both once a
day. It keeps its mirrors in the Actions cache, so only new data is fetched.
If anything fails, for example a source cannot be reached or looks stale,
nothing is published and the page keeps its old date: the page says when it
was built. Its results have not been checked by hand yet.

## Usage

There is no release yet. From a checkout, on Linux with Python 3.10 or newer
and `git`:

```bash
python3 -m venv ~/.venvs/stable-backlog
~/.venvs/stable-backlog/bin/pip install .
~/.venvs/stable-backlog/bin/stable-backlog sync
```

It also runs from the checkout without installing anything, since it needs
nothing but the standard library:

```bash
PYTHONPATH=src python3 -m stable_backlog sync
```

### sync

`sync` keeps bare mirrors of the lore stable archive and of stable-queue in
`~/.cache/stable-backlog` (or `$XDG_CACHE_HOME/stable-backlog`) and rebuilds a
local snapshot from them. The first run clones the last 90 days of the list
(about 240 MB) and the current stable-queue (about 310 MB); later runs fetch
only what is new. Rebuilding the snapshot takes about a minute, most of it
parsing the lore messages.

To keep the load on lore and git.kernel.org low, `sync` uses the network at
most once an hour: within an hour of the last successful run it stops with an
error before contacting anyone. `--offline` rebuilds the snapshot from the
local mirrors at any time.

`sync` fails, and keeps the previous snapshot, when anything goes wrong, and
also when a source looks stale: no new lore message for 2 days, or no
stable-queue commit for 7 days. It prints a summary on stderr, including the
messages it skipped and why.

| Option | |
|---|---|
| `--cache-dir DIR` | keep the mirrors and the snapshot in `DIR` |
| `--offline` | rebuild the snapshot from the local mirrors without fetching |
| `--reclone lore` / `--reclone queue` | clone a source again, for example after its history was rewritten |

### list

`stable-backlog list` reads the snapshot (no network) and prints one row per
upstream commit that has a FAILED report with no backport found, the
longest-waiting first:

```
AGE  UPSTREAM      TREES      REPLIES  TITLE
80d  8bc4d7209611  6.1        -        btrfs: zoned: fix missing chunk metadata reservation
80d  40de8160ca7f  6.6        1        fs/proc/task_mmu: fix PAGEMAP_SCAN written state for PMD holes
80d  cdc36db204ff  5.10 5.15  -        Bluetooth: hci_sync: Fix advertising data UAFs
```

AGE counts the days from the first FAILED report to the newest message in the
snapshot. REPLIES counts the other messages in the FAILED threads: a reply
such as "not needed for this tree" is not a backport, so the request stays
listed; read it with `show`. A summary goes to stderr: how many requests and
commits matched, how current the data is (with a warning when it is old), and
the messages that could not be read when the snapshot was built.

| Option | |
|---|---|
| `--tree X.Y` | only this tree (repeatable) |
| `--status S,...` / `--all` | `failed` (the default: no backport found), `backport-posted`, `queued`, `released` / every status |
| `--since 30d` / `--since 2026-10-01` | first reported in the last 30 days / on or after that date (UTC) |
| `--author TEXT` | the commit's author name or address contains `TEXT` (any case) |
| `--path PATH` | the commit changes this file, or a file under this directory |
| `--cache-dir DIR` | read the snapshot in `DIR` |

A request that a filter cannot check, for example one whose author was not
found, is left out, and the number of such requests is printed on stderr.

### show

`stable-backlog show <commit>` takes 12 to 40 hex digits of the upstream
commit, or the Message-ID of one of its FAILED reports, and prints where it
stands in each tree, with lore links:

```
92465f46f3c3a92e4758af20d2363b7adc43e1f5 net: expect instance lock in netdev_queue_get_dma_dev()
Author: Jakub Kicinski <kuba@kernel.org>
Files:  net/core/netdev_queues.c

6.18  no backport found
      FAILED    2026-08-17  https://lore.kernel.org/stable/2026081708-grout-zipping-a932@gregkh/
7.1   released in 7.1.10
      FAILED    2026-08-17  https://lore.kernel.org/stable/2026081708-gracious-yard-1b78@gregkh/
      backport  2026-08-19  sashal@kernel.org  https://lore.kernel.org/stable/20260819230925.944372-2-sashal@kernel.org/
      reply     https://lore.kernel.org/stable/20260819230925.944372-1-sashal@kernel.org/
      release   releases/7.1.10/net-expect-instance-lock-in-netdev_queue_get_dma_dev.patch
```

A backport matched only by its title is marked "matched by title only". If
no FAILED report is found, `show` exits with status 1 and says which dates the
snapshot covers: a commit without a FAILED report there may still need a
backport.

### export

`stable-backlog export --json [-o FILE]` writes every request, in every
status, as JSON for scripts: where the data came
from (both mirror commits and their dates), how many messages were read and
skipped, counts per status, and for each (commit, tree) its state, author,
changed files, FAILED reports, backports, replies and stable-queue files,
with lore links. Text from messages is kept as it was mailed, escaped as
`\uXXXX`, so the output is printable ASCII only. With `-o`, the file is
replaced in one step, so a reader never sees half of it. The format is
described in [docs/DESIGN.md](docs/DESIGN.md).

`stable-backlog export --html [-o FILE]` writes the web page: one static
HTML file, without JavaScript, that lists the requests with no backport
found for all trees and for each tree, with links to the FAILED reports and
replies on lore and to the upstream commits on git.kernel.org. Open it
directly in a browser; no web server is needed. It links to
`stable-backlog.json` next to it, so write the JSON there too if you want
that link to work.

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
~/.venvs/stable-backlog/bin/python -m pytest -m slow  # real-scale performance tests
~/.venvs/stable-backlog/bin/ruff check .
~/.venvs/stable-backlog/bin/ruff format --check .
~/.venvs/stable-backlog/bin/mypy
```

The development tools are pinned with hashes in `requirements-dev.txt`
(generated from `requirements-dev.in`; see the comment there). Tests never use
the network. GitHub Actions runs the same checks on Python 3.10 and 3.14 for
every push and pull request (`.github/workflows/ci.yml`, with a read-only
token); the rules the workflows must follow are tested in
`tests/test_workflows.py`. The golden tests use real stable list messages and stable-queue
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
