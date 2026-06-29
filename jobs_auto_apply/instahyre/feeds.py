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
SKILLS_FILTER = '#job-search-section div.filter:has(label:text-is("Skills"))'
SKILLS_SELECTIZE_INPUT = f"{SKILLS_FILTER} .selectize-input input"
SKILLS_SELECTIZE_REMOVE = f"{SKILLS_FILTER} .selectize-input .remove"
SKILLS_DROPDOWN_ITEMS = f"{SKILLS_FILTER} .selectize-dropdown .selectize-dropdown-content .item[data-selectable]"
ROW_WAIT_MS = 35000
ROW_POLL_MS = 250
PAGE_SETTLE_MS = 800

# Built-in fallbacks used only when config.yaml supplies nothing. Everything here
# can be overridden/extended from config (instahyre.filters.default_skills,
# .job_function_aliases, .skill_chip_values, .skill_type_queries) so the tool is
# not tied to a backend/software-engineering search.
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

# Human-name -> Instahyre API path map covering every job function (and the broader
# job categories) Instahyre exposes via /api/v1/job_function/. Both the exact name
# (e.g. "DevOps / Cloud") and a slash/ampersand-free variant ("devops cloud") are
# accepted, case-insensitively. Raw "/api/v1/job_function/<id>" paths also work
# directly (handled in normalize_job_functions). Extend/override via config
# (instahyre.filters.job_function_aliases) for any custom naming.
JOB_FUNCTION_ALIASES: dict[str, str] = {
    # --- Convenience short aliases ---
    "backend": "/api/v1/job_function/10",
    "full stack": "/api/v1/job_function/1",
    "full-stack": "/api/v1/job_function/1",
    "fullstack": "/api/v1/job_function/1",
    "fullstack development": "/api/v1/job_function/1",
    "full stack development": "/api/v1/job_function/1",
    "software development": "/api/v1/job_category/1",
    # --- All Instahyre job functions (name -> resource_uri) ---
    "full-stack development": "/api/v1/job_function/1",
    "frontend development": "/api/v1/job_function/3",
    "project management": "/api/v1/job_function/4",
    "qa / sdet": "/api/v1/job_function/5",
    "qa sdet": "/api/v1/job_function/5",
    "ux / visual design": "/api/v1/job_function/7",
    "ux visual design": "/api/v1/job_function/7",
    "devops / cloud": "/api/v1/job_function/8",
    "devops cloud": "/api/v1/job_function/8",
    "data science / machine learning": "/api/v1/job_function/9",
    "data science machine learning": "/api/v1/job_function/9",
    "backend development": "/api/v1/job_function/10",
    "product management": "/api/v1/job_function/11",
    "engineering management": "/api/v1/job_function/12",
    "big data / dwh / etl": "/api/v1/job_function/17",
    "big data dwh etl": "/api/v1/job_function/17",
    "graphic design / animation": "/api/v1/job_function/18",
    "graphic design animation": "/api/v1/job_function/18",
    "brand management": "/api/v1/job_function/20",
    "online marketing": "/api/v1/job_function/22",
    "customer service": "/api/v1/job_function/24",
    "sales / business development": "/api/v1/job_function/25",
    "sales business development": "/api/v1/job_function/25",
    "operations management": "/api/v1/job_function/28",
    "database admin / development": "/api/v1/job_function/30",
    "database admin development": "/api/v1/job_function/30",
    "content writing": "/api/v1/job_function/31",
    "hr generalist": "/api/v1/job_function/32",
    "talent acquisition": "/api/v1/job_function/33",
    "general management / strategy": "/api/v1/job_function/34",
    "general management strategy": "/api/v1/job_function/34",
    "network administration": "/api/v1/job_function/35",
    "systems administration": "/api/v1/job_function/36",
    "it security": "/api/v1/job_function/37",
    "data analysis / business intelligence": "/api/v1/job_function/39",
    "data analysis business intelligence": "/api/v1/job_function/39",
    "accounting & taxation": "/api/v1/job_function/40",
    "accounting and taxation": "/api/v1/job_function/40",
    "seo / sem": "/api/v1/job_function/42",
    "seo sem": "/api/v1/job_function/42",
    "pr / communications": "/api/v1/job_function/43",
    "pr communications": "/api/v1/job_function/43",
    "embedded / kernel development": "/api/v1/job_function/44",
    "embedded kernel development": "/api/v1/job_function/44",
    "it management / it support": "/api/v1/job_function/57",
    "it management it support": "/api/v1/job_function/57",
    "solution architecture": "/api/v1/job_function/58",
    "mobile development": "/api/v1/job_function/60",
    "event management": "/api/v1/job_function/61",
    "photography / videography": "/api/v1/job_function/62",
    "photography videography": "/api/v1/job_function/62",
    "technical writing": "/api/v1/job_function/63",
    "technical / production support": "/api/v1/job_function/75",
    "technical production support": "/api/v1/job_function/75",
    "other software development": "/api/v1/job_function/76",
    "other design": "/api/v1/job_function/77",
    "functional consulting": "/api/v1/job_function/78",
    "presales": "/api/v1/job_function/79",
    "technical consulting": "/api/v1/job_function/80",
    "management consulting": "/api/v1/job_function/81",
    "sales support & operations": "/api/v1/job_function/82",
    "sales support and operations": "/api/v1/job_function/82",
    "architecture / interior design": "/api/v1/job_function/83",
    "architecture interior design": "/api/v1/job_function/83",
    "fashion design": "/api/v1/job_function/84",
    "advertising / creative": "/api/v1/job_function/85",
    "advertising creative": "/api/v1/job_function/85",
    "market research": "/api/v1/job_function/86",
    "data entry / mis": "/api/v1/job_function/87",
    "data entry mis": "/api/v1/job_function/87",
    "payroll & transactions": "/api/v1/job_function/88",
    "payroll and transactions": "/api/v1/job_function/88",
    "company secretary & compliance": "/api/v1/job_function/89",
    "company secretary and compliance": "/api/v1/job_function/89",
    "finance": "/api/v1/job_function/90",
    "audit & control": "/api/v1/job_function/91",
    "audit and control": "/api/v1/job_function/91",
    "hardware design and research": "/api/v1/job_function/92",
    "asic / fpga engineering": "/api/v1/job_function/93",
    "asic fpga engineering": "/api/v1/job_function/93",
    "pcb / board engineering": "/api/v1/job_function/94",
    "pcb board engineering": "/api/v1/job_function/94",
    "hardware test & validation": "/api/v1/job_function/95",
    "hardware test and validation": "/api/v1/job_function/95",
    "other hardware": "/api/v1/job_function/96",
    # --- Broader job categories (target a whole category instead of one function) ---
    "software engineering": "/api/v1/job_category/1",
    "product / project management": "/api/v1/job_category/2",
    "product project management": "/api/v1/job_category/2",
    "design and creative": "/api/v1/job_category/3",
    "sales and business": "/api/v1/job_category/5",
    "marketing": "/api/v1/job_category/6",
    "data science and analysis": "/api/v1/job_category/8",
    "it operations and support": "/api/v1/job_category/10",
    "operations": "/api/v1/job_category/11",
    "human resources": "/api/v1/job_category/12",
    "consulting": "/api/v1/job_category/22",
    "hardware engineering": "/api/v1/job_category/27",
    "accounting and finance": "/api/v1/job_category/29",
}


def _alias_key(part: str) -> str:
    return re.sub(r"\s+", " ", part.lower().replace("_", " ")).strip()


def normalize_skills(skills: str | list[str] | None, default: str = DEFAULT_SKILLS) -> str:
    if not skills:
        return default
    if isinstance(skills, str):
        return skills.strip()
    return ",".join(s.strip() for s in skills if s.strip())


def normalize_job_functions(
    values: str | list[str] | None,
    aliases: dict[str, str] | None = None,
    defaults: list[str] | None = None,
) -> list[str]:
    aliases = aliases or JOB_FUNCTION_ALIASES
    defaults = list(defaults) if defaults is not None else list(DEFAULT_JOB_FUNCTIONS)
    if not values:
        return list(defaults)
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
        path = aliases.get(_alias_key(part))
        if path and path not in resolved:
            resolved.append(path)
        else:
            logger.warning("Unknown Instahyre job function: %s", part)
    return resolved if resolved else list(defaults)


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
    # Platform chip mappings carried per-spec so feeds.py needs no global config.
    skill_chip_values: dict[str, str] = field(default_factory=lambda: dict(SKILL_DATA_VALUES))
    skill_type_queries: dict[str, str] = field(default_factory=lambda: dict(SKILL_TYPE_QUERY))

    @property
    def feed_key(self) -> str:
        parts = [self.name]
        if self.skills:
            parts.append(self.skills)
        if self.years is not None:
            parts.append(f"y{self.years}")
        return "|".join(parts)


def parse_feed_url(
    url: str,
    *,
    aliases: dict[str, str] | None = None,
    default_skills: str = DEFAULT_SKILLS,
    chip_values: dict[str, str] | None = None,
    type_queries: dict[str, str] | None = None,
) -> InstahyreFeedSpec:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)

    def first(key: str, default: str = "") -> str:
        vals = qs.get(key, [])
        return vals[0] if vals else default

    matching = first("matching").lower() == "true"
    search = first("search").lower() == "true"
    skills_raw = first("skills")
    skills = normalize_skills(skills_raw or None, default=default_skills)
    years_raw = first("years")
    years = int(years_raw) if years_raw.isdigit() else None
    job_functions = normalize_job_functions(first("job_functions") or None, aliases=aliases)

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
        skill_chip_values=dict(chip_values or SKILL_DATA_VALUES),
        skill_type_queries=dict(type_queries or SKILL_TYPE_QUERY),
    )


def parse_feed_dict(
    data: dict[str, Any],
    *,
    aliases: dict[str, str] | None = None,
    default_skills: str = DEFAULT_SKILLS,
    chip_values: dict[str, str] | None = None,
    type_queries: dict[str, str] | None = None,
) -> InstahyreFeedSpec:
    matching = bool(data.get("matching", False))
    search = bool(data.get("search", False))
    skills = "" if matching else normalize_skills(data.get("skills"), default=default_skills)
    years = data.get("years")
    years = int(years) if years is not None else None
    raw_jf = data.get("job_functions")
    job_functions = (
        normalize_job_functions(raw_jf, aliases=aliases)
        if raw_jf is not None
        else normalize_job_functions(None, aliases=aliases)
    )

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
        skill_chip_values=dict(chip_values or SKILL_DATA_VALUES),
        skill_type_queries=dict(type_queries or SKILL_TYPE_QUERY),
    )


def default_search_feeds(
    *,
    default_skills: str = DEFAULT_SKILLS,
    chip_values: dict[str, str] | None = None,
    type_queries: dict[str, str] | None = None,
) -> list[InstahyreFeedSpec]:
    skills = default_skills or DEFAULT_SKILLS
    chips = dict(chip_values or SKILL_DATA_VALUES)
    queries = dict(type_queries or SKILL_TYPE_QUERY)
    return [
        InstahyreFeedSpec(
            name=f"search-y{y}",
            search=True,
            skills=skills,
            years=y,
            skill_chip_values=chips,
            skill_type_queries=queries,
        )
        for y in (3, 4, 5)
    ]


def feeds_from_config(
    *,
    search_urls: list[str] | None = None,
    feed_dicts: list[dict[str, Any]] | None = None,
    default_job_functions: list[str] | None = None,
    job_function_aliases: dict[str, str] | None = None,
    default_skills: str | None = None,
    skill_chip_values: dict[str, str] | None = None,
    skill_type_queries: dict[str, str] | None = None,
) -> list[InstahyreFeedSpec]:
    # Merge config-provided mappings over the built-in fallbacks.
    aliases = dict(JOB_FUNCTION_ALIASES)
    for key, val in (job_function_aliases or {}).items():
        aliases[_alias_key(str(key))] = str(val)
    chip_values = {**SKILL_DATA_VALUES, **{str(k).lower(): str(v) for k, v in (skill_chip_values or {}).items()}}
    type_queries = {**SKILL_TYPE_QUERY, **{str(k): str(v) for k, v in (skill_type_queries or {}).items()}}
    skills_default = default_skills or DEFAULT_SKILLS

    default_jf = normalize_job_functions(default_job_functions, aliases=aliases)
    if feed_dicts:
        specs: list[InstahyreFeedSpec] = []
        for item in feed_dicts:
            merged = dict(item)
            if not merged.get("job_functions"):
                merged["job_functions"] = default_jf
            specs.append(
                parse_feed_dict(
                    merged,
                    aliases=aliases,
                    default_skills=skills_default,
                    chip_values=chip_values,
                    type_queries=type_queries,
                )
            )
        return specs
    if search_urls:
        return [
            parse_feed_url(
                url,
                aliases=aliases,
                default_skills=skills_default,
                chip_values=chip_values,
                type_queries=type_queries,
            )
            for url in search_urls
        ]
    return default_search_feeds(
        default_skills=skills_default,
        chip_values=chip_values,
        type_queries=type_queries,
    )


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


def skill_entry_tokens(skills: str, chip_values: dict[str, str] | None = None) -> list[str]:
    """Instahyre selectize data-value strings, preserving the configured skill order.

    Each skill keyword maps to its chip data-value via ``chip_values`` (config-driven,
    falling back to the built-in map); unknown skills pass through as-is.
    """
    chip_values = chip_values or SKILL_DATA_VALUES
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in normalize_skills(skills).split(","):
        part = raw.strip().lower()
        if not part or part in seen:
            continue
        seen.add(part)
        tokens.append(chip_values.get(part, part))
    return tokens


def _skill_type_query(data_value: str, type_queries: dict[str, str] | None = None) -> str:
    return (type_queries or SKILL_TYPE_QUERY).get(data_value, data_value)


def _is_node_skill(data_value: str) -> bool:
    return data_value in _NODE_CHIP_DATA_VALUES or data_value.lower() in ("node", "node.js", "nodejs")


async def _ui_ensure_search_panel(page: Page) -> None:
    filters = page.locator("#job-search-section .job-search-filters")
    if await filters.count() > 0 and await filters.first.is_visible():
        return
    heading = page.locator("#job-search-section .job-search-heading")
    if await heading.count() > 0:
        # Search section renders asynchronously (Angular) after navigation; wait for the
        # heading to actually become visible instead of racing the 3s click timeout.
        with contextlib.suppress(PlaywrightTimeout):
            await heading.first.wait_for(state="visible", timeout=15000)
        await heading.first.click(timeout=5000)
        await page.wait_for_timeout(500)


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


async def _type_skill_query(page: Page, field, data_value: str, type_queries: dict[str, str] | None = None) -> None:
    query = _skill_type_query(data_value, type_queries)
    await field.click(timeout=3000)
    with contextlib.suppress(PlaywrightTimeout):
        await field.fill("")
    await field.press_sequentially(query, delay=50)
    await page.evaluate(_TRIGGER_SELECTIZE_SEARCH_JS, query)


async def _ui_add_one_skill_chip(
    page: Page, field, data_value: str, type_queries: dict[str, str] | None = None
) -> bool:
    if await _selectize_chip_present(page, data_value):
        logger.info("Skill chip already present: %s", data_value)
        return True

    try:
        await _type_skill_query(page, field, data_value, type_queries)
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
                "skills": skill_entry_tokens(spec.skills, spec.skill_chip_values),
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


async def _ui_add_skills(page: Page, spec: InstahyreFeedSpec) -> None:
    await _ui_ensure_search_panel(page)
    tokens = skill_entry_tokens(spec.skills, spec.skill_chip_values)
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
        if await _ui_add_one_skill_chip(page, field.first, data_value, spec.skill_type_queries):
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
    await _ui_add_skills(page, spec)
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
