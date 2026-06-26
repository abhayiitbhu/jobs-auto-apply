"""Shared chip / range option matching for Naukri and answer fill."""

from __future__ import annotations

import re

_LPA_CHIP = re.compile(r"lpa|lac", re.I)
_NOTICE_CHIP = re.compile(
    r"\b(week|weeks|day|days|month|months|serving|immediate|above)\b",
    re.I,
)


def is_lpa_chip_option(option: str) -> bool:
    return bool(_LPA_CHIP.search(option))


def is_notice_chip_option(option: str) -> bool:
    if is_lpa_chip_option(option):
        return False
    text = option.strip()
    if not text:
        return False
    if _NOTICE_CHIP.search(text):
        return True
    return bool(re.search(r"\d+\s*(days?|weeks?|months?)", text, re.I))


def value_in_chip_range(value: int, option: str) -> bool:
    chip_l = option.lower()
    if "no experience" in chip_l and value == 0:
        return True
    lt_m = re.search(r"<\s*(\d+)", chip_l)
    if lt_m:
        return value < int(lt_m.group(1))
    range_m = re.search(r"(\d+)\s*[-–]\s*(\d+)", chip_l)
    if range_m:
        return int(range_m.group(1)) <= value <= int(range_m.group(2))
    plus_m = re.search(r"(\d+)\s*\+", chip_l)
    if plus_m:
        return value >= int(plus_m.group(1))
    more_m = re.search(r"(?:more than|above|over|greater than|at least)\s*(\d+)", chip_l)
    if more_m:
        return value > int(more_m.group(1))
    less_m = re.search(r"(?:less than|below|under|up\s*to|upto)\s*(\d+)", chip_l)
    if less_m:
        return value < int(less_m.group(1))
    # Naukri often renders "4-6" as "4 6"; treat two bare numbers as a range.
    nums = [int(m.group(1)) for m in re.finditer(r"(\d+)", chip_l)]
    if len(nums) == 2 and nums[0] <= nums[1]:
        return nums[0] <= value <= nums[1]
    return bool(len(nums) == 1 and nums[0] == value)


def lpa_in_chip_range(value: float, option: str) -> bool:
    chip_l = option.lower().strip()
    if "no experience" in chip_l and value == 0:
        return True
    gt_m = re.search(r"[>≥]\s*(\d+(?:\.\d+)?)", chip_l)
    if gt_m:
        return value > float(gt_m.group(1))
    lt_m = re.search(r"<\s*(\d+(?:\.\d+)?)", chip_l)
    if lt_m:
        return value < float(lt_m.group(1))
    range_m = re.search(r"(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)", chip_l)
    if range_m:
        lo, hi = float(range_m.group(1)), float(range_m.group(2))
        return lo <= value <= hi
    plus_m = re.search(r"(\d+(?:\.\d+)?)\s*\+", chip_l)
    if plus_m:
        return value >= float(plus_m.group(1))
    lone = re.search(r"^(\d+(?:\.\d+)?)\s*(?:lpa|lac)", chip_l)
    if lone:
        return value == float(lone.group(1))
    return value_in_chip_range(int(value), option)


def _lpa_band_bounds(option: str) -> tuple[float | None, float | None]:
    chip_l = option.lower()
    range_m = re.search(r"(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)", chip_l)
    if range_m:
        return float(range_m.group(1)), float(range_m.group(2))
    plus_m = re.search(r"(\d+(?:\.\d+)?)\s*\+", chip_l)
    if plus_m:
        return float(plus_m.group(1)), float("inf")
    gt_m = re.search(r"[>≥]\s*(\d+(?:\.\d+)?)", chip_l)
    if gt_m:
        return float(gt_m.group(1)), float("inf")
    if is_lpa_chip_option(option):
        lone = re.search(r"(\d+(?:\.\d+)?)", chip_l)
        if lone:
            v = float(lone.group(1))
            return v, v
    return None, None


def pick_lpa_chip_option(value: float, options: list[str]) -> str | None:
    """Map a numeric LPA value to the best matching salary band chip."""
    lpa_opts = [o.strip() for o in options if o.strip() and is_lpa_chip_option(o)]
    if not lpa_opts:
        return None
    for opt in lpa_opts:
        if lpa_in_chip_range(value, opt):
            return opt
    best_opt: str | None = None
    best_dist = float("inf")
    for opt in lpa_opts:
        lo, hi = _lpa_band_bounds(opt)
        if lo is None:
            continue
        if hi == float("inf"):
            dist = max(0.0, lo - value) if value < lo else 0.0
        elif value < lo:
            dist = lo - value
        elif value > hi:
            dist = value - hi
        else:
            dist = 0.0
        if dist < best_dist:
            best_dist = dist
            best_opt = opt
    return best_opt or lpa_opts[-1]
