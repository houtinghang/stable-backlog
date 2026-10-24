# stable-backlog design

This document describes how stable-backlog reads its data, how it decides the
state of each FAILED request, and the rules that keep it safe to run on data
anyone can write. It covers the data layer (milestone M1); the command line
and the web page will be added later.

## 1. Goal

For each **(upstream commit, stable tree)** pair that received a FAILED report
("FAILED: patch ... failed to apply to X.Y-stable tree"), say where it stands:

| State | Meaning | Decided by |
|---|---|---|
| `RELEASED` | a patch for it is in `releases/X.Y.Z/` | stable-queue |
| `QUEUED` | ... in `queue-X.Y/` or `staging-X.Y/` | stable-queue |
| `BACKPORT_POSTED` | someone mailed a backport for that tree | lore |
| `FAILED` | none of the above was found: "no backport found" | |

The strongest state wins. The current stable-queue snapshot decides QUEUED
and RELEASED, so a patch that is dropped from the queue falls back to
BACKPORT_POSTED by itself.

Non-goals: sending mail, replying to FAILED reports, generating backports,
user accounts, collecting data about users.

## 2. Data sources

Only these fixed HTTPS sources are used; no URL is ever taken from data or
from the user:

- `https://lore.kernel.org/stable/git/<N>.git`: the public-inbox v2 mirror of
  the stable list (epoch 2 is the current one as of 2026-10).
- `https://git.kernel.org/pub/scm/linux/kernel/git/stable/stable-queue.git`.

Facts about these sources that the code relies on (checked 2026-10-24):

- **public-inbox v2**: each commit holds one message as the blob `m`. A
  removal (for example spam) is a later commit whose tree holds the removed
  message as `d` instead; the removed Message-ID must be dropped too.
- The **author date** of a mirror commit is the message's own `Date:` header,
  which is not monotonic. The **committer date** is the import time. Message
  windows are therefore taken with `git log -n <count>` (newest first), never
  with `git log --since`.
- After `git fetch`, the local branch has to be moved explicitly
  (`update-ref`) and the date of the newest message checked; otherwise a fetch
  can silently leave old data in place.
- Neither lore nor git.kernel.org accepts partial-clone filters
  (`--filter=blob:none` is ignored with "filtering not recognized by server"),
  so clones contain the full messages. A shallow clone of three months of the
  stable list is about 240 MB.
- lore's web pages refuse scripted access; only the git mirror is used.

## 3. Reading messages

`mirror.iter_raw()` lists commits with `git log` and reads blobs through one
long-running `git cat-file --batch` process, one message at a time, so memory
use does not grow with the archive. Messages larger than 5 MB are not parsed;
they are counted as skipped.

`parse.parse_message()` classifies each message with Python's `email`
package:

| Kind | Rule |
|---|---|
| FAILED report | subject `FAILED: patch "<title>" failed to apply to X.Y-stable tree`, sent by a stable maintainer (`gregkh@linuxfoundation.org`, `sashal@kernel.org`); the body must contain exactly one `git cherry-pick -x <40-hex>` line, and the `--subject-prefix 'PATCH X.Y.y'` line, if present, must name the same tree. A valid Message-ID is required. |
| review series | `[PATCH X.Y NNN/MMM]` from a stable maintainer: one patch of an -rc review. Not a backport; stable-queue already covers it. |
| backport | subject `[PATCH ...]` whose bracket names one or more trees (`6.1`, `6.1.y`); `[PATCH 6.1.190]` is a version, not a tree. |
| other | everything else, kept for threading (replies to FAILED reports). |

A FAILED-looking subject from anyone else is classified as "other" and noted,
because anyone can mail the list. A FAILED report from a maintainer that does
not match the rules above raises `ParseError`; the caller records it with its
Message-ID and reason instead of dropping it.

The full commit title is taken from the `Subject:` of the original commit
quoted in the FAILED body, because the mail subject is cut for long titles.

### Upstream markers

A backport, or a stable-queue patch, names its upstream commit with one of
these lines (each must start the line, so quoted text does not count):

```
[ Upstream commit <hash> ]
commit <hash> upstream.
(cherry picked from commit <hash>)
```

Hashes of 12 to 40 hex digits are accepted and matched as prefixes. Hashes in
`Fixes:` tags, "This reverts commit ..." lines or prose are ignored.

## 4. Reading stable-queue

`queue.scan()` reads one stable-queue commit:

- `queue-X.Y/` and `staging-X.Y/`: only patches listed in the directory's
  `series` file count (that is what quilt applies). `staging-X.Y` holds
  patches that are later merged into `queue-X.Y`.
- `releases/X.Y.Z/`: every patch counts; `X.Y.Z` is reported as the release.
- Directory and file names must match fixed patterns; anything else is
  ignored. Patches without a marker are counted (`without_marker`).

## 5. Matching

For each (upstream, tree) with at least one FAILED report:

1. **Backports**: mailed backports for the same tree whose markers name the
   upstream commit (confidence `hash`). If a backport names no commit at all
   and its title equals the FAILED title, it is a low-confidence match
   (`title`). A backport that names a different commit never matches by
   title.
2. **Queue / releases**: stable-queue entries for the same tree and commit.
3. **Replies**: other messages whose `In-Reply-To` or `References` point at
   the FAILED report, so a reply such as "not needed for this tree" is shown
   instead of "no reply".

Duplicate FAILED reports for the same pair are merged into one item.

## 6. Failures are never silent

The worst failure for this tool is reporting a request as open when someone
already answered it, so:

- Parse failures are returned as `Skipped(commit, message_id, reason)` and
  stored; they will be shown in the CLI, the JSON and the web page footer.
- A missing object, an unexpected mirror layout or a failing `git` command
  raises an error with the command, repository and stderr. Nothing is turned
  into an empty result.
- Every saved snapshot records the mirror and stable-queue commits it was
  built from.

## 7. Security model

Anyone can mail the stable list and anyone will be able to open issues, so
every message, header and patch file is treated as written by an attacker.

| Rule | Where | Tests |
|---|---|---|
| Never execute or apply message content (no `git am`/`apply`, no `eval`/`exec`/`pickle`, no shell) | whole package | `test_security.py` shell payloads |
| Run only fixed `git` subcommands, with argument lists and `shell=False`; validate commit ids (`^[0-9a-f]{12,40}$`); never pass Message-IDs or subjects to git | `gitio.py` | `test_security.py`, `test_queue.py` |
| Only `https` (`-c protocol.allow=never -c protocol.https.allow=always`); no lazy fetch while reading (`GIT_NO_LAZY_FETCH=1`) | `gitio.py` | |
| No network listener of any kind | whole package | |
| File names never come from message content | whole package | `test_security.py` path-like Message-IDs |
| Size and length limits before any regex (5 MB per message, 1000 characters per subject); no regex with nested unbounded repetition | `parse.py` | `test_security.py` huge lines |
| SQLite only with `?` parameters; all SQL text is constant | `store.py` | `test_security.py` injection strings |
| Strip escape sequences and control characters before printing to a terminal | `sanitize.py` | `test_security.py` |
| HTML output (later): escape everything, link only to lore / git.kernel.org with URL-encoded Message-IDs, CSP `default-src 'none'`, no JavaScript | `report` (M2/M3) | |
| No third-party runtime dependencies; development tools pinned with hashes | `pyproject.toml`, `requirements-dev.txt` | |

## 8. Storage

`store.py` keeps one snapshot in SQLite: the parsed FAILED reports, backports,
review mails, other messages (for threading), skipped messages, removed
Message-IDs and stable-queue entries, plus the mirror and stable-queue commits
they came from. Saving replaces the previous snapshot.

## 9. Modules

| Module | Job |
|---|---|
| `model.py` | data types (`FailedRequest`, `BackportPost`, `QueueEntry`, `Status`, ...) |
| `gitio.py` | the only place that runs git |
| `parse.py` | classify and parse one message |
| `mirror.py` | read a public-inbox v2 repository |
| `queue.py` | read stable-queue |
| `status.py` | derive the state of each (upstream, tree) pair |
| `store.py` | SQLite snapshot |
| `sanitize.py` | make untrusted text safe for a terminal |

## 10. Known limitations (M1)

- No `sync` yet: fetching the mirrors, and the date checks after a fetch, come
  with the CLI (M2).
- Reading all of `releases/` lists one directory per git process; on a full
  stable-queue this takes about two minutes and will be made incremental.
- Replies such as "Queued for 6.1, thanks." are kept as replies but not used
  as a state; stable-queue is the source of truth for QUEUED.
- A patch dropped from the queue is not reported as "dropped" yet; it simply
  falls back to BACKPORT_POSTED. Showing the drop needs the stable-queue
  history.
- Only one public-inbox epoch is read.
