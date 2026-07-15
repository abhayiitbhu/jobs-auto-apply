from __future__ import annotations

import contextlib
import logging
import re
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from ..answers.chip_options import (
    is_lpa_chip_option,
    is_notice_chip_option,
    pick_lpa_chip_option,
    value_in_chip_range,
)
from ..answers.chips import pick_notice_period_option
from ..answers.compensation import resolve_ctc_numeric_answer
from ..application_questions import (
    enrich_field_for_llm,
    infer_field_input_type,
    is_generic_question_label,
    resolve_fill_answer,
)
from ..config import AppConfig
from ..page_load import prepare_interactive_page

logger = logging.getLogger("job_apply")

_CONSENT_LABEL = re.compile(
    r"\b(agree|consent|terms|conditions|confirm|acknowledge|accept|declare)\b",
    re.I,
)

_NOISE_LABEL = re.compile(
    r"similar jobs that you might be interested|posted \d+ days ago",
    re.I,
)

_YES_NO_QUESTION = re.compile(
    r"\b(living in|currently residing|relocate|willing to|within \d+\s*days?|"
    r"contractual role|face.to.face|f2f|walk.?in|ok with)\b",
    re.I,
)

_NOTICE_PERIOD_QUESTION = re.compile(
    r"\b(notice\s*period|\bnp\b|how\s+soon|available\s+to\s+join|can\s+you\s+join|"
    r"within\s+\d+\s*days?|serving\s+notice|join\s+us)\b",
    re.I,
)

_CCTC_LABEL = re.compile(
    r"\bcctc\b|current\s+ctc|current\s+annual\s+salary|your\s+ctc\b|"
    r"current\s+ctc\s+in\s+lacs|what\s+is\s+your\s+current\s+ctc",
    re.I,
)
_ECTC_LABEL = re.compile(
    r"\bectc\b|expected\s+ctc|salary\s+expectation|salary\s+you\s+are\s+expecting|"
    r"how\s+much\s+annual\s+salary\s+are\s+you\s+expecting|"
    r"expected\s+ctc\s+in\s+lacs|what\s+is\s+your\s+expected\s+ctc",
    re.I,
)
_HIRIST_QUESTION_CONTAINERS = (
    ".screening-question-container, .yes-no-answer-question-container, "
    ".short-answer-question-container, .long-answer-question-container, "
    ".numeric-question-container, "
    ".single-answer-question-container, .multi-answer-question-container"
)
_NOTICE_LABEL = re.compile(r"\bnotice\s*period\b|\bnp\b", re.I)

_LPA_CHIP = re.compile(r"lpa|lac", re.I)


def _is_lpa_chip(opt: str) -> bool:
    return is_lpa_chip_option(opt)


def _is_notice_chip(opt: str) -> bool:
    return is_notice_chip_option(opt)


def _looks_like_np_question(label: str) -> bool:
    return bool(_NOTICE_LABEL.search(label)) or bool(re.search(r"\bnp\b.*\b(days?|week|month)\b", label, re.I))


def _field_dedupe_score(field: dict[str, Any]) -> tuple[int, int]:
    """Prefer native inputs/radios over mis-grouped checkbox pools."""
    kind = str(field.get("kind", "text"))
    label = str(field.get("label", "")).strip()
    opts = [str(o).strip() for o in (field.get("options") or []) if str(o).strip()]
    score = 0
    if kind in ("number", "text") and not opts:
        score = 20
    elif kind == "radio":
        score = 15
    elif kind == "checkbox_group":
        lpa_opts = [o for o in opts if _is_lpa_chip(o)]
        notice_opts = [o for o in opts if _is_notice_chip(o)]
        if lpa_opts:
            score = 14
        elif notice_opts and (_CCTC_LABEL.search(label) or _ECTC_LABEL.search(label)):
            score = 2
        else:
            score = 10
    elif kind == "checkbox":
        score = 8
    else:
        score = 6
    return score, len(opts)


def _dedupe_hirist_fields(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_label: dict[str, dict[str, Any]] = {}
    for field in fields:
        label = str(field.get("label", "")).strip()
        if not label:
            continue
        prev = by_label.get(label)
        if not prev or _field_dedupe_score(field) > _field_dedupe_score(prev):
            by_label[label] = field
    return list(by_label.values())


def _repair_hirist_chip_fields(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge LPA / notice chip pools so CCTC/ECTC/NP get complete option sets."""
    all_opts: list[str] = []
    for field in fields:
        all_opts.extend(str(o).strip() for o in (field.get("options") or []) if str(o).strip())
    lpa_pool = list(dict.fromkeys(o for o in all_opts if _is_lpa_chip(o)))
    notice_pool = list(dict.fromkeys(o for o in all_opts if _is_notice_chip(o)))

    repaired: list[dict[str, Any]] = []
    for field in fields:
        out = dict(field)
        label = str(out.get("label", "")).strip()
        kind = str(out.get("kind", "text"))
        opts = [str(o).strip() for o in (out.get("options") or []) if str(o).strip()]

        if _CCTC_LABEL.search(label) or _ECTC_LABEL.search(label):
            lpa_opts = [o for o in opts if _is_lpa_chip(o)]
            if kind in ("number", "text") and not lpa_opts:
                repaired.append(out)
                continue
            lpa_opts = lpa_opts or lpa_pool
            if lpa_opts:
                out["kind"] = "checkbox_group"
                out["options"] = lpa_opts
        elif _looks_like_np_question(label) or _looks_like_notice_period_question(label):
            if kind == "radio":
                out["kind"] = "radio"
                if not opts and notice_pool:
                    out["options"] = notice_pool
                out["input_type"] = "notice_period"
            elif kind == "checkbox_group":
                notice_opts = [o for o in opts if _is_notice_chip(o)] or notice_pool or opts
                if notice_opts:
                    out["options"] = notice_opts
                    out["input_type"] = "notice_period"

        repaired.append(out)
    return repaired


def _coerce_hirist_chip_answer(
    label: str,
    answer: str,
    options: list[str],
    config: AppConfig | None = None,
) -> str:
    """Map numeric CTC or notice answers to Hirist salary / NP chips."""
    text = answer.strip()
    if not text or not options:
        return text

    if _CCTC_LABEL.search(label) or _ECTC_LABEL.search(label):
        lpa_opts = [o for o in options if _is_lpa_chip(o)]
        if lpa_opts:
            numeric = resolve_ctc_numeric_answer(label, text, config)
            if numeric and re.fullmatch(r"\d+(?:\.\d+)?", numeric):
                chip = pick_lpa_chip_option(float(numeric), lpa_opts)
                if chip:
                    return chip

    if _looks_like_np_question(label) or _looks_like_notice_period_question(label):
        picked = pick_notice_period_option(text, options)
        if picked:
            return picked

    return text


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
    const explicit = document.querySelector(".screening-questions-container")
      || document.querySelector(".jobseeker-screening-container");
    if (explicit) return explicit;
    const all = [...document.querySelectorAll("div, section, form, main")];
    for (const el of all) {
      const head = (el.innerText || "").slice(0, 300);
      if (!/mandatory question|tell the recruiter more about yourself/i.test(head)) continue;
      if (el.querySelector('input[type="radio"], textarea, input[type="text"], input[type="number"]')) {
        return el;
      }
    }
    return document.body;
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

  const INTERROGATIVE = /\\b(will|would|are|do|did|can|could|have|has|is|does|should|may)\\s+(you|u|your|this|that|the\\s+role)\\b/i;

  function goodLine(line) {
    if (!line || line.length < 3) return false;
    if (GENERIC.test(line) || NOISE.test(line) || JOB_CARD.test(line)) return false;
    if (THANK_YOU.test(line) || YEAR_RANGE.test(line)) return false;
    if (/^\\d+\\.?$/.test(line)) return false;
    // Recruiter screening questions can be long and may end with a period rather
    // than "?" (e.g. "...come down to office for F2F interview process."). Keep
    // those when they read like a question instead of dropping them.
    if (line.length > 160 && !line.includes("?") && !INTERROGATIVE.test(line)) return false;
    return true;
  }

  function plausibleLabel(label) {
    const line = clean(label);
    if (!goodLine(line)) return false;
    if (line.includes("?")) return true;
    if (/\\b(agree|consent|terms|notice|experience|ctc|salary|location|relocate|f2f|face.to.face|interview|office|work\\s*from|willing|able\\s+to|available|join)\\b/i.test(line) && line.length < 180) {
      return true;
    }
    // Question-like phrasing without a trailing "?" (Hirist recruiters often
    // write "...Will you be able to come down to office...for F2F interview.").
    if (INTERROGATIVE.test(line) && line.length < 200) {
      return true;
    }
    return line.length <= 80;
  }

  // A label read straight from a dedicated .mandatory-question / .question-text
  // element is authoritative — trust it and only reject obvious chrome/noise,
  // instead of gating real questions behind the "?"/keyword/length heuristic.
  function explicitQuestionLabel(container) {
    const el = container.querySelector(".mandatory-question")
      || container.querySelector(".question-text");
    if (!el) return "";
    const line = clean(el.innerText).replace(/^\\d+\\.\\s*/, "");
    if (!line || line.length < 3) return "";
    if (GENERIC.test(line) || NOISE.test(line) || JOB_CARD.test(line)) return "";
    if (THANK_YOU.test(line) || YEAR_RANGE.test(line)) return "";
    if (/^\\d+\\.?$/.test(line)) return "";
    return line;
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
      const container = el.closest(
        ".short-answer-question-container, .long-answer-question-container, "
        + ".yes-no-answer-question-container, "
        + ".numeric-question-container, .single-answer-question-container, "
        + ".multi-answer-question-container, .screening-question-container"
      );
      if (container) {
        const mq = container.querySelector(".mandatory-question");
        if (mq) {
          const line = clean(mq.innerText).replace(/^\\d+\\.\\s*/, "");
          if (line && (goodLine(line) || plausibleLabel(line))) return line;
        }
      }
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
    if (/immediate/i.test(w) && /immediate/i.test(o)) return true;
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
    const inputType = (el.type || "text").toLowerCase();
    const placeholder = el.placeholder || el.getAttribute("data-placeholder") || "";
    results.push({
      kind: inputType === "number" ? "number" : "text",
      label,
      placeholder,
      input_mode: inputType,
    });
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
    // Grouped checkboxes (a question container holding more than one option) are
    // emitted as a single checkbox_group by the container pass below. Skip them
    // here so each option is not mis-emitted as a standalone checkbox — Hirist
    // gives every option in a multi-select a distinct `name` ("0","1","2"…),
    // which would otherwise look like separate single checkboxes.
    const qc = el.closest(
      ".multi-answer-question-container, .screening-question-container, "
      + ".single-answer-question-container"
    );
    if (qc && qc.querySelectorAll('input[type="checkbox"]').length > 1) continue;
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

  for (const container of document.querySelectorAll(
    ".screening-question-container, .numeric-question-container, "
    + ".single-answer-question-container, .multi-answer-question-container, "
    + ".yes-no-answer-question-container, .short-answer-question-container, "
    + ".long-answer-question-container"
  )) {
    const mq = container.querySelector(".mandatory-question");
    const qt = container.querySelector(".question-text");
    if (!mq && !qt) continue;
    // Label comes straight from the DOM's question element — trust it.
    const label = explicitQuestionLabel(container);
    if (!label || seenLabels.has(label)) continue;
    const radios = [...container.querySelectorAll('input[type="radio"]')].filter(radioUsable);
    const checks = [...container.querySelectorAll('input[type="checkbox"]')].filter(checkboxUsable);
    const input = container.querySelector(
      'textarea, input[type="text"], input[type="number"], input:not([type])'
    );
    if (input && visible(input) && container.matches(
      ".numeric-question-container, .short-answer-question-container, "
      + ".long-answer-question-container"
    )) {
      seenLabels.add(label);
      const inputType = (input.type || "text").toLowerCase();
      results.push({
        kind: inputType === "number" ? "number" : "text",
        label,
        placeholder: input.placeholder || "",
        input_mode: inputType,
      });
    } else if (checks.length > 1) {
      seenLabels.add(label);
      results.push({
        kind: "checkbox_group",
        label,
        name: checks[0]?.id?.replace(/-op-.*$/, "") || checks[0]?.name || "",
        options: checkboxOptions(checks),
      });
    } else if (radios.length >= 2) {
      seenLabels.add(label);
      results.push({
        kind: "radio",
        label,
        name: radioGroupId(radios[0]),
        options: radioOptions(radios),
      });
    } else if (checks.length === 1) {
      seenLabels.add(label);
      results.push({ kind: "checkbox", label });
    } else if (input && visible(input)) {
      seenLabels.add(label);
      const inputType = (input.type || "text").toLowerCase();
      results.push({
        kind: inputType === "number" ? "number" : "text",
        label,
        placeholder: input.placeholder || "",
        input_mode: inputType,
      });
    }
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
  if (!root) {
    return pairs.map(({ label }) => ({ label, filled: false }));
  }

  function setNativeValue(el, value) {
    const proto = el.tagName === "TEXTAREA"
      ? window.HTMLTextAreaElement.prototype
      : window.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) setter.call(el, value);
    else el.value = value;
    el.dispatchEvent(new InputEvent("input", {
      bubbles: true,
      cancelable: true,
      inputType: "insertText",
      data: String(value),
    }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.dispatchEvent(new Event("blur", { bubbles: true }));
  }

  function clickRadio(radio) {
    const container = radio.closest(".radio-container-hirist, .radio-container");
    radio.checked = true;
    if (container && visible(container)) {
      container.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
      container.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
      container.click();
    } else {
      const lbl = radio.id ? document.querySelector(`label[for="${CSS.escape(radio.id)}"]`) : null;
      if (lbl && visible(lbl)) {
        lbl.click();
      } else {
    radio.click();
      }
    }
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
    if (/^(yes|true|1|immediate|immediately|available)$/.test(raw)) {
      return ["immediate", "yes", raw];
    }
    return raw.split(/[,;|]/).map((s) => s.trim()).filter(Boolean);
  }

  function optionMatches(want, opt) {
    const w = clean(want).toLowerCase();
    const o = clean(opt).toLowerCase();
    if (o === w || o.includes(w) || w.includes(o)) return true;
    if (/immediate/i.test(w) && /immediate/i.test(o)) return true;
    if (/^(yes|true|1)$/.test(w) && /immediate/i.test(o)) return true;
    const wantNum = String(want).match(/(\\d+)/);
    if (wantNum && valueInRange(parseInt(wantNum[1], 10), opt)) return true;
    return false;
  }

  const results = [];
  for (const { label, answer, kind, name } of pairs) {
    let filled = false;
    if (kind === "radio") {
      const containers = [...root.querySelectorAll(
        ".screening-question-container, .yes-no-answer-question-container, "
        + ".single-answer-question-container, .numeric-question-container, "
        + ".short-answer-question-container, .long-answer-question-container"
      )];
      const want = clean(answer);
      for (const container of containers) {
        const mq = container.querySelector(".mandatory-question");
        const qt = container.querySelector(".question-text");
        let qLabel = "";
        if (mq) qLabel = clean(mq.innerText).replace(/^\\d+\\.\\s*/, "");
        else if (qt) qLabel = clean(qt.innerText).replace(/^\\d+\\.\\s*/, "");
        if (!labelsMatch(qLabel, label)) continue;
        const radios = [...container.querySelectorAll('input[type="radio"]')].filter(radioUsable);
        for (const r of radios) {
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
      if (!filled) {
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
      }
    } else if (kind === "checkbox") {
      const boxes = [...root.querySelectorAll('input[type="checkbox"]')].filter(checkboxUsable);
      const shouldCheck = wantsChecked(answer);
      for (const box of boxes) {
        const q = fieldLabel(box, box.id) || labelForField(box);
        if (!labelsMatch(q, label)) continue;
        clickCheckbox(box, shouldCheck);
        filled = true;
        break;
      }
    } else if (kind === "checkbox_group") {
      const containers = [...root.querySelectorAll(
        ".screening-question-container, .multi-answer-question-container"
      )];
      const wants = parseMultiAnswer(answer);
      const isSingleSelect = /\\b(cctc|ectc|np)\\b/i.test(label)
        || /notice\\s*period/i.test(label);
      for (const container of containers) {
        const mq = container.querySelector(".mandatory-question");
        const qt = container.querySelector(".question-text");
        let qLabel = "";
        if (mq) qLabel = clean(mq.innerText).replace(/^\\d+\\.\\s*/, "");
        else if (qt) qLabel = clean(qt.innerText).replace(/^\\d+\\.\\s*/, "");
        if (!labelsMatch(qLabel, label)) continue;
        const boxes = [...container.querySelectorAll('input[type="checkbox"]')].filter(checkboxUsable);
        if (!boxes.length) continue;
        if (isSingleSelect && wants !== null) {
          for (const box of boxes) {
            const opt = fieldLabel(box, box.id);
            if (wants.some((w) => optionMatches(w, opt))) {
              clickCheckbox(box, true);
              filled = true;
              break;
            }
      }
    } else {
          let anyChecked = false;
          for (const box of boxes) {
            const opt = fieldLabel(box, box.id);
            const shouldCheck = wants === null
              || wants.some((w) => optionMatches(w, opt));
            if (shouldCheck) {
              clickCheckbox(box, true);
              anyChecked = true;
            }
          }
          filled = anyChecked;
        }
        if (filled) break;
      }
    } else {
      const fields = [...root.querySelectorAll(
        'input[type="text"], input[type="number"], textarea, input:not([type])'
      )].filter(visible);
      for (const el of fields) {
        if (!labelsMatch(labelForField(el), label)) continue;
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


_SCROLL_SCREENING_JS = (
    _HIRIST_DOM_HELPERS
    + """
async () => {
  const delay = (ms) => new Promise((r) => setTimeout(r, ms));
  const root = document.querySelector(".screening-questions-container")
    || screeningRoot();
  if (!root) return;
  const step = Math.max(250, Math.floor(root.clientHeight * 0.7));
  for (let y = 0; y <= root.scrollHeight; y += step) {
    root.scrollTop = y;
    window.scrollBy(0, step);
    await delay(80);
  }
  root.scrollTop = 0;
  window.scrollTo(0, 0);
  await delay(100);
}
"""
)

_FORM_STATE_JS = (
    _HIRIST_DOM_HELPERS
    + """
() => {
  const root = screeningRoot();
  const btn = (root || document).querySelector(
    ".submission-btn button, button.black-round, button.a-button"
  );
  const containerSelector =
    ".screening-question-container, .yes-no-answer-question-container, "
    + ".short-answer-question-container, .long-answer-question-container, "
    + ".numeric-question-container, "
    + ".single-answer-question-container, .multi-answer-question-container";
  const empty = [];
  const fields = [];
  const emptySeen = new Set();
  for (const container of (root || document).querySelectorAll(containerSelector)) {
    const mq = container.querySelector(".mandatory-question");
    const qt = container.querySelector(".question-text");
    if (!mq && !qt) continue;
    const label = clean((mq || qt).innerText).replace(/^\\d+\\.\\s*/, "");
    const ta = container.querySelector("textarea");
    const num = container.querySelector('input[type="number"], input[type="text"]');
    const input = ta || (num && !ta ? num : null);
    const radios = [...container.querySelectorAll('input[type="radio"]')];
    const checkboxes = [...container.querySelectorAll('input[type="checkbox"]')];
    let radioLabel = "";
    for (const r of radios) {
      if (r.checked) {
        const lbl = r.id ? document.querySelector(`label[for="${CSS.escape(r.id)}"]`) : null;
        radioLabel = lbl ? clean(lbl.textContent) : clean(r.value);
        break;
      }
    }
    let checkboxLabel = "";
    for (const box of checkboxes) {
      if (box.checked) {
        const lbl = box.id ? document.querySelector(`label[for="${CSS.escape(box.id)}"]`) : null;
        checkboxLabel = lbl ? clean(lbl.textContent) : clean(box.value);
        break;
      }
    }
    const domValue = input
      ? clean(input.value)
      : (checkboxLabel || radioLabel);
    fields.push({ label, domValue, hasRadio: radios.length > 0, radioLabel, checkboxLabel });
    const labelKey = label.toLowerCase();
    function markEmpty() {
      if (!emptySeen.has(labelKey)) {
        emptySeen.add(labelKey);
        empty.push(label);
      }
    }
    if (ta && !clean(ta.value)) markEmpty();
    if (num && !ta && !clean(num.value)) markEmpty();
    if (radios.length && !radios.some((r) => r.checked)) markEmpty();
    if (checkboxes.length && !checkboxes.some((c) => c.checked)) markEmpty();
  }
  return {
    nextDisabled: !!(btn && btn.disabled),
    empty,
    fields,
  };
}
"""
)


_TRIGGER_VALIDATION_JS = """
() => {
  const root = document.querySelector(".jobseeker-screening-container")
    || document.querySelector(".screening-questions-container");
  if (!root) return;

  function fireInput(el, value) {
    const proto = el.tagName === "TEXTAREA"
      ? window.HTMLTextAreaElement.prototype
      : window.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter && value !== undefined) setter.call(el, value);
    el.dispatchEvent(new InputEvent("input", {
      bubbles: true,
      cancelable: true,
      inputType: "insertText",
      data: String(value ?? el.value ?? ""),
    }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.dispatchEvent(new Event("blur", { bubbles: true }));
  }

  for (const el of root.querySelectorAll(
    'textarea, input[type="text"], input[type="number"], input:not([type])'
  )) {
    el.focus();
    fireInput(el, el.value);
    el.blur();
  }
  for (const r of root.querySelectorAll('input[type="radio"]')) {
    if (r.checked) {
      r.dispatchEvent(new Event("input", { bubbles: true }));
      r.dispatchEvent(new Event("change", { bubbles: true }));
      const container = r.closest(".radio-container-hirist, .radio-container");
      if (container) container.dispatchEvent(new Event("click", { bubbles: true }));
    }
  }
  for (const box of root.querySelectorAll('input[type="checkbox"]')) {
    if (box.checked) {
      box.dispatchEvent(new Event("input", { bubbles: true }));
      box.dispatchEvent(new Event("change", { bubbles: true }));
    }
  }
  const form = root.closest("form");
  if (form) {
    form.dispatchEvent(new Event("input", { bubbles: true }));
    form.dispatchEvent(new Event("change", { bubbles: true }));
  }
}
"""


async def wait_for_hirist_next_enabled(page: Page, *, timeout_ms: int = 8000) -> bool:
    """Wait until Hirist enables the screening Next/Submit button."""
    try:
        await page.wait_for_function(
            """() => {
              const btn = document.querySelector(
                '.submission-btn button, button.black-round, button.a-button'
              );
              return btn && !btn.disabled;
            }""",
            timeout=timeout_ms,
        )
        return True
    except Exception:
        return False


def _labels_match_hirist(a: str, b: str) -> bool:
    x = a.strip().lower()
    y = b.strip().lower()
    if not x or not y:
        return False
    return x == y or x.startswith(y) or y.startswith(x) or x in y or y in x


def _is_yes_no_options(options: list[str]) -> bool:
    opts = {o.strip().lower() for o in options if o.strip()}
    return bool(opts) and opts <= {"yes", "no"}


def _looks_like_notice_period_question(label: str) -> bool:
    return bool(_NOTICE_PERIOD_QUESTION.search(label))


def _hirist_answer_for_field(label: str, answers: dict[str, str]) -> str:
    """Resolve answers across duplicate notice/CCTC/ECTC labels with different wording."""
    direct = answers.get(label, "").strip()
    if direct:
        return direct
    ll = label.lower()
    for q_label, q_answer in answers.items():
        ans = q_answer.strip()
        if not ans:
            continue
        if _labels_match_hirist(q_label, label):
            return ans
        ql = q_label.lower()
        if _NOTICE_LABEL.search(ll) and _NOTICE_LABEL.search(ql):
            return ans
        if _CCTC_LABEL.search(ll) and _CCTC_LABEL.search(ql):
            return ans
        if _ECTC_LABEL.search(ll) and _ECTC_LABEL.search(ql):
            return ans
        if _YES_NO_QUESTION.search(ll) and _YES_NO_QUESTION.search(ql):
            return ans
    return ""


def _normalize_hirist_field(field: dict[str, Any]) -> dict[str, Any]:
    """Fix common Hirist discovery misclassifications."""
    label = str(field.get("label", "")).strip()
    kind = str(field.get("kind", "text"))
    opts = [str(o).strip() for o in (field.get("options") or []) if str(o).strip()]
    out = dict(field)

    if opts and _is_yes_no_options(opts):
        out["kind"] = "radio"
        out["options"] = opts
        return out

    if _CCTC_LABEL.search(label) or _ECTC_LABEL.search(label):
        lpa_opts = [o for o in opts if _is_lpa_chip(o)]
        if lpa_opts:
            out["kind"] = "checkbox_group"
            out["options"] = lpa_opts
            out["input_type"] = "ctc_numeric"
            return out
        if kind in ("number", "text") or not opts:
            out["kind"] = "number" if kind == "number" else "text"
            out["input_type"] = "ctc_numeric"
            out.pop("options", None)
            return out

    if (_looks_like_np_question(label) or _looks_like_notice_period_question(label)) and opts:
        if kind == "radio" or _is_yes_no_options(opts):
            out["kind"] = "radio"
            out["options"] = opts
            out["input_type"] = "notice_period"
            return out
        notice_opts = [o for o in opts if _is_notice_chip(o)] or opts
        if kind == "checkbox_group":
            out["kind"] = "checkbox_group"
            out["options"] = notice_opts
            out["input_type"] = "notice_period"
            return out

    if kind == "checkbox_group" and opts:
        year_opts = [o for o in opts if not _LPA_CHIP.search(o) and not _is_notice_chip(o)]
        lpa_opts = [o for o in opts if _LPA_CHIP.search(o)]
        notice_opts = [o for o in opts if _is_notice_chip(o)]
        if year_opts and lpa_opts:
            out["kind"] = "radio"
            out["options"] = year_opts
            return out
        if lpa_opts and notice_opts and (_CCTC_LABEL.search(label) or _ECTC_LABEL.search(label)):
            out["kind"] = "radio"
            out["options"] = lpa_opts
            return out
        if any(re.search(r"\d+\.?\d*\s*years?", o, re.I) for o in opts):
            year_only = [o for o in opts if re.search(r"\d", o) and not _LPA_CHIP.search(o) and not _is_notice_chip(o)]
            if year_only:
                out["kind"] = "radio"
                out["options"] = year_only
                return out

    if kind == "text" and _YES_NO_QUESTION.search(label):
        out["kind"] = "radio"
        if not opts:
            out["options"] = ["Yes", "No"]
        return out

    return out


def _coerce_hirist_radio_answer(
    label: str,
    answer: str,
    options: list[str],
    config: AppConfig | None = None,
) -> str:
    """Map prose answers to Yes/No or notice chips for Hirist radios."""
    chip = _coerce_hirist_chip_answer(label, answer, options, config)
    if chip != answer.strip():
        return chip

    a = answer.strip().lower()
    if not a:
        return answer.strip()
    opts_lower = {o.lower() for o in options if o.strip()}

    if options and _looks_like_notice_period_question(label):
        picked = pick_notice_period_option(answer, options)
        if picked:
            return picked
        if re.search(r"\b(serving|notice period)\b", a):
            for opt in options:
                if re.search(r"serving", opt, re.I):
                    return opt
        if re.search(r"\b15\b", a):
            for opt in options:
                if re.search(r"\b15\s*days?\b", opt, re.I):
                    return opt

    if re.search(r"\b(walk.?in|face.?to.?face|f2f)\b", label, re.I) and config is not None:
        from ..profile.application_facts import load_application_facts

        avail = str(load_application_facts(config).get("f2f_interview_available", "No")).strip()
        if opts_lower <= {"yes", "no"}:
            return next((o for o in options if o.lower() == avail.lower()), avail)
        if avail.lower() in ("yes", "no"):
            return avail

    if opts_lower <= {"yes", "no"}:
        if a in ("yes", "y", "true", "1"):
            return next((o for o in options if o.lower() == "yes"), "Yes")
        if a in ("no", "n", "false", "0"):
            return next((o for o in options if o.lower() == "no"), "No")
        if _YES_NO_QUESTION.search(label) or re.search(r"\bcontractual\b", label, re.I):
            if re.search(r"\b(no|not willing|cannot|won't|not ok)\b", a):
                return next((o for o in options if o.lower() == "no"), "No")
            if re.search(r"\b(yes|willing|ok|agree|current|native|available)\b", a):
                return next((o for o in options if o.lower() == "yes"), "Yes")
        if len(answer.strip()) > 20 and _YES_NO_QUESTION.search(label):
            if re.search(r"\b(no|not willing|cannot relocate)\b", a):
                return next((o for o in options if o.lower() == "no"), "No")
            if re.search(
                r"\b(current|native|bengaluru|bangalore|hyderabad|pune|mumbai|"
                r"delhi|gurgaon|gurugram|noida|chennai)\b",
                a,
            ):
                return next((o for o in options if o.lower() == "yes"), "Yes")

    if re.search(r"\b(within|15\s*days?|immediate|immediately)\b", label, re.I):
        if re.search(r"\b(yes|immediate|immediately|15|0\s*days?)\b", a):
            for opt in options:
                if re.search(r"\byes\b|\bimmediate\b|\b15\b", opt, re.I):
                    return opt
    return answer.strip()


def _field_for_dom_label(questions: list[dict[str, Any]], dom_label: str) -> dict[str, Any] | None:
    for field in questions:
        label = str(field.get("label", "")).strip()
        if label and _labels_match_hirist(label, dom_label):
            return field
    return None


async def _evaluate_fill_js(page: Page, pair: dict[str, Any]) -> list[dict[str, Any]] | None:
    try:
        return await page.evaluate(_FILL_JS, [pair])
    except PlaywrightError as exc:
        if "Execution context was destroyed" not in str(exc):
            raise
        logger.debug("Hirist fill retried after navigation: %s", exc)
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("domcontentloaded", timeout=2000)
        await page.wait_for_timeout(250)
        return await page.evaluate(_FILL_JS, [pair])


async def _scroll_submission_into_view(page: Page) -> None:
    btn = page.locator(".submission-btn button, button.black-round").filter(has_text=re.compile(r"next|submit", re.I))
    if await btn.count() > 0:
        await btn.first.scroll_into_view_if_needed()
    await page.evaluate(
        """() => {
          const b = document.querySelector('.submission-btn, button.black-round');
          if (b) b.scrollIntoView({ block: 'center', behavior: 'instant' });
          window.scrollTo(0, document.body.scrollHeight);
        }"""
    )


def _hirist_label_regex(label: str, max_chars: int = 50) -> re.Pattern:
    """Whitespace-tolerant matcher for a question label.

    Discovery stores the label with whitespace collapsed, but the live DOM often
    renders the question with extra spaces / line breaks (React wraps words in
    nested spans, plus the trailing mandatory "*"). A plain ``re.escape(label)``
    is a contiguous-whitespace pattern that then fails to match, so the question
    box is never located and *every* field on that form fails. Building the regex
    with ``\\s+`` between words matches regardless of how the DOM spaces them.
    """
    normalized = " ".join(label[:max_chars].split())
    escaped = re.escape(normalized)
    flexible = re.sub(r"(?:\\?\s)+", r"\\s+", escaped)
    return re.compile(flexible, re.I)


def _label_in_empty(label: str, empties: list[str]) -> bool:
    """Whether a question label is among the form's still-empty mandatory labels."""
    ll = str(label).strip().lower()
    if not ll:
        return False
    for e in empties:
        el = str(e).strip().lower()
        if not el:
            continue
        if ll == el or ll in el or el in ll:
            return True
    return False


async def _locate_hirist_question_boxes(page: Page, label: str) -> list:
    """Find all screening question containers for a label (Hirist often renders duplicates)."""
    pat = _hirist_label_regex(label)
    text_loc = page.locator(".question-text, .mandatory-question").filter(has_text=pat)
    boxes: list = []
    seen_ids: set[str] = set()
    for sel in (
        ".yes-no-answer-question-container",
        ".short-answer-question-container",
        ".long-answer-question-container",
        ".numeric-question-container",
        ".single-answer-question-container",
        ".multi-answer-question-container",
        ".screening-question-container",
    ):
        loc = page.locator(sel).filter(has=text_loc)
        count = await loc.count()
        for i in range(count):
            box = loc.nth(i)
            cid = (await box.get_attribute("id")) or ""
            if cid and cid in seen_ids:
                continue
            if cid:
                seen_ids.add(cid)
            boxes.append(box)
    if not boxes:
        loc = page.locator(".screening-question-container").filter(has_text=pat)
        count = await loc.count()
        for i in range(count):
            boxes.append(loc.nth(i))
    return boxes


async def _locate_hirist_question_box(page: Page, label: str):
    """Find the first screening question container for a label."""
    boxes = await _locate_hirist_question_boxes(page, label)
    return boxes[0] if boxes else None


def _coerce_hirist_notice_text_answer(label: str, answer: str) -> str:
    """Map canonical notice answers to text Hirist textareas accept."""
    text = answer.strip()
    if not text:
        return text
    if not (_looks_like_np_question(label) or _looks_like_notice_period_question(label)):
        return text
    low = text.lower()
    if low in ("0", "0 days", "0 day") or re.search(r"\b(immediate|immediately|available now)\b", low):
        return "Immediately available"
    if re.fullmatch(r"\d+", text):
        return f"{text} days"
    return text


async def _playwright_fill_one_hirist_box(
    page: Page,
    box,
    field: dict[str, Any],
    answer: str,
    *,
    slow: bool = False,
    config: AppConfig | None = None,
    label: str = "",
    kind: str = "text",
    options: list[str] | None = None,
) -> bool:
    """Fill a single Hirist question container."""
    if options is None:
        options = []

    # DOM decides the control type. Discovery / language inference may tag a question
    # as radio/checkbox, but Hirist frequently renders it as a plain textarea with no
    # choice inputs at all (e.g. "Are you willing to relocate?" as free text). Inspect
    # the live box so we type into the textarea instead of hunting for a radio that
    # doesn't exist — and so a notice-period textarea isn't coerced to "Yes" just
    # because its label reads "…Are you currently serving…".
    has_radios = await box.locator("input[type=radio]").count() > 0
    has_checks = await box.locator("input[type=checkbox]").count() > 0
    has_text_input = await box.locator("textarea, input[type=text], input[type=number], input:not([type])").count() > 0
    choice_in_dom = has_radios or has_checks
    if kind in ("radio", "checkbox", "checkbox_group") and not choice_in_dom and has_text_input:
        # Free-text control: keep a clean Yes/No-ish value, then text-fill below.
        answer = _coerce_hirist_radio_answer(label, answer, options or ["Yes", "No"], config)
        kind = "text"

    if has_checks and (
        kind in ("checkbox", "checkbox_group")
        or (_looks_like_np_question(label) or _looks_like_notice_period_question(label))
    ):
        kind = "checkbox_group"

    if choice_in_dom and (kind == "radio" or _YES_NO_QUESTION.search(label)):
        if not has_radios and has_checks:
            kind = "checkbox_group"
        answer = _coerce_hirist_radio_answer(label, answer, options or ["Yes", "No"], config)
        want = answer.strip().lower()

        async def _radio_selected() -> bool:
            try:
                return await box.locator("input[type=radio]:checked").count() > 0
            except Exception:
                return False

        async def _click_radio_option(radio) -> bool:
            try:
                container = radio.locator("xpath=ancestor::*[contains(@class,'radio-container')][1]")
                if await container.count() > 0:
                    await container.first.click()
                    await page.wait_for_timeout(100)
                    if await _radio_selected():
                        return True
            except Exception:
                pass
            rid = await radio.get_attribute("id") or ""
            if rid:
                label_el = box.locator(f'label[for="{rid}"]')
                if await label_el.count() > 0:
                    try:
                        await label_el.first.click()
                        await page.wait_for_timeout(100)
                        if await _radio_selected():
                            return True
                    except Exception:
                        pass
            try:
                await radio.evaluate(
                    """el => {
                      const c = el.closest('.radio-container-hirist, .radio-container');
                      if (c) c.click();
                      el.checked = true;
                      el.dispatchEvent(new Event('input', {bubbles: true}));
                      el.dispatchEvent(new Event('change', {bubbles: true}));
                    }"""
                )
                await page.wait_for_timeout(100)
                return await _radio_selected()
            except Exception:
                return False

        def _radio_option_matches(match_answer: str, opt_text: str) -> bool:
            tl = match_answer.strip().lower()
            ol = opt_text.strip().lower()
            if not ol:
                return False
            if (
                tl == ol
                or tl in ol
                or ol in tl
                or (tl in ("yes", "y", "true", "1") and ol == "yes")
                or (tl in ("no", "n", "false", "0") and ol == "no")
            ):
                return True
            if _looks_like_notice_period_question(label):
                return pick_notice_period_option(match_answer, [opt_text]) is not None
            if _is_lpa_chip(opt_text):
                num_m = re.search(r"(\d+(?:\.\d+)?)", match_answer)
                if num_m and pick_lpa_chip_option(float(num_m.group(1)), [opt_text]):
                    return True
            return False

        async def _select_radio_by_text(match_answer: str) -> bool:
            if not match_answer.strip():
                return False
            radios = box.locator("input[type=radio]")
            count = await radios.count()
            for i in range(count):
                radio = radios.nth(i)
                rid = await radio.get_attribute("id") or ""
                opt_text = ""
                if rid:
                    label_el = box.locator(f'label[for="{rid}"]')
                    if await label_el.count() > 0:
                        opt_text = (await label_el.first.inner_text()).strip()
                if not opt_text:
                    parent = radio.locator("xpath=..")
                    if await parent.count() > 0:
                        opt_text = (await parent.first.inner_text()).strip()
                if not _radio_option_matches(match_answer, opt_text):
                    continue
                if await _click_radio_option(radio):
                    return True
            return False

        if await _select_radio_by_text(answer):
            return True

        containers = box.locator(".radio-container-hirist, .radio-container")
        count = await containers.count()
        for i in range(count):
            item = containers.nth(i)
            text = (await item.inner_text()).strip()
            if not text or not _radio_option_matches(answer, text):
                continue
            await item.click()
            await page.wait_for_timeout(80)
            if await _radio_selected():
                return True
        for pattern in (
            answer.strip(),
            "Yes" if want in ("yes", "y", "true", "1") else "",
            "No" if want in ("no", "n", "false", "0") else "",
        ):
            if not pattern:
                continue
            for loc in (
                box.locator("label").filter(has_text=re.compile(re.escape(pattern), re.I)),
                box.locator(".radio-container-hirist, .radio-container").filter(
                    has_text=re.compile(re.escape(pattern), re.I)
                ),
            ):
                if await loc.count() > 0:
                    await loc.first.click()
                    await page.wait_for_timeout(80)
                    if await _radio_selected():
                        return True
        return False

    if kind in ("checkbox", "checkbox_group") and has_checks:
        if _looks_like_np_question(label) or _looks_like_notice_period_question(label):
            chip_opts = options or [str(o).strip() for o in (field.get("options") or []) if str(o).strip()]
            if chip_opts:
                answer = _coerce_hirist_chip_answer(label, answer, chip_opts, config)
        want = answer.strip().lower()
        is_single_select = _CCTC_LABEL.search(label) or _ECTC_LABEL.search(label) or _looks_like_np_question(label)

        async def _checkbox_checked() -> bool:
            try:
                return await box.locator("input[type=checkbox]:checked").count() > 0
            except Exception:
                return False

        if re.search(r"\b(walk.?in|face.?to.?face|f2f)\b", label, re.I):
            if want in ("yes", "y", "true", "1", "available", "immediate", "immediately"):
                for loc in (
                    box.locator("input[type=checkbox]"),
                    box.locator("label"),
                    box.locator(".checkbox, .checkbox-container-hirist, .checkbox-container"),
                ):
                    if await loc.count() > 0:
                        await loc.first.click()
                        await page.wait_for_timeout(80)
                        if await _checkbox_checked():
                            return True

        def _option_matches(opt: str) -> bool:
            ol = opt.lower()
            if want == ol or want in ol or ol in want:
                return True
            if want in ("yes", "true", "1", "immediate", "immediately", "available"):
                return "immediate" in ol or ol in ("yes", "true")
            if _is_lpa_chip(opt):
                num_m = re.search(r"(\d+(?:\.\d+)?)", answer)
                if num_m and pick_lpa_chip_option(float(num_m.group(1)), [opt]):
                    return True
            if _is_notice_chip(opt) and pick_notice_period_option(answer, [opt]):
                return True
            ym = re.search(r"(\d+)", want)
            return bool(ym and value_in_chip_range(int(ym.group(1)), opt))

        if kind == "checkbox_group" and options:
            targets = [opt for opt in options if _option_matches(opt)]
            if is_single_select and targets:
                targets = targets[:1]
            for opt in targets:
                opt_pat = re.escape(opt[:40])
                label_loc = box.locator("label").filter(has_text=re.compile(opt_pat, re.I))
                # Idempotent: if this option's checkbox is already ticked, do not
                # click again — a second click toggles a Hirist checkbox back OFF.
                try:
                    if await label_loc.count() > 0:
                        for_id = await label_loc.first.get_attribute("for")
                        if for_id:
                            inp = box.locator(f'input[id="{for_id}"]')
                            if await inp.count() > 0 and await inp.first.is_checked():
                                return True
                except Exception:
                    pass
                for loc in (
                    label_loc,
                    box.locator(".checkbox, .checkbox-container-hirist, .checkbox-container").filter(
                        has_text=re.compile(opt_pat, re.I)
                    ),
                ):
                    if await loc.count() > 0:
                        await loc.first.click()
                        await page.wait_for_timeout(80)
                        if await _checkbox_checked():
                            return True
        elif re.search(r"^(yes|true|1|immediate|immediately|available)$", want, re.I):
            for loc in (
                box.locator("label"),
                box.locator("input[type=checkbox]"),
            ):
                if await loc.count() > 0:
                    await loc.first.click()
                    await page.wait_for_timeout(80)
                    if await _checkbox_checked():
                        return True
        return await _checkbox_checked()

    inp = box.locator("textarea, input[type=text], input[type=number], input:not([type])").first
    if await inp.count() == 0:
        return False
    answer = _coerce_hirist_notice_text_answer(label, answer)
    await inp.scroll_into_view_if_needed()
    await inp.click()
    numeric_answer = answer
    if kind == "number" or infer_field_input_type(label, field) in (
        "ctc_numeric",
        "years_numeric",
    ):
        if _CCTC_LABEL.search(label) or _ECTC_LABEL.search(label):
            numeric_answer = resolve_ctc_numeric_answer(label, answer, config) or answer
        # Normalize a combined "current/expected" value (e.g. "38/45" or "38,45") to a
        # readable "38, 45"; only strip to a bare number for single-value numeric fields.
        combined = re.fullmatch(r"(\d+(?:\.\d+)?)\s*[/,]\s*(\d+(?:\.\d+)?)", (numeric_answer or "").strip())
        if combined:
            numeric_answer = f"{combined.group(1)}, {combined.group(2)}"
        else:
            numeric_answer = re.sub(r"[^\d.]", "", numeric_answer.split()[0] if numeric_answer else "")
    await inp.fill("")
    is_textarea = await box.locator("textarea").count() > 0
    use_slow = (
        slow
        or is_textarea
        or kind == "number"
        or infer_field_input_type(label, field) in ("years_numeric", "ctc_numeric")
    )
    if use_slow:
        await inp.press_sequentially(numeric_answer, delay=25)
    else:
        await inp.fill(numeric_answer)
    await inp.press("Tab")
    await page.wait_for_timeout(60)
    try:
        current = (await inp.input_value()).strip()
    except Exception:
        current = ""
    if current:
        return True
    try:
        await inp.evaluate(
            """(el, value) => {
              const proto = el.tagName === "TEXTAREA"
                ? window.HTMLTextAreaElement.prototype
                : window.HTMLInputElement.prototype;
              const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
              if (setter) setter.call(el, value);
              else el.value = value;
              el.dispatchEvent(new InputEvent('input', {
                bubbles: true, cancelable: true, inputType: 'insertText', data: String(value)
              }));
              el.dispatchEvent(new Event('change', {bubbles: true}));
              el.dispatchEvent(new Event('blur', {bubbles: true}));
            }""",
            numeric_answer,
        )
        await page.wait_for_timeout(80)
        current = (await inp.input_value()).strip()
    except Exception:
        current = ""
    result = bool(current)
    return result


async def _playwright_fill_hirist_field(
    page: Page,
    field: dict[str, Any],
    answer: str,
    *,
    slow: bool = False,
    config: AppConfig | None = None,
) -> bool:
    """Playwright fill fallback when React ignores programmatic DOM updates."""
    label = str(field.get("label", "")).strip()
    kind = str(field.get("kind", "text"))
    if not label or not answer:
        return False

    options = [str(o).strip() for o in field.get("options", []) if str(o).strip()]
    if options and (
        _CCTC_LABEL.search(label)
        or _ECTC_LABEL.search(label)
        or _looks_like_np_question(label)
        or _looks_like_notice_period_question(label)
    ):
        answer = _coerce_hirist_chip_answer(label, answer, options, config)

    snippet = _hirist_label_regex(label, max_chars=40)
    boxes = await _locate_hirist_question_boxes(page, label)
    if not boxes:
        container = page.locator(
            ".short-answer-question-container, .long-answer-question-container, "
            ".yes-no-answer-question-container, "
            ".numeric-question-container, .single-answer-question-container, "
            ".multi-answer-question-container"
        ).filter(has=page.locator(".mandatory-question, .question-text").filter(has_text=snippet))
        if await container.count() == 0:
            container = page.locator(".screening-question-container").filter(
                has=page.locator(".mandatory-question, .question-text").filter(has_text=snippet)
            )
        count = await container.count()
        boxes = [container.nth(i) for i in range(count)]

    if not boxes:
        return False

    results: list[bool] = []
    for box in boxes:
        await box.scroll_into_view_if_needed()
        results.append(
            await _playwright_fill_one_hirist_box(
                page,
                box,
                field,
                answer,
                slow=slow,
                config=config,
                label=label,
                kind=kind,
                options=options,
            )
        )
    return all(results)


async def discover_hirist_questions(page: Page, *, prepped: bool = False) -> list[dict[str, Any]]:
    """Extract recruiter questions from Hirist apply forms (not input placeholders)."""
    if not prepped:
        await prepare_interactive_page(page, fast=True)
    with contextlib.suppress(Exception):
        await page.wait_for_selector(
            "text=/Mandatory Question|tell the recruiter more about yourself/i",
            timeout=8000,
        )

    await page.evaluate(_SCROLL_SCREENING_JS)
    try:
        raw = await page.evaluate(_DISCOVER_JS)
    except PlaywrightError as exc:
        # Happens right after clicking Next when Hirist navigates/re-renders.
        if "Execution context was destroyed" not in str(exc):
            raise
        logger.debug("Hirist discovery retried after navigation: %s", exc)
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("domcontentloaded", timeout=2000)
        await page.wait_for_timeout(250)
    raw = await page.evaluate(_DISCOVER_JS)
    fields: list[dict[str, Any]] = []
    for index, item in enumerate(raw or []):
        label = str(item.get("label", "")).strip()
        # Items come straight from the live form's .mandatory-question elements, so
        # they are real questions even when the wording fails the scraped-chrome
        # plausibility heuristic (e.g. long imperative prompts like "…Paste the
        # link and in 2-3 lines tell what it does…"). Only drop empty/placeholder
        # labels and obvious chrome (NOISE), not legitimate long prompts.
        if is_generic_question_label(label) or _NOISE_LABEL.search(label):
            continue
        kind = str(item.get("kind", "text"))
        field: dict[str, Any] = {
            "kind": kind,
            "label": label,
            "index": index,
            "platform": "hirist",
        }
        placeholder = str(item.get("placeholder") or "").strip()
        if placeholder:
            field["placeholder"] = placeholder
        input_mode = str(item.get("input_mode") or "").strip()
        if input_mode:
            field["input_mode"] = input_mode
        if kind == "radio" or kind in ("checkbox_group",):
            field["name"] = str(item.get("name", ""))
            field["options"] = list(item.get("options") or [])
        fields.append(field)

    fields = _dedupe_hirist_fields(fields)
    fields = [_normalize_hirist_field(f) for f in fields]
    fields = _repair_hirist_chip_fields(fields)
    for i, field in enumerate(fields):
        fields[i] = enrich_field_for_llm(field)

    if fields:
        for field in fields:
            opts = field.get("options") or []
            opt_preview = ", ".join(opts[:4])
            if len(opts) > 4:
                opt_preview += f" (+{len(opts) - 4} more)"
            logger.info(
                "Hirist: [%s/%s] %s%s",
                field.get("kind", "text"),
                field.get("input_type", field.get("kind", "text")),
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


async def _merge_discovered_fields(page: Page, questions: list[dict[str, Any]]) -> None:
    """Second discovery pass — catches CCTC/ECTC in per-container scan missed on first pass."""
    extra = await discover_hirist_questions(page, prepped=False)
    known = {str(q.get("label", "")).strip() for q in questions}
    for field in extra:
        lbl = str(field.get("label", "")).strip()
        if lbl and lbl not in known:
            questions.append(field)
            known.add(lbl)


async def _sync_react_form_state(
    page: Page,
    questions: list[dict[str, Any]],
    answers: dict[str, str],
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    config: AppConfig | None = None,
) -> bool:
    """Re-fill fields when DOM shows values but React keeps Next disabled."""
    for field, pair in pairs:
        label = str(pair.get("label", "")).strip()
        answer = _hirist_answer_for_field(label, answers) or str(pair.get("answer", ""))
        if not answer:
            continue
        answer = resolve_fill_answer(answer, field, config)
        kind = str(field.get("kind", "text"))
        if kind == "radio":
            opts = [str(o) for o in (field.get("options") or []) if str(o).strip()]
            answer = _coerce_hirist_radio_answer(label, answer, opts or ["Yes", "No"], config)
        await _playwright_fill_hirist_field(page, field, answer, slow=True, config=config)
        js_pair: dict[str, Any] = {
            "label": label,
            "answer": answer,
            "kind": kind,
        }
        if field.get("name"):
            js_pair["name"] = field["name"]
        await _evaluate_fill_js(page, js_pair)

    state = await page.evaluate(_FORM_STATE_JS) or {}
    still_empty = state.get("empty") or []
    if still_empty:
        await _retry_empty_hirist_fields(page, questions, answers, still_empty, config=config)

    await _scroll_submission_into_view(page)
    await page.evaluate(_TRIGGER_VALIDATION_JS)
    if await wait_for_hirist_next_enabled(page, timeout_ms=5000):
        return True
    return not (await page.evaluate(_FORM_STATE_JS) or {}).get("nextDisabled")


async def is_hirist_next_enabled(page: Page) -> bool:
    """True when Hirist Next/Submit is present and enabled."""
    return await wait_for_hirist_next_enabled(page, timeout_ms=800)


async def hirist_empty_mandatory_fields(page: Page) -> list[str]:
    """Labels of mandatory fields still empty when Next is disabled."""
    state = await page.evaluate(_FORM_STATE_JS) or {}
    if not state.get("nextDisabled"):
        return []
    return list(state.get("empty") or [])


async def _retry_empty_hirist_fields(
    page: Page,
    questions: list[dict[str, Any]],
    answers: dict[str, str],
    dom_labels: list[str],
    *,
    config: AppConfig | None = None,
) -> None:
    """Re-fill mandatory fields Hirist still reports empty (label text may differ from discovery)."""
    for dom_label in dom_labels:
        field = _field_for_dom_label(questions, dom_label)
        if not field:
            extra = await discover_hirist_questions(page, prepped=False)
            field = _field_for_dom_label(extra, dom_label)
            if field:
                known = {str(q.get("label", "")).strip() for q in questions}
                if str(field.get("label", "")).strip() not in known:
                    questions.append(field)
        if not field:
            continue
        label = str(field.get("label", "")).strip()
        answer = _hirist_answer_for_field(label, answers)
        if not answer:
            for q_label, q_answer in answers.items():
                if q_answer.strip() and _labels_match_hirist(q_label, dom_label):
                    answer = q_answer.strip()
                    break
        if not answer:
            dl = dom_label.lower()
            if re.search(r"\bcctc\b", dl):
                for q_label, q_answer in answers.items():
                    if q_answer.strip() and re.search(r"\b(cctc|current ctc|current)\b", q_label, re.I):
                        answer = q_answer.strip()
                        break
            elif re.search(r"\bectc\b", dl):
                for q_label, q_answer in answers.items():
                    if q_answer.strip() and re.search(
                        r"\b(ectc|expected ctc|expected|salary expectation)\b",
                        q_label,
                        re.I,
                    ):
                        answer = q_answer.strip()
                        break
        if not answer:
            answer = default_checkbox_answer(label, str(field.get("kind", "text"))) or ""
        if not answer:
            continue
        answer = resolve_fill_answer(answer, field, config)
        await _playwright_fill_hirist_field(page, field, answer, slow=True, config=config)
        pair: dict[str, Any] = {
            "label": label,
            "answer": answer,
            "kind": str(field.get("kind", "text")),
        }
        if field.get("name"):
            pair["name"] = field["name"]
        row = await _evaluate_fill_js(page, pair)
        if row and not row[0].get("filled"):
            await _playwright_fill_hirist_field(page, field, answer, slow=True, config=config)


async def fill_hirist_questions(
    page: Page,
    questions: list[dict[str, Any]],
    answers: dict[str, str],
    *,
    prep: bool = True,
    config: AppConfig | None = None,
) -> list[str]:
    """Fill form fields; returns labels that could not be filled."""
    if prep:
        await prepare_interactive_page(page, fast=True)
    await page.evaluate(_SCROLL_SCREENING_JS)
    await _merge_discovered_fields(page, questions)

    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for field in questions:
        label = field.get("label", "").strip()
        if not label or is_generic_question_label(label):
            continue
        kind = str(field.get("kind", "text"))
        answer = _hirist_answer_for_field(label, answers)
        if not answer:
            answer = default_checkbox_answer(label, kind) or ""
        if not answer:
            continue
        answer = resolve_fill_answer(answer, field, config)
        # Only pre-coerce to a Yes/No chip when discovery says this is a real radio.
        # A yes/no-sounding label (e.g. "…Are you currently serving your notice
        # period…") can be a free-text textarea — coercing it here would overwrite the
        # real answer with "Yes". The DOM-aware fill below coerces when the control
        # actually is a radio/checkbox.
        if kind == "radio":
            opts = [str(o) for o in (field.get("options") or []) if str(o).strip()]
            answer = _coerce_hirist_radio_answer(label, answer, opts or ["Yes", "No"], config)
        elif kind in ("checkbox", "checkbox_group") and (
            _CCTC_LABEL.search(label) or _ECTC_LABEL.search(label) or _looks_like_np_question(label)
        ):
            opts = [str(o) for o in (field.get("options") or []) if str(o).strip()]
            answer = _coerce_hirist_chip_answer(label, answer, opts, config)
        elif kind == "number" or (_CCTC_LABEL.search(label) or _ECTC_LABEL.search(label)):
            answer = resolve_ctc_numeric_answer(label, answer, config) or answer
        pair: dict[str, Any] = {"label": label, "answer": answer, "kind": kind}
        if field.get("name"):
            pair["name"] = field["name"]
        pairs.append((field, pair))

    if not pairs:
        return []

    failed: list[str] = []
    for field, pair in pairs:
        label = str(pair["label"])
        ok = await _playwright_fill_hirist_field(page, field, str(pair["answer"]), config=config)
        if not ok:
            row = await _evaluate_fill_js(page, pair)
            ok = bool(row and row[0].get("filled"))
        if not ok:
            ok = await _playwright_fill_hirist_field(page, field, str(pair["answer"]), slow=True, config=config)
        if not ok:
            opts = [str(o).strip() for o in (field.get("options") or []) if str(o).strip()]
            kindf = str(field.get("kind", ""))
            is_choice = kindf in ("radio", "checkbox", "checkbox_group", "single_choice", "multi_choice")
            if config is not None and getattr(config.llm, "enabled", False) and opts and is_choice:
                from ..llm_answers import map_answer_to_option, select_options_for_question

                is_multi = kindf in ("checkbox_group", "multi_choice")
                # First try to map our stored answer onto an option (wording
                # mismatch). If nothing maps, let the LLM pick the best option(s)
                # straight from the candidate profile (e.g. descriptive
                # "what best describes your last 2 years?" multi-selects).
                chosen: list[str] = []
                mapped = map_answer_to_option(config, question=label, options=opts, answer=str(pair["answer"]))
                if mapped:
                    chosen = [mapped]
                else:
                    chosen = select_options_for_question(config, question=label, options=opts, multi=is_multi)
                if chosen:
                    logger.info(
                        "Hirist: LLM chose option(s) %s (saved answer unchanged): %s",
                        [c[:30] for c in chosen],
                        label[:50],
                    )
                    filled_any = False
                    for choice in chosen:
                        pair["answer"] = choice
                        if await _playwright_fill_hirist_field(page, field, choice, slow=True, config=config):
                            filled_any = True
                        else:
                            row = await _evaluate_fill_js(page, pair)
                            if row and row[0].get("filled"):
                                filled_any = True
                    ok = filled_any
        if not ok:
            failed.append(label)
            logger.warning("Could not fill Hirist question: %s", label[:60])

    await _scroll_submission_into_view(page)

    if not await wait_for_hirist_next_enabled(page, timeout_ms=2500):
        # Only re-fill fields the form still reports empty. Blindly re-filling every
        # field would re-click an already-ticked radio/checkbox and toggle it back
        # OFF (Hirist checkboxes toggle on click), leaving a previously satisfied
        # answer empty — a common cause of "Next stayed disabled" loops.
        pre_state = await page.evaluate(_FORM_STATE_JS) or {}
        still_empty = [str(e) for e in (pre_state.get("empty") or [])]
        logger.info("Hirist Next still disabled — retrying empty fields with keystrokes")
        for field, pair in pairs:
            lbl = str(pair["label"])
            if still_empty and not _label_in_empty(lbl, still_empty):
                continue
            await _playwright_fill_hirist_field(page, field, str(pair["answer"]), slow=True, config=config)
        await _scroll_submission_into_view(page)
        await page.evaluate(_TRIGGER_VALIDATION_JS)
        await wait_for_hirist_next_enabled(page, timeout_ms=6000)

    state = await page.evaluate(_FORM_STATE_JS) or {}
    if state.get("nextDisabled"):
        still_empty = state.get("empty") or []
        if still_empty:
            logger.info(
                "Hirist Next still disabled — retrying %d empty mandatory field(s)",
                len(still_empty),
            )
            await _retry_empty_hirist_fields(page, questions, answers, still_empty, config=config)
            await _scroll_submission_into_view(page)
            await page.evaluate(_TRIGGER_VALIDATION_JS)
            await wait_for_hirist_next_enabled(page, timeout_ms=6000)
            state = await page.evaluate(_FORM_STATE_JS) or {}

    if state.get("nextDisabled"):
        still_empty = state.get("empty") or []
        if still_empty:
            preview = "; ".join(str(q)[:50] for q in still_empty[:4])
            logger.warning(
                "Hirist Next disabled — empty mandatory fields: %s%s",
                preview,
                " …" if len(still_empty) > 4 else "",
            )
            for q in still_empty:
                if q not in failed:
                    failed.append(q)
        else:
            diag = state.get("fields") or []
            preview = "; ".join(f"{f.get('label', '')[:35]}={str(f.get('domValue', ''))[:12]}" for f in diag[:6])
            logger.warning(
                "Hirist Next disabled but DOM shows values (React state?): %s",
                preview,
            )
            synced = await _sync_react_form_state(page, questions, answers, pairs, config=config)
            if synced:
                logger.info("Hirist React state synced — Next enabled")
                failed = [f for f in failed if f not in {p[1]["label"] for p in pairs}]
            else:
                state = await page.evaluate(_FORM_STATE_JS) or {}
                still_empty = state.get("empty") or []
                if still_empty:
                    for q in still_empty:
                        if q not in failed:
                            failed.append(q)
                # DOM shows values but React did not enable Next — not a per-field
                # fill failure; apply.py handles Next-disabled separately.

    await page.wait_for_timeout(150)
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

  function fixedFooters() {
    const out = [];
    for (const el of document.querySelectorAll("div, footer, section, nav")) {
      try {
        const s = window.getComputedStyle(el);
        if (s.position !== "fixed" && s.position !== "sticky") continue;
        const r = el.getBoundingClientRect();
        if (r.bottom >= window.innerHeight - 12 && r.height > 16 && r.width > 120) {
          out.push(el);
        }
      } catch (e) {}
    }
    return out;
  }

  function clickCandidates(root) {
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
    return null;
  }

  const roots = [screeningRoot(), document.body];
  for (const footer of fixedFooters()) {
    if (!roots.includes(footer)) roots.unshift(footer);
  }
  for (const root of roots) {
    if (!root) continue;
    const clicked = clickCandidates(root);
    if (clicked) return clicked;
  }
  return null;
}
"""
)


async def click_hirist_advance(page: Page) -> str | None:
    """Click Next / Submit on the Hirist screening form."""
    await _scroll_submission_into_view(page)
    state = await page.evaluate(_FORM_STATE_JS) or {}
    if state.get("nextDisabled") and state.get("empty"):
        return None
    if not await wait_for_hirist_next_enabled(page, timeout_ms=5000):
        return None

    next_btn = page.locator(".submission-btn button, button.black-round, button.a-button").filter(
        has_text=re.compile(r"^next$|^submit$|^confirm$", re.I)
    )
    if await next_btn.count() > 0:
        try:
            btn = next_btn.last
            if await btn.is_visible() and not await btn.is_disabled():
                await btn.scroll_into_view_if_needed()
                await btn.click(timeout=5000)
                await page.wait_for_timeout(350)
                return "next"
        except Exception:
            pass

    try:
        result = await page.evaluate(_ADVANCE_JS)
    except PlaywrightError as exc:
        logger.debug("Hirist advance JS failed: %s", exc)
        result = None
    if result:
        await page.wait_for_timeout(350)
        return str(result).lower()
    return None
