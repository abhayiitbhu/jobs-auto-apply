from __future__ import annotations

import contextlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from .auth import INSTAHYRE_OPPORTUNITIES

logger = logging.getLogger("job_apply")

EMPLOYER_ROW = "div.employer-row"
EMPLOYER_BLOCK = "div.employer-block"
SKILLS_FILTER = '#job-search-section div.filter:has(label:text-is("Skills"))'
SKILLS_SELECTIZE_INPUT = f"{SKILLS_FILTER} .selectize-input input"
SKILLS_SELECTIZE_ITEMS = f"{SKILLS_FILTER} .selectize-input .item"
SKILLS_SELECTIZE_REMOVE = f"{SKILLS_FILTER} .selectize-input .remove"
SKILLS_SELECTIZE_CONTROL = f"{SKILLS_FILTER} .selectize-control"
SKILLS_DROPDOWN_ITEMS = f"{SKILLS_FILTER} .selectize-dropdown .selectize-dropdown-content .item[data-selectable]"
ROW_WAIT_MS = 35000
ROW_POLL_MS = 250
PAGE_SETTLE_MS = 800

DEFAULT_SKILLS = "java,node,python"
DEFAULT_JOB_FUNCTIONS = [
    "/api/v1/job_function/10",  # Backend Development
    "/api/v1/job_function/1",  # Full-Stack Development
    "/api/v1/job_function/76",  # Other Software Development
]
JOB_FUNCTIONS_FILTER = '#job-search-section div.filter:has(label:text-is("Job Functions"))'
JOB_FUNCTIONS_SELECTIZE_INPUT = f"{JOB_FUNCTIONS_FILTER} .selectize-input input"
JOB_FUNCTIONS_SELECTIZE_REMOVE = f"{JOB_FUNCTIONS_FILTER} .selectize-input .remove"
JOB_FUNCTION_DROPDOWN_OPTIONS = f"{JOB_FUNCTIONS_FILTER} .selectize-dropdown .option.selectize-option[data-selectable]"

JOB_FUNCTION_ALIASES: dict[str, str] = {
    "backend development": "/api/v1/job_function/10",
    "backend": "/api/v1/job_function/10",
    "full-stack development": "/api/v1/job_function/1",
    "full stack development": "/api/v1/job_function/1",
    "fullstack development": "/api/v1/job_function/1",
    "full-stack": "/api/v1/job_function/1",
    "full stack": "/api/v1/job_function/1",
    "other software development": "/api/v1/job_function/76",
    "software development": "/api/v1/job_category/1",
    "/api/v1/job_function/10": "/api/v1/job_function/10",
    "/api/v1/job_function/1": "/api/v1/job_function/1",
    "/api/v1/job_function/76": "/api/v1/job_function/76",
}


def normalize_skills(skills: str | list[str] | None) -> str:
    if not skills:
        return DEFAULT_SKILLS
    if isinstance(skills, str):
        return skills.strip()
    return ",".join(s.strip() for s in skills if s.strip())


def normalize_job_functions(values: str | list[str] | None) -> list[str]:
    if not values:
        return list(DEFAULT_JOB_FUNCTIONS)
    raw_parts: list[str] = []
    raw_parts = [values] if isinstance(values, str) else [str(v).strip() for v in values if str(v).strip()]
    parts: list[str] = []
    for raw in raw_parts:
        for piece in raw.split(","):
            p = piece.strip()
            if p:
                parts.append(p)
    resolved: list[str] = []
    for part in parts:
        if part.startswith("/api/"):
            if part not in resolved:
                resolved.append(part)
            continue
        key = re.sub(r"\s+", " ", part.lower().replace("_", " ")).strip()
        path = JOB_FUNCTION_ALIASES.get(key)
        if path and path not in resolved:
            resolved.append(path)
        else:
            logger.warning("Unknown Instahyre job function: %s", part)
    return resolved[:3] if resolved else list(DEFAULT_JOB_FUNCTIONS)


@dataclass
class InstahyreFeedSpec:
    name: str
    matching: bool = False
    search: bool = False
    skills: str = DEFAULT_SKILLS
    years: int | None = None
    job_functions: list[str] = field(default_factory=lambda: list(DEFAULT_JOB_FUNCTIONS))
    company_size: int = 0
    job_type: int = 0

    @property
    def feed_key(self) -> str:
        parts = [self.name]
        if self.skills:
            parts.append(self.skills)
        if self.years is not None:
            parts.append(f"y{self.years}")
        return "|".join(parts)


def parse_feed_url(url: str) -> InstahyreFeedSpec:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)

    def first(key: str, default: str = "") -> str:
        vals = qs.get(key, [])
        return vals[0] if vals else default

    matching = first("matching").lower() == "true"
    search = first("search").lower() == "true"
    skills_raw = first("skills")
    skills = normalize_skills(skills_raw or None)
    years_raw = first("years")
    years = int(years_raw) if years_raw.isdigit() else None
    job_functions = normalize_job_functions(first("job_functions") or None)

    if matching:
        name = "matching"
    elif search and years is not None:
        name = f"search-y{years}"
    elif search:
        name = "search"
    else:
        name = "opportunities"

    return InstahyreFeedSpec(
        name=name,
        matching=matching,
        search=search,
        skills=skills,
        years=years,
        job_functions=job_functions,
        company_size=int(first("company_size") or 0),
        job_type=int(first("job_type") or 0),
    )


def parse_feed_dict(data: dict[str, Any]) -> InstahyreFeedSpec:
    matching = bool(data.get("matching", False))
    search = bool(data.get("search", False))
    skills = "" if matching else normalize_skills(data.get("skills"))
    years = data.get("years")
    years = int(years) if years is not None else None
    raw_jf = data.get("job_functions")
    job_functions = normalize_job_functions(raw_jf) if raw_jf is not None else list(DEFAULT_JOB_FUNCTIONS)

    if data.get("name"):
        name = str(data["name"])
    elif matching:
        name = "matching"
    elif years is not None:
        name = f"search-y{years}"
    else:
        name = "search"

    return InstahyreFeedSpec(
        name=name,
        matching=matching,
        search=search,
        skills=skills,
        years=years,
        job_functions=job_functions,
        company_size=int(data.get("company_size", 0)),
        job_type=int(data.get("job_type", 0)),
    )


def default_search_feeds() -> list[InstahyreFeedSpec]:
    return [
        InstahyreFeedSpec(name="search-y3", search=True, skills=DEFAULT_SKILLS, years=3),
        InstahyreFeedSpec(name="search-y4", search=True, skills=DEFAULT_SKILLS, years=4),
        InstahyreFeedSpec(name="search-y5", search=True, skills=DEFAULT_SKILLS, years=5),
    ]


def feeds_from_config(
    *,
    search_urls: list[str] | None = None,
    feed_dicts: list[dict[str, Any]] | None = None,
    default_job_functions: list[str] | None = None,
) -> list[InstahyreFeedSpec]:
    default_jf = normalize_job_functions(default_job_functions)
    if feed_dicts:
        specs: list[InstahyreFeedSpec] = []
        for item in feed_dicts:
            merged = dict(item)
            if not merged.get("job_functions"):
                merged["job_functions"] = default_jf
            specs.append(parse_feed_dict(merged))
        return specs
    if search_urls:
        return [parse_feed_url(url) for url in search_urls]
    return default_search_feeds()


def feeds_from_urls(urls: list[str]) -> list[InstahyreFeedSpec]:
    return feeds_from_config(search_urls=urls)


async def _ui_click_matching(page: Page) -> bool:
    for pattern in (r"^matching$", r"matching jobs", r"jobs matching"):
        for role in ("link", "tab", "button"):
            el = page.get_by_role(role, name=re.compile(pattern, re.I))
            if await el.count() > 0:
                try:
                    await el.first.click(timeout=3000)
                    return True
                except PlaywrightTimeout:
                    continue
    return False


# Instahyre skill chips / dropdown use data-value (see selectize#skills).
SKILL_DATA_VALUES: dict[str, str] = {
    "java": "Java",
    "python": "Python",
    "node": "Nodejs",
    "node.js": "Nodejs",
    "nodejs": "Nodejs",
}

# What to type in the skills box (may differ from chip data-value).
SKILL_TYPE_QUERY: dict[str, str] = {
    "Java": "Java",
    "Python": "Python",
    "Nodejs": "nodejs",
}

_NODE_CHIP_DATA_VALUES = frozenset({"Nodejs", "Node.js", "nodejs"})


def skill_entry_tokens(skills: str) -> list[str]:
    """Instahyre data-value strings — order: Java, Nodejs, Python."""
    order = ["java", "node", "python"]
    requested = {part.strip().lower() for part in normalize_skills(skills).split(",") if part.strip()}
    tokens: list[str] = []
    for key in order:
        if key in requested:
            tokens.append(SKILL_DATA_VALUES[key])
    for part in requested:
        if part not in order:
            tokens.append(SKILL_DATA_VALUES.get(part, part))
    return tokens


def _skill_type_query(data_value: str) -> str:
    return SKILL_TYPE_QUERY.get(data_value, data_value)


def _is_node_skill(data_value: str) -> bool:
    return data_value in _NODE_CHIP_DATA_VALUES or data_value.lower() in ("node", "node.js", "nodejs")


def _option_matches_token(option_text: str, token: str) -> bool:
    """Match display text — exact only (Java ≠ JavaFX)."""
    opt = option_text.strip().lower()
    tok = token.strip().lower()
    if opt == tok:
        return True
    return bool(token == "Nodejs" and opt in ("node.js", "nodejs", "node"))


async def _ui_ensure_search_panel(page: Page) -> None:
    filters = page.locator("#job-search-section .job-search-filters")
    if await filters.count() > 0 and await filters.first.is_visible():
        return
    heading = page.locator("#job-search-section .job-search-heading")
    if await heading.count() > 0:
        await heading.first.click(timeout=3000)
        await page.wait_for_timeout(500)


async def _skills_selectize_input(page: Page):
    return page.locator(SKILLS_SELECTIZE_INPUT)


async def _ui_clear_skill_chips(page: Page) -> None:
    await _ui_ensure_search_panel(page)
    removes = page.locator(SKILLS_SELECTIZE_REMOVE)
    while await removes.count() > 0:
        try:
            await removes.first.click(timeout=1000)
            await page.wait_for_timeout(200)
        except PlaywrightTimeout:
            break
    field = page.locator(SKILLS_SELECTIZE_INPUT)
    if await field.count() > 0:
        with contextlib.suppress(PlaywrightTimeout):
            await field.first.fill("")


_SKILLS_SELECTIZE_CTX_JS = """
() => {
  const filters = document.querySelectorAll("#job-search-section div.filter");
  let skillsFilter = null;
  for (const f of filters) {
    const label = f.querySelector("label");
    if (label && label.textContent.trim() === "Skills") {
      skillsFilter = f;
      break;
    }
  }
  const input =
    skillsFilter?.querySelector("#skills-selectized") ||
    skillsFilter?.querySelector(".selectize-input input");
  const control = input?.closest(".selectize-control");
  let contents = null;
  const dd = control?.querySelector(".selectize-dropdown");
  if (dd) {
    const style = window.getComputedStyle(dd);
    if (style.display !== "none") {
      contents = dd.querySelector(".selectize-dropdown-content");
    }
  }
  if (!contents) {
    const open = [...document.querySelectorAll(".selectize-dropdown")].find((el) => {
      if (el.classList.contains("selectize-dropdown-hidden")) return false;
      const style = window.getComputedStyle(el);
      return style.display !== "none" && style.visibility !== "hidden";
    });
    contents = open?.querySelector(".selectize-dropdown-content") || null;
  }
  return { hasSelectize: !!control, hasContents: !!contents };
}
"""

_ADD_SELECTIZE_SKILL_JS = """
(dataValue) => {
  const want = String(dataValue || "").trim();
  const matchesWant = (value) => {
    const v = String(value || "").trim();
    if (v === want) return true;
    if (want === "Nodejs") {
      const lower = v.toLowerCase();
      return lower === "nodejs" || lower === "node.js";
    }
    return false;
  };
  const filters = document.querySelectorAll("#job-search-section div.filter");
  let skillsFilter = null;
  for (const f of filters) {
    const label = f.querySelector("label");
    if (label && label.textContent.trim() === "Skills") {
      skillsFilter = f;
      break;
    }
  }
  const input =
    skillsFilter?.querySelector("#skills-selectized") ||
    skillsFilter?.querySelector(".selectize-input input");
  const control = input?.closest(".selectize-control");
  let selectize = control?.selectize || input?.selectize || null;
  if (!selectize && input) {
    let node = input.parentElement;
    while (node && !selectize) {
      selectize = node.selectize;
      node = node.parentElement;
    }
  }
  if (!selectize && window.jQuery && input) {
    const jq = window.jQuery(input);
    selectize = jq.data("selectize") || jq[0]?.selectize;
  }

  const getDropdownContents = () => {
    const dd = control?.querySelector(".selectize-dropdown");
    if (dd) {
      const style = window.getComputedStyle(dd);
      if (style.display !== "none") {
        return dd.querySelector(".selectize-dropdown-content");
      }
    }
    const open = [...document.querySelectorAll(".selectize-dropdown")].find((el) => {
      if (el.classList.contains("selectize-dropdown-hidden")) return false;
      const style = window.getComputedStyle(el);
      return style.display !== "none" && style.visibility !== "hidden";
    });
    return open?.querySelector(".selectize-dropdown-content") || null;
  };

  const pickItemEl = (item) => {
    const value = item.getAttribute("data-value") || "";
    const label = (item.querySelector("span")?.textContent || item.textContent || "").trim();
    item.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true, view: window }));
    item.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true, view: window }));
    item.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
    if (selectize && !selectize.items.includes(String(value))) {
      try { selectize.addItem(String(value), true); } catch (e) { /* mousedown may have added it */ }
    }
    return { ok: true, picked: value || label, via: "dropdown-dom" };
  };

  // Prefer addItem from preloaded skill list — works even when dropdown is closed.
  if (selectize) {
    const inOptions = Object.values(selectize.options || {}).some(
      (opt) => matchesWant(String(opt.value || opt.text || ""))
    );
    const existing = selectize.items.find((item) => matchesWant(item));
    if (inOptions && !existing) {
      const opt = Object.values(selectize.options || {}).find((o) =>
        matchesWant(String(o.value || o.text || ""))
      );
      const toAdd = opt ? String(opt.value) : want;
      try {
        selectize.addItem(toAdd);
        return { ok: true, picked: toAdd, via: "selectize-addItem" };
      } catch (e) { /* fall through to dropdown click */ }
    }
    if (existing || selectize.items.some((item) => matchesWant(item))) {
      return { ok: true, picked: want, via: "already-selected" };
    }
  }

  const contents = getDropdownContents();
  if (contents) {
    for (const item of contents.querySelectorAll(".item[data-selectable]")) {
      const value = item.getAttribute("data-value") || "";
      if (!matchesWant(value)) continue;
      return pickItemEl(item);
    }
  }

  if (selectize) {
    const opt = Object.values(selectize.options || {}).find((o) =>
      matchesWant(String(o.value || o.text || ""))
    );
    const toAdd = opt ? String(opt.value) : want;
    if (!selectize.items.some((item) => matchesWant(item))) {
      selectize.addItem(toAdd);
    }
    return { ok: true, picked: toAdd, via: "selectize-api" };
  }

  return {
    ok: false,
    reason: "no matching item",
    labels: contents
      ? [...contents.querySelectorAll(".item[data-selectable]")].map((o) => o.getAttribute("data-value")).filter(Boolean)
      : [],
  };
}
"""


_SET_ALL_SKILLS_JS = """
(dataValues) => {
  const filters = document.querySelectorAll("#job-search-section div.filter");
  let skillsFilter = null;
  for (const f of filters) {
    const label = f.querySelector("label");
    if (label && label.textContent.trim() === "Skills") {
      skillsFilter = f;
      break;
    }
  }
  const input =
    skillsFilter?.querySelector("#skills-selectized") ||
    skillsFilter?.querySelector(".selectize-input input");
  const control = input?.closest(".selectize-control");
  let selectize = control?.selectize || input?.selectize || null;
  if (!selectize && input) {
    let node = input.parentElement;
    while (node && !selectize) {
      selectize = node.selectize;
      node = node.parentElement;
    }
  }
  if (!selectize && window.jQuery && input) {
    const jq = window.jQuery(input);
    selectize = jq.data("selectize") || jq[0]?.selectize;
  }
  if (!selectize) return { ok: false, reason: "no selectize" };
  const want = dataValues.map((v) => String(v));
  if (typeof selectize.setValue === "function") {
    selectize.setValue(want);
    return { ok: true, via: "setValue", items: selectize.items };
  }
  for (const v of want) {
    if (!selectize.items.includes(v)) {
      selectize.addItem(v);
    }
  }
  return { ok: true, via: "addItem-loop", items: selectize.items };
}
"""


_CLICK_SELECTIZE_OPTION_JS = _ADD_SELECTIZE_SKILL_JS


async def _clear_selectize_query(page: Page, field) -> None:
    await field.click(timeout=3000)
    await page.keyboard.press("ControlOrMeta+A")
    await page.keyboard.press("Backspace")


_LIST_SKILL_DROPDOWN_LABELS_JS = """
() => {
  const filters = document.querySelectorAll("#job-search-section div.filter");
  let skillsFilter = null;
  for (const f of filters) {
    const label = f.querySelector("label");
    if (label && label.textContent.trim() === "Skills") {
      skillsFilter = f;
      break;
    }
  }
  const control = skillsFilter?.querySelector(".selectize-control");
  let contents = null;
  const dd = control?.querySelector(".selectize-dropdown");
  if (dd && getComputedStyle(dd).display !== "none") {
    contents = dd.querySelector(".selectize-dropdown-content");
  }
  if (!contents) {
    const open = [...document.querySelectorAll(".selectize-dropdown")].find((el) => {
      if (el.classList.contains("selectize-dropdown-hidden")) return false;
      const style = window.getComputedStyle(el);
      return style.display !== "none" && style.visibility !== "hidden";
    });
    contents = open?.querySelector(".selectize-dropdown-content") || null;
  }
  if (!contents) return [];
  return [...contents.querySelectorAll(".item[data-selectable]")]
    .map((o) => o.getAttribute("data-value"))
    .filter(Boolean);
}
"""


async def _list_skill_dropdown_labels(page: Page) -> list[str]:
    result = await page.evaluate(_LIST_SKILL_DROPDOWN_LABELS_JS)
    return result if isinstance(result, list) else []


VISIBLE_SELECTIZE_ITEMS = f"{SKILLS_FILTER} .selectize-dropdown .selectize-dropdown-content .item[data-selectable]"


async def _pick_selectize_option(page: Page, data_value: str, *, log_on_fail: bool = True) -> bool:
    """Pick skill by exact data-value (Java ≠ JavaFX). Instahyre uses div.item not div.option."""

    async def _chip_done() -> bool:
        return await _selectize_chip_present(page, data_value)

    if await _chip_done():
        return True

    async def _click_item(locator) -> bool:
        try:
            if await locator.count() == 0 or not await locator.first.is_visible():
                return False
            el = locator.first
            await el.dispatch_event("mousedown")
            await el.dispatch_event("mouseup")
            await el.click(timeout=2000)
            await page.wait_for_timeout(200)
            return await _chip_done()
        except Exception:
            return False

    item_sel = f'{SKILLS_FILTER} .selectize-dropdown .item[data-selectable][data-value="{data_value}"]'

    for attempt in range(4):
        if await _chip_done():
            return True

        result = await page.evaluate(_ADD_SELECTIZE_SKILL_JS, data_value)
        if isinstance(result, dict) and result.get("ok"):
            await page.wait_for_timeout(200)
            if await _chip_done():
                return True

        if await _click_item(page.locator(item_sel)):
            return True

        if await _click_item(
            page.locator(SKILLS_DROPDOWN_ITEMS).filter(has=page.locator(f'[data-value="{data_value}"]'))
        ):
            return True

        await page.wait_for_timeout(150 + attempt * 100)

    if await _chip_done():
        return True

    if log_on_fail:
        ctx = await page.evaluate(_SKILLS_SELECTIZE_CTX_JS)
        labels = await _list_skill_dropdown_labels(page)
        logger.warning(
            "No selectize match for %s (dropdown data-values: %s, ctx: %s)",
            data_value,
            labels[:12] if labels else "(empty)",
            ctx,
        )
    return False


_CHIP_PRESENT_JS = """
(dataValue) => {
  const want = String(dataValue || "").trim();
  const isNode = (value) => {
    const v = (value || "").toLowerCase();
    return v === "nodejs" || v === "node.js";
  };
  const filters = document.querySelectorAll("#job-search-section div.filter");
  let skillsFilter = null;
  for (const f of filters) {
    const label = f.querySelector("label");
    if (label && label.textContent.trim() === "Skills") {
      skillsFilter = f;
      break;
    }
  }
  for (const el of skillsFilter?.querySelectorAll(".selectize-input .item") || []) {
    const value = el.getAttribute("data-value") || "";
    if (value === want) return true;
    if (want === "Nodejs" && isNode(value)) return true;
  }
  return false;
}
"""


async def _selectize_chip_present(page: Page, token: str) -> bool:
    return bool(await page.evaluate(_CHIP_PRESENT_JS, token))


_TRIGGER_SELECTIZE_SEARCH_JS = """
(dataValue) => {
  const filters = document.querySelectorAll("#job-search-section div.filter");
  let skillsFilter = null;
  for (const f of filters) {
    const label = f.querySelector("label");
    if (label && label.textContent.trim() === "Skills") {
      skillsFilter = f;
      break;
    }
  }
  const input =
    skillsFilter?.querySelector("#skills-selectized") ||
    skillsFilter?.querySelector(".selectize-input input");
  const control = input?.closest(".selectize-control");
  let selectize = control?.selectize || null;
  const query = String(dataValue || "");
  if (selectize) {
    if (typeof selectize.setTextboxValue === "function") {
      selectize.setTextboxValue(query);
    } else if (input) {
      input.value = query;
    }
    if (typeof selectize.onSearchChange === "function") {
      selectize.onSearchChange(query);
    } else if (typeof selectize.refreshOptions === "function") {
      selectize.refreshOptions(false);
    }
    return true;
  }
  if (input) {
    input.value = query;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    return true;
  }
  return false;
}
"""


async def _click_first_skill_dropdown_item(page: Page) -> bool:
    """Type nodejs → one option (Node.js) — click the first visible selectable item."""
    item = page.locator(f"{SKILLS_FILTER} .selectize-dropdown .item[data-selectable]").first
    try:
        if await item.count() == 0 or not await item.is_visible():
            return False
        await item.dispatch_event("mousedown")
        await item.dispatch_event("mouseup")
        await item.click(timeout=2000)
        return True
    except Exception:
        return False


async def _type_skill_query(page: Page, field, data_value: str) -> None:
    query = _skill_type_query(data_value)
    await field.click(timeout=3000)
    with contextlib.suppress(PlaywrightTimeout):
        await field.fill("")
    await field.press_sequentially(query, delay=50)
    await page.evaluate(_TRIGGER_SELECTIZE_SEARCH_JS, query)


async def _ui_add_one_skill_chip(page: Page, field, data_value: str) -> bool:
    if await _selectize_chip_present(page, data_value):
        logger.info("Skill chip already present: %s", data_value)
        return True

    try:
        await _type_skill_query(page, field, data_value)
        await page.wait_for_timeout(400)
        if await _selectize_chip_present(page, data_value):
            logger.info("Added skill chip: %s", data_value)
            return True

        # Node: typing "nodejs" shows a single "Node.js" option — click it.
        if _is_node_skill(data_value) and await _click_first_skill_dropdown_item(page):
            await page.wait_for_timeout(300)
            if await _selectize_chip_present(page, data_value):
                logger.info("Added skill chip: %s (via Node.js option)", data_value)
                return True

        if await _pick_selectize_option(page, data_value):
            logger.info("Added skill chip: %s", data_value)
            return True

        if await _selectize_chip_present(page, data_value):
            logger.info("Added skill chip: %s (late verify)", data_value)
            return True

        await page.keyboard.press("Escape")
        return False
    except PlaywrightTimeout:
        logger.warning("Could not add skill chip: %s", data_value)
        return False


_SYNC_SKILLS_YEARS_JS = """
(args) => {
  const section = document.querySelector("#job-search-section");
  if (!section) return { ok: false, reason: "no section" };
  let scope = angular.element(section).scope();
  while (scope && !scope.search) {
    scope = scope.$parent;
  }
  if (!scope?.search?.searchObj) return { ok: false, reason: "no searchObj" };
  const obj = scope.search.searchObj;
  const skills = (args.skills || []).map(String);
  if (skills.length) {
    obj.skills = skills;
  }
  if (args.years != null && args.years !== "") {
    obj.years = String(args.years);
  }
  obj.search = true;
  // Never assign obj.job_functions here — Instahyre treats the joined string as one
  // selectize value. Let the job-functions selectize change handler update searchObj.
  const jfInput = document.querySelector("#job-functions-selectized");
  const jfSelectize = jfInput?.closest(".selectize-control")?.selectize;
  if (jfSelectize && typeof jfSelectize.trigger === "function") {
    jfSelectize.trigger("change");
  }
  scope.$apply();
  return {
    ok: true,
    job_functions: obj.job_functions,
    skills: obj.skills,
    years: obj.years,
    selectize_items: jfSelectize ? [...jfSelectize.items] : [],
  };
}
"""


_REPAIR_JOB_FUNCTION_CHIPS_JS = """
(want) => {
  const input = document.querySelector("#job-functions-selectized");
  const selectize = input?.closest(".selectize-control")?.selectize;
  if (!selectize) return { ok: false, reason: "no selectize" };
  const expected = (want || []).map(String).map((v) => v.trim()).filter(Boolean).slice(0, 3);
  let repaired = false;

  const splitCollapsed = (items) => {
    if (items.length !== 1) return null;
    const only = String(items[0] || "");
    if (!only.includes(",")) return null;
    const parts = only.split(",").map((s) => s.trim()).filter((s) => s.startsWith("/api/"));
    return parts.length > 1 ? parts : null;
  };

  let collapsed = splitCollapsed(selectize.items);
  if (collapsed) {
    selectize.clear();
    for (const val of collapsed) {
      try { selectize.addItem(val, true); } catch (e) { /* ignore */ }
    }
    repaired = true;
  }

  const missing = expected.filter((val) => !selectize.items.includes(val));
  if (missing.length) {
    for (const val of missing) {
      if (selectize.items.length >= 3) break;
      const inOptions = Object.values(selectize.options || {}).some(
        (opt) => String(opt.value) === val
      );
      if (!inOptions) continue;
      try { selectize.addItem(val, true); } catch (e) { /* ignore */ }
    }
    repaired = true;
  }

  const extras = selectize.items.filter((val) => !expected.includes(val));
  if (extras.length) {
    for (const val of extras) {
      try { selectize.removeItem(val, true); } catch (e) { /* ignore */ }
    }
    repaired = true;
  }

  return {
    ok: expected.every((val) => selectize.items.includes(val)),
    repaired,
    items: [...selectize.items],
  };
}
"""


def _url_has_job_functions(url: str, job_functions: list[str]) -> bool:
    if not job_functions:
        return True
    return all(jf in url or jf.replace("/", "%2F") in url for jf in job_functions)


def _url_matches_search_spec(url: str, spec: InstahyreFeedSpec) -> bool:
    if "search=true" not in url:
        return False
    if spec.skills and "skills=" not in url.lower():
        return False
    if spec.years is not None and f"years={spec.years}" not in url:
        return False
    return _url_has_job_functions(url, spec.job_functions)


async def _wait_for_search_url(page: Page, spec: InstahyreFeedSpec, *, timeout_ms: int = 15000) -> bool:
    """Wait until navigation reflects all search filters (avoids stale URL from prior feed)."""
    elapsed = 0
    poll_ms = 300
    while elapsed < timeout_ms:
        if _url_matches_search_spec(page.url, spec):
            logger.info("Instahyre filters in URL: %s", page.url.split("?", 1)[-1][:200])
            return True
        await page.wait_for_timeout(poll_ms)
        elapsed += poll_ms
    logger.warning(
        "Instahyre URL missing expected filters after %dms: %s",
        timeout_ms,
        page.url.split("?", 1)[-1][:200],
    )
    return False


async def _sync_skills_years(page: Page, spec: InstahyreFeedSpec) -> bool:
    try:
        result = await page.evaluate(
            _SYNC_SKILLS_YEARS_JS,
            {
                "skills": skill_entry_tokens(spec.skills),
                "years": spec.years,
            },
        )
        return isinstance(result, dict) and result.get("ok")
    except Exception as exc:
        logger.debug("Could not sync Instahyre skills/years: %s", exc)
        return False


async def _job_functions_ready(page: Page, tokens: list[str]) -> bool:
    for token in tokens:
        if not await _job_function_chip_present(page, token):
            return False
    return True


async def _ensure_job_function_chips(page: Page, job_functions: list[str]) -> bool:
    """Keep job-function selectize as separate chips — repair collapsed API-path blobs."""
    tokens = normalize_job_functions(job_functions)
    if not tokens:
        return True
    if await _job_functions_ready(page, tokens):
        return True
    try:
        result = await page.evaluate(_REPAIR_JOB_FUNCTION_CHIPS_JS, tokens)
    except Exception as exc:
        logger.debug("Could not repair job function chips: %s", exc)
        return False
    if not isinstance(result, dict):
        return False
    items = result.get("items") or []
    if result.get("repaired"):
        logger.info("Repaired job function chips: %s", ", ".join(items))
    if not await _job_functions_ready(page, tokens):
        dom_items = await page.evaluate(_LIST_JOB_FUNCTION_ITEMS_JS)
        logger.warning(
            "Job function chips incomplete (want %s, selectize %s)",
            ", ".join(tokens),
            ", ".join(dom_items) if dom_items else "(none)",
        )
        return False
    return True


async def _dismiss_apply_modal(page: Page) -> None:
    modal = page.locator(".application-modal.candidate-apply-modal")
    if await modal.count() == 0:
        return
    for sel in (".application-modal-close", ".back-button-modal-close", "button.close"):
        close = modal.locator(sel).first
        if await close.count() > 0:
            try:
                if await close.is_visible():
                    await close.click(timeout=2000)
                    await page.wait_for_timeout(400)
                    return
            except Exception:
                pass
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(400)
    except Exception:
        pass


async def _ensure_opportunities_page(page: Page) -> None:
    if "opportunities" in page.url:
        return
    await page.goto(f"{INSTAHYRE_OPPORTUNITIES}?matching=true", wait_until="domcontentloaded", timeout=90000)
    await page.wait_for_timeout(PAGE_SETTLE_MS)


async def _ui_add_skills(page: Page, skills: str) -> None:
    await _ui_ensure_search_panel(page)
    tokens = skill_entry_tokens(skills)
    if not tokens:
        return
    field = page.locator(SKILLS_SELECTIZE_INPUT)
    if await field.count() == 0:
        logger.warning("Instahyre skills selectize input not found")
        return

    await _ui_clear_skill_chips(page)

    bulk = await page.evaluate(_SET_ALL_SKILLS_JS, tokens)
    if isinstance(bulk, dict) and bulk.get("ok"):
        missing = [t for t in tokens if not await _selectize_chip_present(page, t)]
        if not missing:
            logger.info("Added skill chips via bulk: %s", ", ".join(tokens))
            return
        logger.info("Bulk set partial — adding missing: %s", ", ".join(missing))
        tokens = missing

    added: list[str] = []
    for data_value in tokens:
        if await _ui_add_one_skill_chip(page, field.first, data_value):
            added.append(data_value)
        else:
            logger.warning("Stopped adding skills after failure on %s", data_value)
            break
    logger.info("Added skill chips: %s", ", ".join(added) if added else "(none)")


_SET_ALL_JOB_FUNCTIONS_JS = """
(dataValues) => {
  const input = document.querySelector("#job-functions-selectized");
  const control = input?.closest(".selectize-control");
  let selectize = control?.selectize || document.querySelector("selectize#job-functions")?.selectize || null;
  if (!selectize && window.jQuery) {
    const jq = window.jQuery("#job-functions");
    selectize = jq.data("selectize") || jq[0]?.selectize;
  }
  if (!selectize) return { ok: false, reason: "no selectize" };
  const want = dataValues.map(String).map((v) => v.trim()).filter(Boolean).slice(0, 3);
  if (typeof selectize.clear === "function") {
    selectize.clear();
  }
  for (const val of want) {
    if (selectize.items.includes(val)) continue;
    const inOptions = Object.values(selectize.options || {}).some(
      (opt) => String(opt.value) === val
    );
    if (!inOptions) continue;
    try {
      selectize.addItem(val, true);
    } catch (e) { /* mousedown path may have added it */ }
  }
  return { ok: true, via: "addItem-loop", items: [...selectize.items] };
}
"""

_JOB_FUNCTION_CHIP_PRESENT_JS = """
(dataValue) => {
  const want = String(dataValue || "");
  const input = document.querySelector("#job-functions-selectized");
  const selectize = input?.closest(".selectize-control")?.selectize;
  if (selectize?.items?.includes(want)) return true;
  const filter = [...document.querySelectorAll("#job-search-section div.filter")].find((f) => {
    const label = f.querySelector("label");
    return label && label.textContent.trim() === "Job Functions";
  });
  for (const el of filter?.querySelectorAll(".selectize-input .item") || []) {
    if (el.getAttribute("data-value") === want) return true;
  }
  return false;
}
"""

_LIST_JOB_FUNCTION_ITEMS_JS = """
() => {
  const input = document.querySelector("#job-functions-selectized");
  const selectize = input?.closest(".selectize-control")?.selectize;
  return selectize ? [...selectize.items] : [];
}
"""

_CLICK_JOB_FUNCTION_JS = """
(dataValue) => {
  const want = String(dataValue || "");
  const input = document.querySelector("#job-functions-selectized");
  const control = input?.closest(".selectize-control");
  let selectize = control?.selectize || document.querySelector("selectize#job-functions")?.selectize || null;
  if (selectize?.items?.includes(want)) {
    return { ok: true, via: "already-selected" };
  }
  if (selectize && selectize.items.length < 3) {
    const inOptions = Object.values(selectize.options || {}).some(
      (opt) => String(opt.value) === want
    );
    if (inOptions) {
      selectize.addItem(want, true);
      return { ok: true, via: "selectize-addItem", picked: want };
    }
  }
  const filter = [...document.querySelectorAll("#job-search-section div.filter")].find((f) => {
    const label = f.querySelector("label");
    return label && label.textContent.trim() === "Job Functions";
  });
  const opt =
    filter?.querySelector(`.option.selectize-option[data-selectable][data-value="${want}"]`) ||
    filter?.querySelector(`.selectize-dropdown .option[data-value="${want}"]`);
  if (opt) {
    opt.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true, view: window }));
    opt.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true, view: window }));
    opt.click();
    return { ok: true, via: "dropdown-click", picked: want };
  }
  return { ok: false, reason: "no option", want };
}
"""


async def _job_function_chip_present(page: Page, data_value: str) -> bool:
    return bool(await page.evaluate(_JOB_FUNCTION_CHIP_PRESENT_JS, data_value))


async def _ui_clear_job_function_chips(page: Page) -> None:
    await _ui_ensure_search_panel(page)
    removes = page.locator(JOB_FUNCTIONS_SELECTIZE_REMOVE)
    while await removes.count() > 0:
        try:
            await removes.first.click(timeout=1000)
            await page.wait_for_timeout(200)
        except PlaywrightTimeout:
            break


async def _ui_add_job_functions(page: Page, job_functions: list[str]) -> None:
    tokens = normalize_job_functions(job_functions)
    if not tokens:
        return
    field = page.locator(JOB_FUNCTIONS_SELECTIZE_INPUT)
    if await field.count() == 0:
        logger.warning("Instahyre job functions selectize input not found")
        return

    await _ui_clear_job_function_chips(page)

    bulk = await page.evaluate(_SET_ALL_JOB_FUNCTIONS_JS, tokens)
    if isinstance(bulk, dict) and bulk.get("ok"):
        missing = [t for t in tokens if not await _job_function_chip_present(page, t)]
        if not missing:
            logger.info("Added job function chips via bulk: %s", ", ".join(tokens))
            return
        logger.info("Bulk job functions partial — adding missing: %s", ", ".join(missing))
        tokens = missing

    added: list[str] = []
    for data_value in tokens:
        if await _job_function_chip_present(page, data_value):
            added.append(data_value)
            continue
        try:
            await field.first.click(timeout=3000)
            await page.wait_for_timeout(200)
            result = await page.evaluate(_CLICK_JOB_FUNCTION_JS, data_value)
            if isinstance(result, dict) and result.get("ok"):
                await page.wait_for_timeout(200)
                if await _job_function_chip_present(page, data_value):
                    added.append(data_value)
                    continue
            option = page.locator(f'{JOB_FUNCTION_DROPDOWN_OPTIONS}[data-value="{data_value}"]')
            if await option.count() > 0:
                await option.first.click(timeout=2000)
                await page.wait_for_timeout(200)
            if await _job_function_chip_present(page, data_value):
                added.append(data_value)
            else:
                current = await page.evaluate(_LIST_JOB_FUNCTION_ITEMS_JS)
                logger.warning(
                    "Could not add job function chip: %s (selectize items: %s)",
                    data_value,
                    current,
                )
                break
        except PlaywrightTimeout:
            logger.warning("Could not add job function chip: %s", data_value)
            break
    logger.info("Added job function chips: %s", ", ".join(added) if added else "(none)")


async def _ui_set_experience_years(page: Page, years: int) -> bool:
    years_input = page.locator("#years")
    if await years_input.count() > 0:
        try:
            await years_input.first.click(timeout=3000)
            await years_input.first.fill(str(years))
            logger.info("Set experience years to %d via #years", years)
            return True
        except PlaywrightTimeout:
            pass

    logger.warning("Could not set experience years to %d", years)
    return False


async def _ui_click_show_results(page: Page) -> bool:
    show = page.locator("#show-results")
    if await show.count() > 0:
        try:
            await show.first.click(timeout=5000)
            logger.info("Clicked Show results")
            return True
        except PlaywrightTimeout:
            pass
    logger.warning("Show results button not found")
    return False


async def _ui_apply_search_filters(page: Page, spec: InstahyreFeedSpec) -> bool:
    """Apply skills/years first, job functions last; never push joined API paths into searchObj."""
    await _ui_ensure_search_panel(page)
    await _ui_clear_skill_chips(page)
    await _ui_add_skills(page, spec.skills)
    if spec.years is not None:
        await _ui_set_experience_years(page, spec.years)
    await _ui_add_job_functions(page, spec.job_functions)
    await _ensure_job_function_chips(page, spec.job_functions)

    for attempt in range(4):
        await _ensure_job_function_chips(page, spec.job_functions)
        await _sync_skills_years(page, spec)
        await _ensure_job_function_chips(page, spec.job_functions)
        if not await _ui_click_show_results(page):
            return False
        if await _wait_for_search_url(page, spec):
            return True
        logger.warning(
            "Instahyre URL missing filters (attempt %d) — re-applying job functions",
            attempt + 1,
        )
        await _ui_ensure_search_panel(page)
        await _ui_add_job_functions(page, spec.job_functions)
        await _ensure_job_function_chips(page, spec.job_functions)
        if spec.years is not None:
            await _ui_set_experience_years(page, spec.years)
    return False


async def _wait_for_opportunities(page: Page, *, timeout_ms: int = ROW_WAIT_MS) -> bool:
    elapsed = 0
    while elapsed < timeout_ms:
        rows = await page.locator(EMPLOYER_ROW).count()
        views = await page.locator("#interested-btn:visible, button.button-interested:visible").count()
        fetching = await page.locator(".page-banner .loading:visible").count()
        if rows > 0 or views > 0:
            logger.info("Employer rows loaded: %d (%d view buttons)", rows, views)
            return True
        if fetching == 0 and elapsed > 5000:
            no_results = await page.get_by_text(re.compile(r"no result found", re.I)).count()
            if no_results > 0:
                logger.warning("Instahyre search returned no results")
                return False
        await page.wait_for_timeout(ROW_POLL_MS)
        elapsed += ROW_POLL_MS
    logger.warning("No employer rows visible yet on %s", page.url)
    return False


async def activate_feed(page: Page, spec: InstahyreFeedSpec) -> str:
    logger.info(
        "Instahyre activating feed: %s (job_functions=%s, skills=%s, years=%s)",
        spec.name,
        ",".join(spec.job_functions),
        spec.skills,
        spec.years,
    )

    await _dismiss_apply_modal(page)
    await _ensure_opportunities_page(page)

    if spec.matching:
        await page.goto(f"{INSTAHYRE_OPPORTUNITIES}?matching=true", wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(PAGE_SETTLE_MS)
        await _ui_click_matching(page)
    else:
        await page.goto(f"{INSTAHYRE_OPPORTUNITIES}?matching=true", wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(PAGE_SETTLE_MS)
        if not await _ui_apply_search_filters(page, spec):
            logger.warning("Instahyre search UI filters may not have applied for %s", spec.name)

    await _wait_for_opportunities(page)

    row_count = await page.locator(EMPLOYER_ROW).count()
    logger.info("Instahyre feed ready (%s): %d rows, url=%s", spec.name, row_count, page.url)
    return spec.feed_key
