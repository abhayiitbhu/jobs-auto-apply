from __future__ import annotations

import logging
import re
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from ..page_load import ensure_page_ready
from .labels import (
    COVER_NOTE_HINT,
    is_generic_question_label,
    is_plausible_application_question,
    normalize_question_label,
)

logger = logging.getLogger("job_apply")


# Wellfound is a Next.js/React app: option inputs are often visually hidden
# (custom-styled), labels associate via `for`/`aria-labelledby`/wrapping label or
# a fieldset legend, and the apply form lives inside a modal. These shared helpers
# resolve all of that the same way for both discovery and fill so the two passes
# always agree on a question's label and control type.
_WF_DOM_HELPERS = r"""
  const COVER_NOTE = /note|message|cover/i;
  const GENERIC = /^(enter your answer|type here|your answer|answer|select\.{0,3}|choose\.{0,3}|please select)$/i;

  function clean(s) {
    return (s || "").replace(/\s+/g, " ").trim();
  }

  function visible(el) {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    if (style.visibility === "hidden") return false;
    // Custom radio/checkbox inputs are frequently `display:none` / 0-sized while a
    // styled sibling label is what the user clicks. Treat an input as usable when
    // it has an associated visible label even if the input box itself is hidden.
    const r = el.getBoundingClientRect();
    if (style.display !== "none" && r.width > 0 && r.height > 0) return true;
    return false;
  }

  function controlUsable(el) {
    if (visible(el)) return true;
    const lbl = labelEl(el);
    return !!(lbl && visible(lbl));
  }

  function applyRoot() {
    // Prefer the open apply modal. Wellfound flags an open modal on <body> with the
    // `Modal__open` class, and the dialog itself carries role="dialog".
    const dialogs = [...document.querySelectorAll('[role="dialog"], [aria-modal="true"]')]
      .filter((d) => visible(d));
    if (dialogs.length) return dialogs[dialogs.length - 1];
    if (document.body.className && /Modal__open/.test(document.body.className)) {
      const modal = document.querySelector('[class*="modal" i], [class*="Modal" i]');
      if (modal) return modal;
    }
    const form = document.querySelector('form');
    return form || document.body;
  }

  function labelEl(el) {
    if (el.id) {
      const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lbl) return lbl;
    }
    const wrap = el.closest("label");
    if (wrap) return wrap;
    return null;
  }

  function labelText(el) {
    // 1) explicit <label for> or wrapping <label>
    const lbl = labelEl(el);
    if (lbl) {
      const clone = lbl.cloneNode(true);
      clone.querySelectorAll("input, textarea, select, button").forEach((n) => n.remove());
      const t = clean(clone.innerText || clone.textContent);
      if (t) return t;
    }
    // 2) aria-labelledby -> referenced node text
    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const parts = labelledBy.split(/\s+/)
        .map((id) => document.getElementById(id))
        .filter(Boolean)
        .map((n) => clean(n.innerText || n.textContent))
        .filter(Boolean);
      if (parts.length) return parts.join(" ");
    }
    // 3) aria-label
    const aria = clean(el.getAttribute("aria-label"));
    if (aria) return aria;
    // 4) fieldset legend (radio / checkbox groups)
    const fs = el.closest("fieldset");
    if (fs) {
      const legend = fs.querySelector("legend");
      if (legend) {
        const t = clean(legend.innerText || legend.textContent);
        if (t) return t;
      }
    }
    // 5) nearest preceding question-like text within the form group
    const groupSel = '[class*="question" i], [class*="field" i], [class*="formGroup" i], [class*="form-group" i]';
    const group = el.closest(groupSel);
    if (group) {
      const heading = group.querySelector('label, legend, [class*="label" i], [class*="question" i], h2, h3, h4, p');
      if (heading && !heading.contains(el)) {
        const t = clean(heading.innerText || heading.textContent);
        if (t) return t;
      }
    }
    // 6) walk previous siblings / ancestors looking for the prompt text
    let node = el;
    for (let depth = 0; depth < 8 && node; depth++) {
      let sib = node.previousElementSibling;
      while (sib) {
        if (!sib.querySelector("input, textarea, select")) {
          const clone = sib.cloneNode(true);
          clone.querySelectorAll("button").forEach((n) => n.remove());
          const t = clean(clone.innerText || clone.textContent);
          if (t && t.length >= 3) return t;
        }
        sib = sib.previousElementSibling;
      }
      node = node.parentElement;
    }
    // 7) placeholder as a last resort
    return clean(el.getAttribute("placeholder"));
  }

  function optionLabel(el) {
    const lbl = labelEl(el);
    if (lbl) {
      const clone = lbl.cloneNode(true);
      clone.querySelectorAll("input, textarea, select, button").forEach((n) => n.remove());
      const t = clean(clone.innerText || clone.textContent);
      if (t) return t;
    }
    const aria = clean(el.getAttribute("aria-label"));
    if (aria) return aria;
    return clean(el.value);
  }

  function groupLabelForRadio(radio) {
    // Radios in a group share a label; derive it from the fieldset/group, not the
    // per-option label (which is "Yes"/"No"/etc.).
    const fs = radio.closest("fieldset");
    if (fs) {
      const legend = fs.querySelector("legend");
      if (legend) {
        const t = clean(legend.innerText || legend.textContent);
        if (t) return t;
      }
    }
    const groupSel = '[class*="question" i], [class*="field" i], [class*="formGroup" i], [class*="form-group" i], [role="radiogroup"]';
    const group = radio.closest(groupSel);
    if (group) {
      const heading = group.querySelector('label:not([for]), legend, [class*="label" i], [class*="question" i], h2, h3, h4, p');
      if (heading) {
        const clone = heading.cloneNode(true);
        clone.querySelectorAll("input, textarea, select, button").forEach((n) => n.remove());
        const t = clean(clone.innerText || clone.textContent);
        if (t && !/^(yes|no)$/i.test(t)) return t;
      }
    }
    // Fall back to the generic prompt-finder, skipping the option's own label.
    return labelText(radio);
  }

  function radioGroupId(radio) {
    if (radio.name) return "name:" + radio.name;
    const fs = radio.closest("fieldset");
    if (fs && fs.id) return "fs:" + fs.id;
    const label = groupLabelForRadio(radio);
    if (label) return "label:" + label.toLowerCase();
    return "id:" + (radio.id || "");
  }

  function labelsMatch(a, b) {
    const x = clean(a).toLowerCase().replace(/[*?:]+$/, "").trim();
    const y = clean(b).toLowerCase().replace(/[*?:]+$/, "").trim();
    if (!x || !y) return false;
    return x === y || x.includes(y) || y.includes(x);
  }

  function setNativeValue(el, value) {
    const proto = el.tagName === "TEXTAREA"
      ? window.HTMLTextAreaElement.prototype
      : window.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) setter.call(el, value);
    else el.value = value;
    el.dispatchEvent(new InputEvent("input", {
      bubbles: true, cancelable: true, inputType: "insertText", data: String(value),
    }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.dispatchEvent(new Event("blur", { bubbles: true }));
  }

  function clickOption(input) {
    const lbl = labelEl(input);
    const target = (lbl && visible(lbl)) ? lbl : input;
    target.click();
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }
"""


_DISCOVER_JS = (
    _WF_DOM_HELPERS
    + r"""
() => {
  const root = applyRoot();
  if (!root) return [];
  const results = [];

  // Free-text-ish inputs and textareas.
  const TEXT_INPUT = 'input[type="text"], input[type="email"], input[type="tel"], '
    + 'input[type="url"], input[type="number"], input[type="date"], input[type="search"], '
    + 'input:not([type])';
  for (const el of root.querySelectorAll("textarea, " + TEXT_INPUT)) {
    if (!controlUsable(el)) continue;
    const tag = el.tagName.toLowerCase();
    const inputType = tag === "textarea" ? "textarea" : (el.getAttribute("type") || "text").toLowerCase();
    const label = labelText(el);
    const placeholder = clean(el.getAttribute("placeholder"));
    const resolved = label || placeholder;
    if (!resolved) continue;
    if (COVER_NOTE.test(label + " " + placeholder)) continue;
    // A label read from an explicit <label>/aria/placeholder is authoritative; one
    // derived by walking the DOM is weak and must clear the plausibility filter.
    const strong = !!(labelEl(el) || el.getAttribute("aria-label")
      || el.getAttribute("aria-labelledby") || placeholder);
    results.push({
      kind: tag === "textarea" ? "textarea" : (inputType === "number" ? "number" : "input"),
      label: resolved,
      placeholder,
      input_mode: inputType,
      strong_label: strong,
      required: el.required || el.getAttribute("aria-required") === "true",
    });
  }

  // Native <select>.
  for (const el of root.querySelectorAll("select")) {
    if (!controlUsable(el)) continue;
    const label = labelText(el);
    const options = [...el.querySelectorAll("option")]
      .map((o) => clean(o.innerText || o.textContent))
      .filter((t) => t && !/^(select|choose|please select)\.{0,3}$/i.test(t));
    results.push({
      kind: "select",
      label: label || "",
      options,
      required: el.required || el.getAttribute("aria-required") === "true",
    });
  }

  // Radio groups (group by name / fieldset / shared prompt).
  const radiosByGroup = {};
  for (const el of root.querySelectorAll('input[type="radio"]')) {
    if (!controlUsable(el)) continue;
    const gid = radioGroupId(el);
    (radiosByGroup[gid] ||= []).push(el);
  }
  for (const radios of Object.values(radiosByGroup)) {
    const label = groupLabelForRadio(radios[0]);
    if (!label) continue;
    const options = [...new Set(radios.map(optionLabel).filter((t) => t && !GENERIC.test(t)))];
    results.push({
      kind: "radio",
      label,
      name: radios[0].name || "",
      options,
      required: radios.some((r) => r.required || r.getAttribute("aria-required") === "true"),
    });
  }

  // ARIA radiogroups (no native <input type=radio>).
  for (const grp of root.querySelectorAll('[role="radiogroup"]')) {
    if (!controlUsable(grp)) continue;
    const radios = [...grp.querySelectorAll('[role="radio"]')];
    if (!radios.length) continue;
    const label = labelText(grp);
    if (!label) continue;
    const options = [...new Set(radios.map((r) => clean(r.getAttribute("aria-label") || r.innerText)).filter(Boolean))];
    results.push({ kind: "radio", label, name: "", options, aria: true });
  }

  // Checkboxes: a group sharing a prompt becomes checkbox_group, otherwise a single checkbox.
  const checksByGroup = {};
  for (const el of root.querySelectorAll('input[type="checkbox"]')) {
    if (!controlUsable(el)) continue;
    const fs = el.closest("fieldset");
    const groupSel = 'fieldset, [class*="question" i], [class*="field" i], [role="group"]';
    const group = el.closest(groupSel);
    const gid = fs && fs.id ? "fs:" + fs.id
      : (group ? "grp:" + (group.getAttribute("data-test") || clean(labelText(el)).toLowerCase()) : "solo:" + (el.id || el.name || Math.random()));
    (checksByGroup[gid] ||= []).push(el);
  }
  for (const boxes of Object.values(checksByGroup)) {
    if (boxes.length > 1) {
      const label = labelText(boxes[0].closest("fieldset, [class*='question' i], [role='group']") || boxes[0]);
      const options = [...new Set(boxes.map(optionLabel).filter(Boolean))];
      if (label) {
        results.push({ kind: "checkbox_group", label, name: boxes[0].name || "", options });
        continue;
      }
    }
    const box = boxes[0];
    const label = optionLabel(box) || labelText(box);
    if (label) results.push({ kind: "checkbox", label, required: box.required });
  }

  return results;
}
"""
)


_FILL_JS = (
    _WF_DOM_HELPERS
    + r"""
(pairs) => {
  const root = applyRoot();
  const results = [];
  if (!root) return pairs.map((p) => ({ label: p.label, filled: false }));

  function wantsChecked(answer) {
    return /^(yes|true|1|checked|agree|accept|i agree|on)$/i.test(clean(answer));
  }

  function optionMatches(want, opt) {
    const w = clean(want).toLowerCase();
    const o = clean(opt).toLowerCase();
    if (!o) return false;
    if (o === w || o.includes(w) || w.includes(o)) return true;
    if (o === "yes") return /\byes\b/i.test(want) && !/\bno\b/i.test(want);
    if (o === "no") return /\bno\b/i.test(want);
    return false;
  }

  for (const { label, answer, kind } of pairs) {
    let filled = false;
    const want = clean(answer);

    if (kind === "select") {
      for (const sel of root.querySelectorAll("select")) {
        if (!labelsMatch(labelText(sel), label)) continue;
        const opts = [...sel.querySelectorAll("option")];
        const match = opts.find((o) => optionMatches(want, clean(o.innerText || o.textContent)));
        if (match) {
          sel.value = match.value;
          sel.dispatchEvent(new Event("input", { bubbles: true }));
          sel.dispatchEvent(new Event("change", { bubbles: true }));
          filled = true;
        }
        break;
      }
    } else if (kind === "radio") {
      const radios = [...root.querySelectorAll('input[type="radio"]')].filter(controlUsable);
      const byGroup = {};
      for (const r of radios) (byGroup[radioGroupId(r)] ||= []).push(r);
      for (const group of Object.values(byGroup)) {
        if (!labelsMatch(groupLabelForRadio(group[0]), label)) continue;
        for (const r of group) {
          if (optionMatches(want, optionLabel(r))) { clickOption(r); filled = true; break; }
        }
        if (filled) break;
      }
      if (!filled) {
        for (const grp of root.querySelectorAll('[role="radiogroup"]')) {
          if (!labelsMatch(labelText(grp), label)) continue;
          for (const r of grp.querySelectorAll('[role="radio"]')) {
            const opt = clean(r.getAttribute("aria-label") || r.innerText);
            if (optionMatches(want, opt)) { r.click(); filled = true; break; }
          }
          if (filled) break;
        }
      }
    } else if (kind === "checkbox" || kind === "checkbox_group") {
      const boxes = [...root.querySelectorAll('input[type="checkbox"]')].filter(controlUsable);
      if (kind === "checkbox_group" && /[,;|]/.test(want)) {
        const wants = want.toLowerCase().split(/[,;|]/).map((s) => s.trim()).filter(Boolean);
        for (const box of boxes) {
          const opt = optionLabel(box);
          if (wants.some((w) => optionMatches(w, opt)) && !box.checked) { clickOption(box); filled = true; }
        }
      } else {
        const shouldCheck = wantsChecked(answer);
        for (const box of boxes) {
          const q = optionLabel(box) || labelText(box);
          if (!labelsMatch(q, label)) continue;
          if (shouldCheck !== box.checked) clickOption(box);
          filled = true;
          break;
        }
      }
    } else {
      const fields = [...root.querySelectorAll("textarea, input")].filter(controlUsable);
      for (const el of fields) {
        const lt = el.tagName.toLowerCase();
        if (lt === "input" && /radio|checkbox|submit|button|file/i.test(el.type || "")) continue;
        if (!labelsMatch(labelText(el) || el.getAttribute("placeholder"), label)) continue;
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


async def _apply_container(page: Page):
    """Return the open apply modal locator, falling back to the page body."""
    dialog = page.locator('[role="dialog"]:visible, [aria-modal="true"]:visible')
    if await dialog.count() > 0:
        return dialog.last
    dialog = page.locator('[role="dialog"]')
    if await dialog.count() > 0:
        return dialog.last
    return page.locator("body")


def _accept_question_label(label: str) -> bool:
    """Keep real questions/field prompts; drop cover-note and chrome/noise labels."""
    norm = normalize_question_label(label)
    if is_generic_question_label(norm):
        return False
    if COVER_NOTE_HINT.search(norm):
        return False
    return is_plausible_application_question(norm)


def _normalize_discovered(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw or []):
        label = normalize_question_label(str(item.get("label", "")))
        kind = str(item.get("kind", "input"))
        options = [str(o).strip() for o in (item.get("options") or []) if str(o).strip()]
        # Choice fields with real options, and text fields whose label came from an
        # explicit <label>/aria/placeholder, are authoritative even if the wording
        # fails the prose plausibility heuristic. Only weak (DOM-derived) labels are
        # gated through the question-plausibility filter to avoid scraping chrome.
        is_choice = kind in ("radio", "checkbox", "checkbox_group", "select")
        authoritative = is_choice or bool(item.get("strong_label"))
        if not label:
            continue
        if is_generic_question_label(label) or COVER_NOTE_HINT.search(label):
            continue
        if not authoritative and not _accept_question_label(label):
            continue
        if label in seen:
            continue
        seen.add(label)
        field: dict[str, Any] = {"kind": kind, "label": label, "index": index, "platform": "wellfound"}
        placeholder = str(item.get("placeholder") or "").strip()
        if placeholder:
            field["placeholder"] = placeholder
        input_mode = str(item.get("input_mode") or "").strip()
        if input_mode:
            field["input_mode"] = input_mode
        if item.get("name"):
            field["name"] = str(item.get("name"))
        if options:
            field["options"] = options
        if item.get("required"):
            field["required"] = True
        fields.append(field)
    return fields


async def discover_questions(page: Page) -> list[dict[str, Any]]:
    """Find mandatory application fields (beyond the cover-note textarea).

    Covers text inputs, textareas, native selects, radio groups, ARIA radiogroups
    and checkboxes/checkbox-groups inside the open apply modal, capturing options,
    placeholder, input mode and required-ness for accurate downstream answering.
    """
    await ensure_page_ready(page, for_form=True)
    try:
        raw = await page.evaluate(_DISCOVER_JS)
    except PlaywrightError as exc:
        logger.debug("Wellfound JS discovery failed (%s); falling back to locator scan", exc)
        raw = await _discover_questions_fallback(page)
    fields = _normalize_discovered(raw)
    if fields:
        for f in fields:
            opts = f.get("options") or []
            preview = ", ".join(opts[:4]) + (f" (+{len(opts) - 4} more)" if len(opts) > 4 else "")
            logger.info(
                "Wellfound question [%s] %s%s",
                f.get("kind", "input"),
                str(f.get("label", ""))[:70],
                f" — options: {preview}" if opts else "",
            )
    return fields


async def _discover_questions_fallback(page: Page) -> list[dict[str, Any]]:
    """Locator-based discovery if the JS scan cannot run (e.g. CSP)."""
    container = await _apply_container(page)
    out: list[dict[str, Any]] = []

    textareas = container.locator("textarea:visible")
    for i in range(await textareas.count()):
        el = textareas.nth(i)
        label = await _label_for(page, el)
        placeholder = (await el.get_attribute("placeholder")) or ""
        if COVER_NOTE_HINT.search(label + placeholder):
            continue
        out.append({"kind": "textarea", "label": label or placeholder, "placeholder": placeholder})

    inputs = container.locator('input[type="text"]:visible, input:not([type]):visible')
    for i in range(await inputs.count()):
        el = inputs.nth(i)
        label = await _label_for(page, el)
        placeholder = (await el.get_attribute("placeholder")) or ""
        if not label and not placeholder:
            continue
        out.append({"kind": "input", "label": label or placeholder, "placeholder": placeholder})

    selects = container.locator("select:visible")
    for i in range(await selects.count()):
        el = selects.nth(i)
        label = await _label_for(page, el)
        out.append({"kind": "select", "label": label or f"Question {i + 1}"})

    return out


async def _label_for(page: Page, el) -> str:
    el_id = await el.get_attribute("id")
    if el_id:
        label = page.locator(f'label[for="{el_id}"]')
        if await label.count() > 0:
            return (await label.first.inner_text()).strip()
    aria = await el.get_attribute("aria-label")
    return (aria or "").strip()


async def fill_questions(page: Page, answers: dict[str, str]) -> None:
    """Fill resolved answers into the apply modal across all control types."""
    await ensure_page_ready(page, for_form=True)
    pairs = [{"label": question, "answer": answer, "kind": ""} for question, answer in answers.items() if answer]
    if not pairs:
        return

    # Tag each answer with the live control kind so the JS filler picks the right
    # strategy (select/radio/checkbox/text) even though resolve() only returns text.
    try:
        discovered = await page.evaluate(_DISCOVER_JS)
    except PlaywrightError:
        discovered = []
    kind_by_label = {
        normalize_question_label(str(d.get("label", ""))).lower(): str(d.get("kind", ""))
        for d in (discovered or [])
        if d.get("label")
    }
    for pair in pairs:
        key = normalize_question_label(pair["label"]).lower()
        pair["kind"] = kind_by_label.get(key, "")
        if not pair["kind"]:
            for dlabel, dkind in kind_by_label.items():
                if dlabel and (dlabel in key or key in dlabel):
                    pair["kind"] = dkind
                    break

    try:
        rows = await page.evaluate(_FILL_JS, pairs)
    except PlaywrightError as exc:
        logger.debug("Wellfound JS fill failed (%s); using locator fallback", exc)
        rows = []

    filled = {r["label"] for r in (rows or []) if r.get("filled")}
    unfilled = [p for p in pairs if p["label"] not in filled]
    if unfilled:
        await _fill_questions_fallback(page, {p["label"]: p["answer"] for p in unfilled})


async def _fill_questions_fallback(page: Page, answers: dict[str, str]) -> None:
    """Locator-based text fill (matches the previous behaviour) as a safety net."""
    container = await _apply_container(page)
    for question, answer in answers.items():
        if not answer:
            continue
        label = container.locator("label").filter(has_text=re.compile(re.escape(question[:40]), re.I))
        if await label.count() > 0:
            for_id = await label.first.get_attribute("for")
            if for_id:
                target = container.locator(f"#{for_id}")
                if await target.count() > 0:
                    kind = await _control_kind(target.first)
                    if kind in ("text", "textarea"):
                        await target.first.fill(answer)
                        continue
        field = container.locator(
            f'textarea[placeholder*="{question[:20]}" i], input[placeholder*="{question[:20]}" i]'
        )
        if await field.count() > 0:
            await field.first.fill(answer)


async def _control_kind(locator) -> str:
    try:
        tag = await locator.evaluate("el => el.tagName.toLowerCase()")
        if tag == "textarea":
            return "textarea"
        if tag == "select":
            return "select"
        input_type = (await locator.get_attribute("type")) or "text"
        if input_type in ("radio", "checkbox"):
            return input_type
        return "text"
    except PlaywrightError:
        return "text"
