"""Negev Toto MCP connector — read (and optionally edit) your friends' Toto app.

The app (negev-toto.web.app) is a Firebase app: Firebase Auth (email/password) +
Cloud Firestore. This server signs in with YOUR account and exposes Firestore via
clean tools, so Claude can read standings, broad bets, side bets, matches and your
settings — and (only if you opt in) edit your preferences/picks.

Security: credentials come from environment variables ONLY (never hard-coded).
Writes are OFF unless you set NEGEV_ALLOW_WRITES=1 — reads are always safe.

Run (local stdio):
    pip install "mcp[cli]" requests
    export NEGEV_API_KEY=...        # public Firebase web apiKey (from the JS config)
    export NEGEV_PROJECT_ID=...     # e.g. negev-toto
    export NEGEV_EMAIL=...          # your login email
    export NEGEV_PASSWORD=...       # your login password
    python -m integrations.negev_toto_mcp     # or register it (see README)
"""
from __future__ import annotations
import json
import os
import time
import requests

# The `mcp` package is only needed when this module is RUN as an MCP server
# (stdio transport, `python -m integrations.negev_toto_mcp`). When imported
# as a LIBRARY (e.g. by tools/sync_negev_standings.py or the test suite),
# we don't need the FastMCP runtime — so make the import optional and
# stub @mcp.tool() to a passthrough decorator. This keeps the VM venv lean
# (no `mcp[cli]` dependency required for the cron sync).
try:
    from mcp.server.fastmcp import FastMCP   # type: ignore
    mcp = FastMCP("negev-toto")
except ImportError:                          # noqa: BLE001
    class _StubMCP:                          # pragma: no cover (covered by serve mode)
        """Pass-through stub so @mcp.tool() decorators no-op when MCP isn't
        installed. The module remains usable as a plain library."""
        def tool(self, *a, **kw):
            def deco(fn):
                return fn
            return deco
        def run(self):
            raise RuntimeError(
                "Install 'mcp[cli]' to run this as an MCP server "
                "(`.venv/bin/pip install \"mcp[cli]\"`)."
            )
    mcp = _StubMCP()

IDENTITY = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
REFRESH = "https://securetoken.googleapis.com/v1/token"
# Public Firebase web config (from negev-toto.web.app/__/firebase/init.json).
# Not secret — shipped to every browser; security is enforced by auth + rules.
DEFAULT_API_KEY = "AIzaSyDID-UVdaQ3v8zeyT-3uk8ToVOhcrFCdlg"
DEFAULT_PROJECT_ID = "negev-toto"
_token = {"id": None, "refresh": None, "exp": 0.0, "uid": None}


def _api_key() -> str:
    return os.environ.get("NEGEV_API_KEY", DEFAULT_API_KEY)


def _project() -> str:
    return os.environ.get("NEGEV_PROJECT_ID", DEFAULT_PROJECT_ID)


def _cfg(name: str) -> str:
    """For the only things YOU must provide: your login email + password."""
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"Missing env {name}. Set NEGEV_EMAIL and NEGEV_PASSWORD "
                           f"(your normal Toto login) in your .env / MCP config.")
    return v


def _id_token() -> str:
    """Return a valid Firebase ID token, signing in or refreshing as needed.

    Two seeding paths:
      (a) email/password (NEGEV_EMAIL + NEGEV_PASSWORD): signInWithPassword.
      (b) Google-Sign-In path: paste a Firebase refresh token captured from the
          browser (NEGEV_REFRESH_TOKEN). The connector exchanges it for an
          ID token via the secure-token refresh endpoint. The refresh token
          auto-rotates on each refresh; cached in-memory afterwards.

    Day-9.23 hardening: when NEGEV_REFRESH_TOKEN is set, that path is the
    OPERATOR'S EXPLICIT CHOICE. A refresh failure raises LOUDLY with a clear
    remediation hint. Silent fall-through to email/password masks token-
    rotation issues and is now opt-in only via
    `NEGEV_ALLOW_PASSWORD_FALLBACK=1` (default 0).

    Why this matters (real incident, 2026-06-10):
      A long-lived daemon process was started with .env containing inline
      `#` comments. systemd's EnvironmentFile parser does NOT strip inline
      comments, so the daemon's `NEGEV_EMAIL` literal value was
      `<your-email>   # your Negev Toto login email`. When the
      refresh-token's cached rotation drifted out-of-sync after ~15 hours
      of uptime, the connector silently fell through to email auth, which
      Firebase rejected with INVALID_EMAIL — and the daily summary
      degraded to the legacy DB line, undetected. Loud failure forces the
      operator to notice + fix immediately.
    """
    if _token["id"] and time.time() < _token["exp"] - 60:
        return _token["id"]
    key = _api_key()
    # Seed the refresh token from env on first call (Google-Sign-In path).
    if not _token["refresh"]:
        env_rt = os.environ.get("NEGEV_REFRESH_TOKEN", "").strip()
        if env_rt:
            _token["refresh"] = env_rt
    if _token["refresh"]:
        # Day-9.25: wrap auth refresh in obs.external_call so token-rotation
        # latency + failure counts land in the api_calls ledger like every
        # other Negev call. Uses requests.post directly (not _fs) since the
        # auth endpoint isn't a Firestore URL; we still want it on the same
        # provider name "negev_toto" for budget aggregation.
        try:
            from core import obs as _obs_auth
            _auth_wrap = _obs_auth.external_call("negev_toto", "auth:refresh", units=1)
        except Exception:                                  # noqa: BLE001
            from contextlib import nullcontext as _nullcontext_auth
            _auth_wrap = _nullcontext_auth()
        with _auth_wrap:
            r = requests.post(f"{REFRESH}?key={key}", timeout=20,
                              data={"grant_type": "refresh_token", "refresh_token": _token["refresh"]})
        if r.ok:
            d = r.json()
            _token.update(id=d["id_token"], refresh=d["refresh_token"],
                          uid=d.get("user_id") or _token.get("uid"),
                          exp=time.time() + int(d.get("expires_in", 3600)))
            return _token["id"]
        # Day-9.23: refresh failed. Operator chose refresh-token auth; default
        # is loud failure with a remediation message. Only fall through to
        # email/password if explicitly opted-in.
        fallback_ok = (os.environ.get("NEGEV_ALLOW_PASSWORD_FALLBACK", "0").strip() == "1"
                       and os.environ.get("NEGEV_EMAIL")
                       and os.environ.get("NEGEV_PASSWORD"))
        if not fallback_ok:
            raise RuntimeError(
                f"Firebase refresh failed ({r.status_code}): {r.text[:160]}. "
                "Your NEGEV_REFRESH_TOKEN likely expired or rotation desynced — "
                "re-capture it from negev-toto.web.app DevTools (IndexedDB → "
                "firebaseLocalStorageDb → stsTokenManager.refreshToken), paste "
                "into .env, and restart the daemon. To intentionally allow "
                "silent fallback to email/password, set "
                "NEGEV_ALLOW_PASSWORD_FALLBACK=1 (NOT recommended — masks "
                "token-rotation drift).")
        _token["refresh"] = None      # explicit opt-in: let the password path try
    try:
        from core import obs as _obs_signin
        _signin_wrap = _obs_signin.external_call("negev_toto", "auth:signin", units=1)
    except Exception:                                  # noqa: BLE001
        from contextlib import nullcontext as _nullcontext_signin
        _signin_wrap = _nullcontext_signin()
    with _signin_wrap:
        r = requests.post(f"{IDENTITY}?key={key}", timeout=20, json={
            "email": _cfg("NEGEV_EMAIL"), "password": _cfg("NEGEV_PASSWORD"),
            "returnSecureToken": True})
    if not r.ok:
        raise RuntimeError(f"Firebase sign-in failed ({r.status_code}): {r.text[:200]}")
    d = r.json()
    _token.update(id=d["idToken"], refresh=d["refreshToken"], uid=d["localId"],
                  exp=time.time() + int(d.get("expiresIn", 3600)))
    return _token["id"]


def _base() -> str:
    return (f"https://firestore.googleapis.com/v1/projects/{_project()}"
            f"/databases/(default)/documents")


def _headers() -> dict:
    return {"Authorization": f"Bearer {_id_token()}"}


def _fs(method: str, url: str, *, endpoint: str, **kw):
    """Day-9.25: every Firestore call goes through `obs.external_call` so the
    shared rate-limit bucket gates it AND the cost ledger records it with a
    correlation_id (Honeycomb sees the span under the active stage:negev).
    Before this, raw `requests.get/post/patch` in this module would skip
    rate-limiting unless the CALL SITE wrapped them. A new tool calling
    `toto_*` would silently bypass instrumentation — a pure observability
    hole. Endpoint label tells you which logical Firestore endpoint per row
    (e.g. 'firestore:get_document', 'firestore:runQuery').

    Dispatch via the named `requests.<verb>` functions (not `request()`) so
    existing tests that monkeypatch `requests.get/post/patch` keep working
    without modification."""
    try:
        from core import obs as _obs
        wrap = _obs.external_call("negev_toto", endpoint, units=1)
    except Exception:                                      # noqa: BLE001
        from contextlib import nullcontext
        wrap = nullcontext()
    verb = method.upper()
    fn = {"GET": requests.get, "POST": requests.post,
          "PATCH": requests.patch, "PUT": requests.put,
          "DELETE": requests.delete}.get(verb)
    if fn is None:
        raise ValueError(f"unsupported HTTP verb: {method!r}")
    with wrap:
        return fn(url, **kw)


def _decode(v: dict):
    """Firestore typed value -> plain Python."""
    if "stringValue" in v: return v["stringValue"]
    if "integerValue" in v: return int(v["integerValue"])
    if "doubleValue" in v: return float(v["doubleValue"])
    if "booleanValue" in v: return v["booleanValue"]
    if "timestampValue" in v: return v["timestampValue"]
    if "nullValue" in v: return None
    if "referenceValue" in v: return v["referenceValue"]
    if "mapValue" in v: return {k: _decode(x) for k, x in v["mapValue"].get("fields", {}).items()}
    if "arrayValue" in v: return [_decode(x) for x in v["arrayValue"].get("values", [])]
    return v


def _doc(d: dict) -> dict:
    out = {k: _decode(x) for k, x in d.get("fields", {}).items()}
    out["_path"] = d.get("name", "").split("/databases/(default)/documents/")[-1]
    return out


def _encode(val):
    if isinstance(val, bool): return {"booleanValue": val}
    if isinstance(val, int): return {"integerValue": str(val)}
    if isinstance(val, float): return {"doubleValue": val}
    if isinstance(val, str): return {"stringValue": val}
    if val is None: return {"nullValue": None}
    if isinstance(val, list): return {"arrayValue": {"values": [_encode(x) for x in val]}}
    if isinstance(val, dict): return {"mapValue": {"fields": {k: _encode(x) for k, x in val.items()}}}
    return {"stringValue": str(val)}


# ---------------- READ TOOLS (always safe) ----------------
@mcp.tool()
def toto_ping() -> dict:
    """Sign in and list the top-level Firestore collections — run this FIRST to
    discover the data model (standings, broad bets, side bets, etc.)."""
    r = _fs("POST", f"{_base()}:listCollectionIds", endpoint="firestore:listCollectionIds",
            headers=_headers(), json={}, timeout=20)
    cols = r.json().get("collectionIds", []) if r.ok else []
    return {"signed_in_as_uid": _token.get("uid"), "collections": cols,
            "note": None if r.ok else f"listCollectionIds blocked ({r.status_code}); "
            "capture exact collection names from the app's network tab instead."}


@mcp.tool()
def toto_read_collection(collection: str, page_size: int = 50) -> dict:
    """Read documents from a top-level collection (e.g. 'standings', 'matches',
    'broadBets', 'sideBets', 'users'). Returns decoded documents."""
    r = _fs("GET", f"{_base()}/{collection}", endpoint="firestore:read_collection",
            headers=_headers(), params={"pageSize": page_size}, timeout=20)
    if not r.ok:
        return {"error": f"{r.status_code}: {r.text[:200]}", "collection": collection}
    return {"collection": collection, "documents": [_doc(d) for d in r.json().get("documents", [])]}


@mcp.tool()
def toto_get_document(path: str) -> dict:
    """Read one document by its Firestore path (e.g. 'standings/<id>' or
    'users/<uid>/settings/preferences')."""
    r = _fs("GET", f"{_base()}/{path}", endpoint="firestore:get_document",
            headers=_headers(), timeout=20)
    if not r.ok:
        return {"error": f"{r.status_code}: {r.text[:200]}", "path": path}
    return _doc(r.json())


@mcp.tool()
def toto_query(collection: str, field: str, op: str, value: str, limit: int = 50) -> dict:
    """Filter a collection. op is one of EQUAL, GREATER_THAN, LESS_THAN,
    ARRAY_CONTAINS, etc. (Firestore structured-query operators)."""
    body = {"structuredQuery": {
        "from": [{"collectionId": collection}],
        "where": {"fieldFilter": {"field": {"fieldPath": field}, "op": op,
                                  "value": _encode(value)}},
        "limit": limit}}
    r = _fs("POST", f"{_base()}:runQuery", endpoint="firestore:runQuery",
            headers=_headers(), json=body, timeout=20)
    if not r.ok:
        return {"error": f"{r.status_code}: {r.text[:200]}"}
    return {"results": [_doc(row["document"]) for row in r.json() if "document" in row]}


# ---------------- WRITE TOOL (opt-in only) ----------------
@mcp.tool()
def toto_patch_document(path: str, fields_json: str) -> dict:
    """Update fields on one document (e.g. your notification preferences or a pick).
    DISABLED unless NEGEV_ALLOW_WRITES=1. `fields_json` is a JSON object of the
    fields to set, e.g. '{"newSideBets": false}'."""
    if os.environ.get("NEGEV_ALLOW_WRITES") != "1":
        return {"error": "writes disabled. Set NEGEV_ALLOW_WRITES=1 to enable editing."}
    fields = json.loads(fields_json)
    params = [("updateMask.fieldPaths", k) for k in fields]
    body = {"fields": {k: _encode(v) for k, v in fields.items()}}
    r = _fs("PATCH", f"{_base()}/{path}", endpoint="firestore:patch_document",
            headers=_headers(), params=params, json=body, timeout=20)
    if not r.ok:
        return {"error": f"{r.status_code}: {r.text[:200]}", "path": path}
    return {"updated": path, "fields": list(fields)}


# ─────────────────────────────────────────────────────────────────────────────
# Typed convenience tools (Step 2 per CLAUDE_CODE_HANDOFF_negev.md)
#
# These are thin wrappers over the 5 generic tools above. They live alongside
# the generic ones so power-users can still drop down to raw Firestore when
# something new appears in the schema.
#
# `tournament_id` is a parameter on every typed tool — never hard-coded —
# so the connector works against any pool without code changes. When not
# passed, falls back to NEGEV_TOURNAMENT_ID env var. Our live tournament is
# "Negev Toto 2026" (id n40ykJlOIA9Mg839hz91, top-10 paid — matches
# config/rules.py::PRIZE_LADDER exactly).
# ─────────────────────────────────────────────────────────────────────────────

# Stage label mapping: Negev free-text → our RULES_STAGE labels (config/rules.py).
# Source: live probe of Negev matches collection + manual mapping by 48-team
# bracket structure.
_STAGE_MAP = {
    "Group Stage - 1": "Group", "Group Stage - 2": "Group", "Group Stage - 3": "Group",
    "Group Stage": "Group",
    "Round of 32": "R32", "Round of 16": "R16",
    "Quarter-finals": "QF", "Quarter Finals": "QF",
    "Semi-finals": "SF", "Semi Finals": "SF",
    "3rd Place Final": "3rd", "Third-place Play-off": "3rd",
    "Final": "Final",
}


def _tid(tournament_id: str | None) -> str:
    """Resolve the tournament id from arg or NEGEV_TOURNAMENT_ID env var."""
    tid = tournament_id or os.environ.get("NEGEV_TOURNAMENT_ID", "").strip()
    if not tid:
        raise RuntimeError(
            "tournament_id required. Pass it explicitly or set NEGEV_TOURNAMENT_ID "
            "in your .env (look it up via toto_list_tournaments)."
        )
    return tid


def _read_all(collection: str, page_size: int = 100,
              http_get=None) -> list[dict]:
    """Read all docs from a collection with pagination via Firestore's
    nextPageToken. http_get is injectable so tests can mock without network.

    Day-9.25: when no http_get is injected (production path), each HTTP page
    is wrapped in obs.external_call so the rate-limit bucket gates every
    page request individually. With http_get injected (tests), the wrap is
    skipped — the test owns instrumentation."""
    docs: list[dict] = []
    page_token = None
    while True:
        params = {"pageSize": page_size}
        if page_token:
            params["pageToken"] = page_token
        if http_get is not None:
            r = http_get(f"{_base()}/{collection}", headers=_headers(),
                          params=params, timeout=20)
        else:
            r = _fs("GET", f"{_base()}/{collection}",
                     endpoint="firestore:read_all_paged",
                     headers=_headers(), params=params, timeout=20)
        if not r.ok:
            return docs                                # caller may inspect
        data = r.json()
        docs.extend(_doc(d) for d in data.get("documents", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            return docs


@mcp.tool()
def toto_list_tournaments() -> list[dict]:
    """Discover every tournament id referenced by any readable user, then
    fetch its name + prize pool if accessible to us. Use this to find the
    Negev Toto 2026 id once if you've forgotten it. Returns sorted by
    descending prize pool so the real one is first."""
    users = _read_all("users")
    tids: set[str] = set()
    for u in users:
        for t in u.get("tournaments", []) or []:
            tids.add(t)
    out = []
    for tid in tids:
        t = toto_get_document(f"tournaments/{tid}")
        if "error" in t:
            out.append({"id": tid, "accessible": False, "error": t["error"][:80]})
            continue
        settings = t.get("settings") or {}
        out.append({
            "id": tid,
            "name": t.get("name"),
            "accessible": True,
            "prize_pool": settings.get("totalPrizePool"),
            "created_at": t.get("createdAt"),
            "last_rank_snapshot": t.get("lastRankSnapshot"),
        })
    return sorted(out, key=lambda x: -(x.get("prize_pool") or 0))


def _is_bot(u: dict) -> bool:
    """Triple-redundant bot detection. The Negev Toto app currently has 3 bots
    (The Chinchilla, The Monkey, The Owl) — each carries ALL three signals:

      * role == "bot"
      * isBot == True
      * uid starts with "bot_"

    We OR all three so future bots that drop one signal (e.g. forget the
    role field) are still caught. Bots are pure entertainment in the app —
    they auto-pick and their position is decorative. Excluding them from
    OUR standings is required for the strategy layer's leader_gap math
    to be correct.
    """
    if u.get("role") == "bot":
        return True
    if u.get("isBot") is True:
        return True
    uid = u.get("uid") or ""
    if isinstance(uid, str) and uid.startswith("bot_"):
        return True
    return False


def _load_side_bet_overrides(tid: str) -> dict:
    """Day-9.26: read the operator's manual side-bet override file.

    Negev stores per-user side-bet picks at a Firestore path our
    regular-user auth token cannot read (403 across every probed
    convention). So the operator maintains a small JSON file mapping
    `displayName → cumulative_side_bet_points`, updated after each
    match-day to mirror the app.

    File path: `store/side_bet_overrides_<tid>.json` (gitignored).
    Shape:
      {"users": {"Igor": 1.0, "Vaadia": 1.0, ...},
       "captured_at": "2026-06-12T18:30:00+00:00",
       "notes": "post Mexico v SA side bet"}

    Returns {} when file missing → side stays 0 for everyone (under-
    reported but no false data). Missing file path is safe; bad JSON
    logs a warning and falls back to {} so a typo can't break the sync.
    """
    import json
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, "store", f"side_bet_overrides_{tid}.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        users = data.get("users") or {}
        return {k: float(v) for k, v in users.items()
                if isinstance(v, (int, float))}
    except Exception as e:                                 # noqa: BLE001
        import logging
        logging.getLogger("negev_toto").warning(
            "side-bet override file %s exists but failed to load: %s",
            path, e)
        return {}


@mcp.tool()
def toto_get_side_bet_voters(side_bet_id: str,
                              tournament_id: str | None = None) -> dict:
    """Day-9.27: read who voted Yes vs No on a specific side bet.

    Reads `tournaments/{tid}/sideBetAnswers` (one doc per user with an
    `answers: {sb_id: 'Yes'|'No'}` map) + the side-bet shell to get the
    correct answer.

    Returns:
      {
        "side_bet_id": "sb_2026-06-11",
        "question": "Mexico - South Africa total goals over 2.5",
        "correct_answer": "No" | "Yes" | None,
        "is_resolved": True | False,
        "yes_voters": [{"player": "Vaadia", "uid": "..."}, ...],
        "no_voters":  [{"player": "Gilad Cain", "uid": "..."}, ...],
        "yes_count": 22, "no_count": 40,
        "yes_winners": [...],  # who got pts (correctAnswer == 'Yes')
        "no_winners":  [...],  # who got pts (correctAnswer == 'No')
      }
    """
    tid = _tid(tournament_id)
    shell = toto_get_document(
        f"tournaments/{tid}/sideBets/{side_bet_id}")
    if isinstance(shell, dict) and shell.get("error"):
        return {"error": shell["error"], "side_bet_id": side_bet_id}

    users = _read_all("users")
    uid_to_name = {u.get("uid"): u.get("displayName") or u.get("uid")
                    for u in users if u.get("uid")}

    answer_docs = _read_all(f"tournaments/{tid}/sideBetAnswers")
    yes_voters, no_voters = [], []
    for d in answer_docs:
        uid = d.get("userId")
        ans = (d.get("answers") or {}).get(side_bet_id)
        if ans == "Yes":
            yes_voters.append({"player": uid_to_name.get(uid, uid), "uid": uid})
        elif ans == "No":
            no_voters.append({"player": uid_to_name.get(uid, uid), "uid": uid})
    correct = shell.get("correctAnswer") if isinstance(shell, dict) else None
    out = {
        "side_bet_id": side_bet_id,
        "question": shell.get("question") if isinstance(shell, dict) else None,
        "correct_answer": correct,
        "is_resolved": bool(shell.get("isResolved")) if isinstance(shell, dict) else False,
        "yes_voters": sorted(yes_voters, key=lambda x: x["player"]),
        "no_voters": sorted(no_voters, key=lambda x: x["player"]),
        "yes_count": len(yes_voters),
        "no_count": len(no_voters),
    }
    if correct == "Yes":
        out["winners"] = out["yes_voters"]
        out["winner_count"] = out["yes_count"]
    elif correct == "No":
        out["winners"] = out["no_voters"]
        out["winner_count"] = out["no_count"]
    return out


def _build_rows_from_stats(tid: str, tid_stats: list[dict],
                             uid_to_user: dict,
                             include_bots: bool = True,
                             extended: bool = False) -> list[dict]:
    """Day-9.27: assemble the standings rows from precomputed
    tournamentStats docs. This is what the Negev SPA does itself when it
    renders the standings page — we just mirror it byte-for-byte.
    """
    rows = []
    for s in tid_stats:
        uid = s.get("userId")
        u = uid_to_user.get(uid) or {}
        if not include_bots and _is_bot(u):
            continue
        direction = float(s.get("groupGamesPoints") or 0)
        knockout  = float(s.get("koutGamesPoints") or 0)
        side      = float(s.get("sideBetPoints") or 0)
        broad     = float(s.get("broadBetPoints") or 0)
        # Negev's precomputed pointsTotal already includes kodBonus +
        # deductions. Use it directly so we match the app exactly even
        # when those small adjustments are in play.
        total = float(s.get("pointsTotal") or
                       (direction + knockout + side + broad))
        rows.append({
            "player": u.get("displayName") or uid or "?",
            "uid": uid,
            "total":      round(total, 3),
            "direction":  round(direction, 3),
            "knockout":   round(knockout, 3),
            "side":       round(side, 3),
            "broad":      round(broad, 3),
            "exactCount": int(s.get("exactScoreCount") or 0),
            "previousRank": s.get("previousRank"),
            "role": u.get("role"),
            **({"_full": u} if extended and u else {}),
        })
    rows.sort(key=lambda r: (-r["total"], -r["exactCount"], r.get("uid") or ""))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def _load_baseline(tid: str) -> dict:
    """Day-9.16: load the pre-tournament pointsTotal snapshot if it exists.

    Returns {uid: {pointsTotal, directionPoints, broadBetPoints,
    exactScoreCount}} so a caller can compute WC2026-specific contribution
    as `current - baseline`. Returns {} if no snapshot exists yet — caller
    falls back to the Day-9.15 bot-override behaviour.
    """
    import json
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, "store", f"negev_baseline_{tid}.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            snap = json.load(f)
        return snap.get("users") or {}
    except Exception:                                  # noqa: BLE001
        return {}


@mcp.tool()
def toto_get_standings(tournament_id: str | None = None,
                       extended: bool = False,
                       include_bots: bool = True) -> list[dict]:
    """Sorted leaderboard for a tournament: [{rank, player, total, direction,
    knockout, side, broad, exactCount, role, uid}]. Filters users whose
    tournaments[] contains the tid. Ties broken by exactScoreCount desc
    (per PDF §19), then by uid ascending — matches what the Negev web app
    shows when fully tied.

    Day-9.27 BREAKTHROUGH — read from `tournamentStats/{tid}_{uid}` directly.
    Discovered by grepping the Negev SPA's JS bundle: there's a precomputed
    stats doc per user-per-tournament that the app's standings page reads
    verbatim. Fields:
      - pointsTotal      → total
      - groupGamesPoints → direction (group matches)
      - koutGamesPoints  → knockout (KO matches)
      - sideBetPoints    → side (Side Bets column in app)
      - broadBetPoints   → broad (futures, awarded at tournament end)
      - exactScoreCount  → exactCount
      - previousRank     → app's "rank change" arrow
    Live-verified 2026-06-13: Gilad Cain 22.33/20.33/0/2 ✓, Hershko
    17.78/16.78/0/1 ✓, Igor 2.91 ✓ — all match the app exactly.

    Day-9.26 history (replaced by Day-9.27):
      Old approach aggregated bets/ client-side and used a side-bet
      override JSON for the side column. That under-reported side bets
      for non-tracked users and required operator action per resolution.
      Day-9.27 removes the override file requirement entirely: the
      tournamentStats path is fully precomputed, instant, and ZERO
      manual intervention.

    Fallback chain (defensive — handles Negev service degradation):
      1. tournamentStats/{tid}_{uid} (primary) — direct read, no aggregation
      2. If tournamentStats is empty/blocked → fall back to bet aggregation
         from bets/ (the Day-9.26 path), + side override JSON.

    Day-9.15 sort behaviour preserved: total desc, exactCount desc, uid asc.
    """
    tid = _tid(tournament_id)
    users = _read_all("users")
    uid_to_user = {u.get("uid"): u for u in users if u.get("uid")}

    # ── PRIMARY: read precomputed tournamentStats (Day-9.27) ──
    # Path discovered from SPA bundle. One doc per user-per-tournament with
    # all columns precomputed by Negev's Cloud Function. Listing the whole
    # collection returns docs across ALL tournaments; we filter by the path
    # prefix `tournamentStats/{tid}_`.
    try:
        all_stats = _read_all("tournamentStats")
    except Exception:                                  # noqa: BLE001
        all_stats = []
    tid_stats = [s for s in all_stats
                  if (s.get("_path") or "").startswith(f"tournamentStats/{tid}_")]
    if tid_stats:
        return _build_rows_from_stats(tid, tid_stats, uid_to_user,
                                        include_bots=include_bots,
                                        extended=extended)

    # ── FALLBACK: bet aggregation (Day-9.26 path) ──
    # Only reached if tournamentStats is empty/blocked. Keeps the system
    # working under partial Negev outages.
    import logging
    logging.getLogger("negev_toto").warning(
        "tournamentStats empty/unreadable; falling back to bet aggregation")

    # ── Match bets (group + KO) ──
    # Filter `bets/` to this tournament. The schema is consistent across
    # all 1026 docs (verified live 2026-06-12): every bet has matchId,
    # tournamentId, userId, points, isExactScore.
    bets = [b for b in _read_all("bets") if b.get("tournamentId") == tid]

    # We need the stage of each match to bucket points into group vs KO.
    # Negev's match docs are in the top-level `matches` collection with
    # `tournamentId` field. Their `stage` is a string like 'Group Stage - 1'
    # or 'Round of 16' / 'Quarter-final' / 'Semi-final' / 'Final' / 'Third place'.
    # Build apiFixtureId → stage map.
    matches = [m for m in _read_all("matches") if m.get("tournamentId") == tid]
    fixture_to_stage = {}
    for m in matches:
        # Match doc paths look like 'matches/{tid}_{apiFixtureId}', so apiFixtureId
        # is also queryable directly.
        fid = m.get("apiFixtureId")
        if fid is not None:
            fixture_to_stage[str(fid)] = (m.get("stage") or "").strip()

    def _is_ko_stage(stage: str) -> bool:
        s = (stage or "").lower()
        return any(k in s for k in ("round of", "quarter", "semi",
                                      "final", "third"))
    # Note: "Group Stage - 1/2/3" contains "Stage" but NOT any KO marker.
    # "Round of 16" / "Quarter-final" / "Semi-final" / "Final" / "Third place"
    # all match _is_ko_stage. Anything unrecognised defaults to direction (safe
    # default — won't double-count).

    agg = {}                                            # uid -> dict
    for u in users:
        if tid not in (u.get("tournaments") or []):
            continue
        is_bot = _is_bot(u)
        if not include_bots and is_bot:
            continue
        agg[u.get("uid")] = {
            "uid": u.get("uid"),
            "player": u.get("displayName") or u.get("uid", "?"),
            "role": u.get("role"),
            "direction": 0.0,
            "knockout": 0.0,
            "side": 0.0,
            "broad": 0.0,
            "exactCount": 0,
            **({"_full": u} if extended else {}),
        }

    for b in bets:
        uid = b.get("userId")
        if uid not in agg:
            continue
        pts = float(b.get("points") or 0)
        match_id = str(b.get("matchId") or "")
        # matchId format on real tournament: '{tid}_{apiFixtureId}'.
        # Mock tournament docs have a different prefix; we filtered above
        # via tournamentId so those are already excluded.
        fid = match_id.rsplit("_", 1)[-1] if "_" in match_id else match_id
        stage = fixture_to_stage.get(fid, "")
        if _is_ko_stage(stage):
            agg[uid]["knockout"] += pts
        else:
            agg[uid]["direction"] += pts
        if b.get("isExactScore"):
            agg[uid]["exactCount"] += 1

    # ── Futures (broad bets) — placeholder; points awarded at tournament end ──
    # The selection docs at tournaments/{tid}/broadBets/{uid} carry only the
    # user's selections, not points. Negev scores futures only after the
    # tournament resolves the categories. Leave `broad` at 0 here; when
    # Negev publishes futures points (likely via its Cloud Function writing
    # to a separate collection or the user doc), we'll wire that up.

    # ── Side bets (Day-9.26 override mechanism) ──
    # Side-bet user picks live at a Firestore path we can't read with the
    # operator's regular-user token (403 across every probed convention —
    # `tournaments/{tid}/standings`, `sideBetPicks/`, `users/{uid}/picks/{sb}`,
    # subcollections of the shell, etc.). The Negev web app shows them via
    # a privileged read our token doesn't include. So we let the operator
    # supply the per-user side-bet totals via a JSON override that gets
    # ADDED to the bet-aggregated direction/knockout numbers.
    #
    # File: store/side_bet_overrides_<tid>.json  (gitignored runtime state)
    # Shape: {"users": {"<displayName>": <total side bet pts so far>, …},
    #         "captured_at": "<ISO timestamp>"}
    # After each match-day the operator updates the file (manually or via
    # `tools/standings_set.py side-bet <name> <pts>`); the next 📊 sync
    # picks it up and the leaderboard totals + ranks match the app exactly.
    side_overrides = _load_side_bet_overrides(tid)
    # Build a case-insensitive partial-match index so the operator can write
    # "G. Cain" or "Cain" or "GILAD CAIN" and we'll still find "Gilad Cain"
    # in the displayName. Exact match wins over partial.
    by_name = {a["player"]: a for a in agg.values()}
    for override_name, pts in side_overrides.items():
        # Exact match first
        if override_name in by_name:
            by_name[override_name]["side"] = float(pts)
            continue
        # Fall back to a normalized startswith / contains match
        on = "".join(c for c in override_name.lower() if c.isalnum())
        for player_name, a in by_name.items():
            pn = "".join(c for c in player_name.lower() if c.isalnum())
            if on and (on == pn or on in pn or pn in on):
                a["side"] = float(pts)
                break

    rows = []
    for a in agg.values():
        total = a["direction"] + a["knockout"] + a["side"] + a["broad"]
        rows.append({
            "player": a["player"],
            "uid": a["uid"],
            "total": round(total, 3),
            "direction": round(a["direction"], 3),
            "knockout": round(a["knockout"], 3),
            "side": round(a["side"], 3),
            "broad": round(a["broad"], 3),
            "exactCount": a["exactCount"],
            "role": a["role"],
            **({"_full": a.get("_full")} if extended and a.get("_full") else {}),
        })

    rows.sort(key=lambda r: (-r["total"], -r["exactCount"], r.get("uid") or ""))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def _normalize_match_row(m: dict, _norm=None) -> dict:
    """Shared shape for one Negev match row."""
    if _norm is None:
        try:
            from core.data.teams import normalize as _norm
        except Exception:                              # noqa: BLE001
            _norm = lambda x: x
    ko_iso = m.get("date")
    mapped_stage = _STAGE_MAP.get(m.get("stage", ""), m.get("stage"))
    return {
        "match_id": m.get("_path", "").split("/")[-1],
        "apiFixtureId": m.get("apiFixtureId"),
        "tournamentId": m.get("tournamentId"),
        "home": _norm(m.get("homeTeam")),
        "away": _norm(m.get("awayTeam")),
        "homeLogo": m.get("homeLogo"),
        "awayLogo": m.get("awayLogo"),
        "kickoff_utc": ko_iso,
        "stage": mapped_stage,
        "stage_raw": m.get("stage"),
        "status": m.get("status"),
        "scoreFullTimeHome": m.get("scoreFullTimeHome"),
        "scoreFullTimeAway": m.get("scoreFullTimeAway"),
        "scorePenaltyHome": m.get("scorePenaltyHome"),
        "scorePenaltyAway": m.get("scorePenaltyAway"),
        "oddsHome": m.get("oddsHome"),
        "oddsDraw": m.get("oddsDraw"),
        "oddsAway": m.get("oddsAway"),
        "oddsSource": m.get("oddsSource"),
        "isDetonator": m.get("isDetonator"),
        "exactScoreMultiplier": m.get("exactScoreMultiplier"),
        "winnerTeam": m.get("winnerTeam"),
    }


@mcp.tool()
def toto_get_matches(tournament_id: str | None = None,
                     date_after: str | None = None,
                     status: str | None = None,
                     stage: str | None = None,
                     limit: int = 200) -> list[dict]:
    """Read Negev's match catalog SCOPED TO ONE TOURNAMENT, normalized.

    Negev's global `matches` collection mixes fixtures across many tournaments
    (J-League, Allsvenskan, side pools, etc.). This tool filters to one
    tournament via a Firestore structured query on `tournamentId`, so you
    only get the 72 WC fixtures (or whichever pool's id you pass).

    Filters (all optional, all post-query):
      tournament_id: defaults to NEGEV_TOURNAMENT_ID env var
      date_after:    ISO 8601 date, e.g. '2026-06-11'
      status:        'NS' (not started) / 'FT' / 'PEN' / 'IP' (in play)
      stage:         post-normalization label, e.g. 'Group', 'R16'
    """
    tid = _tid(tournament_id)
    res = toto_query("matches", "tournamentId", "EQUAL", tid, limit=300)
    if "error" in res:
        return [res]
    rows = res.get("results", [])
    try:
        from core.data.teams import normalize as _norm
    except Exception:                                  # noqa: BLE001
        _norm = lambda x: x
    out = []
    for m in rows:
        norm = _normalize_match_row(m, _norm)
        if date_after and norm["kickoff_utc"] and norm["kickoff_utc"] < date_after:
            continue
        if status and norm["status"] != status:
            continue
        if stage and norm["stage"] != stage:
            continue
        out.append(norm)
        if len(out) >= limit:
            break
    out.sort(key=lambda x: x.get("kickoff_utc") or "")
    return out


@mcp.tool()
def toto_get_match_details(home: str | None = None,
                            away: str | None = None,
                            match_id: str | None = None,
                            tournament_id: str | None = None) -> dict:
    """Full per-match view combining everything the Matches tab shows for ONE
    game: match row + my prediction + friends' picks + the applicable exact-
    score multiplier grid. Stats/Lineups/Events are NOT included (those come
    from api-football directly, not Negev's Firestore — our daemon already
    has core/data/api_football.py for that).

    Lookup by team-name pair (home + away) OR by match_id. team names pass
    through core.data.teams.normalize so 'Mexico' / 'mexico' / 'Mexico ' all
    match. Returns:
      match:        full normalized match row (see toto_get_matches shape)
      myPrediction: {home, away} score OR None if not submitted
      friendsPicks: [{displayName, homeScore, awayScore, points, breakdown}]
                    sorted by points desc
      exactPtsGrid: the multiplier table for this match's stage type
                    (groupStage / round16AndQuarter / semiAndFinal)
      bingoMultiplier: convenience — the exact-PTS multiplier for THIS pick if
                      myPrediction is set (None otherwise)
    """
    tid = _tid(tournament_id)
    # 1. Find the match
    matches = toto_get_matches(tournament_id=tid, limit=300)
    target = None
    if match_id:
        target = next((m for m in matches if m["match_id"] == match_id
                        or str(m.get("apiFixtureId")) == match_id), None)
    elif home and away:
        try:
            from core.data.teams import normalize as _norm
        except Exception:                              # noqa: BLE001
            _norm = lambda x: x
        h_n = _norm(home).lower() if home else None
        a_n = _norm(away).lower() if away else None
        target = next((m for m in matches if (m["home"] or "").lower() == h_n
                        and (m["away"] or "").lower() == a_n), None)
    if not target:
        return {"error": f"match not found: home={home!r} away={away!r} "
                f"match_id={match_id!r} in tournament {tid}"}

    mid = target["match_id"]
    apifid = target["apiFixtureId"]

    # 2. My prediction — query bets where userId==me and matchId==this
    my_pred = None
    uid = _token.get("uid") or _id_token() and _token.get("uid")
    if not uid:
        _id_token(); uid = _token.get("uid")
    my_bets = toto_query("bets", "userId", "EQUAL", uid, limit=200)
    for b in my_bets.get("results", []):
        if b.get("tournamentId") != tid:
            continue
        # matchId stored as e.g. "n40y..._1489369"; sometimes just apiFixtureId
        if (b.get("matchId") == mid
            or b.get("matchId") == str(apifid)
            or (apifid and str(apifid) in str(b.get("matchId", "")))):
            my_pred = {"home": b.get("homeScore"), "away": b.get("awayScore")}
            break

    # 3. Friends' picks
    friends = toto_get_match_bets(mid, tournament_id=tid)

    # 4. Exact-PTS grid for this stage type
    grids = toto_get_scoring_grids(tournament_id=tid).get("grids", {})
    stage_to_grid = {"Group": "groupStage",
                      "R32": "round16AndQuarter",
                      "R16": "round16AndQuarter",
                      "QF":  "round16AndQuarter",
                      "SF":  "semiAndFinal",
                      "3rd": "semiAndFinal",
                      "Final": "semiAndFinal"}
    grid_key = stage_to_grid.get(target["stage"])
    pts_grid = grids.get(grid_key, {}) if grid_key else {}

    # 5. If I have a prediction, look up its multiplier value
    bingo_mult = None
    if my_pred and my_pred.get("home") is not None and my_pred.get("away") is not None:
        key = f"{my_pred['home']}-{my_pred['away']}"
        bingo_mult = pts_grid.get(key)

    return {
        "match": target,
        "myPrediction": my_pred,
        "friendsPicks": friends,
        "exactPtsGrid": pts_grid,
        "exactPtsGridName": grid_key,
        "bingoMultiplier": bingo_mult,
    }


@mcp.tool()
def toto_get_broad_bets(tournament_id: str | None = None) -> list[dict]:
    """All futures picks in a tournament. Each row:
        {userId, displayName, winner, goldenBoot, cinderella, bestPlayer,
         updatedAt}
    Joins on `users` collection for displayName (uid → display name)."""
    tid = _tid(tournament_id)
    raw = _read_all(f"tournaments/{tid}/broadBets")
    # Build uid → displayName map from users collection
    users = _read_all("users")
    name_by_uid = {u.get("uid"): u.get("displayName") for u in users if u.get("uid")}
    out = []
    for b in raw:
        sel = b.get("selections") or {}
        uid = b.get("userId")
        out.append({
            "userId": uid,
            "displayName": name_by_uid.get(uid, "?"),
            "winner": sel.get("winner"),
            "goldenBoot": sel.get("goldenBoot"),
            "cinderella": sel.get("cinderella"),
            "bestPlayer": sel.get("bestPlayer"),
            "updatedAt": b.get("updatedAt"),
        })
    out.sort(key=lambda r: r["displayName"])
    return out


@mcp.tool()
def toto_get_side_bets(tournament_id: str | None = None,
                       active_only: bool = False,
                       published_only: bool = False) -> list[dict]:
    """All side-bet docs in a tournament. The Negev UI splits them:
      'Upcoming Side Bets' = has a non-empty question AND not resolved
      'Past Results'       = isResolved == True

    18 shell docs are pre-created (one per match day, ids 'sb_YYYY-MM-DD'),
    but `question` is empty until the founder publishes the prompt. Use
    published_only=True to filter to bets that have actually been announced
    (matches the UI's visible upcoming list).

    Each row: {id, question, points, stage, startTime, isActive, isLocked,
               isResolved, correctAnswer, matchId}
    `matchId` may be None for free-form (joke) questions.
    """
    tid = _tid(tournament_id)
    raw = _read_all(f"tournaments/{tid}/sideBets")
    out = []
    for s in raw:
        question = (s.get("question") or "").strip()
        if active_only and (not s.get("isActive") or s.get("isResolved")):
            continue
        if published_only and not question:
            continue
        out.append({
            "id": s.get("_path", "").split("/")[-1],
            "question": question,
            "points": s.get("points"),
            "stage": _STAGE_MAP.get(s.get("stage", ""), s.get("stage")),
            "stage_raw": s.get("stage"),
            "startTime": s.get("startTime"),
            "isActive": s.get("isActive", False),
            "isLocked": s.get("isLocked", False),
            "isResolved": s.get("isResolved", False),
            "correctAnswer": s.get("correctAnswer"),
            "matchId": s.get("matchId"),
        })
    out.sort(key=lambda r: r.get("startTime") or "")
    return out


@mcp.tool()
def toto_get_side_bets_upcoming(tournament_id: str | None = None) -> list[dict]:
    """Negev UI's 'Upcoming Side Bets' panel — only docs with a published
    question that hasn't been resolved yet."""
    return toto_get_side_bets(tournament_id, published_only=True)


@mcp.tool()
def toto_get_side_bets_resolved(tournament_id: str | None = None) -> list[dict]:
    """Negev UI's 'Past Results' panel — only docs where isResolved==True
    (the correct answer is filled in)."""
    return [s for s in toto_get_side_bets(tournament_id) if s["isResolved"]]


# ─────────────────────────────────────────────────────────────────────────────
# WRITE TOOLS — match-result update (gated by NEGEV_ALLOW_WRITES=1)
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def toto_update_match_result(match_id: str,
                              home_score: int,
                              away_score: int,
                              tournament_id: str | None = None,
                              status: str = "FT",
                              penalty_home: int | None = None,
                              penalty_away: int | None = None,
                              winner_team: str | None = None) -> dict:
    """Update one match's final score in Negev (gated by NEGEV_ALLOW_WRITES=1).

    Args:
      match_id:     Negev doc id ('<tid>_<apiFixtureId>' or just apiFixtureId)
      home_score:   final-time home goals
      away_score:   final-time away goals
      status:       'FT' (full time, default) / 'PEN' (decided on penalties) /
                    'IP' (in play) / 'NS' (reset to not-started)
      penalty_home: penalty shootout home score (only for KO + PEN status)
      penalty_away: penalty shootout away score
      winner_team:  team name that advances (required on KO when status=PEN)

    For group-stage games: pass only home_score / away_score / status='FT'.
    For knockouts decided in 90/120 min: same.
    For knockouts decided on pens: status='PEN', plus penalty_home/away,
      plus winner_team (the team that advances).

    The fields patched mirror Negev's match-doc schema:
      scoreFullTimeHome, scoreFullTimeAway, goalsHome, goalsAway, status,
      scorePenaltyHome, scorePenaltyAway, winnerTeam
    """
    if os.environ.get("NEGEV_ALLOW_WRITES") != "1":
        return {"error": "writes disabled. Set NEGEV_ALLOW_WRITES=1 to enable. "
                f"Would have updated match_id={match_id} → "
                f"{home_score}-{away_score} status={status}"}
    tid = _tid(tournament_id)
    # Resolve the actual doc path. Negev uses '<tid>_<apiFixtureId>' as the
    # doc id for some tournaments; if user passes just the apiFixtureId we
    # construct it.
    if "_" not in match_id:
        match_id = f"{tid}_{match_id}"
    fields = {
        "scoreFullTimeHome": int(home_score),
        "scoreFullTimeAway": int(away_score),
        "goalsHome": int(home_score),
        "goalsAway": int(away_score),
        "status": status,
    }
    if penalty_home is not None:
        fields["scorePenaltyHome"] = int(penalty_home)
    if penalty_away is not None:
        fields["scorePenaltyAway"] = int(penalty_away)
    if winner_team is not None:
        fields["winnerTeam"] = winner_team
    return toto_patch_document(f"matches/{match_id}", json.dumps(fields))


@mcp.tool()
def toto_submit_match_prediction(home: str | None = None,
                                  away: str | None = None,
                                  home_score: int = 0,
                                  away_score: int = 0,
                                  advances_team: str | None = None,
                                  match_id: str | None = None,
                                  tournament_id: str | None = None) -> dict:
    """Save MY per-match score prediction to Negev (the equivalent of clicking
    YOUR PREDICTION → enter score → Save Prediction in the Matches tab).

    DISABLED unless NEGEV_ALLOW_WRITES=1.

    The Negev `bets` doc path is `bets/{tournamentId}_{apiFixtureId}_{userId}`.
    We UPSERT only the fields the user owns; the points/breakdown/processedAt
    are populated server-side AFTER the match plays. Idempotent — re-submitting
    overwrites the previous prediction (matches the app's behavior up until
    the match locks at kickoff).

    Lookup the match by team-name pair OR by match_id (Negev internal doc id).
    Example: toto_submit_match_prediction(home='Mexico', away='South Africa',
                                          home_score=2, away_score=1)
    """
    if os.environ.get("NEGEV_ALLOW_WRITES") != "1":
        return {"error": "writes disabled. Set NEGEV_ALLOW_WRITES=1 to enable. "
                f"Would have saved {home or '?'} {home_score}-{away_score} {away or '?'}"}
    tid = _tid(tournament_id)
    # Find the match
    target = None
    matches = toto_get_matches(tournament_id=tid, limit=300)
    if match_id:
        target = next((m for m in matches if m["match_id"] == match_id), None)
    elif home and away:
        try:
            from core.data.teams import normalize as _norm
        except Exception:                              # noqa: BLE001
            _norm = lambda x: x
        h_n = (_norm(home) or "").lower()
        a_n = (_norm(away) or "").lower()
        target = next((m for m in matches if (m["home"] or "").lower() == h_n
                        and (m["away"] or "").lower() == a_n), None)
    if not target:
        return {"error": f"match not found in tournament {tid}: "
                         f"home={home!r} away={away!r} match_id={match_id!r}"}
    if target["status"] not in ("NS", "TIMED"):
        return {"error": f"match has already started ({target['status']}); "
                "predictions are locked"}

    apifid = target["apiFixtureId"]
    uid = _token.get("uid")
    if not uid:
        _id_token(); uid = _token.get("uid")
    bet_doc_id = f"{tid}_{apifid}_{uid}"
    bet_matchid = f"{tid}_{apifid}"

    from datetime import datetime, timezone
    fields = {
        "userId": uid,
        "matchId": bet_matchid,
        "tournamentId": tid,
        "homeScore": int(home_score),
        "awayScore": int(away_score),
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "isBot": False,
    }
    # advances_team only meaningful on knockout matches when prediction is a
    # draw (the team that wins on penalties). Negev's bet schema has the
    # field; we set it when the caller provides it. For group matches just
    # omit (server will store null).
    if advances_team is not None:
        is_ko = target["stage"] in ("R32", "R16", "QF", "SF", "3rd", "Final")
        if not is_ko:
            return {"error": f"advances_team is only valid for knockout matches; "
                    f"this is {target['stage']}"}
        if home_score != away_score:
            return {"error": f"advances_team is only meaningful when the "
                    f"prediction is a draw (you predicted {home_score}-{away_score})"}
        if advances_team not in (target["home"], target["away"]):
            return {"error": f"advances_team={advances_team!r} must be one of "
                    f"the two teams: {target['home']!r} or {target['away']!r}"}
        fields["advancesTeam"] = advances_team
    return toto_patch_document(f"bets/{bet_doc_id}", json.dumps(fields))


@mcp.tool()
def toto_next_match(tournament_id: str | None = None) -> dict:
    """The next un-finished match in time order — what you'd ask before
    submitting a result via toto_update_match_result. Returns:
      {match, requires_score, requires_penalties, stage_type,
       instructions: 'Please give me ...'}

    `requires_penalties` is True only when stage is KO/SF/Final AND a draw
    in regulation could happen; this is conservative (always True for KO
    stages), since you'd otherwise have to ask twice if the game ended
    1-1 after extra time.
    """
    tid = _tid(tournament_id)
    matches = toto_get_matches(tournament_id=tid, limit=300)
    pending = [m for m in matches if m["status"] in ("NS", "IP")]
    if not pending:
        return {"error": "no pending matches in tournament", "tournament_id": tid}
    next_m = pending[0]
    stage = next_m["stage"]
    is_ko = stage in ("R32", "R16", "QF", "SF", "3rd", "Final")
    instr = (f"Next match: {next_m['home']} vs {next_m['away']} "
             f"({next_m['kickoff_utc']}, stage={stage}). ")
    if is_ko:
        instr += ("This is a KNOCKOUT — give me: home score, away score, "
                  "AND (only if 1-1/2-2/etc. went to penalties) the penalty "
                  "shootout score + winner.")
    else:
        instr += "Give me: home score, away score."
    return {
        "match": next_m,
        "stage_type": "knockout" if is_ko else "group",
        "requires_score": True,
        "requires_penalties": is_ko,
        "instructions": instr,
    }


@mcp.tool()
def toto_get_my_preferences() -> dict:
    """Read MY user doc and return only the notification-prefs flags + role +
    status. One network call (users/<my-uid>). Useful before
    toto_update_preferences to know the current state."""
    uid = _token.get("uid") or _id_token() and _token.get("uid")
    if not uid:
        # _id_token() above ensures sign-in if not cached; uid should now be set
        _id_token()
        uid = _token.get("uid")
    me = toto_get_document(f"users/{uid}")
    if "error" in me:
        return me
    return {
        "uid": me.get("uid"),
        "displayName": me.get("displayName"),
        "role": me.get("role"),
        "status": me.get("status"),
        "pref_results": me.get("pref_results"),
        "pref_reminders": me.get("pref_reminders"),
        "pref_announcements": me.get("pref_announcements"),
        "pref_broadBets": me.get("pref_broadBets"),
        "pref_sideBets": me.get("pref_sideBets"),
    }


@mcp.tool()
def toto_get_scoring_grids(tournament_id: str | None = None) -> dict:
    """Negev's per-stage exact-score multiplier tables — the *real* tables used
    by their server-side scoring. Three grids: `groupStage`, `round16AndQuarter`,
    `semiAndFinal`. Each cell key is the home-away scoreline ('1-0', '2-1',
    '6+-3') and the value is the multiplier.

    Use this to verify our config/rules.py::SCORE_TABLE against the source of
    truth. If a cell disagrees, our scoring engine would compute differently
    from what the app awards — fix config/rules.py to match.
    """
    tid = _tid(tournament_id)
    doc = toto_get_document(f"tournaments/{tid}/settings/managerTables")
    if "error" in doc:
        return doc
    return {"tournament_id": tid, "grids": doc.get("grids", {})}


@mcp.tool()
def toto_get_broad_bet_categories(tournament_id: str | None = None) -> dict:
    """The four futures categories with their full option lists:
      * winner / cinderella  — 48 team options each ({id, name, points, isKilled})
      * goldenBoot           — striker roster (19 strikers)
      * bestPlayer           — **the META-BET: which PARTICIPANT will finish
                                  highest in the pool**, NOT a football player.

    The `bestPlayer` category in `settings/broadBets` only stores 1 placeholder
    option. The Negev web app dynamically composes the dropdown from the
    `users` collection — every approved human participant becomes an option.
    We replicate the same logic here so the returned `bestPlayer.options`
    matches what the app shows. Bots are excluded (same _is_bot() filter).

    Points default to 5 (the lowest Kod-bonus rank) for participants whose
    standing hasn't yet earned a higher payout.
    """
    tid = _tid(tournament_id)
    doc = toto_get_document(f"tournaments/{tid}/settings/broadBets")
    if "error" in doc:
        return doc
    categories = doc.get("categories", []) or []

    # bestPlayer needs dynamic synthesis from the users collection (see
    # explanation above). We do this even if the doc has 1+ options because
    # the doc only stores a placeholder; the live UI list is per-tournament.
    users = _read_all("users")
    humans = [u for u in users
              if tid in (u.get("tournaments") or [])
              and not _is_bot(u)
              and u.get("status") == "approved"]
    # 5 = the lowest Kod-bonus value per `tournaments/{tid}.settings.kodBonuses`
    # — matches what the Negev UI displays before standings settle.
    # Day-9.11.d: option id MUST carry the `roster_` prefix so the app's UI
    # dropdown can match the saved selection (confirmed by inspecting every
    # other submitter's broadBets doc — all 8 use roster_<uid>).
    DEFAULT_BEST_PLAYER_POINTS = 5
    synth_options = sorted([
        {"name": u.get("displayName") or u.get("uid", "?"),
         "id": f"roster_{u.get('uid', '?')}",
         "points": DEFAULT_BEST_PLAYER_POINTS,
         "isKilled": False}
        for u in humans
    ], key=lambda o: (o["name"] or "").lower())

    out_categories = []
    for c in categories:
        if c.get("id") == "bestPlayer":
            # Replace the placeholder list with the synthesized full roster
            out_categories.append({
                **c,
                "options": synth_options,
                "_synthesized": True,
                "_source": "users collection (filtered to approved human "
                           "participants in this tournament)",
            })
        else:
            out_categories.append(c)
    # If the Negev settings doc somehow lacks a bestPlayer entry, append one
    if not any(c.get("id") == "bestPlayer" for c in categories):
        out_categories.append({
            "id": "bestPlayer", "options": synth_options,
            "_synthesized": True,
            "_source": "users collection (settings missing the category)",
        })

    return {
        "tournament_id": tid,
        "isPublished": doc.get("isPublished"),
        "isLocked": doc.get("isLocked"),
        "categories": out_categories,
    }


@mcp.tool()
def toto_get_match_bets(match_id: str,
                         tournament_id: str | None = None) -> list[dict]:
    """All picks (across users) for ONE match in ONE tournament.
        Returns: [{userId, displayName, homeScore, awayScore, points,
                   isCorrectDir, isExactScore, breakdown, processedAt}]

    breakdown contains: basePoints, totalPoints, odds, multiplier,
    detonatorMultiplier, penaltiesBonus — the full scoring breakdown Negev's
    server computed. Useful for verifying our score_match() against theirs.
    """
    tid = _tid(tournament_id)
    res = toto_query("bets", "matchId", "EQUAL", match_id, limit=500)
    if "error" in res:
        return [res]
    rows = res.get("results", [])
    # Filter to the tournament (matchIds can recur across pools)
    rows = [r for r in rows if r.get("tournamentId") == tid]
    # Join displayName
    users = _read_all("users")
    name_by_uid = {u.get("uid"): u.get("displayName") for u in users if u.get("uid")}
    out = []
    for r in rows:
        out.append({
            "userId": r.get("userId"),
            "displayName": name_by_uid.get(r.get("userId"), r.get("userId", "?")),
            "homeScore": r.get("homeScore"),
            "awayScore": r.get("awayScore"),
            "points": r.get("points"),
            "isCorrectDir": r.get("isCorrectDir"),
            "isExactScore": r.get("isExactScore"),
            "advancesTeam": r.get("advancesTeam"),
            "breakdown": r.get("breakdown") or {},
            "processedAt": r.get("processedAt"),
            "updatedAt": r.get("updatedAt"),
            "isBot": r.get("isBot", False),
        })
    out.sort(key=lambda r: (-(r["points"] or 0), r["displayName"]))
    return out


@mcp.tool()
def toto_get_my_bets(tournament_id: str | None = None,
                      limit: int = 200) -> list[dict]:
    """All MY picks in a tournament — for verifying our daemon's persisted
    predictions agree with what landed in the Negev app."""
    tid = _tid(tournament_id)
    uid = _token.get("uid")
    if not uid:
        _id_token()
        uid = _token.get("uid")
    res = toto_query("bets", "userId", "EQUAL", uid, limit=limit)
    if "error" in res:
        return [res]
    rows = [r for r in res.get("results", []) if r.get("tournamentId") == tid]
    rows.sort(key=lambda r: (r.get("updatedAt") or ""), reverse=True)
    return rows


@mcp.tool()
def toto_update_preferences(pref_results: bool | None = None,
                            pref_reminders: bool | None = None,
                            pref_announcements: bool | None = None,
                            pref_broadBets: bool | None = None,
                            pref_sideBets: bool | None = None) -> dict:
    """Patch my notification preferences. Only fields explicitly passed are
    sent (None = unchanged). DISABLED unless NEGEV_ALLOW_WRITES=1."""
    fields = {k: v for k, v in {
        "pref_results": pref_results,
        "pref_reminders": pref_reminders,
        "pref_announcements": pref_announcements,
        "pref_broadBets": pref_broadBets,
        "pref_sideBets": pref_sideBets,
    }.items() if v is not None}
    if not fields:
        return {"error": "nothing to update — pass at least one pref_* arg"}
    uid = _token.get("uid")
    if not uid:
        _id_token()
        uid = _token.get("uid")
    return toto_patch_document(f"users/{uid}", json.dumps(fields))


# ─────────────────────────────────────────────────────────────────────────────
# WRITE TOOLS — broad bets + side bets (Day-9.11; gated by NEGEV_ALLOW_WRITES=1)
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_option_id(category: str, choice: str, categories: dict) -> str | None:
    """Match the caller's choice (either a literal option id or a human name)
    against the options for one category. Returns the matched id or None.

    Lookup order (each tier independently — first match wins):
      1. Exact `option.id` match (e.g. "team_Portugal")
      2. Exact `option.name` match (case-insensitive)
      3. Fold-equal: accent-strip + lowercase + drop non-alphanumeric
         (handles "Curaçao" ↔ "Curacao", "Lautaro Martínez" ↔ "Lautaro Martinez")
      4. Suffix-strip: tier 3 + drop common name suffixes
         ("jr", "jr.", "junior", "islands", "republic of korea", etc.)
         Handles "Vinicius Jr." ↔ "Vinicius", "Cape Verde Islands" ↔ "Cape Verde"
      5. Alias-equal: route both sides through `core.data.teams.normalize()`
         and re-apply tier 3. Handles "USA" ↔ "United States",
         "Cabo Verde" ↔ "Cape Verde" — every alias _ALIASES knows about.

    Day-9.14: tiers 3-5 cover the 5 known mismatches between
    config/rules.py keys and Negev's published option names. The full
    matrix is verified by tests/test_resolve_option_id_name_variants.
    """
    if not choice:
        return None
    cats = (categories or {}).get("categories") or []
    target = next((c for c in cats if c.get("id") == category), None)
    if not target:
        return None
    options = target.get("options") or []
    needle = str(choice).strip()
    needle_l = needle.lower()

    # tier 1: exact id
    for o in options:
        if o.get("id") == needle:
            return o["id"]
    # tier 2: exact displayName (case-insensitive)
    for o in options:
        if (o.get("name") or "").strip().lower() == needle_l:
            return o["id"]

    import re
    import unicodedata

    def _fold(s: str) -> str:
        """Accent-strip + lowercase + drop non-alphanumeric — Curaçao → curacao."""
        s = unicodedata.normalize("NFKD", s or "")
        s = "".join(c for c in s if unicodedata.category(c) != "Mn")
        return re.sub(r"[^a-z0-9]+", "", s.lower())

    needle_fold = _fold(needle)
    # tier 3: fold-equal
    for o in options:
        if needle_fold and _fold(o.get("name") or "") == needle_fold:
            return o["id"]

    # tier 4: drop common suffixes from both sides, then fold-equal
    _SUFFIXES_PAT = re.compile(
        r"\s+(jr\.?|junior|sr\.?|senior|islands|repblic|republic|"
        r"democratic\s+republic|dr|i+)$", re.IGNORECASE)
    def _strip_suffix(s: str) -> str:
        prev = None
        cur = (s or "").strip()
        while cur != prev:
            prev = cur
            cur = _SUFFIXES_PAT.sub("", cur).strip()
        return cur
    needle_sufx = _fold(_strip_suffix(needle))
    for o in options:
        if needle_sufx and _fold(_strip_suffix(o.get("name") or "")) == needle_sufx:
            return o["id"]

    # tier 5: route both sides through teams.normalize() so "USA" ↔
    # "United States" and "Cabo Verde" ↔ "Cape Verde" match cleanly.
    try:
        from core.data.teams import normalize as _team_norm
    except Exception:                                  # noqa: BLE001
        _team_norm = lambda x: x
    needle_canon = _fold(_team_norm(needle) or needle)
    for o in options:
        opt_canon = _fold(_team_norm(o.get("name") or "") or (o.get("name") or ""))
        if needle_canon and opt_canon == needle_canon:
            return o["id"]
    return None


@mcp.tool()
def toto_save_broad_bets(winner: str | None = None,
                          cinderella: str | None = None,
                          golden_boot: str | None = None,
                          best_player: str | None = None,
                          tournament_id: str | None = None,
                          dry_run: bool = False) -> dict:
    """Save MY futures (broad bet) picks to Negev — Tournament Winner,
    Cinderella, Golden Boot, Best Placed Player. All four parameters are
    OPTIONAL and accept EITHER the full option id (`"team_Portugal"`) OR the
    human-readable name shown in the app (`"Portugal"`). Partial updates
    work — pass only the categories you want to set.

    DISABLED unless NEGEV_ALLOW_WRITES=1 (the env var is the first gate;
    Negev's Firestore rules are the last).

    `dry_run=True` resolves every name → id, validates against the published
    options, and returns the planned PATCH WITHOUT calling Firestore. Use
    this before flipping NEGEV_ALLOW_WRITES=1 to confirm names map to the
    intended IDs.

    Doc path: `tournaments/{tid}/broadBets/{my_uid}`
    Field shape (mirrors what the web app's "Save Predictions" button writes):
      {
        "userId":     <my_uid>,
        "updatedAt":  ISO-8601 UTC now,
        "selections": {
          "winner":     "team_Portugal",
          "cinderella": "team_Uzbekistan",
          "goldenBoot": "1780580161396",
          "bestPlayer": "<participant_uid>"
        }
      }
    """
    tid = _tid(tournament_id)
    # Resolve every passed choice via the published categories doc so the
    # caller can use display names. bestPlayer is special: its options are
    # synthesised from the users collection (each option.id is a user UID).
    cats = toto_get_broad_bet_categories(tournament_id=tid)
    if "error" in cats:
        return {"error": f"could not read broadBet categories: {cats['error']}"}

    sel_input = {
        "winner":     winner,
        "cinderella": cinderella,
        "goldenBoot": golden_boot,
        "bestPlayer": best_player,
    }
    resolved: dict[str, str] = {}
    unresolved: list[dict] = []
    for cat_id, choice in sel_input.items():
        if choice is None:
            continue
        rid = _resolve_option_id(cat_id, choice, cats)
        if rid is None:
            unresolved.append({"category": cat_id, "choice": choice})
        else:
            resolved[cat_id] = rid

    if not resolved and not unresolved:
        return {"error": "nothing to save — pass at least one of winner / "
                          "cinderella / golden_boot / best_player"}
    if unresolved:
        return {"error": "could not resolve some choices against the "
                          "published options",
                "unresolved": unresolved,
                "hint": "Pass a published option name exactly (case-insensitive). "
                        "For bestPlayer, pass the participant's displayName."}

    uid = _token.get("uid")
    if not uid:
        _id_token()
        uid = _token.get("uid")

    from datetime import datetime, timezone
    fields = {
        "userId":     uid,
        "tournamentId": tid,
        "selections": resolved,
        "updatedAt":  datetime.now(timezone.utc).isoformat(),
    }

    if dry_run:
        return {"dry_run": True, "would_patch": f"tournaments/{tid}/broadBets/{uid}",
                "fields": fields, "resolved": resolved}

    if os.environ.get("NEGEV_ALLOW_WRITES") != "1":
        return {"error": "writes disabled. Set NEGEV_ALLOW_WRITES=1 to enable. "
                          "Re-run with dry_run=True to see the planned PATCH.",
                "would_patch": f"tournaments/{tid}/broadBets/{uid}",
                "resolved": resolved}

    out = toto_patch_document(f"tournaments/{tid}/broadBets/{uid}",
                                json.dumps(fields))
    if "error" in out:
        return out
    return {"ok": True, "path": f"tournaments/{tid}/broadBets/{uid}",
            "selections": resolved, "patched": out}


_SIDE_BET_ID_RE = __import__("re").compile(r"^sb_\d{4}-\d{2}-\d{2}$")


@mcp.tool()
def toto_submit_side_bet_answer(side_bet_id: str,
                                  answer: bool,
                                  tournament_id: str | None = None,
                                  dry_run: bool = False) -> dict:
    """Submit MY Yes/No answer to one side bet question.

    DISABLED unless NEGEV_ALLOW_WRITES=1.

    **Verified 2026-06-11** against the live Negev v2.60 JS bundle
    (negev-toto.web.app/assets/index-D_Y7NaNn.js). The app's side-bet
    save handler is:

        const $ = doc(db, "tournaments", tid, "sideBetAnswers", uid);
        await setDoc($, {userId, answers, updatedAt}, {merge: true});

    So the canonical path is `tournaments/{tid}/sideBetAnswers/{uid}` —
    ONE doc per user, with all side-bet answers accumulated in a `answers`
    MAP field. Answers are STRING "Yes" / "No" (not bool). Earlier guesses
    that wrote to `bets/{tid}_{sbid}_{uid}` or `tournaments/{tid}/sideBets/
    {sbid}/answers/{uid}` were both ignored by the app (the first was
    accepted by Firestore rules but never read by the UI; the second was
    rejected with 403). The orphan `bets/...sb_...` docs from those guesses
    have been cleaned up via toto_delete_document.

    Verified doc shape:
      userId    — my uid (top-level)
      answers   — MAP { "sb_2026-06-11": "Yes", "sb_2026-06-12": "No", ... }
      updatedAt — ISO-8601 timestamp

    We use Firestore's updateMask with dotted path `answers.{side_bet_id}`
    so subsequent submissions for OTHER side bets merge into the same
    answers map instead of overwriting it. Idempotent.

    UI propagation: the app's Side Bets component reads this doc via
    getDoc (one-shot, NOT onSnapshot), so a write from this tool does
    NOT live-update the running UI. To see the change, the user must
    re-mount the Side Bets page (navigate Side Bets → Standings → Side
    Bets, or hard-refresh). After re-mount: the button highlight follows
    `a[side_bet_id]` (the saved value), and "✅ BET RECEIVED" appears
    when the highlighted answer matches the saved answer.

    Future side-bet variants (multi-choice, numeric threshold) would
    need their own tool — the `answers` map is typed as Yes/No string in
    the current app; submitting other strings may render but not score.

    Args:
      side_bet_id: side-bet doc id, expected pattern `sb_YYYY-MM-DD`
                   (validated pre-network — typos fail fast).
      answer:      True → "Yes", False → "No"
      dry_run:     plan-and-return without calling Firestore
    """
    if not _SIDE_BET_ID_RE.match(side_bet_id):
        return {"error": f"invalid side_bet_id {side_bet_id!r}: expected "
                          f"format 'sb_YYYY-MM-DD'. Look it up via "
                          f"toto_get_side_bets_upcoming()."}
    tid = _tid(tournament_id)
    uid = _token.get("uid")
    if not uid:
        _id_token()
        uid = _token.get("uid")
    from datetime import datetime, timezone
    answer_str = "Yes" if answer else "No"
    updated_at = datetime.now(timezone.utc).isoformat()
    path = f"tournaments/{tid}/sideBetAnswers/{uid}"

    # Build Firestore body with nested map for `answers.{side_bet_id}`.
    # updateMask with the dotted path tells Firestore to set ONLY that
    # single entry in the map, preserving any other entries already in
    # `answers`. If the doc doesn't exist yet, it's created.
    body = {
        "fields": {
            "userId":    {"stringValue": uid},
            "answers":   {"mapValue": {"fields": {
                            side_bet_id: {"stringValue": answer_str}}}},
            "updatedAt": {"stringValue": updated_at},
        }
    }
    # Backtick-quote the side_bet_id segment: Firestore's unquoted property
    # path regex is [a-zA-Z_][a-zA-Z_0-9]* (no hyphens), so `sb_2026-06-11`
    # must be quoted to be a valid dotted field path.
    params = [
        ("updateMask.fieldPaths", "userId"),
        ("updateMask.fieldPaths", f"answers.`{side_bet_id}`"),
        ("updateMask.fieldPaths", "updatedAt"),
    ]

    if dry_run:
        return {"dry_run": True, "would_patch": path,
                "answer_string": answer_str,
                "update_mask": [p[1] for p in params],
                "body": body}

    if os.environ.get("NEGEV_ALLOW_WRITES") != "1":
        return {"error": "writes disabled. Set NEGEV_ALLOW_WRITES=1 to enable. "
                          "Re-run with dry_run=True to see the planned PATCH.",
                "would_patch": path}

    r = _fs("PATCH", f"{_base()}/{path}",
             endpoint="firestore:patch_document",
             headers=_headers(), params=params, json=body, timeout=20)
    if not r.ok:
        return {"error": f"{r.status_code}: {r.text[:300]}", "path": path}
    return {"updated": path, "side_bet_id": side_bet_id,
            "answer": answer_str,
            "note": "App reads this via getDoc (one-shot), not onSnapshot — "
                    "user must re-mount Side Bets page (navigate away+back, "
                    "or hard-refresh) to see the change in the UI."}


@mcp.tool()
def toto_delete_document(path: str) -> dict:
    """Delete one Firestore document by its path.

    DISABLED unless NEGEV_ALLOW_WRITES=1. Use sparingly — there is no undo.
    Typical use: clean up orphan/stray docs left over from prior schema
    guesses (e.g. an early-tournament write to a guessed side-bet path
    before the canonical path was pinned). Returns {"deleted": path} on
    success or {"error": "..."} on failure (e.g. 403 from security rules
    if you don't own the doc, or 404 if it never existed).

    Args:
      path: Firestore doc path, e.g.
            "bets/n40ykJlOIA9Mg839hz91_sb_2026-06-11_<uid>".
    """
    if os.environ.get("NEGEV_ALLOW_WRITES") != "1":
        return {"error": "writes disabled. Set NEGEV_ALLOW_WRITES=1 to enable."}
    r = _fs("DELETE", f"{_base()}/{path}",
             endpoint="firestore:delete_document",
             headers=_headers(), timeout=20)
    if not r.ok:
        return {"error": f"{r.status_code}: {r.text[:200]}", "path": path}
    return {"deleted": path}


if __name__ == "__main__":
    mcp.run()      # stdio transport
