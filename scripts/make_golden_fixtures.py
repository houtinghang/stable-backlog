#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Regenerate tests/fixtures/golden/ from local lore and stable-queue mirrors.

The golden fixtures are real messages from the lore stable archive and real
stable-queue patch files, trimmed to the headers and the few body lines the
parser needs. This script records where each one came from (MANIFEST.tsv) so
the fixtures can be checked against lore.

Usage (from the repo root, inside WSL/Linux):
    python3 -I scripts/make_golden_fixtures.py \
        ~/src/lore/stable-2.git ~/src/stable-queue.git

Message contents are treated as data only: commit ids are validated, output
file names come from the tables below, never from the messages.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

LORE_REV = "ad6d7f02cd7ca5eb26595aca896865be20d0c70a"  # 2026-10-24T16:47:00Z
QUEUE_REV = "748fbe08cf39265d93f36f101ce9f4a3b8c73e39"  # 2026-10-24T14:21:52Z

# fixture name -> lore public-inbox commit (one message per commit)
MAILS: dict[str, str] = {
    # net: gso: limit recursive IP-in-IP segmentation (006c8b834aed)
    "gso-failed-6.18": "c2845d59856316eee33f81699f432168527c57de",
    "gso-failed-6.12": "3cd880dea0073127847d0ff7698d982fa36d36e2",
    "gso-failed-6.6": "621680af2032d3a3747ddea097711303ca18ebc5",
    "gso-failed-6.1": "eb19fa0bb944b41a508f1303946cc7c0997dd9cd",
    "gso-failed-5.15": "0870975c9f2b38dc95928a63d9246808bbf3753b",
    "gso-failed-5.10": "30be5babb98c8b7a3817db68739df7ee6f0c2679",
    "gso-backport-6.18": "3c843fd799bd17f6cc5140812e9f4c225e84f2bf",
    "gso-backport-6.12": "36b9e257ba1d028fe490fc9f63be30dd5a12b6cc",
    "gso-backport-6.6": "69db672178035686516f1c916c4f32fb2824ff9c",
    "gso-backport-6.1": "488b884edec08b1a39dd082df729e3ac3d1031c7",
    "gso-backport-5.15": "7a869765e68dc253f40443f592211dc4e258abde",
    "gso-backport-5.10": "3fb878855e1616f36f6626aca0cf1dd57a2a27ba",
    # mm/huge_memory: transfer the pmd dirty bit to the folio on zap (fe6cf984939d)
    "pmd-failed-6.18": "9058566fb155f07dc577808eca9cf538a5e9372b",
    "pmd-failed-6.12": "cd674523085c7bcee8818f4147abcca61b530281",
    "pmd-failed-6.6": "89aa2748a3b57c9f571c545ad760a9953fafaa13",
    "pmd-failed-6.1": "6e9cf28ecc34d6db03d6eadf7c38d5e605abb797",
    "pmd-failed-5.15": "b455d04648ddce3c776e8c9289c0f027e7a60b2f",
    "pmd-failed-5.10": "fae50366bb136edd3060bf11150b66ff21f12f39",
    "pmd-backport-6.18-sasha": "b3514e92c54e526e687c1c2a992fd23a073e1015",
    "pmd-backport-6.1": "50c7871fad2022d2cca2daec9271783906793273",
    "pmd-backport-5.15": "d4fdd8bf60e294063e0431332692a5753cc7e931",
    "pmd-backport-5.10": "db6fd79605368170b04bc2a0fe28fc7b09376c71",
    "pmd-review-6.12-greg": "ff346adf004212726d3d5fcd016e4ce2be3cb266",
    # ACPI: scan: Avoid registering platform devices with resource overlaps
    # (f234fdaae1ca), answered by a 3-patch series
    "acpi-failed-6.18": "aebb3c3206cc1018e9e2494e4a64e5e2504acf0a",
    "acpi-series-6.18-0": "d2de90cc3bcbb563275df311f631d43b36df523f",
    "acpi-series-6.18-1": "c32977610782d8c0822ed80fedc61f90e520d8ec",
    "acpi-series-6.18-2": "6618c00d08e1c808b4f00dad75ec25a42b499f7c",
    "acpi-series-6.18-3": "a9e9d7af4643086b3d6a375728e0b530dd46e1e8",
    # tty: port: fix tty_port_tty_vhangup() race (b48820de8c1a)
    "tty-failed-6.12": "abeeabcc9a5346d53d641bf92d153ffcd145270d",
    "tty-failed-6.6": "dea8fe81f9f19701b6afb8915781b24dc8692c91",
    "tty-failed-6.1": "f0e34e27eff4adc8426c4e7025d1fcc86a1bff53",
    "tty-failed-5.15": "52a9617ad9c65a6e885c55bc2615f8e74d68145d",
    "tty-failed-5.10": "5b5eb7335866b0c71fc26524f74ba33d4cf11d97",
    "tty-backport-6.6": "b07912df39b3b711a3200013640c7ad698cbcecf",
    "tty-backport-6.1": "c35a886863c1b514ed9c82c9a046d625fe9280a0",
    "tty-backport-5.15-1": "d11f9cbf644c804fcc6c6b9bd38f8be49079a2dd",
    "tty-backport-5.15-2": "86bdc3224fd119124de4533e0eb645b2abb935c8",
    "tty-backport-5.10-1": "493ef0a6f0ba3ad19b6014798807d54ab3ebcb7c",
    "tty-backport-5.10-2": "886a445abc5a6ed36cca71aa9e0b641f00921e09",
    "tty-reply-6.6-sasha": "d366263c2071cfb063cf122b1d3aa578af191b27",
    "tty-reply-6.1-sasha": "9bf173679ea50ab79587d4f5d5724fdffe8f4762",
    "tty-reply-5.15-sasha": "0d8a8570a7d32206c2b9077f52cece24c495a6bb",
    "tty-reply-5.10-sasha": "e672d7152188ec3a48bc1b321c63e77f0c39ef7b",
    "tty-review-6.6-greg": "9bc385443fe2bf78fe8e3c2dfb62a5631fac2fb6",
    # mm/damon/core: allow esz to be set to zero (90179da203ba): backport sent
    # as a new thread, plus a non-backport reply that quotes the FAILED mail
    "damon-failed-6.18": "e87c972dd50470c2d316f73eb21f167177627b4a",
    "damon-backport-6.18-1": "42a3b0929414f5b9d456904c1174056ed0b3971f",
    "damon-reply-6.18-sj": "a65d2bcfb5a1615b7d8182f4b970cf49223036a1",
}

# stable-queue paths to copy (trimmed); series files are rebuilt from these
QUEUE_FILES: list[str] = [
    "queue-5.10/tty-port-fix-tty_port_tty_vhangup-race.patch",
    "queue-5.15/tty-port-fix-tty_port_tty_vhangup-race.patch",
    "queue-6.1/tty-port-fix-tty_port_tty_vhangup-race.patch",
    "releases/6.6.160/tty-port-fix-tty_port_tty_vhangup-race.patch",
    "releases/6.12.113/tty-port-fix-tty_port_tty_vhangup-race.patch",
    "releases/6.18.56/tty-port-fix-tty_port_tty_vhangup-race.patch",
    "releases/6.6.160/mm-huge_memory-transfer-the-pmd-dirty-bit-to-the-fol.patch",
    "releases/6.12.114/mm-huge_memory-transfer-the-pmd-dirty-bit-to-the-fol.patch",
    "releases/6.18.52/mm-huge_memory-transfer-the-pmd-dirty-bit-to-the-folio-on-zap.patch",
    "releases/7.2.10/net-gso-limit-recursive-ip-in-ip-segmentation.patch",
]

KEEP_HEADERS = (
    "from",
    "date",
    "subject",
    "message-id",
    "in-reply-to",
    "references",
    "mime-version",
    "content-type",
    "content-transfer-encoding",
)
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
UPSTREAM_RE = re.compile(
    rb"^(\[ Upstream commit [0-9a-f]{40} \]|commit [0-9a-f]{40} upstream"
    rb"|\(cherry picked from commit [0-9a-f]{40}\))",
)
TRIM_NOTE = b"[ stable-backlog test fixture: rest of the message trimmed ]\n"
FAILED_TRIM_NOTE = (
    b"[ stable-backlog test fixture: rest of the message trimmed except diff --git ]"
)
SHORT_BODY_LINES = 40


def git_blob(repo: str, rev_path: str) -> bytes:
    out = subprocess.run(
        ["git", "-C", repo, "cat-file", "blob", rev_path],
        check=True,
        capture_output=True,
        timeout=60,
        env={"GIT_NO_LAZY_FETCH": "1", "PATH": "/usr/bin:/bin"},
    )
    return out.stdout


def split_message(raw: bytes) -> tuple[list[bytes], list[bytes]]:
    head, sep, body = raw.partition(b"\n\n")
    if not sep:
        raise ValueError("no header/body separator")
    return head.split(b"\n"), body.split(b"\n")


def keep_headers(lines: list[bytes]) -> list[bytes]:
    out: list[bytes] = []
    keep = False
    for line in lines:
        if line[:1] in (b" ", b"\t"):
            if keep:
                out.append(line)
            continue
        name = line.split(b":", 1)[0].decode("ascii", "replace").lower()
        keep = name in KEEP_HEADERS
        if keep:
            out.append(line)
    return out


def trim_failed(body: list[bytes]) -> list[bytes]:
    """Keep everything up to the original commit's Subject (with folding),
    then only the `diff --git` lines of its patch (the changed files)."""
    out: list[bytes] = []
    in_orig = False
    in_subject = False
    for i, line in enumerate(body):
        if in_subject:
            if line[:1] in (b" ", b"\t"):
                out.append(line)
                continue
            diffs = [d for d in body[i:] if d.startswith(b"diff --git ")]
            return [*out, b"", FAILED_TRIM_NOTE, *diffs]
        out.append(line)
        if line.startswith(b"------------------ original commit"):
            in_orig = True
        elif in_orig and line.startswith(b"Subject: "):
            in_subject = True
    raise ValueError("FAILED mail without original commit Subject")


def trim_other(body: list[bytes]) -> list[bytes]:
    """Keep short replies whole; otherwise up to the upstream marker line."""
    if len(body) <= SHORT_BODY_LINES:
        return body
    for i, line in enumerate(body):
        if UPSTREAM_RE.match(line):
            return [*body[: i + 1], b"", TRIM_NOTE.rstrip(b"\n")]
    return [*body[:15], b"", TRIM_NOTE.rstrip(b"\n")]


def write_mails(lore: str, out: Path) -> list[str]:
    manifest = []
    maildir = out / "mail"
    maildir.mkdir(parents=True, exist_ok=True)
    for name, commit in MAILS.items():
        if not COMMIT_RE.match(commit):
            raise ValueError(f"bad commit id for {name}")
        raw = git_blob(lore, f"{commit}:m")
        head, body = split_message(raw)
        body = trim_failed(body) if "-failed-" in name else trim_other(body)
        data = b"\n".join(keep_headers(head)) + b"\n\n" + b"\n".join(body)
        if not data.endswith(b"\n"):
            data += b"\n"
        (maildir / f"{name}.eml").write_bytes(data)
        mid = next((h for h in head if h.lower().startswith(b"message-id:")), b"?").decode(
            "ascii", "replace"
        )
        manifest.append(f"mail/{name}.eml\t{commit}\t{mid.split(':', 1)[1].strip()}")
    return manifest


def write_queue(sq: str, out: Path) -> list[str]:
    manifest = []
    root = out / "stable-queue"
    series: dict[str, list[str]] = {}
    for path in QUEUE_FILES:
        raw = git_blob(sq, f"{QUEUE_REV}:{path}")
        lines = raw.split(b"\n")
        for i, line in enumerate(lines):
            if UPSTREAM_RE.match(line):
                lines = [*lines[: i + 1], b"", TRIM_NOTE.rstrip(b"\n"), b""]
                break
        else:
            raise ValueError(f"no upstream marker in {path}")
        dest = root / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\n".join(lines))
        top, name = path.split("/", 1) if path.startswith("queue-") else ("", "")
        if top:
            series.setdefault(top, []).append(name)
        manifest.append(f"stable-queue/{path}\t{QUEUE_REV}\t-")
    for top, names in series.items():
        (root / top / "series").write_text("".join(f"{n}\n" for n in names))
    (root / "active_kernel_versions").write_bytes(
        git_blob(sq, f"{QUEUE_REV}:active_kernel_versions")
    )
    return manifest


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    lore, sq = argv[1], argv[2]
    out = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "golden"
    lines = [
        "# fixture\tsource commit (lore stable-2.git or stable-queue)\tMessage-ID",
        f"# lore stable-2.git at {LORE_REV}; stable-queue at {QUEUE_REV}",
        *write_mails(lore, out),
        *write_queue(sq, out),
    ]
    (out / "MANIFEST.tsv").write_text("\n".join(lines) + "\n")
    print(f"wrote {len(MAILS)} mails, {len(QUEUE_FILES)} queue files to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
