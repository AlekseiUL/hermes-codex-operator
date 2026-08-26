#!/usr/bin/env python3
"""Fail closed on local Markdown links, fences, Mermaid blocks, and CLI examples."""

from __future__ import annotations

import argparse
import re
import shlex
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import unquote

FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})([^`]*)$")
MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
HTML_LINK_RE = re.compile(r"<(?:a|img)\b[^>]*(?:href|src)=[\"']([^\"']+)[\"']", re.I)
HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
CLI_RE = re.compile(r"(?:^|\s)hermes-codex(?:\s+([a-z][a-z0-9-]*))?")
EXTERNAL_PREFIXES = ("http://", "https://", "mailto:", "tel:", "data:")
MERMAID_STARTS = (
    "flowchart",
    "graph",
    "sequenceDiagram",
    "classDiagram",
    "stateDiagram",
    "stateDiagram-v2",
    "erDiagram",
    "journey",
    "gantt",
    "pie",
    "mindmap",
    "timeline",
    "quadrantChart",
    "gitGraph",
    "sankey-beta",
    "xychart-beta",
    "block-beta",
)
SKIP_DIRECTORIES = {".git", ".venv", "build", "dist", "node_modules"}


def github_slug(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[`*_~]", "", text).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text, flags=re.UNICODE)
    return re.sub(r"\s+", "-", text)


def anchors_for(path: Path) -> set[str]:
    anchors: set[str] = set()
    seen: Counter[str] = Counter()
    in_fence = False
    fence_char = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        match = FENCE_RE.match(line)
        if match:
            marker = match.group(1)
            if not in_fence:
                in_fence = True
                fence_char = marker[0]
            elif marker[0] == fence_char:
                in_fence = False
            continue
        if in_fence:
            continue
        heading = HEADING_RE.match(line)
        if not heading:
            continue
        base = github_slug(heading.group(2))
        if not base:
            continue
        suffix = seen[base]
        seen[base] += 1
        anchors.add(base if suffix == 0 else f"{base}-{suffix}")
    return anchors


def documented_commands_from_parser() -> set[str]:
    try:
        import argparse as argparse_module
        from hermes_codex_operator.cli import _parser
    except (ImportError, TypeError) as exc:
        raise RuntimeError(
            "cannot load the project CLI parser; use supported Python with PYTHONPATH=src"
        ) from exc
    parser = _parser()
    commands: set[str] = set()
    for action in parser._actions:
        if isinstance(action, argparse_module._SubParsersAction):
            commands.update(action.choices)
    return commands


def _clean_destination(raw: str) -> str:
    value = raw.strip()
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1]
    # Markdown permits an optional quoted title after a destination.
    if " " in value and not value.startswith(EXTERNAL_PREFIXES):
        try:
            value = shlex.split(value)[0]
        except ValueError:
            pass
    return unquote(value)


def scan_docs(root: Path, valid_commands: set[str] | None = None) -> list[str]:
    root = root.resolve()
    markdown_files = sorted(
        path
        for path in root.rglob("*.md")
        if not SKIP_DIRECTORIES.intersection(path.relative_to(root).parts)
    )
    findings: list[str] = []
    anchor_cache: dict[Path, set[str]] = {}
    if valid_commands is None:
        try:
            commands = documented_commands_from_parser()
        except RuntimeError as exc:
            return [f"docs gate: {exc}"]
    else:
        commands = valid_commands

    for path in markdown_files:
        rel = path.relative_to(root).as_posix()
        lines = path.read_text(encoding="utf-8").splitlines()
        in_fence = False
        fence_char = ""
        fence_language = ""
        fence_body: list[str] = []

        for line_number, line in enumerate(lines, 1):
            fence = FENCE_RE.match(line)
            if fence:
                marker = fence.group(1)
                if not in_fence:
                    in_fence = True
                    fence_char = marker[0]
                    fence_language = fence.group(2).strip().lower()
                    fence_body = []
                elif marker[0] == fence_char:
                    if fence_language == "mermaid":
                        content = "\n".join(fence_body).strip()
                        if not content:
                            findings.append(f"{rel}:{line_number}: empty Mermaid block")
                        elif not content.startswith(MERMAID_STARTS):
                            findings.append(
                                f"{rel}:{line_number}: unsupported Mermaid diagram start"
                            )
                    in_fence = False
                    fence_char = ""
                    fence_language = ""
                    fence_body = []
                continue

            if in_fence:
                fence_body.append(line)
                if fence_language in {"bash", "sh", "shell", "console"}:
                    for match in CLI_RE.finditer(line):
                        command = match.group(1)
                        if command and commands and command not in commands:
                            findings.append(
                                f"{rel}:{line_number}: unknown hermes-codex command {command!r}"
                            )
                continue

            destinations = [match.group(1) for match in MARKDOWN_LINK_RE.finditer(line)]
            destinations.extend(match.group(1) for match in HTML_LINK_RE.finditer(line))
            for raw in destinations:
                destination = _clean_destination(raw)
                if not destination or destination.startswith(EXTERNAL_PREFIXES):
                    continue
                if destination.startswith("#"):
                    target = path
                    fragment = destination[1:]
                else:
                    target_text, separator, fragment = destination.partition("#")
                    target = (path.parent / target_text).resolve()
                    if root != target and root not in target.parents:
                        findings.append(f"{rel}:{line_number}: link escapes repository: {destination}")
                        continue
                    if not target.exists():
                        findings.append(f"{rel}:{line_number}: missing link target: {destination}")
                        continue
                    if target.is_dir():
                        target = target / "README.md"
                        if not target.exists():
                            findings.append(f"{rel}:{line_number}: directory link has no README: {destination}")
                            continue
                if fragment and target.suffix.lower() == ".md":
                    anchors = anchor_cache.setdefault(target, anchors_for(target))
                    if fragment.lower() not in anchors:
                        findings.append(f"{rel}:{line_number}: missing anchor: {destination}")

        if in_fence:
            findings.append(f"{rel}:{len(lines)}: unclosed {fence_char * 3} fence")

    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    findings = scan_docs(args.root)
    if findings:
        for finding in findings:
            print(finding, file=sys.stderr)
        print(f"docs gate: FAIL ({len(findings)} finding(s))", file=sys.stderr)
        return 1
    print("docs gate: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
