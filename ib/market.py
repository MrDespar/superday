"""Market awareness questions.

These break the assumption the rest of the bank rests on: their answer expires.
So nothing is stored as a fact. The question carries a binding to a live series,
the value is fetched at drill time (cached for its TTL), and grading is a
numeric tolerance check rather than a rubric.

Both providers here need no API key.
"""
from __future__ import annotations

import csv
import io
import re
import sqlite3
import ssl
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import certifi

from . import tagging, ui
from .db import now

UA = {"User-Agent": "superday/0.1 (personal interview prep)"}

# Python on macOS does not use the system keychain, so stdlib urllib fails
# every https call with CERTIFICATE_VERIFY_FAILED until it is pointed at a CA
# bundle explicitly. certifi ships one.
_SSL = ssl.create_default_context(cafile=certifi.where())

TREASURY_CSV = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve"
    "&field_tdr_date_value={year}&page&_format=csv"
)
ECB_XML = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"


def _get(url: str, timeout: int = 25) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
        return r.read()


def _treasury_rows(year: int) -> list[dict]:
    try:
        text = _get(TREASURY_CSV.format(year=year)).decode("utf8", "ignore")
        return [r for r in csv.DictReader(io.StringIO(text)) if r.get("Date")]
    except Exception:
        return []


def fetch_treasury(today: datetime | None = None) -> dict[str, tuple[float, str]]:
    """Latest daily par yield curve. Returns {series_key: (value, as_of)}.

    `today` is injectable so the January fallback can be tested in August.
    """
    today = today or datetime.now(timezone.utc)
    rows = _treasury_rows(today.year)
    # Last year's file is the right answer for the few days before the new
    # year's first print, and is never the right answer after that: in August
    # it hands back the 31 December curve, which the cache then stores with a
    # fetched_at of now and every reader downstream calls current. Falling
    # back on a transient failure in month eight cost a whole day of drills
    # graded against a curve 50bp away from the real one, so the fallback is
    # now bounded to the window it was written for. Outside January a feed
    # that cannot be read returns nothing, and the caller serves its cache
    # marked stale rather than being handed a fresher-looking wrong number.
    if not rows and today.month == 1:
        rows = _treasury_rows(today.year - 1)
    if not rows:
        return {}
    latest = max(rows, key=lambda r: datetime.strptime(r["Date"], "%m/%d/%Y"))
    as_of = datetime.strptime(latest["Date"], "%m/%d/%Y").date().isoformat()

    out: dict[str, tuple[float, str]] = {}
    for k, v in latest.items():
        if k == "Date" or not v:
            continue
        try:
            out[k.strip()] = (float(v), as_of)
        except ValueError:
            continue
    if "10 Yr" in out and "2 Yr" in out:
        out["2s10s"] = (round(out["10 Yr"][0] - out["2 Yr"][0], 3), as_of)
    if "10 Yr" in out and "3 Mo" in out:
        out["3m10y"] = (round(out["10 Yr"][0] - out["3 Mo"][0], 3), as_of)
    return out


def fetch_ecb() -> dict[str, tuple[float, str]]:
    """ECB daily euro reference rates, quoted per 1 EUR."""
    root = ET.fromstring(_get(ECB_XML))
    ns = {"e": "http://www.ecb.int/vocabulary/2002-08-01/eurofxref"}
    day = root.find(".//e:Cube[@time]", ns)
    if day is None:
        return {}
    as_of = day.attrib["time"]
    return {
        c.attrib["currency"]: (float(c.attrib["rate"]), as_of)
        for c in day.findall("e:Cube", ns)
    }


# The ECB Data Portal. No key, no registration, one series per request.
#
# This is a separate provider from `fetch_ecb`, which pulls the daily FX
# reference rates off a static XML file and nothing else. Everything a
# European desk actually opens with -- the policy rate, the Bund, Euribor,
# the BTP spread -- lives in the statistical warehouse behind this API and
# needed its own fetcher.
ECB_DATA = "https://data-api.ecb.europa.eu/service/data/{key}?lastNObservations=1&format=csvdata"

# label -> (dataflow/series key, note). One request each: batching within a
# dataflow is possible but a single 404 would then take the whole group down,
# and this runs rarely enough that twelve small requests is the cheaper trade.
ECB_SERIES: dict[str, str] = {
    # Policy corridor. All three, because "where are ECB rates" is answered
    # with the corridor and a candidate who gives only the depo rate is
    # giving two thirds of an answer.
    "depo":      "FM/D.U2.EUR.4F.KR.DFR.LEV",
    "mro":       "FM/D.U2.EUR.4F.KR.MRR_FR.LEV",
    "mlf":       "FM/D.U2.EUR.4F.KR.MLFR.LEV",
    "estr":      "EST/B.EU000A2X2A25.WT",
    # The AAA-rated euro area central government spot curve. Germany dominates
    # it, so the 10y is the everyday proxy for the Bund and is what moves
    # daily. It is *not* literally the Bund yield, which is why the question
    # bound to it says euro area AAA.
    "aaa 2y":    "YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y",
    "aaa 5y":    "YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_5Y",
    "aaa 10y":   "YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y",
    "aaa 30y":   "YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_30Y",
    # Monthly. Euribor is not published daily through this API, so the value
    # is a month-average close and the tolerance on the bound question is set
    # wider to match.
    "euribor 3m": "FM/M.U2.EUR.RT.MM.EURIBOR3MD_.HSTA",
    # Long-term rates for convergence purposes: the 10-year benchmark
    # government yield per member state, monthly. The German one is the actual
    # Bund; the Italian one gives the BTP spread everyone quotes.
    "de 10y":    "IRS/M.DE.L.L40.CI.0000.EUR.N.Z",
    "it 10y":    "IRS/M.IT.L.L40.CI.0000.EUR.N.Z",
    "fr 10y":    "IRS/M.FR.L.L40.CI.0000.EUR.N.Z",
}


def fetch_ecb_data() -> dict[str, tuple[float, str]]:
    """Euro area policy rates, curve points and benchmark yields.

    One series per request, and a failure on any one is skipped rather than
    raised: a 404 on a renamed key should cost you that number, not the whole
    euro area panel on the morning of an interview.
    """
    out: dict[str, tuple[float, str]] = {}
    for label, key in ECB_SERIES.items():
        try:
            text = _get(ECB_DATA.format(key=key), timeout=15).decode("utf8", "ignore")
            rows = list(csv.DictReader(io.StringIO(text)))
        except Exception:
            continue
        if not rows:
            continue
        row = rows[-1]
        try:
            out[label] = (round(float(row["OBS_VALUE"]), 3), row["TIME_PERIOD"])
        except (KeyError, TypeError, ValueError):
            continue

    # Derived series. Computed here rather than asked of the API so they carry
    # the as-of date of the legs they were built from and cannot silently mix
    # two different observation dates.
    def spread(name: str, a: str, b: str) -> None:
        if a in out and b in out and out[a][1] == out[b][1]:
            out[name] = (round(out[a][0] - out[b][0], 3), out[a][1])

    spread("eur 2s10s", "aaa 10y", "aaa 2y")
    spread("btp-bund", "it 10y", "de 10y")
    spread("oat-bund", "fr 10y", "de 10y")
    return out


PROVIDERS = {"treasury": fetch_treasury, "ecb": fetch_ecb, "ecb_data": fetch_ecb_data}

# How old the newest print may be before it stops being an answer.
#
# Fetch time and observation time are different questions, and the cache only
# ever answered the first. Nothing here is listed series by series: the stamp
# says which cadence it belongs to, because `2026-08-14` is a daily print and
# `2026-07` a monthly one, so a monthly series added to ECB_SERIES tomorrow is
# handled the day it lands.
#
# Daily gets a week - a Friday print read on the Tuesday after a long weekend
# is still the number a desk would quote. Monthly gets a quarter, because
# July's figure publishes in August and is still the latest print deep into
# September. A tighter bound there would fire on a value that is correct, and
# a staleness warning you have learned to ignore is worth less than none.
DAILY_MAX_AGE = timedelta(days=7)
MONTHLY_MAX_AGE = timedelta(days=100)

# An old print is worth asking the feed about again - the last fetch may have
# caught it mid-failure - but once per quarter of an hour, not once per
# question in the drill.
STALE_RETRY = timedelta(minutes=15)


def observation_stale(as_of: str | None) -> bool:
    """True when the newest print on file is too old to be the answer.

    A provider handing back an old observation writes it with a fetched_at of
    now, so every check that measured the fetch called it fresh. That is the
    one failure this module exists to prevent: an eight-month-old 10-year
    yield reported as current marks a correct answer wrong.
    """
    if not as_of:
        return False
    try:
        if len(as_of) == 7:            # YYYY-MM: a monthly print
            obs = datetime.strptime(as_of, "%Y-%m")
            limit = MONTHLY_MAX_AGE
        else:
            obs = datetime.strptime(as_of[:10], "%Y-%m-%d")
            limit = DAILY_MAX_AGE
    except ValueError:
        return False                   # unreadable stamp: not a staleness claim
    return datetime.now(timezone.utc) - obs.replace(tzinfo=timezone.utc) > limit


def _verdict(value: float, as_of: str) -> tuple[float, str, str | None]:
    """A served value plus why it should not be trusted, if it should not."""
    return value, as_of, (f"no print since {as_of}" if observation_stale(as_of) else None)


# The SQL every QA pass uses to skip a question whose answer is not on file.
#
# It used to be `kind != 'market_awareness'`, on the reasoning that a market
# question resolves its answer from a live feed at drill time so there is
# nothing stored to check. True of a *bound* one. Six questions were filed as
# market_awareness with no row in `live_bindings` at all, so nothing would ever
# resolve them -- and what they carried instead was a stored answer full of
# levels ("deposit facility 2.25%", "around 3.16%, as of yesterday"). The kind
# test made those the only questions in the bank that `enrich`, `audit` and
# `cross-audit` had all never looked at, which is precisely backwards: an
# expiring number nobody checks is the worst thing the bank can hold.
#
# So the question is whether a live binding exists, which is the thing that
# actually decides whether there is a stored fact.
UNBOUND_SQL = ("NOT EXISTS (SELECT 1 FROM live_bindings b "
               "WHERE b.question_id = q.id)")


def value_for(conn: sqlite3.Connection, provider: str, series_key: str,
              ttl_seconds: int = 86400) -> tuple[float | None, str | None, str | None]:
    """Latest value for a series, as (value, as_of, stale).

    `stale` is None when the number is one you can say out loud, and a short
    reason when it is not. Four outcomes, and the caller has to be able to
    tell them apart:
      - fresh from cache or a live fetch     -> (value, as_of, None)
      - the feed is down but a cached figure -> (value, as_of, "feed unreachable")
      - the newest print is months old       -> (value, as_of, "no print since ...")
      - no feed and nothing cached           -> (None, None, "no data")

    The old signature could not distinguish "the 10-year is 4.7" from "the
    10-year was 4.7 a fortnight ago", which in a market-awareness drill is the
    difference between a correct answer and a wrong one. Offline is a normal
    state for this tool, not an error -- but it has to be visible.

    Staleness is a property of the observation as well as of the fetch. A
    fetch that succeeds and returns an old print is the case the fetch clock
    cannot see, and it is the one that reads as fresh while being wrong.
    """
    row = conn.execute(
        "SELECT value, as_of, fetched_at FROM live_cache WHERE provider = ? AND series_key = ?",
        (provider, series_key),
    ).fetchone()
    if row:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(row["fetched_at"])
        if age < timedelta(seconds=ttl_seconds):
            # An old print inside its TTL is worth one more ask, so a cache
            # poisoned at 09:00 is not still being drilled off at 17:00.
            if not observation_stale(row["as_of"]) or age < STALE_RETRY:
                return _verdict(row["value"], row["as_of"])

    def stored(reason: str) -> tuple[float | None, str | None, str | None]:
        # Serve stale rather than nothing, but never quietly: a silent
        # fallback to a two-week-old yield is worse than no yield at all.
        if not row:
            return None, None, "no data"
        return row["value"], row["as_of"], reason

    try:
        data = PROVIDERS[provider]()
    except Exception as e:
        print(ui.warn(f"  {provider} unreachable")
              + ui.dim(f" - {type(e).__name__}; serving the stored value"))
        return stored("feed unreachable")

    if not data:
        return stored("feed returned nothing")

    stamp = now()
    conn.executemany(
        "INSERT INTO live_cache (provider, series_key, value, as_of, fetched_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(provider, series_key) DO UPDATE SET "
        "value = excluded.value, as_of = excluded.as_of, fetched_at = excluded.fetched_at",
        [(provider, key, val, as_of, stamp) for key, (val, as_of) in data.items()],
    )
    conn.commit()
    got = data.get(series_key)
    if got:
        return _verdict(got[0], got[1])
    return stored("the feed no longer carries this series")


def cache_age(conn: sqlite3.Connection, provider: str) -> float | None:
    """Hours since this provider was last successfully fetched."""
    row = conn.execute(
        "SELECT MAX(fetched_at) f FROM live_cache WHERE provider = ?", (provider,)
    ).fetchone()
    if not row or not row["f"]:
        return None
    return (datetime.now(timezone.utc) - datetime.fromisoformat(row["f"])).total_seconds() / 3600.0


def refresh(conn: sqlite3.Connection) -> dict[str, tuple[bool, str]]:
    """Pull every provider into the cache so a later drill works offline.

    Returns {provider: (went well, what happened)}. A provider that answers
    with an old print did not go well: it is the shape of failure that looks
    like success, so it is reported as loudly as a dead connection.

    Worth running before a flight or before an interview: the cache is the
    only thing standing between a market-awareness drill and a dead feed.
    """
    out: dict[str, tuple[bool, str]] = {}
    for name, fetch in PROVIDERS.items():
        try:
            data = fetch()
        except Exception as e:
            out[name] = (False, f"failed: {type(e).__name__}")
            continue
        if not data:
            out[name] = (False, "empty response")
            continue
        stamp = now()
        conn.executemany(
            "INSERT INTO live_cache (provider, series_key, value, as_of, fetched_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(provider, series_key) DO UPDATE SET "
            "value = excluded.value, as_of = excluded.as_of, fetched_at = excluded.fetched_at",
            [(name, key, val, as_of, stamp) for key, (val, as_of) in data.items()],
        )
        newest = max(as_of for _, as_of in data.values())
        out[name] = ((True, f"{len(data)} series")
                     if not observation_stale(newest)
                     else (False, f"{len(data)} series, nothing newer than {newest}"))
    conn.commit()
    return out


def extract_number(answer: str) -> float | None:
    """Pull the figure out of a spoken-style answer: '10y is about 4.7' -> 4.7,
    'around 50bps' -> 0.50."""
    txt = answer.lower().replace(",", "")
    bp = re.search(r"(-?\d+(?:\.\d+)?)\s*(?:bps?|basis points?)\b", txt)
    if bp:
        return float(bp.group(1)) / 100.0
    m = re.search(r"-?\d+(?:\.\d+)?", txt)
    return float(m.group(0)) if m else None


SEEDS = [
    ("Where is the US 10-year Treasury yield trading?", "treasury", "10 Yr", "%", 0.10),
    ("Where is the US 2-year Treasury yield?", "treasury", "2 Yr", "%", 0.10),
    ("What is the 2s10s spread right now, and what does it imply?", "treasury", "2s10s", "%", 0.10),
    ("Where is the 30-year Treasury yield?", "treasury", "30 Yr", "%", 0.15),
    ("Where is 3-month T-bill yield?", "treasury", "3 Mo", "%", 0.10),
    ("Where is EURUSD trading?", "ecb", "USD", "USD per EUR", 0.02),
    ("Where is GBP against the euro?", "ecb", "GBP", "GBP per EUR", 0.02),
    ("Where is CHF against the euro?", "ecb", "CHF", "CHF per EUR", 0.02),
    # The euro area panel. Nobody in Munich or Frankfurt opens with the US
    # 10-year, and being a week out on the Bund reads worse than any technical
    # slip -- so these are bound live rather than stored as a fact that goes
    # stale the day after it is written.
    ("Where is the ECB deposit facility rate?", "ecb_data", "depo", "%", 0.05),
    ("Where is the ECB main refinancing rate?", "ecb_data", "mro", "%", 0.05),
    ("Where is the ECB marginal lending rate?", "ecb_data", "mlf", "%", 0.05),
    ("Where is €STR fixing?", "ecb_data", "estr", "%", 0.05),
    ("Where is the euro area AAA 10-year spot rate, the everyday proxy for the Bund?",
     "ecb_data", "aaa 10y", "%", 0.12),
    ("Where is the euro area AAA 2-year spot rate?", "ecb_data", "aaa 2y", "%", 0.12),
    ("Where is the euro area AAA 5-year spot rate, the tenor most EUR benchmarks price at?",
     "ecb_data", "aaa 5y", "%", 0.12),
    ("Where is the euro area AAA 30-year spot rate?", "ecb_data", "aaa 30y", "%", 0.15),
    ("What is the euro 2s10s spread, and what is the curve telling you?",
     "ecb_data", "eur 2s10s", "%", 0.12),
    ("Where is 3-month Euribor, the reference for a floating rate note?",
     "ecb_data", "euribor 3m", "%", 0.15),
    ("Where is the German 10-year benchmark yield?", "ecb_data", "de 10y", "%", 0.15),
    ("What is the BTP-Bund spread, and why is it the European risk barometer?",
     "ecb_data", "btp-bund", "%", 0.20),
    ("What is the OAT-Bund spread?", "ecb_data", "oat-bund", "%", 0.15),
]


def seed(conn: sqlite3.Connection) -> int:
    """Install the starter market-awareness questions. Idempotent."""
    from .admission import admit

    source_id = conn.execute(
        "SELECT id FROM sources WHERE kind = 'live' AND title = 'Live market data'"
    ).fetchone()
    if source_id is None:
        cur = conn.execute(
            "INSERT INTO sources (kind, title, added_at) VALUES ('live', 'Live market data', ?)",
            (now(),),
        )
        sid = int(cur.lastrowid)
    else:
        sid = source_id["id"]

    added = 0
    for text, provider, key, unit, tol in SEEDS:
        v = admit(
            conn,
            source_id=sid,
            question_text=text,
            answer_text=None,
            locator=f"{provider}:{key}",
            kind="market_awareness",
            status="active",           # no review needed, the answer is a number
        )
        qid = v.matched_id
        # Tagged on every run rather than only when the question is new, and
        # from what the seed row already knows rather than from its wording.
        # Whether one of these ended up tagged used to be an accident of
        # whether the lexical rules fired: "What is the 2s10s spread" matched
        # and got `market-awareness`, "Where is EURSTR fixing?" matched
        # nothing and got none -- twelve of the euro panel carried no tag at
        # all, so they were missing from the tag map and from every `tag:`
        # filter. The unit is what separates a cross rate from a yield.
        tagging.attach(conn, qid, ["market-awareness",
                                   "fx" if " per " in unit else "rates"])
        if v.kind != "new":
            continue
        conn.execute(
            "UPDATE questions SET topic = 'markets', difficulty = 2 WHERE id = ?", (qid,)
        )
        conn.execute("UPDATE answers SET answer_status = 'volatile' WHERE question_id = ?", (qid,))
        conn.execute(
            "INSERT OR REPLACE INTO live_bindings "
            "(question_id, provider, series_key, unit, tolerance) VALUES (?, ?, ?, ?, ?)",
            (qid, provider, key, unit, tol),
        )
        added += 1
    conn.commit()
    return added
