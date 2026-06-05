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
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("negev-toto")

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
    Path (b) takes precedence when NEGEV_REFRESH_TOKEN is set, so no password
    is required for Google-only accounts.
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
        r = requests.post(f"{REFRESH}?key={key}", timeout=20,
                          data={"grant_type": "refresh_token", "refresh_token": _token["refresh"]})
        if r.ok:
            d = r.json()
            _token.update(id=d["id_token"], refresh=d["refresh_token"],
                          uid=d.get("user_id") or _token.get("uid"),
                          exp=time.time() + int(d.get("expires_in", 3600)))
            return _token["id"]
        # Refresh failed: don't silently fall through to password (the user may
        # be a Google-Sign-In-only account). Surface a clear, actionable error
        # unless an email/password is also configured as a deliberate fallback.
        if not (os.environ.get("NEGEV_EMAIL") and os.environ.get("NEGEV_PASSWORD")):
            raise RuntimeError(
                f"Firebase refresh failed ({r.status_code}): {r.text[:160]}. "
                "Your NEGEV_REFRESH_TOKEN likely expired or was revoked — "
                "re-capture it from negev-toto.web.app DevTools (IndexedDB → "
                "firebaseLocalStorageDb → stsTokenManager.refreshToken).")
        _token["refresh"] = None      # let the password path try
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
    r = requests.post(f"{_base()}:listCollectionIds", headers=_headers(), json={}, timeout=20)
    cols = r.json().get("collectionIds", []) if r.ok else []
    return {"signed_in_as_uid": _token.get("uid"), "collections": cols,
            "note": None if r.ok else f"listCollectionIds blocked ({r.status_code}); "
            "capture exact collection names from the app's network tab instead."}


@mcp.tool()
def toto_read_collection(collection: str, page_size: int = 50) -> dict:
    """Read documents from a top-level collection (e.g. 'standings', 'matches',
    'broadBets', 'sideBets', 'users'). Returns decoded documents."""
    r = requests.get(f"{_base()}/{collection}", headers=_headers(),
                     params={"pageSize": page_size}, timeout=20)
    if not r.ok:
        return {"error": f"{r.status_code}: {r.text[:200]}", "collection": collection}
    return {"collection": collection, "documents": [_doc(d) for d in r.json().get("documents", [])]}


@mcp.tool()
def toto_get_document(path: str) -> dict:
    """Read one document by its Firestore path (e.g. 'standings/<id>' or
    'users/<uid>/settings/preferences')."""
    r = requests.get(f"{_base()}/{path}", headers=_headers(), timeout=20)
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
    r = requests.post(f"{_base()}:runQuery", headers=_headers(), json=body, timeout=20)
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
    r = requests.patch(f"{_base()}/{path}", headers=_headers(), params=params, json=body, timeout=20)
    if not r.ok:
        return {"error": f"{r.status_code}: {r.text[:200]}", "path": path}
    return {"updated": path, "fields": list(fields)}


if __name__ == "__main__":
    mcp.run()      # stdio transport
