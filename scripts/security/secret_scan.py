#!/usr/bin/env python3
"""Fail closed when private credentials or secret-bearing files enter the repo.

Findings deliberately omit matched values so CI and terminal logs cannot become
another credential leak path.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable


MAX_SCAN_BYTES = 2 * 1024 * 1024
SKIP_PARTS = {
    ".git",
    ".vs",
    ".vscode",
    ".venv",
    "venv",
    "node_modules",
    "bin",
    "obj",
    "dist",
    "build",
    "out",
    "__pycache__",
}
SENSITIVE_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".dev.vars",
    "config.private.toml",
    "local.settings.json",
    "secrets.json",
    "credentials.json",
    "cert.pem",
}
SENSITIVE_SUFFIXES = (".pem", ".p12", ".pfx", ".key")


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]


RULES = (
    Rule(
        "private-key-material",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"),
    ),
    Rule("github-token", re.compile(r"(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})")),
    Rule("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    Rule(
        "credential-in-url",
        re.compile(r"\b(?:https?|postgres(?:ql)?|mysql|redis)://[^\s:/]+:[^\s/@]+@", re.IGNORECASE),
    ),
    Rule(
        "jwt-token",
        re.compile(r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b"),
    ),
    Rule(
        "literal-secret-assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
            r"cloudflare[_-]?token|github[_-]?token|password|passwd|connection[_-]?string)\b"
            r"\s*[:=]\s*[\"']?([A-Za-z0-9_./+=:@-]{16,})"
        ),
    ),
)

PLACEHOLDER_MARKERS = (
    "your_",
    "replace_me",
    "replace-with",
    "example",
    "placeholder",
    "redacted",
    "dummy",
    "changeme",
    "<token>",
    "<secret>",
    "${",
    "***",
)


def run_git(*args: str, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
    )


def normalize(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def should_skip(path: str) -> bool:
    parts = PurePosixPath(normalize(path)).parts
    return any(part in SKIP_PARTS for part in parts)


def is_sensitive_path(path: str) -> bool:
    normalized = normalize(path).lower()
    name = PurePosixPath(normalized).name
    if name == ".env.example" or name == ".dev.vars.example":
        return False
    if name in SENSITIVE_NAMES:
        return True
    if name.startswith(".env.") or name.startswith(".dev.vars."):
        return True
    if name.endswith(SENSITIVE_SUFFIXES):
        return True
    return "service-account" in name and name.endswith(".json")


def looks_like_placeholder(line: str) -> bool:
    lowered = line.lower()
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def scan_text(source: str, content: bytes) -> list[tuple[str, int, str]]:
    if len(content) > MAX_SCAN_BYTES or b"\x00" in content:
        return []
    text = content.decode("utf-8", errors="ignore")
    findings: list[tuple[str, int, str]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if looks_like_placeholder(line):
            continue
        for rule in RULES:
            if rule.pattern.search(line):
                findings.append((source, line_number, rule.name))
    return findings


def read_worktree(path: str) -> bytes | None:
    candidate = Path(path)
    try:
        if not candidate.is_file() or candidate.stat().st_size > MAX_SCAN_BYTES:
            return None
        return candidate.read_bytes()
    except OSError:
        return None


def worktree_paths() -> list[str]:
    result = run_git("ls-files", "--cached", "--others", "--exclude-standard", "-z", text=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors="ignore"))
    return [item.decode(errors="surrogateescape") for item in result.stdout.split(b"\x00") if item]


def staged_paths() -> list[str]:
    result = run_git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z", text=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors="ignore"))
    return [item.decode(errors="surrogateescape") for item in result.stdout.split(b"\x00") if item]


def staged_content(path: str) -> bytes | None:
    result = run_git("show", f":{path}", text=False)
    if result.returncode != 0 or len(result.stdout) > MAX_SCAN_BYTES:
        return None
    return result.stdout


def scan_paths(paths: Iterable[str], staged: bool = False) -> list[tuple[str, int, str]]:
    findings: list[tuple[str, int, str]] = []
    for path in sorted(set(normalize(item) for item in paths)):
        if should_skip(path):
            continue
        if is_sensitive_path(path):
            findings.append((path, 0, "secret-bearing-file-name"))
            continue
        content = staged_content(path) if staged else read_worktree(path)
        if content is not None:
            findings.extend(scan_text(path, content))
    return findings


def history_objects() -> list[tuple[str, str]]:
    result = run_git("rev-list", "--objects", "--all")
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    objects: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        oid, separator, path = line.partition(" ")
        if separator and path:
            objects.append((oid, normalize(path)))
    return objects


def scan_history() -> list[tuple[str, int, str]]:
    findings: list[tuple[str, int, str]] = []
    seen_blobs: set[str] = set()
    for oid, path in history_objects():
        if should_skip(path):
            continue
        source = f"history:{path}"
        if is_sensitive_path(path):
            findings.append((source, 0, "secret-bearing-file-name"))
        if oid in seen_blobs:
            continue
        seen_blobs.add(oid)
        size = run_git("cat-file", "-s", oid)
        if size.returncode != 0:
            continue
        try:
            if int(size.stdout.strip()) > MAX_SCAN_BYTES:
                continue
        except ValueError:
            continue
        blob = run_git("cat-file", "blob", oid, text=False)
        if blob.returncode == 0:
            findings.extend(scan_text(source, blob.stdout))
    return findings


def print_findings(findings: Iterable[tuple[str, int, str]]) -> int:
    unique = sorted(set(findings))
    if not unique:
        print("[SECURITY] Secret scan passed; no credential material detected.")
        return 0
    print("[SECURITY ALERT] Secret leakage detected. Commit and deployment are blocked.", file=sys.stderr)
    for path, line, rule in unique:
        location = f"{path}:{line}" if line else path
        print(f"  - {location} [{rule}]", file=sys.stderr)
    print(
        "[SECURITY ALERT] Remove the file from tracking, rotate exposed credentials, and rerun the scan.",
        file=sys.stderr,
    )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan TBBFX source and Git history for secrets.")
    parser.add_argument("--staged", action="store_true", help="Scan only the staged index.")
    parser.add_argument("--history", action="store_true", help="Scan every reachable Git blob.")
    parser.add_argument("--worktree", action="store_true", help="Scan tracked and untracked worktree files.")
    args = parser.parse_args()

    if run_git("rev-parse", "--is-inside-work-tree").returncode != 0:
        print("[SECURITY] Run this command from inside the Git repository.", file=sys.stderr)
        return 2

    findings: list[tuple[str, int, str]] = []
    explicit_mode = args.staged or args.history or args.worktree
    if args.staged:
        findings.extend(scan_paths(staged_paths(), staged=True))
    if args.history:
        findings.extend(scan_history())
    if args.worktree or not explicit_mode:
        findings.extend(scan_paths(worktree_paths()))
    return print_findings(findings)


if __name__ == "__main__":
    raise SystemExit(main())
