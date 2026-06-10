"""Day-9.23: .env hygiene + Negev auth probe.

Two checks designed for daily cron:

  1. Scan .env for the systemd inline-comment trap that bit us on
     2026-06-10 (NEGEV_EMAIL had an inline `# comment` that systemd's
     EnvironmentFile parser didn't strip → Firebase rejected as
     INVALID_EMAIL → silent daily-summary degradation for 15 hours).

  2. Probe Negev auth (one cheap Firestore call) to confirm the daemon's
     refresh-token actually still works. Catches a stale refresh token
     BEFORE it causes a real card to ship empty.

Both checks LOUDLY (Telegram alert via `--telegram`) on failure. Silent
on success. Designed to be safe to run alongside the existing crons.

  PYTHONPATH=. .venv/bin/python tools/audit_env.py
  PYTHONPATH=. .venv/bin/python tools/audit_env.py --telegram
"""
from __future__ import annotations
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_ENV_PATH = "/home/mondial/mondial2026/.env"


def _scan_env_file(path: str) -> list[tuple[int, str, str]]:
    """Return [(lineno, varname, full_line)] for every line whose value carries
    a systemd-hazardous inline `# comment`. Skips comment-only lines + blank
    lines + `export FOO=…` re-exports."""
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for i, raw in enumerate(f, 1):
                line = raw.rstrip("\n")
                stripped = line.lstrip()
                if not stripped or stripped.startswith("#"):
                    continue
                # Only lines that look like KEY=value
                m = re.match(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
                if not m:
                    continue
                key, value = m.group(1), m.group(2)
                # Strip the value's quoted form to find inline `#`.
                # Inline-comment hazard = whitespace + `#` somewhere in `value`.
                if re.search(r"\s+#", value):
                    out.append((i, key, line))
    except FileNotFoundError:
        pass
    return out


def _scan_indented_assignments(path: str) -> list[tuple[int, str]]:
    """Day-9.24: lines that LOOK like KEY=value but start with whitespace.
    bash `source` will fail with 'command not found'; systemd silently skips.
    Both cases mask the assignment from the daemon. Catches heredoc-indent
    bugs like the 2026-06-10 STRATEGY_OVERRIDES corruption."""
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for i, raw in enumerate(f, 1):
                line = raw.rstrip("\n")
                if re.match(r"^[ \t]+(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*=", line):
                    out.append((i, line))
    except FileNotFoundError:
        pass
    return out


def _scan_stray_lines(path: str) -> list[tuple[int, str]]:
    """Day-9.24: non-blank, non-comment, non-KEY=VALUE lines. These are
    typically heredoc leftovers (literal 'EOF') or stray script text that
    bash will misparse on `source`."""
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for i, raw in enumerate(f, 1):
                line = raw.rstrip("\n")
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                # If it's a valid KEY=VALUE line (possibly with leading
                # whitespace), the other scanners handle it. Stray = doesn't
                # match KEY=VALUE at all.
                if re.match(r"^[ \t]*(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*=",
                             line):
                    continue
                out.append((i, line))
    except FileNotFoundError:
        pass
    return out


def _scan_duplicate_keys(path: str) -> list[tuple[str, list[int]]]:
    """Day-9.24: duplicate KEY= lines (case-sensitive). Last-wins for bash
    `source` but systemd's behaviour depends on version. Either way the
    operator probably intended only one."""
    seen: dict[str, list[int]] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for i, raw in enumerate(f, 1):
                line = raw.rstrip("\n")
                m = re.match(r"^[ \t]*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=",
                              line)
                if not m:
                    continue
                seen.setdefault(m.group(1), []).append(i)
    except FileNotFoundError:
        pass
    return [(k, lines) for k, lines in seen.items() if len(lines) > 1]


def _probe_negev_auth() -> tuple[bool, str]:
    """Return (ok, reason). One cheap Firestore call via the connector to
    confirm refresh-token auth works end-to-end RIGHT NOW."""
    try:
        from integrations import negev_toto_mcp as ntm
        # _id_token() runs the refresh path + caches; raises on failure
        # (Day-9.23: no silent fallback unless NEGEV_ALLOW_PASSWORD_FALLBACK=1)
        ntm._id_token()
        return True, "ok"
    except Exception as e:                                # noqa: BLE001
        return False, str(e)[:240]


def _alert(title: str, body: str) -> bool:
    try:
        from core import delivery
        return bool(delivery.alert(title, body))
    except Exception as e:                                # noqa: BLE001
        print(f"  Telegram alert failed: {e}", file=sys.stderr)
        return False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="audit_env")
    p.add_argument("--env-path", default=DEFAULT_ENV_PATH,
                   help=f"Path to .env (default {DEFAULT_ENV_PATH})")
    p.add_argument("--telegram", action="store_true",
                   help="On any failure, fire ⚠ via delivery.alert")
    p.add_argument("--skip-auth", action="store_true",
                   help="Skip the live Negev auth probe (saves 1 API call)")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    if not args.quiet:
        print()
        print(f"  ╔════════════════════════════════════════════════════════════╗")
        print(f"  ║  .env hygiene + Negev auth probe")
        print(f"  ╚════════════════════════════════════════════════════════════╝")
        print()
        print(f"  Scanning: {args.env_path}")

    leaks = _scan_env_file(args.env_path)
    indented = _scan_indented_assignments(args.env_path)
    strays = _scan_stray_lines(args.env_path)
    dupes = _scan_duplicate_keys(args.env_path)

    issues = bool(leaks or indented or strays or dupes)
    if issues:
        if not args.quiet:
            if leaks:
                print(f"  ⚠ {len(leaks)} line(s) with inline-comment hazard:")
                for lineno, key, line in leaks[:10]:
                    print(f"    line {lineno}  {key}:  {line[:80]}")
            if indented:
                print(f"  ⚠ {len(indented)} indented KEY=VALUE line(s) "
                      f"(bash 'command not found', systemd skips silently):")
                for lineno, line in indented[:10]:
                    print(f"    line {lineno}:  {line[:80]}")
            if strays:
                print(f"  ⚠ {len(strays)} stray non-assignment line(s) "
                      f"(heredoc leftover? bash will misparse):")
                for lineno, line in strays[:10]:
                    print(f"    line {lineno}:  {line[:80]}")
            if dupes:
                print(f"  ⚠ {len(dupes)} duplicate KEY=VALUE line(s):")
                for key, lines in dupes[:10]:
                    print(f"    {key}  on lines {lines}")
        if args.telegram:
            body_parts = []
            if leaks:
                body_parts.append("INLINE-COMMENT HAZARDS:\n" +
                                  "\n".join(f"  line {l}: {k}"
                                              for l, k, _ in leaks[:5]))
            if indented:
                body_parts.append("INDENTED ASSIGNMENTS (bash chokes):\n" +
                                  "\n".join(f"  line {l}: {ln[:60]}"
                                              for l, ln in indented[:5]))
            if strays:
                body_parts.append("STRAY LINES (heredoc leftovers?):\n" +
                                  "\n".join(f"  line {l}: {ln[:60]}"
                                              for l, ln in strays[:5]))
            if dupes:
                body_parts.append("DUPLICATE KEYS:\n" +
                                  "\n".join(f"  {k} on lines {ls}"
                                              for k, ls in dupes[:5]))
            body = (".env contains lines that will be silently mis-parsed:\n\n"
                    + "\n\n".join(body_parts)
                    + "\n\nFix manually + systemctl restart mondial2026.")
            _alert(".env hygiene issues detected", body)
        return 1

    if not args.quiet:
        print(f"  ✓ No inline-comment hazards, no indented assignments, "
              f"no stray lines, no duplicate keys.")

    if not args.skip_auth:
        if not args.quiet:
            print(f"\n  Probing Negev auth (one cheap Firestore call)...")
        ok, reason = _probe_negev_auth()
        if not ok:
            print(f"  ✗ Negev auth FAILED: {reason}")
            if args.telegram:
                _alert("Negev auth probe failed",
                       f"Daily env audit cannot reach Negev:\n\n{reason}\n\n"
                       f"Most likely: NEGEV_REFRESH_TOKEN expired or "
                       f"rotation desynced. Re-capture from "
                       f"negev-toto.web.app DevTools (IndexedDB → "
                       f"firebaseLocalStorageDb → stsTokenManager."
                       f"refreshToken), paste into .env, "
                       f"systemctl restart mondial2026.")
            return 1
        if not args.quiet:
            print(f"  ✓ Negev auth working.")

    if not args.quiet:
        print(f"\n  ✓ All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
