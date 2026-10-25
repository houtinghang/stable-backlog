# stable-backlog design

This document describes how stable-backlog reads its data, how it decides the
state of each FAILED request, and the rules that keep it safe to run on data
anyone can write. It covers the data layer, keeping the mirrors up to date,
the commands that read the snapshot and the web page.

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

### Keeping the mirrors up to date

`sync.sync()` keeps both mirrors in a cache directory
(`$XDG_CACHE_HOME/stable-backlog`, else `~/.cache/stable-backlog`), created
with mode 0700. An existing directory that is a symlink, is owned by someone
else or is writable by group or others is refused, not repaired. A lock file
lets only one sync run at a time.

To keep the load on lore and git.kernel.org low, the network is used at most
once an hour: a sync within an hour of the last successful one that used the
network fails before contacting anyone. That time is kept in `last-fetch` in
the cache directory. Failed syncs and `--offline` rebuilds do not count; an
unreadable time, or one in the future, is ignored with a warning.

For each source:

1. **First use**: a bare, shallow clone into `<name>.new`, moved into place
   only when it worked. lore: the messages imported in the last 90 days
   (`--shallow-since`); stable-queue: `--depth=1`, since only its current
   state is used.
2. **Later**: `git fetch <URL> +refs/heads/master:refs/stable-backlog/incoming`.
   The URL is the constant from the list above, never the repository's
   `remote.origin.url`. (A bare clone has no fetch refspec, so
   `git fetch origin` would only update `FETCH_HEAD`.)
3. `master` only moves forward. If the old `master` is not an ancestor of
   the fetched commit (for example after a public-inbox purge rewrote the
   history), nothing is changed and sync asks for `--reclone`.
4. The newest commit must be recent: its committer date within 2 days for
   lore (the longest gap seen in 85 days was 2.4 hours) and within 7 days
   for stable-queue (32 hours in 19 days). Otherwise sync fails instead of
   presenting old data as current.
5. lore only: if the next epoch (`.../stable/git/3.git`) exists, sync fails,
   because the current epoch would no longer receive messages.

The snapshot is then rebuilt from scratch into `snapshot.sqlite.tmp`, which
replaces `snapshot.sqlite` only when everything worked. It records both
commits, their dates, the time of the sync and whether it was an `--offline`
rebuild (no fetch and no freshness check). A failed sync leaves the previous
snapshot as it was.

Commands that use the network also get `-c credential.helper=` (no
credential helper is ever asked), `-c http.followRedirects=false` (no
redirect to another host) and `-c transfer.fsckObjects=true` (malformed
objects are rejected), and `--` before the URL. The user's own git
configuration, such as a proxy, still applies.

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
The same quote (`git format-patch` output, after the "original commit in
Linus's tree" separator line; nothing above it counts) gives the commit's
author, from its `From:` header (folded and RFC 2047 encoded forms occur),
and the changed files, from its `diff --git a/<path> b/<path>` lines (both
names of a rename). In 2026-10 all 3,604 FAILED reports in the archive had
both. Paths that git would quote (spaces, non-ASCII) are not read; the
kernel has none.

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
  ignored. Patches without a marker are counted (`without_marker`). A
  patch or `series` name that is not a file (a directory, a submodule) is an
  error.

The commit's tree is listed once (`git ls-tree -r`) and every file is read
by its blob id through one `git cat-file --batch`. Asking for `rev:path`
instead makes git search the `releases/` tree (4,378 entries in October
2026) again for each file: 2 minutes for the 201,662 patches of eight trees,
against 15 seconds by blob id. A blob shared by several files is read once,
and only the upstream commits found in it are kept, not its content.

Only the first 5 MB of a patch are searched for markers, and the rest is read
and thrown away: the markers are at the top, and the limit bounds the work
for a huge patch (the largest, 6.9 MB in October 2026, is a big diff with its
marker in the first lines). A `series` file over 5 MB is an error, not a
partial list.

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
  stored. Every command prints their number with samples on stderr, the
  JSON export has `skipped` and `skipped_samples`, and the web page shows
  their number at the top and samples at the end.
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
| Contact only the fixed sources: every network command checks its URL against an allow-list; no credential helper, no redirects, objects checked with fsck | `gitio.py`, `sync.py` | `test_security.py` URL allow-list; `test_sync.py` runs the real commands against local repositories, with no network |
| The cache directory can be written only by its owner (0700; symlinks, other owners and group- or world-writable directories are refused); the lock file is opened without following symlinks | `cache.py` | `test_cache.py` |
| A failed sync never replaces the snapshot; stale or rewritten mirrors fail loudly | `sync.py` | `test_sync.py` |
| The network is used at most once an hour | `sync.py` | `test_sync.py` |
| No network listener of any kind | whole package | |
| File names never come from message content | whole package | `test_security.py` path-like Message-IDs |
| Size and length limits before any regex (5 MB per message, the first 5 MB of a stable-queue patch, 1000 characters per subject); no regex with nested unbounded repetition | `parse.py`, `queue.py` | `test_security.py` huge lines, `test_queue.py` huge patch |
| SQLite only with `?` parameters; all SQL text is constant | `store.py` | `test_security.py` injection strings |
| Strip escape sequences and control characters before printing to a terminal | `sanitize.py` | `test_security.py` |
| Links go only to lore message pages; the Message-ID is percent-encoded, and an id without `@` gets no link | `report.py` | `test_report.py` hostile Message-IDs |
| Commands that read the snapshot open it read-only and run neither git nor anything on the network | `store.py`, `cli.py` | `test_cli.py`, `test_store.py` |
| The web page has no JavaScript; its Content-Security-Policy allows nothing but its own style (by hash); text from messages is escaped after control characters are removed, and titles and names are isolated with `<bdi>`; links built from data go only to lore and git.kernel.org; the page is ASCII | `page.py`, `report.py` | `test_page.py` hostile titles, names, Message-IDs and tree names |
| No third-party runtime dependencies; development tools pinned with hashes and installed from wheels only | `pyproject.toml`, `requirements-dev.txt`, `ci.yml` | `test_workflows.py` |
| GitHub Actions: only GitHub's own actions, pinned to commit ids; no trigger that runs with others' input (`pull_request_target`, `workflow_run`, `issue_comment`); a read-only token, not kept on disk; no secrets; no expression that could carry data into a command; a time limit on every job | `.github/` | `test_workflows.py` |

## 8. Storage

`store.py` keeps one snapshot in SQLite: the parsed FAILED reports, backports,
review mails, other messages (for threading), skipped messages, removed
Message-IDs and stable-queue entries, plus the mirror and stable-queue commits
they came from. Saving replaces the previous snapshot. The snapshot records
its schema version; one written by another version is refused with a hint
to rebuild it with `stable-backlog sync` (`--offline` is enough).

## 9. Commands that read the snapshot

`list`, `show` and `export` only read `snapshot.sqlite`. It is opened read-only
(`mode=ro`): nothing is written or created, git is not run and the network is
not used. A missing snapshot, or one from another schema version, stops the
command with a hint to run `sync`. Building the item states takes about 0.1
seconds on 90 days of the list: backports and stable-queue entries are looked
up by the first 12 digits of their hash, the shortest hash that is accepted.

- **Ages** count the days from a request's first FAILED report to the newest
  lore message in the snapshot, not to the current time, so the same
  snapshot always gives the same list.
- **list** prints one row per upstream commit, the longest-waiting first;
  TREES lists the trees whose requests matched. The default is the requests
  with no backport found (`--status failed`).
- **Filters**: `--tree`, `--status`, `--since` (first FAILED report on or
  after a date), `--author` (part of "name <address>", any case) and
  `--path` (a file, or a directory as whole path components: `drivers/tty`
  matches `drivers/tty/tty_port.c`, not `drivers/ttyx.c`). A request that a
  filter cannot check (no date, no author, no files found) is left out and
  counted on stderr.
- **show** finds a commit by 12 to 40 hex digits (a prefix, compared in
  memory and never passed to git) or by the Message-ID of a FAILED report.
  An ambiguous prefix or no match exits with status 1; "no FAILED report"
  comes with the dates the snapshot covers, since it does not mean that
  nothing needs a backport.
- **stderr** carries the data time (newest lore message, stable-queue
  commit, when the snapshot was built and whether it was an offline
  rebuild), the skipped messages with samples, and a warning when the newest
  lore message is more than 2 days old or the stable-queue commit more than
  7 days.
- **Links** go only to `https://lore.kernel.org/stable/<Message-ID>/` and are
  built in one place (`report.lore_url`). The Message-ID is percent-encoded
  except for the RFC 3986 path characters that public-inbox itself leaves
  as they are, so `/ ? # %` cannot lead off the message page. An id without
  `@` (every RFC 5322 Message-ID has one) gets no link, because `.` or `..`
  would be a dot segment.

### JSON export

`export --json` writes one document with every item, in every status, to
stdout or, with `-o FILE`, to a temporary file in the same directory that
then replaces `FILE` (`os.replace`), so a reader sees the old file or the new
one. `-o` naming a directory is refused. Text from messages is kept as it
was mailed; the JSON is written with `ensure_ascii`, and DEL is escaped too,
so the output is printable ASCII only. Consumers must still escape it for
their own output (HTML, terminals). Times are ISO 8601 in UTC; a field that
is not known is `null`.

```
{
  "format": "stable-backlog/1",       changed on any incompatible change
  "generated_at": "...",               when export ran
  "tool": "stable-backlog <version>",
  "snapshot": {
    "synced_at": "...", "offline": false,       offline: rebuilt without fetching
    "lore": {"url": "...", "rev": "<commit>", "newest": "..."},
    "stable_queue": {"url": "...", "rev": "<commit>", "newest": "..."}
  },
  "messages_read": 70784,
  "skipped": 1,                        messages that could not be read
  "skipped_samples": [{"commit": "...", "message_id": null, "reason": "too-large"}],
  "counts": {"FAILED": 540, "BACKPORT_POSTED": 661, "QUEUED": 702, "RELEASED": 1694},
  "items": [{                          sorted by upstream, then tree
    "upstream": "<40 hex>", "tree": "6.6",
    "status": "FAILED",                FAILED = no backport found
    "label": "no backport found",      as list / show print it
    "confidence": null,                "hash", "title" (low confidence) or null
    "title": "...",
    "author": {"name": "...", "email": "..."},   or null
    "files": ["..."],
    "first_failed": "...",
    "failed": [{"message_id": "...", "date": "...", "url": "..."}],
    "backports": [{"message_id": "...", "sender": "...", "date": "...",
                   "confidence": "hash", "url": "..."}],
    "replies": [{"message_id": "...", "url": "..."}],
    "queue": [{"location": "release", "directory": "releases/6.6.160",
               "path": "...", "release": "6.6.160"}],
    "release": "6.6.160"               first release that has it, or null
  }]
}
```

`url` is a lore link built by `report.lore_url`, or `null` for a Message-ID
without `@`. `skipped_samples` holds at most 100 entries. On 90 days of the
list the file is about 4.5 MB and takes under a second to write.

### Web page

`export --html` writes one static HTML file, the same way as `--json` (to
stdout, or with `-o FILE` replacing `FILE` in one step). It shows the
requests with no backport found; the other states are only counted. From
top to bottom:

- a notice: not an official tool; early development, not yet checked by
  hand; "no backport found" is not "nobody is working on it";
- the number of messages that could not be read, if any;
- where the data comes from: the newest lore message, the stable-queue
  commit (linked) and its date, the dates of the FAILED reports covered,
  and when the page was built;
- the number of requests per tree and state;
- all trees, one row per commit as in `list`: age, commit (linked to
  git.kernel.org), title, author name, the trees (each linked to its FAILED
  report) and the number of replies;
- one section per tree, one row per request: the FAILED reports (dates
  linked to lore) and each reply (linked);
- the messages that could not be read (at most 20), a link to the JSON
  published next to the page, and a link to the project.

Ages are counted as in `list`, so the same snapshot always gives the same
page. Author addresses are left out of the page; the JSON has them.

There is no JavaScript: the sections are reached through links within the
page, and the browser's own search finds a name or a path. GitHub Pages
cannot set HTTP headers, so the Content-Security-Policy is a `<meta>`
element at the top of `<head>`:

```
default-src 'none'; style-src 'sha256-<hash of the style>'; base-uri 'none'; form-action 'none'
```

`base-uri` and `form-action` do not fall back to `default-src`, so they are
given as well. Text from messages passes through one function that removes
escape sequences and control characters (as for the terminal) and then
escapes it for HTML; titles and names are wrapped in `<bdi>`, so
bidirectional control characters in them cannot reorder the rest of a row.
Tree names are used in element ids and are checked to be `X.Y` first. Links
built from data come from `report` only (`lore_url`, `commit_url`,
`queue_commit_url`; the last two accept only full commit ids). The page is
written as ASCII, with other characters as `&#NNNN;`. On 90 days of the
list it is about 370 KB.

## 10. Modules

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
| `cache.py` | the cache directory and the sync lock |
| `sync.py` | update the mirrors and rebuild the snapshot |
| `report.py` | select, group and label items for `list` and `show`; links to lore and git.kernel.org; the JSON export |
| `page.py` | the web page (`export --html`) |
| `cli.py` | the `stable-backlog` command |

## 11. Known limitations

- Every sync rebuilds the snapshot from all messages in the local mirror.
- Every sync also reads all stable-queue patches of the trees with FAILED
  reports again (about 15 seconds in October 2026), not only the new ones.
- The local lore mirror keeps growing (about 3 MB a day); old messages are
  not pruned.
- Replies such as "Queued for 6.1, thanks." are kept as replies but not used
  as a state; stable-queue is the source of truth for QUEUED.
- "Replies" are all other messages threaded below a FAILED report, including
  the other patches of a backport series (for example its prerequisites).
- A patch dropped from the queue is not reported as "dropped" yet; it simply
  falls back to BACKPORT_POSTED. Showing the drop needs the stable-queue
  history.
- Only one public-inbox epoch is read.
