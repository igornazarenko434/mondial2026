"""Zero-infra dashboard: render one static HTML file from the stores.

No server, no framework — run it (or schedule it) and open reports/dashboard.html.
Shows upcoming matches + latest picks, standings, run health, and quota usage.
This is the optional "one screen" view; the primary UX is push notifications.

For a richer live UI later, a ~100-line Streamlit app over the same SQLite is the
minimal next step (see docs/USER_GUIDE.md) — but it requires running a server.
"""
from __future__ import annotations
import os
from datetime import datetime
from core.obs.runs import runs
from core.obs.cost import ledger

REPORTS_DIR = os.path.join(os.path.dirname(__file__), "..", "reports")


def render_html() -> str:
    summary = runs().summary(72)
    quota = {p: ledger().quota_status(p)
             for p in ("odds_api", "api_football", "gemini")}
    rows = "".join(
        f"<tr><td>{f['match_id']}</td><td>{f['window']}</td>"
        f"<td>{f['status']}</td><td>{f.get('detail','') or ''}</td></tr>"
        for f in runs().recent(72))
    quota_rows = "".join(
        f"<tr><td>{p}</td><td>{q.get('used','-')}/{q.get('budget','-')}</td>"
        f"<td>{'⚠️' if q.get('warn') else 'ok'}</td></tr>"
        for p, q in quota.items())
    # per-provider metrics (calls / tokens / avg latency / cost) from the ledger
    prov_rows = ""
    for p in ("football_data", "odds_api", "api_football", "claude", "gemini", "openai"):
        u = ledger().usage(p)
        if u["calls"]:
            prov_rows += (f"<tr><td>{p}</td><td>{u['calls']}</td><td>{u['tokens']}</td>"
                          f"<td>${u['est_cost']}</td></tr>")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Mondial 2026 — dashboard</title>
<style>body{{font-family:system-ui;margin:2rem;max-width:900px}}
h1{{color:#1F4E78}}table{{border-collapse:collapse;width:100%;margin:1rem 0}}
td,th{{border:1px solid #ccc;padding:6px;text-align:left}}
.k{{display:inline-block;background:#DDEBF7;padding:4px 10px;border-radius:6px;margin:4px}}</style>
</head><body>
<h1>Mondial 2026 — system dashboard</h1>
<p>generated {datetime.now():%Y-%m-%d %H:%M}</p>
<h2>Run health (72h)</h2>
<span class="k">total {summary['total']}</span>
<span class="k">ok {summary['ok']}</span>
<span class="k">failed {summary['failed']}</span>
<span class="k">stuck {summary['stuck']}</span>
<span class="k">fallbacks {summary['fallbacks']}</span>
<span class="k">cards {summary['cards_delivered']}</span>
<h2>Recent runs</h2>
<table><tr><th>match</th><th>window</th><th>status</th><th>detail</th></tr>{rows}</table>
<h2>Quota usage</h2>
<table><tr><th>provider</th><th>used/budget</th><th>state</th></tr>{quota_rows}</table>
<h2>Provider metrics (calls / tokens / est. cost)</h2>
<table><tr><th>provider</th><th>calls</th><th>tokens</th><th>est. cost</th></tr>{prov_rows}</table>
<p>Latest picks: see <code>reports/feed.md</code>. Per-game metrics:
<code>python -m tools.metrics match-&lt;id&gt;-T-7m</code></p>
</body></html>"""


def write(path: str | None = None) -> str:
    os.makedirs(REPORTS_DIR, exist_ok=True)
    path = path or os.path.join(REPORTS_DIR, "dashboard.html")
    with open(path, "w") as f:
        f.write(render_html())
    return path


if __name__ == "__main__":
    print("wrote", write())
