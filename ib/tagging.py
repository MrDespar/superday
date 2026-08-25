"""Concept and firm tags.

Topics are coarse -- "accounting" is 177 questions -- and the thing you are
actually weak at is narrower than that: the EV bridge, or unlevering beta, or
the debt schedule. Tags are the narrow axis. They cut across topics too: a
deferred-tax question is accounting on one source and M&A on another, and both
are `#dta-dtl`.

The taxonomy below is matched lexically, no LLM and no API key. That is a
deliberate ceiling: a keyword hit is evidence a question *is about* a concept,
never evidence about how good the answer is. Anything needing judgement stays
with `enrich`.
"""
from __future__ import annotations

import re
import sqlite3

# concept tag -> the phrases that mean it. Matched against question + answer,
# word-boundary anchored, case-insensitive.
CONCEPTS: dict[str, list[str]] = {
    "3-statement-linkage": [
        "three statements", "3 statements", "three financial statements",
        "link the statements", "flow through the", "how does .{0,30} affect the",
        "walk me through .{0,20}statements", "tie together",
    ],
    "ev-bridge": [
        "enterprise value", "equity value to enterprise", "bridge",
        "net debt", "tev", "from equity value",
    ],
    "wacc": ["wacc", "weighted average cost of capital", "cost of capital"],
    "capm": ["capm", "cost of equity", "risk[- ]free rate", "equity risk premium",
             "market risk premium"],
    "beta": ["beta", "unlever", "relever", "levered beta", "unlevered beta"],
    "dcf": ["dcf", "discounted cash flow", "discount rate", "present value"],
    "terminal-value": ["terminal value", "gordon growth", "perpetuity growth",
                       "exit multiple method"],
    "free-cash-flow": ["free cash flow", "unlevered fcf", "levered fcf", "ufcf", "lfcf"],
    "working-capital": ["working capital", "receivable", "payable", "inventory turn",
                        "cash conversion cycle", "days sales outstanding", "dso", "dpo"],
    "deferred-revenue": ["deferred revenue", "unearned revenue"],
    "dta-dtl": ["deferred tax", "dta", "dtl", "book.{0,10}tax difference"],
    "goodwill": ["goodwill", "impairment", "intangible"],
    "purchase-price-allocation": ["purchase price allocation", "ppa", "write[- ]up",
                                  "step[- ]up", "allocation of purchase price"],
    "accretion-dilution": ["accretion", "dilution", "accretive", "dilutive", "eps impact",
                           "pro forma eps"],
    "synergies": ["synergy", "synergies", "cost savings", "revenue synergy"],
    "merger-model": ["merger model", "combined company", "pro forma", "acquirer",
                     "consideration mix"],
    "lbo-returns": ["irr", "moic", "money multiple", "cash[- ]on[- ]cash", "returns to the sponsor"],
    "irr-drivers": ["drives the irr", "irr driver", "sources of return", "value creation bridge",
                    "multiple expansion", "deleveraging"],
    "debt-schedule": ["debt schedule", "cash sweep", "revolver", "mandatory amortization",
                      "optional prepayment", "paydown"],
    "credit-stats": ["leverage ratio", "coverage ratio", "debt/ebitda", "debt to ebitda",
                     "interest coverage", "fixed charge", "credit stat"],
    "capital-structure": ["capital structure", "seniority", "subordinated", "mezzanine",
                          "waterfall", "priority of claims"],
    # Deliberately not the bare word "multiple": it appears in a third of the
    # bank as ordinary English ("multiple ways to value a company") and tagging
    # on it turns `--tag multiples` into "most of the bank", which is no filter
    # at all.
    "multiples": ["ev/ebitda", "ev/ebit", "p/e", "price to earnings", "ev/revenue",
                  "ev/sales", "trading multiple", "valuation multiple",
                  "multiples? (?:analysis|approach|method)", "forward multiple",
                  "ltm multiple", "ntm multiple"],
    "comps": ["comparable compan", "trading comps", "public comps", "peer group"],
    "precedents": ["precedent transaction", "transaction comps", "deal comps",
                   "control premium"],
    "sum-of-the-parts": ["sum of the parts", "sotp", "segment valuation"],
    "normalization": ["normalize", "non[- ]recurring", "one[- ]time", "adjusted ebitda",
                      "add[- ]back"],
    "ebitda-bridge": ["ebitda to", "net income to ebitda", "from ebitda to"],
    "nol": ["net operating loss", "nols", "tax loss carryforward"],
    "sbc": ["stock[- ]based compensation", "share[- ]based compensation", "sbc",
            "options", "treasury stock method", "rsu"],
    "minority-interest": ["minority interest", "noncontrolling", "non[- ]controlling",
                          "associates", "equity method"],
    "leases": ["operating lease", "capital lease", "finance lease", "asc 842", "ifrs 16"],
    "wacc-vs-irr": ["hurdle rate", "required return", "cost of capital vs"],
    "market-awareness": ["10[- ]year", "treasury yield", "fed funds", "s&p 500",
                         "inflation", "yield curve", "where is the market"],
    "deal-process": ["sell[- ]side process", "buy[- ]side", "cim", "teaser",
                     "management presentation", "due diligence", "data room",
                     "auction process"],
    "behavioural": ["tell me about a time", "why investment banking", "why our firm",
                    "walk me through your resume", "greatest weakness", "why should we hire"],

    # --- Debt capital markets -------------------------------------------
    # The narrow axis inside `dcm`. "bond" alone is useless as a tag: it is in
    # a third of the topic. These are the concepts a syndicate conversation
    # actually turns on, and each is something you can be separately bad at.
    "bond-math": ["yield to maturity", "yield to worst", "current yield",
                  "clean price", "dirty price", "accrued interest",
                  "\\bytm\\b", "\\bytw\\b", "priced at par", "discount to par"],
    "duration-convexity": ["macaulay", "modified duration", "\\bdv01\\b",
                           "convexity", "spread duration", "rate duration",
                           "basis point value"],
    "spread-mechanics": ["z[- ]spread", "asset swap", "\\bASW\\b", "g[- ]spread",
                         "i[- ]spread", "mid[- ]swap", "swap spread",
                         "credit spread", "spread over", "\\bOAS\\b"],
    "new-issue-pricing": ["new issue premium", "\\bNIP\\b",
                          "initial price thoughts", "\\bIPTs?\\b", "guidance",
                          "tighten", "greenium", "price inside", "reoffer"],
    "syndicate-process": ["orderbook", "order book", "books subject",
                          "allocation", "bookrunner", "pot deal", "retention",
                          "pre[- ]sound", "wall[- ]cross", "break performance",
                          "issuance window", "reverse enquiry", "attrition"],
    "ratings": ["rating agency", "moody", "standard & poor", "\\bfitch\\b",
                "investment grade", "high yield", "sub[- ]investment grade",
                "outlook", "watchlist", "notch", "fallen angel", "rising star"],
    "covenants": ["incurrence covenant", "maintenance covenant", "covenant[- ]lite",
                  "negative pledge", "restricted payment", "\\bbasket\\b",
                  "cross[- ]default", "affirmative covenant"],
    "schuldschein": ["schuldschein", "\\bSSD\\b", "namensschuldverschreibung",
                     "registered bond", "private placement", "\\bMTN\\b"],
    "hybrids": ["corporate hybrid", "equity credit", "extension risk",
                "\\bAT1\\b", "contingent convertible", "\\bcoco\\b",
                "perpetual note", "reset date"],
    "liability-management": ["liability management", "tender offer for its bonds",
                             "consent solicitation", "exchange offer",
                             "make[- ]whole", "call protection", "\\bNC/?\\d"],
    "levfin-structure": ["unitranche", "private credit", "direct lend",
                         "term loan b", "\\bTLB\\b", "first lien", "second lien",
                         "senior secured note", "intercreditor", "\\bAAL\\b",
                         "margin ratchet", "\\bOID\\b", "\\bPIK\\b toggle"],

    # --- Equity capital markets -----------------------------------------
    "ipo-process": ["intention to float", "\\bITF\\b", "pilot fish", "early look",
                    "price range", "bookbuild", "red herring",
                    "preliminary prospectus", "analyst presentation",
                    "registration document", "conditional dealing"],
    "stabilisation": ["greenshoe", "brownshoe", "over[- ]allotment",
                      "stabilis", "stabiliz", "stabilizing manager",
                      "stock loan", "\\bshoe\\b"],
    "follow-on-products": ["accelerated bookbuild", "\\bABB\\b", "block trade",
                           "bought deal", "follow[- ]on offering", "rights issue",
                           "rights offering", "\\bTERP\\b", "nil[- ]paid",
                           "sub[- ]underwrit", "shelf offering", "\\bPIPE\\b",
                           "at[- ]the[- ]market"],
    "free-float-index": ["free float", "index inclusion", "\\bFTSE\\b",
                         "\\bSTOXX\\b", "fast entry", "passive demand",
                         "listing rules", "\\bAIM\\b", "listing venue"],
    "cornerstone": ["cornerstone investor", "anchor investor", "pre[- ]commit"],
    "lock-up": ["lock[- ]up", "lockup", "orderly market", "insider selling"],
    "equity-linked": ["convertible bond", "conversion price", "call spread",
                      "capped call", "mandatory convertible", "exchangeable",
                      "delta[- ]hedge", "equity[- ]linked"],
    "underwriting-economics": ["gross spread", "underwriting fee",
                               "selling concession", "league table",
                               "firm commitment", "best efforts",
                               "underwriting agreement", "engagement letter"],

    # --- Deal process and mechanics -------------------------------------
    "completion-mechanism": ["locked box", "completion accounts",
                             "cash[- ]free debt[- ]free", "leakage",
                             "equity ticker", "true[- ]up", "\\bCFDF\\b"],
    "working-capital-peg": ["working capital peg", "normalised working capital",
                            "normalized working capital", "target working capital",
                            "net debt adjustment"],
    "deferred-consideration": ["earn[- ]out", "earnout", "vendor loan",
                               "deferred consideration", "rollover equity",
                               "escrow", "retention amount"],
    "diligence-quality": ["quality of earnings", "\\bQoE\\b", "add[- ]back",
                          "vendor due diligence", "\\bVDD\\b", "long[- ]form report",
                          "financial position and prospects"],
    "sale-agreement": ["sale and purchase agreement", "warranty and indemnity",
                       "\\bW&I\\b", "representations and warranties",
                       "material adverse", "\\bMAC\\b clause", "disclosure letter",
                       "conditions precedent"],
    "takeover-code": ["takeover code", "takeover panel", "rule 2\\.7",
                      "put up or shut up", "\\bPUSU\\b", "irrevocable undertaking",
                      "scheme of arrangement", "contractual offer", "rule 9",
                      "mandatory offer", "frustrating action", "cash confirmation",
                      "court meeting"],
    "german-public-m-a": ["wp[üu]g", "\\bBaFin\\b", "beherrschungsvertrag",
                          "domination agreement", "profit and loss transfer",
                          "squeeze[- ]out", "delisting offer", "aufsichtsrat",
                          "betriebsrat", "works council", "co[- ]determination"],
    "german-accounting": ["\\bHGB\\b", "stille reserve", "hidden reserve",
                          "vorsichtsprinzip", "realisationsprinzip",
                          "handelsbilanz", "steuerbilanz", "gewerbesteuer",
                          "organschaft", "einzelabschluss", "konzernabschluss"],
    "private-company-valuation": ["illiquidity discount", "lack of marketability",
                                  "\\bDLOM\\b", "size premium", "minority discount",
                                  "key[- ]man", "owner compensation",
                                  "customer concentration", "founder[- ]dependent"],
}

# Sector tags. A separate dimension from CONCEPTS on purpose: "how do you
# value a bank" is a valuation question AND a FIG question, and if you are
# interviewing with a coverage group the sector axis is the one you want to
# drill on.
#
# These are matched far more strictly than concepts. A sector question is one
# that could not be asked about a generic company -- it turns on the sector's
# own economics or its own metrics. Merely naming an industry in passing is
# not enough, which is why each entry carries an `exclude`: "investment bank"
# and "bulge bracket" are the single largest source of false FIG hits in a
# bank of interview questions, and tagging on them would make `--tag fig`
# useless on day one.
INDUSTRIES: dict[str, dict[str, list[str]]] = {
    "fig": {
        "match": [
            r"commercial bank", r"retail bank", r"bank holding", r"regional bank",
            r"net interest margin", r"\bnim\b", r"tier 1", r"basel",
            r"loan loss", r"provision for credit loss", r"allowance for loan",
            r"deposit franchise", r"insurance compan", r"insurer",
            r"combined ratio", r"loss ratio", r"underwriting margin",
            r"embedded value", r"regulatory capital", r"risk[- ]weighted asset",
            r"price to book", r"p/b\b", r"tangible book value", r"\bcet1\b",
            r"value a bank", r"valuing a bank", r"banks? (?:are|is) valued",
        ],
        "exclude": [r"investment bank", r"bulge bracket", r"boutique bank",
                    r"bank debt", r"bank of america"],
    },
    "oil-gas": {
        "match": [
            r"oil and gas", r"oil & gas", r"\bE&P\b", r"exploration and production",
            # `upstream` and `downstream` are corporate-finance words too: an
            # upstream merger, an upstream guarantee, "everything downstream of
            # the mandate". Three of the four questions in this bank carrying
            # the bare word are M&A and credit questions, so the segment sense
            # has to be anchored to the noun that follows it. `midstream` has
            # no such second life.
            r"(?:up|down)stream\s+(?:oil|gas|compan|producer|operator|asset|"
            r"segment|business|player|refin|e&p)",
            r"midstream", r"oilfield",
            r"oil well", r"proved reserves", r"\bPV-?10\b", r"reserve report", r"\bboe\b",
            r"production decline", r"decline curve", r"netback",
            r"\bMLP\b", r"master limited partnership", r"refining margin",
            r"crack spread", r"drilling",
        ],
        "exclude": [],
    },
    "real-estate": {
        "match": [
            r"\bREIT\b", r"real estate investment trust", r"\bFFO\b", r"\bAFFO\b",
            r"cap rate", r"capitalization rate", r"net asset value per share",
            r"same[- ]store net operating income", r"\bNOI\b", r"occupancy rate",
            r"real estate compan", r"property portfolio",
        ],
        "exclude": [],
    },
    "tech-saas": {
        "match": [
            r"\bSaaS\b", r"software as a service", r"\bARR\b", r"\bMRR\b",
            r"recurring revenue", r"net revenue retention", r"\bNRR\b",
            r"churn rate", r"customer churn", r"\bLTV\b", r"\bCAC\b",
            r"rule of 40", r"bookings? (?:to|vs) revenue", r"seat[- ]based",
            r"pre[- ]revenue tech compan", r"\btech compan",
        ],
        "exclude": [],
    },
    "healthcare": {
        "match": [
            r"biotech", r"pharmaceutical", r"\bpharma\b", r"drug pipeline",
            r"clinical trial", r"phase (?:i|ii|iii|1|2|3)\b", r"\brNPV\b",
            r"risk[- ]adjusted npv", r"patent cliff", r"\bFDA\b",
            r"medical device", r"hospital", r"payor", r"reimbursement rate",
        ],
        "exclude": [],
    },
    "utilities-power": {
        "match": [
            r"regulated utilit", r"\butility\b", r"utilities", r"rate base",
            r"allowed return on equity", r"regulated asset base", r"\bRAB\b",
            r"power generation", r"independent power", r"transmission and distribution",
            r"renewable energy", r"solar project", r"wind farm", r"\bPPA\b (?:contract|agreement)",
            r"project finance",
        ],
        "exclude": [r"utility of", r"marginal utility"],
    },
    "consumer-retail": {
        "match": [
            r"same[- ]store sales", r"comparable store sales", r"\bSSS\b",
            r"retailer", r"retail compan", r"consumer packaged goods", r"\bCPG\b",
            r"footfall", r"store count", r"e[- ]commerce compan", r"gross merchandise value",
            r"\bGMV\b", r"restaurant compan",
        ],
        "exclude": [],
    },
    "telecom-media": {
        "match": [
            r"\bARPU\b", r"average revenue per user", r"subscriber",
            r"telecom", r"wireless carrier", r"spectrum", r"cable compan",
            r"streaming", r"content library", r"media compan", r"advertising revenue",
        ],
        "exclude": [],
    },
    "transport-shipping": {
        "match": [
            r"airline", r"available seat mile", r"\bASM\b", r"\bRASM\b", r"\bCASM\b",
            r"load factor", r"shipping compan", r"charter rate", r"dry bulk",
            r"tanker", r"freight", r"railroad", r"trucking", r"fleet age",
        ],
        "exclude": [],
    },
    "mining-metals": {
        "match": [
            r"mining compan", r"\bmine\b", r"ore body", r"ore grade", r"mineral reserve",
            r"\bNPV\b of the mine", r"commodity price deck", r"smelter", r"metals? and mining",
        ],
        "exclude": [],
    },
    "industrials": {
        "match": [
            r"\bindustrials\b", r"aerospace", r"defense contractor",
            r"capital goods", r"book[- ]to[- ]bill", r"aftermarket",
            r"cyclical manufactur",
        ],
        # "industrial properties" and "industrial parks" are real-estate terms.
        "exclude": [r"industrial (?:propert|park|space|asset)"],
    },
}


# Firms, for "this is what Evercore actually asked" filtering.
FIRMS: dict[str, list[str]] = {
    "goldman-sachs": ["goldman", "gs "],
    "morgan-stanley": ["morgan stanley"],
    "jpmorgan": ["jpmorgan", "jp morgan", "j.p. morgan"],
    "bofa": ["bank of america", "bofa", "merrill"],
    "citi": ["citigroup", "citi "],
    "barclays": ["barclays"],
    "ubs": ["ubs"],
    "deutsche-bank": ["deutsche bank"],
    "evercore": ["evercore"],
    "centerview": ["centerview"],
    "lazard": ["lazard"],
    "moelis": ["moelis"],
    "pjt": ["pjt partners", "pjt"],
    "perella-weinberg": ["perella"],
    "houlihan-lokey": ["houlihan"],
    "jefferies": ["jefferies"],
    "rothschild": ["rothschild"],
    "guggenheim": ["guggenheim"],
    "blackstone": ["blackstone"],
    "kkr": ["kkr"],
}

_COMPILED = {
    name: re.compile("|".join(rf"\b(?:{p})" for p in pats), re.I)
    for name, pats in CONCEPTS.items()
}
# Anchored at both ends. The patterns above spell two of these with a trailing
# space -- "gs ", "citi " -- which is the author saying "the whole word, not a
# prefix", and `p.strip()` threw that away: `\bciti` matches "citing" and
# "cities". A closing `\b` says the same thing and says it for every entry.
_FIRMS_COMPILED = {
    name: re.compile("|".join(rf"\b(?:{re.escape(p.strip())})\b" for p in pats), re.I)
    for name, pats in FIRMS.items()
}
_INDUSTRY_COMPILED = {
    name: (re.compile("|".join(f"(?:{p})" for p in spec["match"]), re.I),
           re.compile("|".join(f"(?:{p})" for p in spec["exclude"]), re.I)
           if spec["exclude"] else None)
    for name, spec in INDUSTRIES.items()
}


def normalize_name(raw: str) -> str:
    return raw.strip().lower().lstrip("#").replace(" ", "-")


def suggest(question_text: str, answer_text: str | None = None,
            max_tags: int = 4) -> list[str]:
    """Concept tags a question's own words justify.

    Question text is weighted over the answer: an answer that merely mentions
    WACC in passing does not make the question a WACC question, and tagging on
    that turns `--tag wacc` into "half the bank".
    """
    q = question_text or ""
    a = (answer_text or "")[:2000]
    scored: list[tuple[int, str]] = []
    for name, pattern in _COMPILED.items():
        in_q = len(pattern.findall(q))
        in_a = len(pattern.findall(a))
        score = in_q * 3 + in_a
        if in_q or in_a >= 2:
            scored.append((score, name))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [name for _, name in scored[:max_tags]]


def suggest_industries(question_text: str, answer_text: str | None = None,
                      max_tags: int = 2) -> list[str]:
    """Sector tags a question's own words justify.

    Stricter than `suggest`. Excluded phrases are struck out of the text
    before matching rather than merely vetoing the tag, so "how do you value a
    commercial bank, unlike an investment bank" still tags as FIG -- only the
    incidental mention is removed, not the real one.

    A sector question also has to be about the sector, not merely mention it,
    so a single hit only counts when it is in the question itself; the answer
    alone needs two.
    """
    q = question_text or ""
    a = (answer_text or "")[:2000]
    out: list[tuple[int, str]] = []
    for name, (pattern, veto) in _INDUSTRY_COMPILED.items():
        qq, aa = (veto.sub(" ", q), veto.sub(" ", a)) if veto else (q, a)
        in_q = len(pattern.findall(qq))
        in_a = len(pattern.findall(aa))
        if in_q or in_a >= 2:
            out.append((in_q * 3 + in_a, name))
    out.sort(key=lambda t: (-t[0], t[1]))
    return [name for _, name in out[:max_tags]]


def suggest_firms(text: str) -> list[str]:
    return sorted(name for name, pat in _FIRMS_COMPILED.items() if pat.search(text or ""))


def attach(conn: sqlite3.Connection, question_id: int, names: list[str],
           kind: str = "concept") -> list[str]:
    """Link tags to a question. Returns the ones that were not already there."""
    added: list[str] = []
    for raw in names:
        name = normalize_name(raw)
        if len(name) < 2:
            continue
        conn.execute("INSERT OR IGNORE INTO tags (name, kind) VALUES (?, ?)", (name, kind))
        row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
        cur = conn.execute(
            "INSERT OR IGNORE INTO question_tags (question_id, tag_id) VALUES (?, ?)",
            (question_id, row["id"]),
        )
        if cur.rowcount:
            added.append(name)
    conn.commit()
    return added


def detach(conn: sqlite3.Connection, question_id: int, names: list[str]) -> list[str]:
    removed = []
    for raw in names:
        name = normalize_name(raw)
        cur = conn.execute(
            "DELETE FROM question_tags WHERE question_id = ? AND tag_id IN "
            "(SELECT id FROM tags WHERE name = ?)", (question_id, name))
        if cur.rowcount:
            removed.append(name)
    conn.commit()
    return removed


def tags_for(conn: sqlite3.Connection, question_id: int) -> list[str]:
    return [
        r["name"]
        for r in conn.execute(
            "SELECT t.name FROM tags t JOIN question_tags qt ON qt.tag_id = t.id "
            "WHERE qt.question_id = ? ORDER BY t.kind, t.name", (question_id,))
    ]


def all_tags(conn: sqlite3.Connection, min_count: int = 1) -> list[dict]:
    """Every tag with how many active questions carry it, and how they have gone."""
    return [
        dict(name=r["name"], kind=r["kind"], n=r["n"], avg_rating=r["avg_rating"],
             due=r["due"])
        for r in conn.execute(
            """
            SELECT t.name, t.kind, COUNT(DISTINCT q.id) n,
                   (SELECT AVG(rv.rating) FROM reviews rv
                     JOIN question_tags qt2 ON qt2.question_id = rv.question_id
                    WHERE qt2.tag_id = t.id AND rv.rating IS NOT NULL) avg_rating,
                   SUM(CASE WHEN s.due_at IS NULL OR s.due_at <= datetime('now')
                            THEN 1 ELSE 0 END) due
              FROM tags t
              JOIN question_tags qt ON qt.tag_id = t.id
              JOIN questions q ON q.id = qt.question_id AND q.status = 'active'
              LEFT JOIN schedule s ON s.question_id = q.id
          GROUP BY t.id
            HAVING n >= ?
          ORDER BY n DESC, t.name
            """,
            (min_count,),
        )
    ]


def resolve(conn: sqlite3.Connection, name: str) -> str | None:
    """Accept a prefix or a near-miss, the way topic names already are."""
    name = normalize_name(name)
    names = [r["name"] for r in conn.execute("SELECT name FROM tags ORDER BY name")]
    if name in names:
        return name
    starts = [n for n in names if n.startswith(name)]
    if len(starts) == 1:
        return starts[0]
    contains = [n for n in names if name in n]
    if len(contains) == 1:
        return contains[0]
    return None


def autotag(conn: sqlite3.Connection, limit: int | None = None,
            only_untagged: bool = True) -> dict:
    """Sweep the bank and apply the taxonomy. Idempotent, local, free."""
    sql = (
        "SELECT q.id, q.canonical_text, a.answer_key FROM questions q "
        "LEFT JOIN answers a ON a.question_id = q.id "
        "WHERE q.status IN ('active', 'needs_review')"
    )
    if only_untagged:
        sql += " AND NOT EXISTS (SELECT 1 FROM question_tags qt WHERE qt.question_id = q.id)"
    sql += " ORDER BY q.id"
    if limit:
        sql += f" LIMIT {int(limit)}"

    scanned = tagged = links = 0
    per_tag: dict[str, int] = {}
    for r in conn.execute(sql).fetchall():
        scanned += 1
        # Two dimensions, stored side by side with different kinds: what the
        # question is about, and whose sector it belongs to. A question is
        # routinely both, which is the whole reason tags exist rather than a
        # second topic column.
        added = attach(conn, r["id"], suggest(r["canonical_text"], r["answer_key"]))
        added += attach(conn, r["id"],
                        suggest_industries(r["canonical_text"], r["answer_key"]),
                        kind="industry")
        # And whose material it came out of. `FIRMS`, `suggest_firms` and the
        # `firms` family in TREE all existed and nothing called the middle one,
        # so the family was seeded on every run and could never fill: the tree
        # advertised a chapter with no way in. Almost nothing in a generalist
        # bank matches, which is the honest answer rather than a reason to have
        # left the taxonomy half-wired.
        added += attach(conn, r["id"],
                        suggest_firms(f"{r['canonical_text']} {r['answer_key'] or ''}"),
                        kind="firm")
        if added:
            tagged += 1
            links += len(added)
            for n in added:
                per_tag[n] = per_tag.get(n, 0) + 1
    # A tag this run just invented has no parent yet, and an unparented tag
    # shows up under "unfiled" in the browse tree rather than in the family it
    # belongs to. `ensure_tree` only ever fills a NULL parent, so calling it
    # here cannot undo a tag you re-filed by hand.
    ensure_tree(conn)
    return {"scanned": scanned, "tagged": tagged, "links": links, "per_tag": per_tag}


# ---------------------------------------------------------------- the tree

# Which family each tag hangs under. Families are tags too -- `kind='family'`
# -- so the whole hierarchy is one table and a filter on a family is just a
# filter on a tag that happens to have children.
#
# One parent per tag, deliberately. A few tags could defensibly sit in two
# places (`liquidation` is both a valuation method and a restructuring tool),
# and the tie is broken by where you would go LOOKING for it rather than by
# what it is about. Filters stack, so anything that genuinely spans two
# families is still reachable by selecting both.
TREE: dict[str, list[str]] = {
    "accounting": [
        "3-statement-linkage", "statement-walkthrough", "cash-adjustments",
        "working-capital", "deferred-revenue", "normalization", "dta-dtl",
        "nol", "leases", "sbc", "goodwill", "securities-classification",
        "equity-method", "minority-interest", "projections", "calendarization",
        "german-accounting",
    ],
    "enterprise-value": ["ev-bridge", "dilution"],
    "valuation": [
        "multiples", "comps", "precedents", "ebitda-bridge", "sum-of-the-parts",
        "roic", "cap-rate", "reits", "private-company-valuation",
    ],
    "intrinsic-value": [
        "dcf", "wacc", "capm", "beta", "terminal-value", "free-cash-flow",
        "wacc-vs-irr",
    ],
    "m-and-a": [
        "merger-model", "accretion-dilution", "synergies",
        "purchase-price-allocation", "transaction-structure", "deal-process",
        "sale-agreement", "completion-mechanism", "working-capital-peg",
        "deferred-consideration", "diligence-quality", "takeover-code",
        "german-public-m-a",
    ],
    "lbo": [
        "lbo-returns", "irr-drivers", "dividend-recap", "financial-sponsors",
        "private-company", "sources-and-uses",
    ],
    "credit": [
        "capital-structure", "credit-stats", "covenants", "debt-schedule",
        "ratings", "spread-mechanics", "levfin-structure", "liability-management",
    ],
    "capital-markets": [
        "ecm", "dcm", "bonds", "convertibles", "new-issue-pricing",
        "syndicate-process", "underwriting-economics", "hybrids", "schuldschein",
        "bond-math", "duration-convexity", "ipo-process", "equity-linked",
        "follow-on-products", "stabilisation", "free-float-index", "cornerstone",
        "lock-up", "dcm-syndicate",
    ],
    "special-situations": ["restructuring", "liquidation", "project-finance"],
    "fit": [
        "behavioural", "why-banking", "why-this-firm", "walk-me-through-resume",
        "strengths-weaknesses", "failure-setback", "leadership", "teamwork",
        "career-goals", "red-flag-defence", "curveball", "process-questions",
        "deal-discussion", "market-sizing", "business-case",
    ],
    "markets": ["market-awareness", "rates", "fx"],
    # Whose deck a question came out of, rather than what it is about. Seeded
    # empty on purpose: a firm tag is yours, created the first time you ingest
    # a bank's own material, and FIRM_FAMILY below files it here the moment it
    # exists rather than leaving it under "unfiled".
    "firms": [],
}

# Every `kind='industry'` tag hangs under one family without being listed
# individually, so a new sector tag is filed the moment it is created. Firm
# tags work the same way.
SECTOR_FAMILY = "sectors"
FIRM_FAMILY = "firms"

FAMILY_KIND = "family"


def ensure_tree(conn: sqlite3.Connection) -> int:
    """Create the family tags and hang the flat taxonomy under them.

    Only ever fills a parent that is NULL. Re-filing a tag by hand therefore
    survives the next call, which is the whole reason the tree is seeded here
    rather than frozen into the migration. A tag nobody has placed stays
    unparented and `browse` shows it under "unfiled" -- visible and annoying,
    which is the correct amount of pressure to file it.
    """
    if not _has_parent_column(conn):
        return 0
    placed = 0
    for family, children in TREE.items():
        fid = _ensure_family(conn, family)
        for child in children:
            placed += _place(conn, child, fid)
    for family, kind in ((SECTOR_FAMILY, "industry"), (FIRM_FAMILY, "firm")):
        fid = _ensure_family(conn, family)
        for r in conn.execute(
                "SELECT name FROM tags WHERE kind = ? AND parent_id IS NULL", (kind,)):
            placed += _place(conn, r["name"], fid)
    conn.commit()
    return placed


def _has_parent_column(conn: sqlite3.Connection) -> bool:
    return any(r["name"] == "parent_id"
               for r in conn.execute("PRAGMA table_info(tags)"))


def _ensure_family(conn: sqlite3.Connection, name: str) -> int:
    conn.execute("INSERT OR IGNORE INTO tags (name, kind) VALUES (?, ?)",
                 (name, FAMILY_KIND))
    row = conn.execute("SELECT id, kind FROM tags WHERE name = ?", (name,)).fetchone()
    if row["kind"] != FAMILY_KIND:
        conn.execute("UPDATE tags SET kind = ? WHERE id = ?", (FAMILY_KIND, row["id"]))
    return row["id"]


def _place(conn: sqlite3.Connection, child: str, parent_id: int) -> int:
    cur = conn.execute(
        "UPDATE tags SET parent_id = ? WHERE name = ? AND parent_id IS NULL AND id != ?",
        (parent_id, child, parent_id))
    return cur.rowcount


def set_parent(conn: sqlite3.Connection, child: str, parent: str | None) -> bool:
    """Re-file a tag by hand. `parent=None` lifts it back out to unfiled.

    Refuses to make a tag its own ancestor, because a cycle turns
    `descendants` into an infinite walk and the recursive CTE would happily
    follow it.
    """
    child = normalize_name(child)
    row = conn.execute("SELECT id FROM tags WHERE name = ?", (child,)).fetchone()
    if row is None:
        return False
    if parent is None:
        conn.execute("UPDATE tags SET parent_id = NULL WHERE id = ?", (row["id"],))
        conn.commit()
        return True
    parent = normalize_name(parent)
    pid = _ensure_family(conn, parent)
    if pid == row["id"] or child in descendants(conn, child) - {child} or \
            parent in descendants(conn, child):
        return False
    conn.execute("UPDATE tags SET parent_id = ? WHERE id = ?", (pid, row["id"]))
    conn.commit()
    return True


def descendants(conn: sqlite3.Connection, name: str) -> set[str]:
    """A tag plus everything filed beneath it, however deep.

    This is what makes a broad filter broad: selecting `accounting` has to
    match a question tagged only `dta-dtl`, or the tree is decoration.
    """
    name = normalize_name(name)
    if not _has_parent_column(conn):
        return {name}
    rows = conn.execute(
        """
        WITH RECURSIVE sub(id) AS (
            SELECT id FROM tags WHERE name = ?
            UNION
            SELECT t.id FROM tags t JOIN sub ON t.parent_id = sub.id
        )
        SELECT name FROM tags WHERE id IN (SELECT id FROM sub)
        """,
        (name,),
    ).fetchall()
    return {r["name"] for r in rows} or {name}


def orphans(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Tags no question carries.

    A family with nothing under it is not an orphan: `firms` and `sectors` are
    seeded empty on purpose and fill as you ingest. Anything else with a count
    of zero is left over from a run that went away, and it costs a row in the
    browse tree for ever.
    """
    return list(conn.execute(
        "SELECT t.id, t.name, t.kind FROM tags t "
        " WHERE t.kind != ? "
        "   AND NOT EXISTS (SELECT 1 FROM question_tags qt WHERE qt.tag_id = t.id) "
        "   AND NOT EXISTS (SELECT 1 FROM tags c WHERE c.parent_id = t.id) "
        " ORDER BY t.name", (FAMILY_KIND,)))


def prune(conn: sqlite3.Connection, names: list[str] | None = None) -> list[str]:
    """Delete tags nothing carries. Returns the names that went.

    Deliberately narrow: it will not remove a family, and it will not remove a
    tag that still has a child hanging off it, because either would leave the
    tree with a gap rather than one fewer row.
    """
    targets = orphans(conn)
    if names is not None:
        wanted = {normalize_name(n) for n in names}
        targets = [t for t in targets if t["name"] in wanted]
    if not targets:
        return []
    conn.executemany("DELETE FROM tags WHERE id = ?", [(t["id"],) for t in targets])
    conn.commit()
    return [t["name"] for t in targets]
