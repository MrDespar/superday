"""The topic vocabulary, defined once.

It used to be spelled out separately in the two LLM schemas, the `list`
parser, the completion table and the mock spreads. That meant adding a topic
in four places and discovering the fifth when a Gemini call 400'd on an enum
it had never been told about -- Gemini validates a `responseSchema` enum
before it checks anything else, so the whole batch failed rather than one
item.

This module deliberately imports nothing. `llm.py` holds the dependency-free
clients and must stay that way, so the list cannot live in `admission.py`
where it would drag `sqlite3` and `db` into them.

`selftest` fails if any rule in `admission.TOPIC_RULES`, any stored row, or
any schema enum names a topic that is not here.
"""
from __future__ import annotations

# Order is display order: the generalist technicals first in the sequence an
# interview actually walks them, then the three product topics, then market
# awareness, fit, and the catch-all.
TOPICS: tuple[str, ...] = (
    "accounting",
    "ev_eqv",
    "valuation",
    "dcf",
    "ma",
    "lbo",
    "dcm",
    "ecm",
    "deal_process",
    "markets",
    "behavioural",
    "general",
)

# Human labels, for anywhere a bare `deal_process` would read as a column name
# rather than a thing you study.
TOPIC_LABELS: dict[str, str] = {
    "accounting": "Accounting",
    "ev_eqv": "EV / Equity Value",
    "valuation": "Valuation",
    "dcf": "DCF",
    "ma": "M&A",
    "lbo": "LBO",
    "dcm": "Debt Capital Markets",
    "ecm": "Equity Capital Markets",
    "deal_process": "Deal Process & Mechanics",
    "markets": "Market Awareness",
    "behavioural": "Behavioural",
    "general": "General",
}


def label(topic: str | None) -> str:
    return TOPIC_LABELS.get(topic or "", topic or "-")
