from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass
class SalaryRange:
    currency: str  # INR | USD | GBP | EUR | CAD | OTHER
    min_value: float
    max_value: float
    raw: str

    @property
    def max_inr_lpa(self) -> float:
        if self.currency == "INR":
            return self.max_value
        return 0.0


# ₹7.2L – ₹9.6L | ₹35L | ₹60,000 – ₹1L (monthly) | Rs 35 LPA
_L_SUFFIX = r"(?:L|LPA|lakhs?)"
_INR_RANGE = re.compile(
    rf"(?:₹|Rs\.?)\s*([\d,]+(?:\.\d+)?)\s*({_L_SUFFIX})?"
    rf"(?:\s*[–-]\s*(?:₹|Rs\.?)?\s*([\d,]+(?:\.\d+)?)\s*({_L_SUFFIX})?)?",
    re.I,
)
# 2.5L – 15L | 7.2L-9.6L (Wellfound sometimes omits ₹ on the range)
_INR_BARE = re.compile(
    rf"\b([\d.]+)\s*{_L_SUFFIX}\s*[–-]\s*([\d.]+)\s*{_L_SUFFIX}\b",
    re.I,
)
_INR_SINGLE = re.compile(
    rf"(?:₹|Rs\.?)\s*([\d,]+(?:\.\d+)?)\s*{_L_SUFFIX}\b",
    re.I,
)
_MONTHLY_HINT = re.compile(r"(?:/month|per\s+month|monthly|p\.?m\.?)", re.I)
_USD = re.compile(r"\$\s*([\d.]+)\s*k(?:\s*[–-]\s*\$?\s*([\d.]+)\s*k)?", re.I)
_GBP = re.compile(r"£\s*([\d.]+)\s*k(?:\s*[–-]\s*£?\s*([\d.]+)\s*k)?", re.I)
_EUR = re.compile(r"€\s*([\d.]+)\s*k(?:\s*[–-]\s*€?\s*([\d.]+)\s*k)?", re.I)
_CAD = re.compile(
    r"\$\s*([\d.]+)\s*k\s*CAD|\bCAD\s*\$?\s*([\d.]+)\s*k",
    re.I,
)

LOCATION_BLOCKED = re.compile(
    r"not accepting applications from your current location|"
    r"cannot apply from your (?:current )?location|"
    r"applications (?:are )?not (?:being )?accepted from",
    re.I,
)


def _parse_amount(s: str) -> float:
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return 0.0


def _has_lakh_suffix(suffix: str | None) -> bool:
    return bool(suffix and suffix.strip())


def _is_monthly_inr_range(
    lo_amt: float,
    lo_suffix: str | None,
    hi_amt: float,
    hi_suffix: str | None,
    text: str,
) -> bool:
    if _MONTHLY_HINT.search(text):
        return True
    lo_has_l = _has_lakh_suffix(lo_suffix)
    hi_has_l = _has_lakh_suffix(hi_suffix)
    if lo_has_l and hi_has_l:
        return False
    if not lo_has_l and 1_000 <= lo_amt < 1_000_000:
        return True
    if not hi_has_l and 1_000 <= hi_amt < 1_000_000:
        return True
    return False


def _inr_token_to_lpa(amount: float, suffix: str | None, *, monthly: bool) -> float:
    """Convert one INR salary token to annual LPA."""
    if _has_lakh_suffix(suffix):
        if monthly and amount < 10:
            # ₹1L in "₹60,000 – ₹1L" means ₹1 lakh/month → 12 LPA
            return amount * 12.0
        return amount
    if amount >= 100_000:
        return amount / 100_000.0
    if amount >= 1_000:
        return amount * 12.0 / 100_000.0
    return amount


def _inr_range_from_match(m: re.Match[str], text: str) -> SalaryRange | None:
    lo_amt = _parse_amount(m.group(1))
    lo_suffix = m.group(2)
    hi_amt = _parse_amount(m.group(3)) if m.group(3) else lo_amt
    hi_suffix = m.group(4) if m.group(3) else lo_suffix
    if lo_amt <= 0:
        return None

    monthly = _is_monthly_inr_range(lo_amt, lo_suffix, hi_amt, hi_suffix, text)
    lo_lpa = _inr_token_to_lpa(lo_amt, lo_suffix, monthly=monthly)
    hi_lpa = _inr_token_to_lpa(hi_amt, hi_suffix, monthly=monthly)
    return SalaryRange(
        currency="INR",
        min_value=min(lo_lpa, hi_lpa),
        max_value=max(lo_lpa, hi_lpa),
        raw=m.group(0),
    )


def parse_salary_ranges(text: str) -> list[SalaryRange]:
    found: list[SalaryRange] = []
    seen_raw: set[str] = set()
    covered_spans: list[tuple[int, int]] = []

    def add_range(rng: SalaryRange | None) -> None:
        if rng is None or rng.raw in seen_raw or rng.max_value <= 0:
            return
        seen_raw.add(rng.raw)
        found.append(rng)

    def _num(s: str | None) -> float:
        return _parse_amount(s) if s else 0.0

    def add(currency: str, lo: float, hi: float, raw: str) -> None:
        if raw in seen_raw or lo <= 0 and hi <= 0:
            return
        seen_raw.add(raw)
        found.append(SalaryRange(currency=currency, min_value=lo, max_value=hi, raw=raw))

    for pattern, currency in (
        (_USD, "USD"),
        (_GBP, "GBP"),
        (_EUR, "EUR"),
        (_CAD, "CAD"),
    ):
        for m in pattern.finditer(text):
            g = m.groups()
            lo = _num(g[0])
            hi = _num(g[1]) if len(g) > 1 and g[1] else lo
            if lo > 0 or hi > 0:
                add(currency, lo, hi, m.group(0))

    for m in _INR_RANGE.finditer(text):
        covered_spans.append(m.span())
        add_range(_inr_range_from_match(m, text))

    for m in _INR_BARE.finditer(text):
        lo = _num(m.group(1))
        hi = _num(m.group(2))
        if lo > 0:
            add("INR", lo, hi, m.group(0))

    for m in _INR_SINGLE.finditer(text):
        if any(start <= m.start() < end for start, end in covered_spans):
            continue
        val = _num(m.group(1))
        raw = m.group(0)
        if val > 0 and raw not in seen_raw:
            add("INR", val, val, raw)

    return found


def is_location_blocked(text: str) -> bool:
    return bool(LOCATION_BLOCKED.search(text))


def combined_salary_text(*, jd: str = "", meta: dict[str, Any] | None = None, modal: str = "") -> str:
    """Merge salary-bearing snippets (modal header, JD, stored meta)."""
    meta = meta or {}
    parts: list[str] = []
    if meta.get("salary_display"):
        parts.append(str(meta["salary_display"]))
    if modal:
        parts.append(modal)
    if jd:
        parts.append(jd)
    return "\n".join(p for p in parts if p)


def is_salary_eligible(text: str, *, min_inr_lpa: float = 25.0) -> tuple[bool, str]:
    """
    Apply only when:
    - no salary listed, OR
    - salary in a currency other than INR, OR
    - INR max >= min_inr_lpa
    """
    ranges = parse_salary_ranges(text)
    if not ranges:
        return True, "no salary listed"

    if any(r.currency != "INR" for r in ranges):
        non_inr = next(r for r in ranges if r.currency != "INR")
        return True, f"non-INR salary ({non_inr.raw})"

    max_inr = max(r.max_inr_lpa for r in ranges)
    if max_inr >= min_inr_lpa:
        return True, f"INR {max_inr:g}L >= {min_inr_lpa:g}L"

    return False, f"INR {max_inr:g}L < {min_inr_lpa:g}L threshold"


def eligibility_summary(text: str, *, min_inr_lpa: float = 25.0) -> dict[str, Any]:
    salary_ok, salary_reason = is_salary_eligible(text, min_inr_lpa=min_inr_lpa)
    loc_blocked = is_location_blocked(text)
    ranges = parse_salary_ranges(text)
    return {
        "salary_eligible": salary_ok,
        "salary_reason": salary_reason,
        "salary_display": ranges[0].raw if ranges else "",
        "location_blocked": loc_blocked,
        "eligible_to_apply": salary_ok and not loc_blocked,
        "block_reason": (
            "location not accepted for your profile"
            if loc_blocked
            else (salary_reason if not salary_ok else "")
        ),
    }


def extract_salary_from_text(text: str) -> str:
    """Pull the salary snippet from a Wellfound card or page header."""
    if not text:
        return ""
    ranges = parse_salary_ranges(text)
    if ranges:
        return ranges[0].raw
    for line in text.split("\n"):
        line = line.strip()
        if not line or len(line) > 120:
            continue
        if "₹" in line or re.search(r"\b\d+(?:\.\d+)?\s*L\b", line, re.I):
            return line
    return ""


def job_eligibility(
    *,
    jd: str = "",
    meta: dict[str, Any] | None = None,
    modal: str = "",
    min_inr_lpa: float = 25.0,
) -> dict[str, Any]:
    return eligibility_summary(
        combined_salary_text(jd=jd, meta=meta, modal=modal),
        min_inr_lpa=min_inr_lpa,
    )


def is_job_salary_eligible(
    *,
    jd: str = "",
    meta: dict[str, Any] | None = None,
    modal: str = "",
    min_inr_lpa: float = 25.0,
) -> bool:
    text = combined_salary_text(jd=jd, meta=meta, modal=modal)
    return is_salary_eligible(text, min_inr_lpa=min_inr_lpa)[0]
