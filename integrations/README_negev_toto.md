# Negev Toto MCP connector

Reads (and optionally edits) your friends' Toto app (negev-toto.web.app), which is
a Firebase Auth + Cloud Firestore app. Signs in as YOU and exposes Firestore.

## Tools
- `toto_ping` — sign in + list collections (run first, to discover the schema)
- `toto_read_collection(collection)` — read a collection (standings, matches, broadBets, sideBets, users…)
- `toto_get_document(path)` — read one document
- `toto_query(collection, field, op, value)` — filtered read
- `toto_patch_document(path, fields_json)` — edit (preferences/picks); OFF unless NEGEV_ALLOW_WRITES=1

## Setup
```bash
pip install "mcp[cli]" requests
```
`.env` (or shell exports) — apiKey + projectId are the PUBLIC Firebase web config:
```
NEGEV_API_KEY=AIza...
NEGEV_PROJECT_ID=negev-toto
NEGEV_EMAIL=you@example.com
NEGEV_PASSWORD=********        # local only, never in chat/git
NEGEV_ALLOW_WRITES=0           # set 1 only when you want editing enabled
```

## Register with Claude Desktop / Cowork
Add to your MCP config (e.g. claude_desktop_config.json):
```json
{
  "mcpServers": {
    "negev-toto": {
      "command": "python",
      "args": ["-m", "integrations.negev_toto_mcp"],
      "cwd": "/ABSOLUTE/PATH/TO/mondial2026",
      "env": {
        "NEGEV_API_KEY": "AIza...", "NEGEV_PROJECT_ID": "negev-toto",
        "NEGEV_EMAIL": "you@example.com", "NEGEV_PASSWORD": "********"
      }
    }
  }
}
```
Then ask Claude: "run toto_ping" to discover the collections, and we map the
typed tools (get_standings / get_broad_bets / get_side_bets) to the real paths.

## Pipeline use (optional)
The same Firestore client can be imported by the prediction system so
`get_results` feeds Day-5 scoring, `get_standings` feeds the strategy layer, and
the matches' official odds become the EV multiplier.
