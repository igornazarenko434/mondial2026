"""Regression guard — fail loudly if personal / production-identifying data
leaks back into any TRACKED file.

Why this exists (Day-9.34): after the README was rewritten for public portfolio
publication, we did a manual scrub of personal email + production VM IP + money
mentions across every committed file. This test makes sure a future commit
doesn't reintroduce them (e.g. a copy/paste from a working note, an LLM-
assisted edit that pulls back a previously-deleted line, a careless rebase).

The patterns are intentionally repo-specific (the exact values that already
leaked once). Broader, generic API-key shapes (`sk-…`, `AKIA…`, `ghp_…`, etc.)
are better caught by a dedicated secret scanner (gitleaks, trufflehog,
GitHub secret-scanning) which we recommend wiring into CI separately —
this file is the project-internal belt-and-braces layer.
"""
from __future__ import annotations
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent


# ──────────────────────────────────────────────────────────────────────────
# Forbidden patterns. Each entry: (regex, human-readable reason).
#
# Real-world values previously committed and now scrubbed. The reason text
# is shown in the assertion failure so a future contributor sees WHY the
# pattern is blocklisted, not just THAT it's blocklisted.
# ──────────────────────────────────────────────────────────────────────────
FORBIDDEN_PATTERNS: list[tuple[str, str]] = [
    # --- personal identifiers ---
    (r"igor434@gmail\.com",
     "Personal email — use `<your-email>` in docs/text or "
     "`test@example.com` in tests."),

    # --- production VM addressing ---
    (r"167\.233\.66\.192",
     "Production VM IPv4 — use `<vm-ip>` placeholder. The real IP belongs "
     "in operator notes (.env, password manager, encrypted vault), not "
     "in a public repo."),
    (r"2a01:4f8:c015:8eb2",
     "Production VM IPv6 — use `<vm-ipv6>` placeholder. Same rationale "
     "as the IPv4 rule above."),
]


# Files allowed to mention these strings (this test itself stores the
# patterns, so it must be exempt). Paths are repo-relative (POSIX style).
ALLOWLIST: set[str] = {
    "tests/test_no_secrets_leaked.py",
}


def _tracked_files() -> list[str]:
    """Return every git-tracked file in the repo, as POSIX paths."""
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


@pytest.fixture(scope="module")
def tracked_files() -> list[str]:
    return _tracked_files()


@pytest.mark.parametrize(
    "pattern,reason",
    FORBIDDEN_PATTERNS,
    ids=[r for r, _ in FORBIDDEN_PATTERNS],
)
def test_forbidden_pattern_not_in_tracked_files(
    pattern: str, reason: str, tracked_files: list[str]
) -> None:
    """Every tracked file (minus the allowlist) must NOT match the pattern.

    Fails with a multi-line message listing every hit so the contributor
    can scrub them in one pass.
    """
    rx = re.compile(pattern)
    hits: list[str] = []
    for rel in tracked_files:
        if rel in ALLOWLIST:
            continue
        path = REPO_ROOT / rel
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except (OSError, UnicodeDecodeError):
            # Binary or unreadable file — skip; we only worry about
            # text files that humans actually edit.
            continue
        for lineno, line in enumerate(content.splitlines(), start=1):
            if rx.search(line):
                hits.append(f"  {rel}:{lineno}: {line.strip()[:120]}")

    assert not hits, (
        f"Forbidden pattern /{pattern}/ found in tracked file(s).\n"
        f"Reason: {reason}\n"
        f"Hits:\n" + "\n".join(hits)
    )


# ──────────────────────────────────────────────────────────────────────────
# Bonus check: catch common-shape secrets even if specific values change.
# Kept conservative so we don't flag false positives (the codebase has
# many legitimate identifiers — token-bucket variables, JWT-like
# placeholders in tests, "AIza"-prefixed Firebase WEB API keys which are
# explicitly NOT secret per Firebase docs, etc.).
# ──────────────────────────────────────────────────────────────────────────
GENERIC_PATTERNS: list[tuple[str, str]] = [
    # Anthropic API keys (real ones start with sk-ant-)
    (r"\bsk-ant-[A-Za-z0-9_-]{30,}",
     "Anthropic API key shape — move to .env"),
    # OpenAI session-style keys (sk- followed by alnum, not sk-ant-)
    (r"\bsk-(?!ant-)[A-Za-z0-9]{30,}",
     "OpenAI-style API key shape — move to .env"),
    # GitHub PATs
    (r"\bgh[poushr]_[A-Za-z0-9]{30,}",
     "GitHub PAT shape — move to .env / GH Secrets"),
    # Slack tokens
    (r"\bxox[abprs]-[A-Za-z0-9-]{20,}",
     "Slack token shape — move to .env"),
    # Telegram bot tokens (Negev's Firebase web-API "AIza…" is allowed; this
    # is the NNNNNN:XXX… bot format)
    (r"\b\d{8,11}:[A-Za-z0-9_-]{30,}",
     "Telegram bot-token shape — move to .env"),
    # PEM / private-key block headers
    (r"-----BEGIN (?:RSA |DSA |EC |OPENSSH )?PRIVATE KEY-----",
     "Private-key block — must never live in the repo, even gitignored. "
     "Move to .ssh/ outside the repo or a secrets manager."),
]


@pytest.mark.parametrize(
    "pattern,reason",
    GENERIC_PATTERNS,
    ids=[r[:40] for r, _ in GENERIC_PATTERNS],
)
def test_generic_secret_shapes_absent(
    pattern: str, reason: str, tracked_files: list[str]
) -> None:
    """Catch shape-based leaks of new credentials we haven't seen before."""
    rx = re.compile(pattern)
    hits: list[str] = []
    for rel in tracked_files:
        if rel in ALLOWLIST:
            continue
        # .env.example is allowed to demonstrate placeholder shapes if any.
        # The placeholder convention in this repo is `KEY=` (empty) or
        # `KEY=your_*_key`, neither of which matches the regexes above —
        # but skip the file from this check for safety.
        if rel == ".env.example":
            continue
        path = REPO_ROOT / rel
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(content.splitlines(), start=1):
            if rx.search(line):
                hits.append(f"  {rel}:{lineno}: {line.strip()[:120]}")

    assert not hits, (
        f"Generic secret shape /{pattern}/ found.\n"
        f"Reason: {reason}\n"
        f"Hits:\n" + "\n".join(hits)
    )


def test_env_file_is_not_tracked(tracked_files: list[str]) -> None:
    """Explicit guard: no variant of .env should ever be in git ls-files."""
    leaks = [
        f for f in tracked_files
        if Path(f).name == ".env"
        or Path(f).name.startswith(".env.")
        and Path(f).name not in {".env.example"}
    ]
    assert not leaks, (
        f"A .env-shaped file is tracked: {leaks}. Only .env.example may be "
        f"committed; everything else holds secrets."
    )
