#!/usr/bin/env bash
# One-shot setup for the Negev Toto MCP connector.
# Run on YOUR Mac:  bash integrations/setup_negev.sh
# It installs deps, tests your login, and (optionally) registers the connector
# with Claude Desktop. Your password is read into a local prompt — never stored
# in chat or in this repo.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

echo "==> 1/4  Installing packages (mcp + requests)…"
python3 -m pip install -q "mcp[cli]" requests

echo "==> 2/4  Your Negev Toto login (typed locally, not sent anywhere)"
read -r -p "    email: " NEGEV_EMAIL
read -r -s -p "    password: " NEGEV_PASSWORD; echo
export NEGEV_EMAIL NEGEV_PASSWORD

echo "==> 3/4  Testing sign-in + listing your collections…"
if PYTHONPATH="$REPO" python3 -c "
import json,sys
from integrations.negev_toto_mcp import toto_ping
r=toto_ping(); print(json.dumps(r,indent=2))
sys.exit(0 if (r.get('signed_in_as_uid') or r.get('collections')) else 1)
"; then
  echo "    ✓ login works."
else
  echo "    ✗ sign-in failed — check email/password and try again."; exit 1
fi

echo "==> 4/4  Register with Claude Desktop?"
read -r -p "    write the Claude config now? [y/N] " yn
if [[ "${yn:-N}" =~ ^[Yy]$ ]]; then
  PYTHONPATH="$REPO" python3 - "$REPO" "$NEGEV_EMAIL" "$NEGEV_PASSWORD" <<'PY'
import json, os, sys
repo, email, pw = sys.argv[1], sys.argv[2], sys.argv[3]
d = os.path.expanduser("~/Library/Application Support/Claude")
os.makedirs(d, exist_ok=True)
p = os.path.join(d, "claude_desktop_config.json")
cfg = json.load(open(p)) if os.path.exists(p) else {}
cfg.setdefault("mcpServers", {})["negev-toto"] = {
    "command": "python3", "args": ["-m", "integrations.negev_toto_mcp"], "cwd": repo,
    "env": {"NEGEV_EMAIL": email, "NEGEV_PASSWORD": pw}}
json.dump(cfg, open(p, "w"), indent=2)
print(f"    ✓ registered in {p}")
PY
  echo "    ➜ Fully QUIT and reopen Claude, then say: run toto_ping"
else
  echo "    Skipped. To register later, copy integrations/claude_desktop_config.json"
  echo "    into ~/Library/Application Support/Claude/claude_desktop_config.json"
fi
echo "Done."
