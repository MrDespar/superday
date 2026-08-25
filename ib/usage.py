"""What has actually been asked of the provider, recorded locally.

There is no endpoint for "how much quota is left". Google publishes free-tier
rate limits only inside AI Studio, behind a login, and the API answers no
question about your standing until the moment it refuses a call. So this is
not a balance and it is deliberately not drawn as one: it is a log of the
calls *this tool* made, plus the refusals the provider actually sent back.

Three things follow from that and none of them are cosmetic:

- **Calls made is a fact; calls remaining is not.** Every number in here is
  counted from rows written after a request went out. Where a limit is shown
  it is a limit *you* put in `settings`, and it is labelled as yours.
- **A 429 is the only authoritative signal there is.** When the provider
  refuses, it says so and often says for how long (`retryDelay`). Those rows
  are the closest thing to a real quota reading available, so they are kept
  apart from the ordinary failures rather than averaged into them.
- **Nothing here may break a drill.** A logging failure is not a reason to
  lose a graded answer, so `record` swallows everything. A log that
  occasionally misses a row is worth having; one that can raise mid-sitting
  is not.

A file rather than a table, because `llm.py` runs on a worker thread and must
never hold a database connection -- that rule is what keeps the spinner
moving while a call is in flight. Appending a line needs no connection, no
migration and no schema on the derived/permanent line at all.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import home

# Rows are small (~200 bytes), so this is a few megabytes at the cap. `prune`
# is called by the command that reads the log rather than by the one that
# writes it: an append that sometimes rewrites the whole file is an append
# that sometimes blocks a drill.
MAX_ROWS = 20_000

_lock = threading.Lock()


def path() -> Path:
    """Where the log lives. `IB_USAGE_LOG` moves it, which is what the tests
    use -- they must never append to the real one."""
    override = os.environ.get("IB_USAGE_LOG")
    return Path(override) if override else home() / "usage.jsonl"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record(*, provider: str, caller: str, model: str, outcome: str,
           prompt_tokens: int | None = None, output_tokens: int | None = None,
           total_tokens: int | None = None, thinking_tokens: int | None = None,
           status: int | None = None, reason: str = "",
           retry_after: float | None = None, seconds: float | None = None) -> None:
    """Append one call. Never raises: a broken log must not lose an answer."""
    row = {
        "at": now(), "provider": provider, "caller": caller or "unknown",
        "model": model, "outcome": outcome,
    }
    for name, value in (("prompt_tokens", prompt_tokens),
                        ("output_tokens", output_tokens),
                        ("total_tokens", total_tokens),
                        ("thinking_tokens", thinking_tokens),
                        ("status", status), ("retry_after", retry_after),
                        ("seconds", round(seconds, 2) if seconds else None)):
        if value is not None:
            row[name] = value
    if reason:
        row["reason"] = reason[:200]
    try:
        line = json.dumps(row, ensure_ascii=False) + "\n"
        with _lock:
            with path().open("a", encoding="utf8") as fh:
                fh.write(line)
    except Exception:
        # Deliberately bare. Nothing this module does is worth interrupting a
        # sitting for, and the one thing worse than no usage log is a usage
        # log that can raise out of `grade`.
        pass


def tokens_from(payload: dict) -> dict:
    """Gemini's `usageMetadata`, in the names this log uses.

    The only per-call usage signal the API gives at all, and the field names
    have moved before, so anything missing stays missing rather than becoming
    a zero -- a zero here would read as "that call was free".
    """
    meta = (payload or {}).get("usageMetadata") or {}

    def get(*names):
        for name in names:
            value = meta.get(name)
            if isinstance(value, int):
                return value
        return None

    return {
        "prompt_tokens": get("promptTokenCount"),
        "output_tokens": get("candidatesTokenCount"),
        "total_tokens": get("totalTokenCount"),
        "thinking_tokens": get("thoughtsTokenCount", "thinkingTokenCount"),
    }


# ---------------------------------------------------------------- reading


def entries(*, since: datetime | None = None, limit: int | None = None) -> list[dict]:
    """Every logged call, oldest first. A damaged line is skipped, not fatal."""
    p = path()
    if not p.exists():
        return []
    out: list[dict] = []
    try:
        text = p.read_text(encoding="utf8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or "at" not in row:
            continue
        if since is not None and _at(row) < since:
            continue
        out.append(row)
    return out[-limit:] if limit else out


def _at(row: dict) -> datetime:
    try:
        when = datetime.fromisoformat(row.get("at") or "")
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def within(rows: list[dict], seconds: float,
           end: datetime | None = None) -> list[dict]:
    end = end or datetime.now(timezone.utc)
    start = end - timedelta(seconds=seconds)
    return [r for r in rows if start <= _at(r) <= end]


def since_midnight(rows: list[dict], end: datetime | None = None) -> list[dict]:
    """Today's calls, UTC.

    UTC rather than local time because that is the day a per-day quota is
    counted against; a local-midnight reading would disagree with the
    provider's by however many hours you are offset from it.
    """
    end = end or datetime.now(timezone.utc)
    start = end.replace(hour=0, minute=0, second=0, microsecond=0)
    return [r for r in rows if _at(r) >= start]


def refusals(rows: list[dict]) -> list[dict]:
    """The calls the provider itself turned away.

    Kept apart from ordinary failures because they mean something different:
    a 500 is the provider having a bad minute, a 429 is the provider telling
    you where your quota actually is -- which is the one authoritative reading
    available anywhere.
    """
    return [r for r in rows if r.get("status") == 429]


def tally(rows: list[dict], field: str = "caller") -> list[tuple[str, dict]]:
    """Group rows and total them, busiest first."""
    groups: dict[str, dict] = {}
    for row in rows:
        g = groups.setdefault(str(row.get(field) or "unknown"), {
            "calls": 0, "ok": 0, "failed": 0, "refused": 0,
            "prompt_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            "thinking_tokens": 0, "seconds": 0.0, "counted": 0})
        g["calls"] += 1
        if row.get("outcome") == "ok":
            g["ok"] += 1
        else:
            g["failed"] += 1
        if row.get("status") == 429:
            g["refused"] += 1
        for name in ("prompt_tokens", "output_tokens", "total_tokens",
                     "thinking_tokens"):
            value = row.get(name)
            if isinstance(value, int):
                g[name] += value
                if name == "total_tokens":
                    g["counted"] += 1
        if isinstance(row.get("seconds"), (int, float)):
            g["seconds"] += row["seconds"]
    return sorted(groups.items(), key=lambda kv: -kv[1]["calls"])


def prune(keep: int = MAX_ROWS) -> int:
    """Drop all but the newest `keep` rows. Returns how many went."""
    p = path()
    if not p.exists():
        return 0
    try:
        lines = p.read_text(encoding="utf8", errors="replace").splitlines()
    except OSError:
        return 0
    if len(lines) <= keep:
        return 0
    with _lock:
        p.write_text("\n".join(lines[-keep:]) + "\n", encoding="utf8")
    return len(lines) - keep


def clear() -> None:
    p = path()
    if p.exists():
        p.unlink()
