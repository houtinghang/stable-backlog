# SPDX-License-Identifier: GPL-2.0-or-later
"""Rules for the GitHub Actions workflows and Dependabot (docs/DESIGN.md).

The workflows run with a GitHub token, and the page workflow parses messages
anyone can write, so these rules are checked here, as text (the standard
library has no YAML parser): a later change cannot drop one unnoticed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
EXPECTED = ["ci.yml"]
# Only GitHub's own actions, each pinned to a full commit id with its version noted.
USES_RE = re.compile(r"^\s*(?:- )?uses: (?P<ref>\S+)(?P<rest>.*)$", re.MULTILINE)
PINNED_RE = re.compile(r"actions/[a-z-]+(?:/[a-z-]+)?@[0-9a-f]{40}")
VERSION_RE = re.compile(r"  # v\d+\.\d+\.\d+")
EXPRESSION_RE = re.compile(r"\$\{\{(.*?)\}\}")
# Expressions that never carry data from messages, issues or pull requests.
ALLOWED_EXPRESSIONS = {
    "matrix.python",
    "github.ref",
    "github.run_id",
    "github.run_attempt",
    "steps.deployment.outputs.page_url",
}
UNSAFE_TRIGGERS = ["pull_request_target", "workflow_run", "issue_comment", "issues:"]
WRITE_RE = re.compile(r"^\s*(?P<scope>[a-z-]+): write\s*$", re.MULTILINE)


def read(path: Path) -> str:
    assert path.is_file(), f"{path.relative_to(ROOT)} is missing"
    return path.read_text(encoding="utf-8")


def workflow(name: str) -> str:
    return read(WORKFLOWS / name)


def jobs(text: str) -> dict[str, str]:
    """Job id -> the lines of that job (jobs are indented by two spaces)."""
    lines = text.splitlines()
    out: dict[str, list[str]] = {}
    current = None
    for line in lines[lines.index("jobs:") + 1 :]:
        if line and not line.startswith(" "):
            break
        m = re.fullmatch(r"  ([A-Za-z0-9_-]+):", line)
        if m:
            current = m.group(1)
            out[current] = []
        elif current is not None:
            out[current].append(line)
    return {job: "\n".join(body) for job, body in out.items()}


def steps_using(text: str, action: str) -> list[str]:
    """The text of every step that uses this action."""
    out = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if f"uses: {action}@" not in line:
            continue
        indent = len(line) - len(line.lstrip(" -"))
        step = [line]
        for following in lines[i + 1 :]:
            if (
                following.lstrip().startswith("- ")
                or len(following) - len(following.lstrip()) < indent
            ):
                break
            step.append(following)
        out.append("\n".join(step))
    return out


def test_the_expected_workflows_exist() -> None:
    assert sorted(p.name for p in WORKFLOWS.glob("*.y*ml")) == EXPECTED


@pytest.mark.parametrize("name", EXPECTED)
def test_actions_are_github_s_own_pinned_to_a_commit(name: str) -> None:
    uses = USES_RE.findall(workflow(name))
    assert uses
    for ref, rest in uses:
        assert PINNED_RE.fullmatch(ref), ref
        assert VERSION_RE.fullmatch(rest), f"{ref}: note the version as '  # vX.Y.Z'"


@pytest.mark.parametrize("name", EXPECTED)
def test_no_trigger_that_runs_with_others_input(name: str) -> None:
    text = workflow(name)
    for trigger in UNSAFE_TRIGGERS:
        assert trigger not in text


@pytest.mark.parametrize("name", EXPECTED)
def test_expressions_carry_no_data(name: str) -> None:
    # Text from issues, pull requests or messages must never reach `run:`.
    for expression in EXPRESSION_RE.findall(workflow(name)):
        assert expression.strip() in ALLOWED_EXPRESSIONS, expression


@pytest.mark.parametrize("name", EXPECTED)
def test_no_secrets_and_no_token_left_on_disk(name: str) -> None:
    text = workflow(name)
    assert "secrets." not in text
    checkouts = steps_using(text, "actions/checkout")
    assert checkouts
    for step in checkouts:
        assert "persist-credentials: false" in step


@pytest.mark.parametrize("name", EXPECTED)
def test_every_job_has_a_time_limit(name: str) -> None:
    for job, body in jobs(workflow(name)).items():
        assert re.search(r"^    timeout-minutes: \d+$", body, re.MULTILINE), job


@pytest.mark.parametrize("name", EXPECTED)
def test_one_line_commands_are_plain_yaml_values(name: str) -> None:
    # In a one-line value, ": " would start a mapping (the file would not load)
    # and " #" a comment; such commands go into a `run: |` block.
    for line in workflow(name).splitlines():
        m = re.fullmatch(r"\s*(?:- )?run: (?P<value>.*)", line)
        if m and m.group("value") != "|":
            assert ": " not in m.group("value"), line
            assert " #" not in m.group("value"), line


@pytest.mark.parametrize("name", EXPECTED)
def test_packages_are_installed_only_pinned_and_prebuilt(name: str) -> None:
    for line in workflow(name).splitlines():
        if "pip install" in line:
            assert "--require-hashes" in line
            assert "--only-binary=:all:" in line
            assert "-r requirements-dev.txt" in line


def test_ci_can_only_read() -> None:
    text = workflow("ci.yml")
    assert re.search(r"^permissions:\n  contents: read\n", text, re.MULTILINE)
    assert WRITE_RE.findall(text) == []
    assert "write-all" not in text


def test_ci_runs_every_check_on_the_oldest_and_newest_python() -> None:
    text = workflow("ci.yml")
    assert re.search(r'^        python: \["3\.10", "3\.14"\]$', text, re.MULTILINE)
    runs = [line.strip() for line in text.splitlines() if line.strip().startswith("- run: ")]
    assert runs == [
        "- run: python -m pytest",
        "- run: python -m pytest -m slow",
        "- run: ruff check .",
        "- run: ruff format --check .",
        "- run: mypy",
    ]
    assert re.search(r"^  push:\n    branches: \[main\]$", text, re.MULTILINE)
    assert re.search(r"^  pull_request:\n    branches: \[main\]$", text, re.MULTILINE)


def test_dependabot_updates_the_actions_after_a_cooldown() -> None:
    text = read(ROOT / ".github" / "dependabot.yml")
    assert "package-ecosystem: github-actions" in text
    assert re.search(r"^    cooldown:\n      default-days: 14$", text, re.MULTILINE)
