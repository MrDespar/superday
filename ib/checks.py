"""Mechanical correctness checks over stored answers.

Both audit passes in this tool are language models reading prose and forming an
opinion. That is the right tool for "is this a good answer" and the wrong tool
for "does 100 + 50 equal 140", which is decidable. This module does the
decidable part: no API key, no judgement, no opinion, and a finding only where
the answer states something that is *provably* wrong.

The bar is deliberately high, and it is one-sided. A check here must never fire
on a correct answer, because a false positive teaches you to ignore the whole
report, at which point the true positives stop mattering too. So every pattern
below is anchored to wording that has exactly one reading, and anything
ambiguous -- "net debt" in a sentence that might be about the reverse bridge,
an arithmetic claim with an implied unit conversion -- is left alone and
handed to the models, which can weigh context.

Silence from this module is not a claim that an answer is right. It is a claim
that nothing here could prove it wrong.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

SEVERITY = {"arithmetic": 3, "bridge": 3, "linkage": 3, "formula": 2, "convention": 1}


@dataclass
class Finding:
    kind: str            # arithmetic | bridge | linkage | formula | convention
    message: str         # what is wrong, named specifically
    excerpt: str         # the span that triggered it
    severity: int = 2

    def __str__(self) -> str:
        return f"[{self.kind}] {self.message}"


# ---------------------------------------------------------------- arithmetic

_MAGNITUDE = {"": 1.0, "k": 1e3, "m": 1e6, "mm": 1e6, "bn": 1e9, "b": 1e9,
              "tn": 1e12, "t": 1e12}

_SUFFIX = r"(?:k|mm|m|bn|b|tn|t)?"
# Thousands groups are matched as a unit. A looser `\d[\d,]*` backtracks under
# a trailing full stop and matches "$257" out of "$257,000.", which then reads
# as an arithmetic error three orders of magnitude wide.
_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_TERM = rf"\$?\s*-?{_NUMBER}\s*{_SUFFIX}"
# Extraction leaves en and em dashes where a minus sign belongs.
_OPS = r"[-+*/x×\u2013\u2014]"
_CHAIN = re.compile(
    rf"(?<![\w.]){_TERM}(?:\s*{_OPS}\s*{_TERM})+\s*=\s*({_TERM})(?![\d,])", re.I)
_TOKEN = re.compile(rf"({_TERM})|({_OPS})", re.I)
_PARTS = re.compile(rf"\$?\s*(-?{_NUMBER})\s*({_SUFFIX})", re.I)


def _value(number: str, suffix: str | None) -> float:
    return float(number.replace(",", "")) * _MAGNITUDE.get((suffix or "").lower(), 1.0)


def _term(text: str) -> tuple[float, str]:
    m = _PARTS.match(text.strip())
    return _value(m.group(1), m.group(2)), (m.group(2) or "").lower()


def _evaluate(tokens: list) -> float | None:
    """Left to right with the usual precedence. Numbers and operators only."""
    values = [tokens[0]]
    ops: list[str] = []
    for op, val in zip(tokens[1::2], tokens[2::2]):
        if op in ("*", "x", "×"):
            values[-1] = values[-1] * val
        elif op == "/":
            if val == 0:
                return None
            values[-1] = values[-1] / val
        else:
            ops.append("-" if op in ("-", "\u2013", "\u2014") else "+")
            values.append(val)
    total = values[0]
    for op, val in zip(ops, values[1:]):
        total = total + val if op == "+" else total - val
    return total


def check_arithmetic(text: str) -> list[Finding]:
    """Verify any arithmetic the answer states outright.

    The whole chain is evaluated, not the last two terms of it. Matching
    `$30,000 + $15,000 = $257,000` out of the middle of
    `$222,000 - $10,000 + $30,000 + $15,000 = $257,000` reports a correct EV
    bridge as an arithmetic error, which is the single most damaging thing a
    checker like this can do.

    Skipped when the terms carry different magnitude suffixes: "$1.2bn + 300 =
    $1.5bn" is an author writing the second term in millions, not a mistake.
    """
    out: list[Finding] = []
    for m in _CHAIN.finditer(text or ""):
        expression = m.group(0)
        left = expression.split("=")[0]

        tokens: list = []
        suffixes: set[str] = set()
        for t in _TOKEN.finditer(left):
            if t.group(1):
                value, suffix = _term(t.group(1))
                tokens.append(value)
                suffixes.add(suffix)
            else:
                tokens.append(t.group(2))
        result, result_suffix = _term(m.group(1))
        suffixes.add(result_suffix)
        if len(suffixes) > 1 or len(tokens) < 3:
            continue

        expected = _evaluate(tokens)
        if expected is None:
            continue
        # A tenth of a percent covers rounding in a written answer without
        # covering a genuine mistake.
        tolerance = max(abs(expected) * 0.001, 0.01)
        if abs(expected - result) > tolerance:
            out.append(Finding(
                "arithmetic",
                f"states {' '.join(expression.split())}, but it comes to "
                f"{_pretty(expected)}",
                " ".join(expression.split()), SEVERITY["arithmetic"]))
    return out


def _pretty(v: float) -> str:
    return f"{v:,.2f}".rstrip("0").rstrip(".") if v % 1 else f"{v:,.0f}"


# ---------------------------------------------------------------- negation

# A pattern that describes an error also matches the sentence that warns
# against it. "Goodwill is not amortized" is the correct answer, and flagging
# it teaches you to ignore the report.
_NEGATION = re.compile(
    r"\b(?:not|never|n't|cannot|can't|do not|does not|is not|are not|rather than|"
    r"instead of|as opposed to|incorrect|wrong|mistake|myth|common error|"
    r"a trap|candidates often say|people think)\b", re.I)


def _negated(text: str, start: int, end: int, lookbehind: int = 90) -> bool:
    """Is the matched span inside a sentence that denies it?"""
    window = text[max(0, start - lookbehind):end]
    return bool(_NEGATION.search(window))


# ---------------------------------------------------------------- EV bridge

_EV = r"(?:enterprise value|ev|tev|firm value)"
_EQV = r"(?:equity value|market cap(?:italization)?|market value of equity)"

# Correct: EV = equity value + net debt. Equity value = EV - net debt.
# Each pattern below is the reverse of one of those, spelled out.
_BRIDGE_ERRORS = [
    (re.compile(rf"\b{_EV}\s*(?:=|equals|is)\s*{_EQV}\s*(?:-|minus|less)\s*net debt", re.I),
     "reverses the EV bridge: enterprise value is equity value PLUS net debt"),
    (re.compile(rf"\b{_EQV}\s*(?:=|equals|is)\s*{_EV}\s*(?:\+|plus|add)\s*net debt", re.I),
     "reverses the EV bridge: equity value is enterprise value MINUS net debt"),
    (re.compile(rf"\bsubtract\s+net debt\s+(?:from\s+{_EQV}\s+)?to\s+(?:get|arrive at|reach)\s+"
                rf"{_EV}", re.I),
     "reverses the EV bridge: you ADD net debt to get to enterprise value"),
    (re.compile(rf"\badd\s+net debt\s+to\s+{_EV}\s+to\s+(?:get|arrive at|reach)\s+{_EQV}", re.I),
     "reverses the EV bridge: you SUBTRACT net debt to get back to equity value"),
    (re.compile(rf"\b{_EV}\s*(?:=|equals|is)\s*{_EQV}\s*(?:-|minus|less)\s*(?:total\s+)?debt\s*"
                rf"(?:\+|plus)\s*cash", re.I),
     "reverses the EV bridge: debt is added and cash subtracted, not the other way round"),
]


def _pattern_check(text: str, patterns: list, kind: str) -> list[Finding]:
    out = []
    for pattern, message in patterns:
        for m in pattern.finditer(text or ""):
            if _negated(text, m.start(), m.end()):
                continue
            out.append(Finding(kind, message, " ".join(m.group(0).split()),
                               SEVERITY[kind]))
            break
    return out


def check_ev_bridge(text: str) -> list[Finding]:
    return _pattern_check(text, _BRIDGE_ERRORS, "bridge")


# ---------------------------------------------------------------- statement links

_LINKAGE_ERRORS = [
    (re.compile(r"\bdepreciation\s+(?:\w+\s+){0,2}increases?\s+net income", re.I),
     "depreciation is an expense: it DECREASES net income"),
    (re.compile(r"\b(?:d&a|depreciation and amortization)\s+(?:\w+\s+){0,2}increases?\s+"
                r"net income", re.I),
     "D&A is an expense: it DECREASES net income"),
    (re.compile(r"\badd(?:ing)?\s+back\s+(?:d&a|depreciation)\w*\s+(?:\w+\s+){0,3}"
                r"(?:decreases?|reduces?)\s+cash", re.I),
     "adding back a non-cash charge INCREASES cash"),
    (re.compile(r"\bcapex\s+(?:appears?|is recorded|shows? up|is an expense)\s+"
                r"(?:\w+\s+){0,2}(?:on|in)\s+the income statement", re.I),
     "capex is an investing cash outflow and a balance sheet addition, "
     "not an income statement expense"),
    (re.compile(r"\ban?\s+increase\s+in\s+(?:net\s+|operating\s+)?working capital\s+"
                r"(?:\w+\s+){0,3}increases?\s+cash", re.I),
     "an increase in working capital is a USE of cash: it decreases cash"),
    (re.compile(r"\ban?\s+increase\s+in\s+accounts receivable\s+(?:\w+\s+){0,3}"
                r"increases?\s+cash", re.I),
     "a rise in receivables means cash has not been collected: it decreases cash"),
    (re.compile(r"\bdeferred revenue\s+is\s+(?:an?\s+)?asset\b", re.I),
     "deferred revenue is a liability: the cash is collected but the service is owed"),
    (re.compile(r"\bgoodwill\s+is\s+amortized\b", re.I),
     "goodwill is tested for impairment, not amortized, under current US GAAP"),
]


def check_linkage(text: str) -> list[Finding]:
    return _pattern_check(text, _LINKAGE_ERRORS, "linkage")


# ---------------------------------------------------------------- formulas

_FORMULA_ERRORS = [
    # "times", "by", "multiplied by" and a bare symbol all appear in the corpus.
    (re.compile(r"cost of debt\s*(?:\*|x|×|times|by|multiplied by)?\s*"
                r"\(\s*1\s*\+\s*(?:the\s+)?(?:tax|t\s*\))", re.I),
     "the tax shield multiplies the cost of debt by (1 - tax rate), not (1 + t)"),
    (re.compile(r"after[- ]tax cost of equity", re.I),
     "the cost of equity is not tax affected: only debt carries a tax shield"),
    (re.compile(r"terminal value\s+(?:is|does)\s+not\s+(?:\w+\s+){0,2}discount", re.I),
     "terminal value is a future value and must be discounted back to today"),
    (re.compile(r"\bunlevered free cash flow\s+(?:\w+\s+){0,3}(?:subtracts?|deducts?|"
                r"is after)\s+interest", re.I),
     "unlevered FCF is before interest: that is what makes it unlevered"),
    (re.compile(r"\bwacc\s+(?:\w+\s+){0,3}discount\w*\s+levered free cash flow", re.I),
     "levered FCF is discounted at the cost of equity; WACC discounts unlevered FCF"),
    (re.compile(r"\bdiscount(?:ed|ing)?\s+(?:\w+\s+){0,3}levered free cash flow\s+"
                r"(?:\w+\s+){0,2}at\s+(?:the\s+)?wacc", re.I),
     "levered FCF is discounted at the cost of equity, not WACC"),
]


def check_formulas(text: str) -> list[Finding]:
    return _pattern_check(text, _FORMULA_ERRORS, "formula")


# ---------------------------------------------------------------- runner

CHECKS = (check_arithmetic, check_ev_bridge, check_linkage, check_formulas)


def inspect(text: str) -> list[Finding]:
    """Every finding against one piece of text, worst first."""
    out: list[Finding] = []
    for check in CHECKS:
        out.extend(check(text or ""))
    out.sort(key=lambda f: -f.severity)
    return out


def scan(conn: sqlite3.Connection, *, status: str | None = "active",
         limit: int | None = None) -> list[dict]:
    """Sweep the bank. Returns one row per question that has findings."""
    sql = ("SELECT q.id, q.canonical_text, q.topic, q.status, a.answer_key, "
           "a.rubric_points FROM questions q "
           "LEFT JOIN answers a ON a.question_id = q.id WHERE 1=1")
    params: list = []
    if status:
        sql += " AND q.status = ?"
        params.append(status)
    sql += " ORDER BY q.id"
    if limit:
        sql += f" LIMIT {int(limit)}"

    out = []
    for r in conn.execute(sql, params):
        # The rubric is drilled as hard as the answer, so it gets checked too:
        # a rubric point that states the bridge backwards teaches the error
        # even when the answer beneath it is right.
        body = "\n".join(x for x in (r["answer_key"], r["rubric_points"]) if x)
        findings = inspect(body)
        if findings:
            out.append({"id": r["id"], "topic": r["topic"], "status": r["status"],
                        "question": r["canonical_text"], "findings": findings})
    out.sort(key=lambda d: -max(f.severity for f in d["findings"]))
    return out
