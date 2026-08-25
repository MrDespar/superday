"""Build accounting and modelling questions out of real filings.

Every other source in this bank teaches the mechanics on invented numbers. An
interviewer does not: they hand you a real company and ask what its margin was,
what its net debt is, or what happens to its statements if D&A goes up by 100.
This turns a filing into that kind of question.

Deliberately not an LLM extractor. A model reading a 10-K and reporting the
revenue figure is a model that will eventually report the wrong revenue figure
with total confidence, and a question bank whose *numbers* are wrong is worse
than one that is merely thin. So the facts come from XBRL -- the structured,
tagged data the filer themselves submitted -- and every answer is arithmetic
over those facts, computed here, checkable by hand.

The one thing that needs judgement, phrasing the question well, is the thing
templates are good at.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


def user_agent() -> dict[str, str]:
    """SEC returns 403 to any client that does not identify itself.

    They ask for a contact address, and that is their access condition rather
    than an obstacle, so it is supplied from config -- `superday settings
    sec_contact you@example.com` -- instead of being faked.
    """
    from ..config import load
    contact = (load().get("sec_contact") or "").strip()
    # No Accept-Encoding: urllib does not transparently decompress, and a gzip
    # body handed to json.loads fails with a decode error that looks nothing
    # like the header problem that caused it.
    return {"User-Agent": f"superday interview prep ({contact or 'no contact set'})"}


class NeedsContact(Exception):
    """No contact address configured, so the SEC will refuse us."""


# The XBRL tags that carry each line, most-preferred first. Filers tag the same
# economic line differently -- "Revenues" versus "RevenueFromContractWith
# CustomerExcludingAssessedTax" -- so each line needs a list, not a name.
CONCEPTS: dict[str, list[str]] = {
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax",
                "Revenues", "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax"],
    "cost_of_revenue": ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "pretax_income": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                      "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"],
    "tax_expense": ["IncomeTaxExpenseBenefit"],
    "interest_expense": ["InterestExpense", "InterestExpenseDebt",
                         "InterestIncomeExpenseNet"],
    "d_and_a": ["DepreciationDepletionAndAmortization",
                "DepreciationAmortizationAndAccretionNet", "DepreciationAndAmortization",
                "Depreciation"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment",
              "PaymentsToAcquireProductiveAssets"],
    "cfo": ["NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    "current_assets": ["AssetsCurrent"],
    "current_liabilities": ["LiabilitiesCurrent"],
    "inventory": ["InventoryNet"],
    "receivables": ["AccountsReceivableNetCurrent"],
    "total_assets": ["Assets"],
    "total_equity": ["StockholdersEquity",
                     "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "long_term_debt": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "short_term_debt": ["LongTermDebtCurrent", "ShortTermBorrowings"],
    "shares": ["WeightedAverageNumberOfDilutedSharesOutstanding",
               "WeightedAverageNumberOfSharesOutstandingBasic"],
}

# Balance sheet items are a point in time; income and cash flow are a period.
INSTANT = {"cash", "current_assets", "current_liabilities", "inventory",
           "receivables", "total_assets", "total_equity", "long_term_debt",
           "short_term_debt"}


def _get_json(url: str, timeout: int = 30) -> dict:
    import ssl
    import urllib.request

    import certifi
    from ..config import load

    if not (load().get("sec_contact") or "").strip():
        raise NeedsContact(
            "the SEC requires a contact address from automated clients. "
            "Set one:  superday settings sec_contact you@example.com")
    ctx = ssl.create_default_context(cafile=certifi.where())
    req = urllib.request.Request(url, headers=user_agent())
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return json.loads(r.read().decode("utf8", "ignore"))


def fetch_facts(cik: int) -> dict:
    return _get_json(FACTS_URL.format(cik=cik))


def resolve_cik(ticker: str) -> int | None:
    """Ticker to CIK, via the SEC's own published mapping."""
    table = _get_json(TICKERS_URL)
    want = ticker.strip().upper()
    for row in table.values():
        if str(row.get("ticker", "")).upper() == want:
            return int(row["cik_str"])
    return None


def _pick_unit(concept: dict) -> list[dict]:
    units = concept.get("units") or {}
    for key in ("USD", "shares", "USD/shares"):
        if units.get(key):
            return units[key]
    return next(iter(units.values()), [])


def annual_figures(facts: dict, fiscal_year: int | None = None) -> dict:
    """One year of the lines we care about, from the tags this filer used.

    Only full-year (`FY`, 10-K) frames are taken. Mixing a quarterly revenue
    into an annual margin produces a number that is wrong by a factor of four
    and looks perfectly plausible, which is the worst kind of wrong.
    """
    us_gaap = (facts.get("facts") or {}).get("us-gaap") or {}
    out: dict[str, dict] = {}

    for line, tags in CONCEPTS.items():
        for tag in tags:
            concept = us_gaap.get(tag)
            if not concept:
                continue
            rows = [
                r for r in _pick_unit(concept)
                if r.get("form") in ("10-K", "20-F")
                and r.get("fp") == "FY"
                and r.get("val") is not None
                and (line in INSTANT or r.get("start"))
            ]
            if fiscal_year:
                rows = [r for r in rows if r.get("fy") == fiscal_year]
            if not rows:
                continue
            # Latest filed wins: a restated figure supersedes the original.
            row = max(rows, key=lambda r: (r.get("end") or "", r.get("filed") or ""))
            if line not in INSTANT and row.get("start"):
                days = _span_days(row["start"], row["end"])
                if days is not None and not (300 <= days <= 400):
                    continue                 # not actually a twelve-month period
            out[line] = {"value": float(row["val"]), "end": row.get("end"),
                         "fy": row.get("fy"), "tag": tag, "accn": row.get("accn")}
            break
    return out


def _span_days(start: str, end: str) -> int | None:
    try:
        return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days
    except (TypeError, ValueError):
        return None


def entity_name(facts: dict) -> str:
    return (facts.get("entityName") or "the company").strip()


# ---------------------------------------------------------------- questions

def money(v: float) -> str:
    """Filing-scale numbers in the units a banker says out loud."""
    a = abs(v)
    if a >= 1e12:
        return f"${v / 1e12:.2f}tn"
    if a >= 1e9:
        return f"${v / 1e9:.2f}bn"
    if a >= 1e6:
        return f"${v / 1e6:.1f}m"
    if a >= 1e3:
        return f"${v / 1e3:.1f}k"
    # Two decimals below a thousand: rounding $7.50 to "$8" in a walkthrough
    # answer makes the arithmetic look like it does not tie.
    return f"${v:,.2f}"


def _q(question: str, answer: str, rubric: list[str], *, topic: str,
       difficulty: int, tags: list[str]) -> dict:
    return {"question": question, "answer": answer, "rubric_points": rubric,
            "topic": topic, "difficulty": difficulty, "tags": tags}


def build_questions(name: str, f: dict, year: int | None = None) -> list[dict]:
    """Real questions off real figures. Every answer is arithmetic done here."""
    def val(k):
        return f[k]["value"] if k in f else None

    fy = year or (f.get("revenue", {}).get("fy") if f.get("revenue") else None) or ""
    label = f"{name} FY{fy}".strip()
    out: list[dict] = []

    rev, ni = val("revenue"), val("net_income")
    op, da = val("operating_income"), val("d_and_a")
    cfo, capex = val("cfo"), val("capex")
    ca, cl = val("current_assets"), val("current_liabilities")
    cash = val("cash")
    ltd, std = val("long_term_debt"), val("short_term_debt")
    pretax, tax = val("pretax_income"), val("tax_expense")

    if rev and ni:
        margin = ni / rev
        out.append(_q(
            f"{label} reported revenue of {money(rev)} and net income of {money(ni)}. "
            "What is the net margin, and what does that tell you about the business?",
            f"Net margin is net income over revenue: {money(ni)} / {money(rev)} = "
            f"{margin:.1%}. "
            + ("A margin at this level points to real pricing power or a capital-light "
               "model." if margin > 0.15 else
               "A margin at this level suggests a competitive or capital-intensive "
               "business where scale matters more than pricing."),
            [f"Divides net income by revenue to get {margin:.1%}",
             "States net margin is a bottom-line measure, after interest and tax",
             "Notes it is affected by capital structure, unlike operating margin"],
            topic="accounting", difficulty=1, tags=["margins", "3-statement-linkage"]))

    if rev and op:
        out.append(_q(
            f"{label} had revenue of {money(rev)} and operating income of {money(op)}. "
            "What is the operating margin, and why might it differ from net margin?",
            f"Operating margin is {op / rev:.1%} ({money(op)} / {money(rev)}). It sits "
            "above interest and taxes, so it measures the operating business "
            "independent of how it is financed, whereas net margin folds in the "
            "capital structure and the tax rate.",
            [f"Computes operating margin as {op / rev:.1%}",
             "States operating margin is pre-interest and pre-tax",
             "Explains net margin is affected by leverage and tax rate"],
            topic="accounting", difficulty=2, tags=["margins", "ebitda-bridge"]))

    if op is not None and da is not None:
        ebitda = op + da
        out.append(_q(
            f"{label} reported operating income of {money(op)} and D&A of {money(da)}. "
            "Walk me from operating income to EBITDA, and say why anyone cares.",
            f"EBITDA = operating income + D&A = {money(op)} + {money(da)} = "
            f"{money(ebitda)}. D&A is a non-cash charge, so adding it back gives a "
            "rough proxy for operating cash generation that is comparable across "
            "companies with different asset ages and depreciation policies. It is a "
            "proxy, not cash flow: it ignores working capital and capex entirely.",
            [f"Adds D&A back to operating income to reach {money(ebitda)}",
             "States D&A is a non-cash charge",
             "Says EBITDA is capital-structure and depreciation-policy neutral",
             "Notes EBITDA ignores working capital and capex, so it is not cash flow"],
            topic="valuation", difficulty=2, tags=["ebitda-bridge", "multiples"]))

    if cfo is not None and capex is not None:
        fcf = cfo - capex
        out.append(_q(
            f"{label} generated {money(cfo)} of cash from operations and spent "
            f"{money(capex)} on capex. What is free cash flow, and why is it not the "
            "same as net income?",
            f"Levered free cash flow = CFO - capex = {money(cfo)} - {money(capex)} = "
            f"{money(fcf)}. It differs from net income because it adds back non-cash "
            "charges, reflects the actual working capital swing, and subtracts the "
            "capital spending that never appears on the income statement.",
            [f"Subtracts capex from cash from operations to get {money(fcf)}",
             "Notes non-cash charges are added back in CFO",
             "Notes the working capital movement is captured in CFO",
             "States capex is a balance sheet item, not an expense"],
            topic="dcf", difficulty=2, tags=["free-cash-flow", "3-statement-linkage"]))

    if ca is not None and cl is not None:
        wc = ca - cl
        ratio = ca / cl if cl else None
        out.append(_q(
            f"{label} had current assets of {money(ca)} and current liabilities of "
            f"{money(cl)}. What is working capital here, and what would you actually "
            "want to look at instead?",
            f"Working capital = {money(ca)} - {money(cl)} = {money(wc)}"
            + (f", a current ratio of {ratio:.2f}x. " if ratio else ". ")
            + "For analysis you want *operating* working capital, which strips out "
              "cash and short-term debt, because those are financing items and their "
              "movement tells you nothing about how the operating business consumes "
              "cash.",
            [f"Computes working capital as {money(wc)}",
             "Distinguishes operating working capital from the textbook definition",
             "States cash and debt are excluded because they are financing items"],
            topic="accounting", difficulty=2, tags=["working-capital"]))

    debt = sum(x for x in (ltd, std) if x is not None) if (ltd or std) else None
    if debt is not None and cash is not None:
        net_debt = debt - cash
        out.append(_q(
            f"{label} carried {money(debt)} of total debt against {money(cash)} of "
            "cash. What is net debt, and how does it get you from equity value to "
            "enterprise value?",
            f"Net debt = total debt - cash = {money(debt)} - {money(cash)} = "
            f"{money(net_debt)}. Enterprise value = equity value + net debt "
            "(plus preferred stock and minority interest, less non-operating assets). "
            "You add net rather than gross debt because an acquirer gets the cash on "
            "the balance sheet and can use it to retire the debt.",
            [f"Computes net debt as {money(net_debt)}",
             "States enterprise value = equity value + net debt (+ preferred + MI)",
             "Explains cash nets off because the acquirer receives it",
             "Mentions preferred stock and minority interest belong in the bridge"],
            topic="ev_eqv", difficulty=2, tags=["ev-bridge"]))

    if pretax and tax:
        rate = tax / pretax
        if 0 < rate < 0.6 and da:
            after_tax = da * 0.1 * (1 - rate)
            out.append(_q(
                f"{label}'s effective tax rate was {rate:.1%}. If its D&A rose by "
                f"{money(da * 0.1)}, walk me through all three statements.",
                "Income statement: D&A is an expense, so pre-tax income falls by "
                f"{money(da * 0.1)} and net income falls by "
                f"{money(after_tax)} at a {rate:.1%} tax rate.\n\n"
                "Cash flow statement: start from the lower net income, add back the "
                f"full {money(da * 0.1)} of D&A because it is non-cash, so cash rises "
                f"by the tax saving of {money(da * 0.1 * rate)}.\n\n"
                "Balance sheet: cash is up by that tax saving and PP&E is down by the "
                f"full {money(da * 0.1)}, so assets fall on net; on the other side, "
                f"retained earnings fall by {money(after_tax)}. Both sides move by the "
                "same amount, so it balances.",
                [f"States pre-tax income falls by the full {money(da * 0.1)}",
                 f"Applies the {rate:.1%} tax rate to get net income down {money(after_tax)}",
                 f"Adds D&A back on the cash flow statement, so cash rises {money(da * 0.1 * rate)}",
                 "Reduces PP&E by the full D&A on the balance sheet",
                 "Reduces retained earnings by the after-tax amount and confirms it balances"],
                topic="accounting", difficulty=3,
                tags=["3-statement-linkage", "dta-dtl"]))

    if op is not None and val("interest_expense"):
        interest = val("interest_expense")
        if interest > 0:
            out.append(_q(
                f"{label} had operating income of {money(op)} and interest expense of "
                f"{money(interest)}. What is its interest coverage, and would a lender "
                "be comfortable?",
                f"EBIT / interest = {money(op)} / {money(interest)} = "
                f"{op / interest:.1f}x. "
                + ("That is comfortable; lenders typically want to see coverage above "
                   "3x, and covenants often sit around there."
                   if op / interest >= 3 else
                   "That is tight. Below roughly 3x, lenders start to worry, and a "
                   "downturn in EBIT puts the company close to a covenant breach."),
                [f"Computes EBIT / interest expense as {op / interest:.1f}x",
                 "References a typical covenant level around 3x",
                 "Explains coverage measures ability to service debt out of earnings"],
                topic="lbo", difficulty=2, tags=["credit-stats", "debt-schedule"]))

    return out


def summary(name: str, f: dict) -> str:
    """One line per figure found, so you can see what the filing actually gave."""
    order = ["revenue", "operating_income", "net_income", "d_and_a", "cfo", "capex",
             "cash", "current_assets", "current_liabilities", "long_term_debt"]
    lines = []
    for key in order:
        if key in f:
            lines.append(f"  {key:<22}{money(f[key]['value']):>14}   "
                         f"{f[key].get('end') or ''}")
    return "\n".join(lines)
