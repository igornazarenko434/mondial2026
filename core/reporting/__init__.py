"""Telegram-message rendering helpers shared across daemon hooks + CLI tools.

Why a new package: people-block rendering is used by FOUR call sites — the
📊 standings sync, the ☀️ daily summary, the ⚽ T+1m kickoff card, and the
🃏 per-match T-60m/-15m/-7m cards. One renderer keeps them visually
consistent and makes "add a field" a single-file change.
"""
