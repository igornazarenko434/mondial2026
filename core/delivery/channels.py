"""Delivery channels. FileReport needs zero setup; Telegram is the recommended
minimal phone-push channel; Console is for local runs/tests."""
from __future__ import annotations
import os
from datetime import datetime
from core.delivery.base import Notifier
from core.obs.logging import get_logger

log = get_logger("delivery")
REPORTS_DIR = os.environ.get("REPORTS_DIR",
                             os.path.join(os.path.dirname(__file__), "..", "..", "reports"))


class ConsoleNotifier(Notifier):
    name = "console"
    def available(self): return True
    def send(self, title, body):
        print(f"\n=== {title} ===\n{body}\n")


class FileReport(Notifier):
    """Writes each item to reports/<ts>.md and appends to reports/feed.md.
    Always available — your durable, browsable record with no infra."""
    name = "file"
    def available(self): return True
    def send(self, title, body):
        os.makedirs(REPORTS_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe = "".join(c if c.isalnum() else "_" for c in title)[:40]
        path = os.path.join(REPORTS_DIR, f"{ts}_{safe}.md")
        with open(path, "w") as f:
            f.write(f"# {title}\n\n{body}\n")
        with open(os.path.join(REPORTS_DIR, "feed.md"), "a") as f:
            f.write(f"\n## {ts} — {title}\n\n{body}\n\n---\n")
        log.info("report written: %s", path)


class TelegramNotifier(Notifier):
    """Phone push via the Telegram Bot HTTP API (no library needed).
    Set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID. ~20 min one-time setup, $0.

    Sends plain text (no parse_mode): card bodies contain dynamic substrings
    like 'with_detonator' whose underscores would break Markdown parsing and
    return 400. Plain text is reliable and renders cleanly on mobile.
    """
    name = "telegram"
    def available(self):
        return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))
    def send(self, title, body):
        import requests
        token = os.environ["TELEGRAM_BOT_TOKEN"]
        chat = os.environ["TELEGRAM_CHAT_ID"]
        text = f"{title}\n{body}"
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text},
                          timeout=15)
        r.raise_for_status()


REGISTRY = {n.name: n for n in (FileReport(), TelegramNotifier(), ConsoleNotifier())}
