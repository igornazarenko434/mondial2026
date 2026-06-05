"""Delivery facade — fan out cards/alerts to the configured channels.

    DELIVERY_CHANNELS="file,telegram,console"   (default "file,console")

    from core import delivery
    delivery.deliver_card(card)          # the per-game recommendation
    delivery.alert("Pipeline FAILED", "Norway vs France: odds source down")
    delivery.health(summary_dict)        # daily run summary
"""
from __future__ import annotations
import os
from core.delivery.base import render_card
from core.delivery.channels import REGISTRY
from core.obs.logging import get_logger

log = get_logger("delivery")


def _channels():
    names = os.environ.get("DELIVERY_CHANNELS", "file,console").split(",")
    out = []
    for n in (x.strip() for x in names):
        ch = REGISTRY.get(n)
        if ch and ch.available():
            out.append(ch)
        elif ch:
            log.info("delivery channel '%s' configured but unavailable (missing creds)", n)
    return out or [REGISTRY["file"]]   # always fall back to a file


def _fanout(title: str, body: str) -> bool:
    delivered = False
    for ch in _channels():
        try:
            ch.send(title, body)
            delivered = True
        except Exception as e:  # noqa: BLE001 - one channel failing shouldn't lose the rest
            log.warning("delivery via '%s' failed: %s", ch.name, e)
    return delivered


def deliver_card(card: dict) -> bool:
    title = f"{card.get('home','?')} vs {card.get('away','?')} — pick"
    return _fanout(title, render_card(card))


def alert(title: str, body: str) -> bool:
    return _fanout(f"⚠️ {title}", body)


def health(summary: dict) -> bool:
    body = (f"runs: {summary['total']} | ok: {summary['ok']} | "
            f"failed: {summary['failed']} | stuck: {summary['stuck']} | "
            f"fallbacks: {summary['fallbacks']} | cards: {summary['cards_delivered']}")
    if summary.get("failures"):
        body += "\nfailures:\n" + "\n".join(
            f"• match {f['match_id']} {f['window']}: {f.get('detail','?')}"
            for f in summary["failures"])
    return _fanout("📊 Mondial run summary", body)
