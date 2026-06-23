from __future__ import annotations

import logging
import re
from typing import Any

from playwright.async_api import Page

from ..application_questions import (
    is_generic_question_label,
    is_plausible_application_question,
    resolve_fill_answer,
)
from ..page_load import ensure_page_ready, prepare_interactive_page

logger = logging.getLogger("job_apply")

_CONSENT_LABEL = re.compile(
    r"\b(agree|consent|terms|conditions|confirm|acknowledge|accept|declare)\b",
    re.I,
)

_NOISE_LABEL = re.compile(
    r"similar jobs that you might be interested|posted \d+ days ago",
    re.I,
)

# Shared DOM helpers for Hirist screening — native radios are display:none; labels are clicked.
_HIRIST_DOM_HELPERS = """
  const GENERIC = /^(enter your answer|type here|your answer|answer|yes|no)$/i;
  const NOISE = /characters? left|mandatory question|tell the recruiter|before you submit|similar jobs that you might be interested|^\\*$/i;
  const THANK_YOU = /thank you for your|thanks for (your )?response/i;
  const YEAR_RANGE = /^\\d+\\s*[-–]\\s*\\d+\\+?$/;
  const JOB_CARD = /posted\\s+(today|yesterday|\\d+\\s+days?\\s+ago)|\\bpremium\\b|\\binfosave\\b|\\d+\\s*-\\s*\\d+\\s*yrs/i;

  function clean(s) {
    return (s || "").replace(/\\s+/g, " ").trim();
  }

  function visible(el) {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }

  function screeningRoot() {
    const all = [...document.querySelectorAll("div, section, form, main")];
    for (const el of all) {
      const head = (el.innerText || "").slice(0, 300);
      if (!/mandatory question|tell the recruiter more about yourself/i.test(head)) continue;
      if (el.querySelector('input[type="radio"], textarea, input[type="text"], input[type="number"]')) {
        return el;
      }
    }
    return null;
  }

  function radioUsable(radio) {
    if (!radio || radio.type !== "radio") return false;
    const lbl = radio.id ? document.querySelector(`label[for="${CSS.escape(radio.id)}"]`) : null;
    if (lbl && visible(lbl)) return true;
    const container = radio.closest(".radio-container-hirist, .radio-container");
    return !!(container && visible(container));
  }

  function checkboxUsable(box) {
    if (!box || box.type !== "checkbox") return false;
    const lbl = box.id ? document.querySelector(`label[for="${CSS.escape(box.id)}"]`) : null;
    if (lbl && visible(lbl)) return true;
    return visible(box);
  }

  function goodLine(line) {
    if (!line || line.length < 3) return false;
    if (GENERIC.test(line) || NOISE.test(line) || JOB_CARD.test(line)) return false;
    if (THANK_YOU.test(line) || YEAR_RANGE.test(line)) return false;
    if (/^\\d+\\.?$/.test(line)) return false;
    if (line.length > 120 && !line.includes("?")) return false;
    return true;
  }

  function plausibleLabel(label) {
    const line = clean(label);
    if (!goodLine(line)) return false;
    if (line.includes("?")) return true;
    if (/\\b(agree|consent|terms|notice|experience|ctc|salary|location)\\b/i.test(line) && line.length < 100) {
      return true;
    }
    return line.length <= 80;
  }

  function linesFromNode(node) {
    const clone = node.cloneNode(true);
    clone.querySelectorAll("input, textarea, select, button").forEach((n) => n.remove());
    return clean(clone.innerText)
      .split("\\n")
      .map((l) => clean(l.replace(/^\\d+\\.\\s*/, "")))
      .filter(goodLine);
  }

  function labelForField(field) {
    let el = field;
    for (let depth = 0; depth < 12; depth++) {
      let sib = el.previousElementSibling;
      while (sib) {
        const lines = linesFromNode(sib);
        if (lines.length) return lines[lines.length - 1];
        sib = sib.previousElementSibling;
      }
      const parent = el.parentElement;
      if (!parent) break;
      let parentSib = parent.previousElementSibling;
      while (parentSib) {
        const lines = linesFromNode(parentSib);
        if (lines.length) return lines[lines.length - 1];
        parentSib = parentSib.previousElementSibling;
      }
      el = parent;
    }
    return "";
  }

  function questionLabelForRadio(radio) {
    let node = radio.parentElement;
    for (let depth = 0; depth < 14; depth++) {
      if (!node) break;
      const lines = (node.innerText || "")
        .split("\\n")
        .map((l) => clean(l.replace(/^\\d+\\.\\s*/, "")))
        .filter((l) => l && !/^(yes|no)$/i.test(l));
      for (const line of lines) {
        if (line.includes("?") && line.length > 8) return line;
      }
      node = node.parentElement;
    }
    return labelForField(radio);
  }

  function labelsMatch(a, b) {
    const x = clean(a).toLowerCase();
    const y = clean(b).toLowerCase();
    if (!x || !y) return false;
    return x === y || x.includes(y) || y.includes(x);
  }

  function radioGroupId(radio) {
    if (radio.name) return radio.name;
    const label = questionLabelForRadio(radio);
    if (label) return `label:${label}`;
    const block = radio.closest(
      ".questionContainer, .question-container, .form-group, .field-group, [class*='question']"
    );
    if (block) return `block:${clean(block.innerText).slice(0, 120)}`;
    return radio.id || "";
  }

  function valueInRange(value, option) {
    const opt = clean(option).toLowerCase();
    const ltM = opt.match(/<\\s*(\\d+)/);
    if (ltM) return value < parseInt(ltM[1], 10);
    const rangeM = opt.match(/(\\d+)\\s*[-–]\\s*(\\d+)/);
    if (rangeM) {
      return value >= parseInt(rangeM[1], 10) && value <= parseInt(rangeM[2], 10);
    }
    const plusM = opt.match(/(\\d+)\\s*\\+/);
    if (plusM) return value >= parseInt(plusM[1], 10);
    const nums = [...opt.matchAll(/(\\d+)/g)].map((m) => parseInt(m[1], 10));
    if (nums.length === 1) return value === nums[0];
    return false;
  }

  function matchRadioOption(want, opt) {
    const w = clean(want).toLowerCase();
    const o = clean(opt).toLowerCase();
    if (!o) return false;
    if (o === w || o.includes(w) || w.includes(o)) return true;
    if (o === "yes") return /\\byes\\b/i.test(want) && !/\\bno\\b/i.test(want);
    if (o === "no") return /\\bno\\b/i.test(want);
    const wantNum = String(want).match(/(\\d+)/);
    if (wantNum && valueInRange(parseInt(wantNum[1], 10), opt)) return true;
    return false;
  }

  function fieldLabel(el, id) {
    if (id) {
      const lbl = document.querySelector(`label[for="${CSS.escape(id)}"]`);
      if (lbl) return clean(lbl.textContent);
    }
    const parent = el.closest("label");
    if (parent) return clean(parent.textContent);
    return clean(el.value);
  }
"""

_DISCOVER_JS = (
    _HIRIST_DOM_HELPERS
    + """
() => {
  const root = screeningRoot();
  if (!root) return [];
  const results = [];
  const seenLabels = new Set();

  function radioOptions(radios) {
    const opts = [];
    for (const r of radios) {
      const label = fieldLabel(r, r.id);
      if (!label) continue;
      if (/^(yes|no)$/i.test(label) || !GENERIC.test(label)) opts.push(label);
    }
    return [...new Set(opts)];
  }

  function checkboxOptions(boxes) {
    const opts = [];
    for (const box of boxes) {
      const label = fieldLabel(box, box.id);
      if (label && !GENERIC.test(label)) opts.push(label);
    }
    return [...new Set(opts)];
  }

  for (const el of root.querySelectorAll(
    'input[type="text"], input[type="number"], textarea, input:not([type])'
  )) {
    if (!visible(el)) continue;
    const label = labelForField(el);
    if (!label || !plausibleLabel(label) || seenLabels.has(label)) continue;
    seenLabels.add(label);
    results.push({ kind: "text", label });
  }

  const radiosByName = {};
  for (const el of root.querySelectorAll('input[type="radio"]')) {
    if (!radioUsable(el)) continue;
    const gid = radioGroupId(el);
    if (!gid) continue;
    (radiosByName[gid] ||= []).push(el);
  }

  for (const radios of Object.values(radiosByName)) {
    const label = questionLabelForRadio(radios[0]);
    if (!label || !plausibleLabel(label) || seenLabels.has(label)) continue;
    seenLabels.add(label);
    results.push({
      kind: "radio",
      label,
      name: radioGroupId(radios[0]),
      options: radioOptions(radios),
    });
  }

  const checksByName = {};
  const soloChecks = [];
  for (const el of root.querySelectorAll('input[type="checkbox"]')) {
    if (!checkboxUsable(el)) continue;
    if (el.name) {
      (checksByName[el.name] ||= []).push(el);
    } else {
      soloChecks.push(el);
    }
  }

  for (const box of soloChecks) {
    const label = fieldLabel(box, box.id) || labelForField(box);
    if (!label || !plausibleLabel(label) || seenLabels.has(label)) continue;
    seenLabels.add(label);
    results.push({ kind: "checkbox", label });
  }

  for (const boxes of Object.values(checksByName)) {
    if (boxes.length === 1) {
      const label = fieldLabel(boxes[0], boxes[0].id) || labelForField(boxes[0]);
      if (!label || !plausibleLabel(label) || seenLabels.has(label)) continue;
      seenLabels.add(label);
      results.push({ kind: "checkbox", label });
      continue;
    }
    const label = labelForField(boxes[0]);
    if (!label || !plausibleLabel(label) || seenLabels.has(label)) continue;
    seenLabels.add(label);
    results.push({
      kind: "checkbox_group",
      label,
      name: boxes[0].name,
      options: checkboxOptions(boxes),
    });
  }

  return results;
}
"""
)

_FILL_JS = (
    _HIRIST_DOM_HELPERS
    + """
(pairs) => {
  const root = screeningRoot();

  function setNativeValue(el, value) {
    const proto = el.tagName === "TEXTAREA"
      ? window.HTMLTextAreaElement.prototype
      : window.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) setter.call(el, value);
    else el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function clickRadio(radio) {
    const container = radio.closest(".radio-container-hirist, .radio-container");
    if (container && visible(container)) {
      container.click();
    } else {
      const lbl = radio.id ? document.querySelector(`label[for="${CSS.escape(radio.id)}"]`) : null;
      if (lbl && visible(lbl)) {
        lbl.click();
      } else {
        radio.click();
      }
    }
    radio.checked = true;
    radio.dispatchEvent(new Event("input", { bubbles: true }));
    radio.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function clickCheckbox(box, shouldCheck) {
    const lbl = box.id ? document.querySelector(`label[for="${CSS.escape(box.id)}"]`) : null;
    const target = lbl || box;
    if (shouldCheck && !box.checked) {
      target.click();
      box.dispatchEvent(new Event("change", { bubbles: true }));
    } else if (!shouldCheck && box.checked) {
      target.click();
      box.dispatchEvent(new Event("change", { bubbles: true }));
    }
  }

  function wantsChecked(answer) {
    const want = clean(answer).toLowerCase();
    return /^(yes|true|1|checked|agree|accept)$/i.test(want);
  }

  function parseMultiAnswer(answer) {
    const raw = clean(answer).toLowerCase();
    if (!raw || raw === "no" || raw === "false") return [];
    if (raw === "all" || raw === "any") return null;
    return raw.split(/[,;|]/).map((s) => s.trim()).filter(Boolean);
  }

  function optionMatches(want, opt) {
    const w = clean(want).toLowerCase();
    const o = clean(opt).toLowerCase();
    if (o === w || o.includes(w) || w.includes(o)) return true;
    const wantNum = String(want).match(/(\\d+)/);
    if (wantNum && valueInRange(parseInt(wantNum[1], 10), opt)) return true;
    return false;
  }

  const results = [];
  for (const { label, answer, kind, name } of pairs) {
    let filled = false;
    if (kind === "radio") {
      const radios = [...root.querySelectorAll('input[type="radio"]')].filter(radioUsable);
      const byName = {};
      for (const r of radios) {
        const gid = radioGroupId(r);
        if (!gid) continue;
        (byName[gid] ||= []).push(r);
      }
      for (const group of Object.values(byName)) {
        const gid = radioGroupId(group[0]);
        if (name && gid !== name) continue;
        const q = questionLabelForRadio(group[0]);
        if (!name && !labelsMatch(q, label)) continue;
        if (name && name.startsWith("label:") && !labelsMatch(q, label)) continue;
        const want = clean(answer);
        for (const r of group) {
          let opt = fieldLabel(r, r.id);
          if (!opt) opt = clean(r.value);
          if (matchRadioOption(want, opt)) {
            clickRadio(r);
            filled = true;
            break;
          }
        }
        if (filled) break;
      }
    } else if (kind === "checkbox") {
      const boxes = [...root.querySelectorAll('input[type="checkbox"]')].filter(checkboxUsable);
      const shouldCheck = wantsChecked(answer);
      for (const box of boxes) {
        const q = fieldLabel(box, box.id) || labelForField(box);
        if (q !== label) continue;
        clickCheckbox(box, shouldCheck);
        filled = true;
        break;
      }
    } else if (kind === "checkbox_group") {
      const boxes = [...root.querySelectorAll('input[type="checkbox"]')].filter(checkboxUsable);
      const byName = {};
      for (const box of boxes) {
        if (!box.name) continue;
        (byName[box.name] ||= []).push(box);
      }
      const wants = parseMultiAnswer(answer);
      for (const group of Object.values(byName)) {
        if (name && group[0].name !== name) continue;
        const q = labelForField(group[0]);
        if (!name && q !== label) continue;
        for (const box of group) {
          const opt = fieldLabel(box, box.id);
          const shouldCheck = wants === null
            || wants.some((w) => optionMatches(w, opt));
          clickCheckbox(box, shouldCheck);
        }
        filled = true;
        break;
      }
    } else {
      const fields = [...root.querySelectorAll(
        'input[type="text"], input[type="number"], textarea, input:not([type])'
      )].filter(visible);
      for (const el of fields) {
        if (labelForField(el) !== label) continue;
        el.focus();
        setNativeValue(el, answer);
        filled = true;
        break;
      }
    }
    results.push({ label, filled });
  }
  return results;
}
"""
)


async def discover_hirist_questions(page: Page) -> list[dict[str, Any]]:
    """Extract recruiter questions from Hirist apply forms (not input placeholders)."""
    await ensure_page_ready(page, for_form=True)
    try:
        await page.wait_for_selector(
            "text=/Mandatory Question|tell the recruiter more about yourself/i",
            timeout=8000,
        )
    except Exception:
        pass

    raw = await page.evaluate(_DISCOVER_JS)
    fields: list[dict[str, Any]] = []
    for index, item in enumerate(raw or []):
        label = str(item.get("label", "")).strip()
        if (
            is_generic_question_label(label)
            or _NOISE_LABEL.search(label)
            or not is_plausible_application_question(label)
        ):
            continue
        kind = str(item.get("kind", "text"))
        field: dict[str, Any] = {"kind": kind, "label": label, "index": index}
        if kind == "radio":
            field["name"] = str(item.get("name", ""))
            field["options"] = list(item.get("options") or [])
        elif kind in ("checkbox_group",):
            field["name"] = str(item.get("name", ""))
            field["options"] = list(item.get("options") or [])
        elif kind == "text" and re.search(r"\bhow many\b.*\byears?\b", label, re.I):
            # Hirist sometimes renders year questions as text inputs; keep as text.
            pass
        fields.append(field)

    if fields:
        for field in fields:
            opts = field.get("options") or []
            opt_preview = ", ".join(opts[:4])
            if len(opts) > 4:
                opt_preview += f" (+{len(opts) - 4} more)"
            logger.info(
                "Hirist: [%s] %s%s",
                field.get("kind", "text"),
                str(field.get("label", ""))[:70],
                f" — options: {opt_preview}" if opts else "",
            )
        logger.info("Hirist: discovered %d question(s)", len(fields))
    return fields


def _default_checkbox_answer(label: str, kind: str) -> str | None:
    if kind == "checkbox" and _CONSENT_LABEL.search(label):
        return "Yes"
    return None


def default_checkbox_answer(label: str, kind: str) -> str | None:
    return _default_checkbox_answer(label, kind)


async def fill_hirist_questions(
    page: Page,
    questions: list[dict[str, Any]],
    answers: dict[str, str],
) -> list[str]:
    """Fill form fields; returns labels that could not be filled."""
    pairs = []
    for field in questions:
        label = field.get("label", "").strip()
        if not label or is_generic_question_label(label):
            continue
        kind = str(field.get("kind", "text"))
        answer = answers.get(label, "").strip()
        if not answer:
            answer = default_checkbox_answer(label, kind) or ""
        if not answer:
            continue
        answer = resolve_fill_answer(answer, field)
        pair: dict[str, Any] = {"label": label, "answer": answer, "kind": kind}
        if field.get("name"):
            pair["name"] = field["name"]
        pairs.append(pair)

    if not pairs:
        return []

    await prepare_interactive_page(page, fast=False)
    results = await page.evaluate(_FILL_JS, pairs)
    failed: list[str] = []
    for row in results or []:
        if not row.get("filled"):
            label = str(row.get("label", ""))
            failed.append(label)
            logger.warning("Could not fill Hirist question: %s", label[:60])
    await page.wait_for_timeout(300)
    return failed


_ADVANCE_JS = (
    _HIRIST_DOM_HELPERS
    + """
() => {
  function scrollIntoViewForClick() {
    const root = screeningRoot();
    const targets = [root, root && root.parentElement, document.body].filter(Boolean);
    for (const node of targets) {
      try {
        node.scrollTop = node.scrollHeight;
      } catch (e) {}
    }
    window.scrollTo(0, document.body.scrollHeight);
  }
  scrollIntoViewForClick();

  const patterns = [
    /submit\\s*application/i,
    /^submit$/i,
    /^next$/i,
    /^confirm$/i,
    /^finish$/i,
    /^done$/i,
    /^continue$/i,
    /^proceed$/i,
  ];

  function textOf(el) {
    return clean(el.value || el.innerText || el.textContent || "");
  }

  function isJobLink(el) {
    if (el.tagName !== "A") return false;
    const href = el.getAttribute("href") || "";
    return /\\/j\\//.test(href) || /\\/job\\//.test(href);
  }

  function tryClick(el) {
    if (!el || !visible(el) || el.disabled) return null;
    const text = textOf(el);
    if (!text || text.length > 48) return null;
    if (!patterns.some((p) => p.test(text))) return null;
    el.scrollIntoView({ block: "center", behavior: "instant" });
    el.click();
    return text;
  }

  const roots = [screeningRoot(), document.body];
  for (const root of roots) {
    const candidates = [
      ...root.querySelectorAll(
        'button, input[type="submit"], input[type="button"], a[role="button"], a.btn, a[class*="btn"], [class*="btn-next"], [class*="btn-submit"]'
      ),
    ].filter((el) => visible(el) && !isJobLink(el));
    for (let i = candidates.length - 1; i >= 0; i--) {
      const clicked = tryClick(candidates[i]);
      if (clicked) return clicked;
    }
    const submit = root.querySelector(
      'input[type="submit"]:not([disabled]), button[type="submit"]:not([disabled])'
    );
    if (submit && visible(submit)) {
      submit.scrollIntoView({ block: "center", behavior: "instant" });
      submit.click();
      return textOf(submit) || "submit";
    }
  }
  return null;
}
"""
)


async def click_hirist_advance(page: Page) -> str | None:
    """Click Next / Submit on the Hirist screening form."""
    result = await page.evaluate(_ADVANCE_JS)
    if result:
        await page.wait_for_timeout(800)
        return str(result).lower()
    return None
