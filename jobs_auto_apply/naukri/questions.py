from __future__ import annotations

import logging
import re
from typing import Any

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from ..application_questions import is_generic_question_label, is_plausible_application_question, resolve_fill_answer

logger = logging.getLogger("job_apply")

_SKIP_CHIP = re.compile(r"skip this question", re.I)

_OPTIONAL_TEXT_FIELD = re.compile(
    r"\b(middle name|first name|last name|maiden name|nick\s*name|nickname)\b",
    re.I,
)

_SKIP_ANSWERS = frozenset({"", "skip", "n/a", "na", "none", "-", "not applicable", "no"})

_YES_NO_QUESTION = re.compile(
    r"\b(are you|do you|will you|can you)\b.{0,60}\b(residing|relocate|willing|located)\b",
    re.I,
)

_CITY_SELECT_QUESTION = re.compile(
    r"\bselect\b.{0,30}\b(city|cities)\b|\b(city|cities)\b.{0,30}\b(residing|relocate)\b",
    re.I,
)

_NOTICE_PERIOD_QUESTION = re.compile(r"\bnotice\s*period\b", re.I)

_DATE_FIELD = re.compile(r"\b(date\s*of\s*birth|dob|d\.o\.b|birth\s*date)\b", re.I)

_CITY_ALIASES = {
    "bangalore": "bengaluru",
    "bengaluru": "bengaluru",
    "hyderabad": "hyderabad",
    "pune": "pune",
    "mumbai": "mumbai",
    "chennai": "chennai",
    "delhi": "delhi",
    "delhi ncr": "delhi",
    "gurgaon": "gurugram",
    "gurugram": "gurugram",
    "noida": "noida",
    "ncr": "delhi",
}

_CITY_OPTION = re.compile(
    r"\b(gurugram|gurgaon|noida|bengaluru|bangalore|hyderabad|pune|mumbai|chennai|delhi|"
    r"haryana|pradesh|karnataka|maharashtra|tamil|telangana)\b",
    re.I,
)

_CHATBOT_SCOPE = "#desktopChatBotContainer, ._chatBotContainer, .chatbot_Drawer"

_CHIP_SELECTOR = (
    "#desktopChatBotContainer .chatbot_Chip, "
    "#desktopChatBotContainer .chipItem, "
    ".chipsContainer .chatbot_Chip, "
    ".chipsContainer .chipItem, "
    ".chatbot_Drawer .chatbot_Chip, "
    ".chatbot_Drawer .chipItem, "
    "[id^='chips_container'] .chatbot_Chip, "
    "[id^='chips_container'] .chipItem"
)

_CHATBOT_HELPERS_JS = """
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();

  function chatbotScope() {
    const roots = [
      document.querySelector('#desktopChatBotContainer'),
      document.querySelector('._chatBotContainer'),
      document.querySelector('.chatbot_Drawer.chatbot_right'),
      document.querySelector('.chatbot_Drawer'),
    ].filter(Boolean);
    if (!roots.length) return null;
    const root = roots[0];
    return root.classList?.contains('chatbot_Drawer')
      ? root
      : (root.querySelector('.chatbot_Drawer') || root);
  }

  function cleanLabel(text) {
    return (text || '').replace(/\\s+/g, ' ').trim().replace(/\\s*\\(\\d[\\d,]*\\)\\s*$/, '');
  }

  function checkboxLabel(box) {
    if (!box) return '';
    if (box.id) {
      const lbl = document.querySelector(`label[for="${CSS.escape(box.id)}"]`);
      if (lbl) {
        const truncated = lbl.querySelector('.truncate');
        if (truncated) return cleanLabel(truncated.innerText);
        return cleanLabel(lbl.innerText);
      }
    }
    const wrap = box.closest('label');
    if (wrap) {
      const truncated = wrap.querySelector('.truncate');
      if (truncated) return cleanLabel(truncated.innerText);
      return cleanLabel(wrap.innerText);
    }
    return cleanLabel(box.value);
  }

  function clickCheckbox(box, shouldCheck) {
    if (!box) return false;
    const lbl = box.id ? document.querySelector(`label[for="${CSS.escape(box.id)}"]`) : null;
    const target = lbl || box.closest('label') || box;
    if (shouldCheck && !box.checked) {
      target.click();
      box.checked = true;
      box.dispatchEvent(new Event('change', { bubbles: true }));
      return true;
    }
    if (!shouldCheck && box.checked) {
      target.click();
      box.checked = false;
      box.dispatchEvent(new Event('change', { bubbles: true }));
      return true;
    }
    return shouldCheck === box.checked;
  }

  function isVisible(el) {
    if (!el) return false;
    let node = el;
    for (let i = 0; i < 10; i++) {
      if (!node) break;
      const style = window.getComputedStyle(node);
      if (style.display === 'none' || style.visibility === 'hidden') return false;
      if (node.classList?.contains('d-none')) return false;
      node = node.parentElement;
    }
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }

  function visibleTextInput(scope) {
    const skipTypes = new Set(['radio', 'checkbox', 'hidden', 'submit', 'button', 'file']);
    const selectors = [
      'div.textArea[contenteditable]',
      '[contenteditable="true"]',
      '[contenteditable=""]',
      'textarea',
      'input[type="text"]',
      'input[type="date"]',
      'input[type="tel"]',
      'input[type="number"]',
      'input:not([type])',
    ];
    for (const root of discoverRoots(scope)) {
      for (const sel of selectors) {
        for (const input of root.querySelectorAll(sel)) {
          if (!isVisible(input)) continue;
          if (input.tagName === 'INPUT' && skipTypes.has((input.type || '').toLowerCase())) continue;
          return input;
        }
      }
    }
    return null;
  }

  function setTextInputValue(input, answer) {
    if (!input || answer == null) return false;
    input.focus();
    const text = String(answer);
    if (input.tagName === 'INPUT' || input.tagName === 'TEXTAREA') {
      input.value = text;
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
      return true;
    }
    input.textContent = text;
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: text }));
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
  }

  function clickSendButton(scope) {
    const deadline = Date.now() + 5000;
    while (Date.now() < deadline) {
      const wrap = scope.querySelector('[id^="sendMsg"], .sendMsgbtn_container .send');
      if (wrap && !wrap.classList.contains('disabled')) break;
    }
    for (const root of discoverRoots(scope)) {
      const save = root.querySelector(
        '.sendMsgbtn_container .sendMsg:not(.disabled), .sendMsgbtn_container .send:not(.disabled), .sendMsg:not(.disabled), button.sendMsg:not([disabled])'
      );
      if (save) {
        save.click();
        return true;
      }
    }
    return false;
  }

  function fillTextInput(scope, answer) {
    const input = visibleTextInput(scope);
    if (!input) return { filled: false, reason: 'no-input' };
    if (!setTextInputValue(input, answer)) return { filled: false, reason: 'set-failed' };
    if (!clickSendButton(scope)) return { filled: false, reason: 'no-save' };
    return { filled: true, method: 'text' };
  }

  function hasChoiceUI(scope) {
    const roots = discoverRoots(scope);
    for (const root of roots) {
      if (root.querySelector(
        '.singleselect-radiobutton input[type="radio"], .ssrc__radio, .chatbot_Chip, .chipItem, input[type="radio"], input[type="checkbox"], [role="radio"], [role="checkbox"]'
      )) {
        return true;
      }
    }
    return false;
  }

  function radioLabel(radio) {
    if (!radio) return '';
    if (radio.id) {
      const lbl = document.querySelector(
        `label.ssrc__label[for="${CSS.escape(radio.id)}"], label[for="${CSS.escape(radio.id)}"]`
      );
      if (lbl) return cleanLabel(lbl.innerText);
    }
    const wrap = radio.closest('label');
    if (wrap) return cleanLabel(wrap.innerText);
    return cleanLabel(radio.value);
  }

  function clickRadio(radio) {
    const lbl = radio.id
      ? document.querySelector(
          `label.ssrc__label[for="${CSS.escape(radio.id)}"], label[for="${CSS.escape(radio.id)}"]`
        )
      : null;
    const target = lbl || radio.closest('label') || radio;
    target.click();
    radio.checked = true;
    radio.dispatchEvent(new Event('input', { bubbles: true }));
    radio.dispatchEvent(new Event('change', { bubbles: true }));
    return radioLabel(radio);
  }

  function discoverRoots(scope) {
    const roots = [scope];
    const overlay = document.querySelector('.chatbot_Overlay.show');
    if (overlay && !roots.includes(overlay)) roots.push(overlay);
    const container = document.querySelector('#desktopChatBotContainer, ._chatBotContainer');
    if (container && !roots.includes(container)) roots.push(container);
    return roots;
  }

  function hasMeaningfulChoiceUI(scope) {
    const opts = discoverAnswerOptions(scope, '');
    if (opts.some((o) => !/skip this question/i.test(o.text || ''))) return true;
    for (const root of discoverRoots(scope)) {
      if (root.querySelector('.singleselect-radiobutton input[type="radio"], .ssrc__radio')) {
        return true;
      }
      for (const box of root.querySelectorAll('input[type="checkbox"], [role="checkbox"]')) {
        if (isVisible(box)) return true;
      }
      for (const radio of root.querySelectorAll('input[type="radio"], [role="radio"]')) {
        if (isVisible(radio)) return true;
      }
    }
    return false;
  }

  function valueInRange(value, option) {
    const opt = norm(option);
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

  function optionMatches(label, answer) {
    const c = norm(label);
    const want = norm(answer);
    if (!c || /skip this question/i.test(c)) return false;
    if (c === want || c.includes(want) || want.includes(c)) return true;
    if (c === 'yes' && /\\byes\\b/i.test(answer) && !/\\bno\\b/i.test(answer)) return true;
    if (c === 'no' && /\\bno\\b/i.test(answer)) return true;
    const wantNum = String(answer).match(/(\\d+)/);
    if (wantNum && valueInRange(parseInt(wantNum[1], 10), label)) return true;
    return false;
  }

  function cityToken(text) {
    const t = norm(text).split(',')[0].trim();
    if (t === 'gurgaon' || t === 'gurugram') return 'gurugram';
    if (t === 'bangalore' || t === 'bengaluru') return 'bengaluru';
    return t;
  }

  function cityOptionMatches(label, answer) {
    if (optionMatches(label, answer)) return true;
    const want = cityToken(answer);
    const opt = cityToken(label);
    if (!want || !opt) return false;
    if (opt === want || opt.includes(want) || want.includes(opt)) return true;
    return false;
  }

  function checkboxUsable(box) {
    if (!box || box.type !== 'checkbox') return false;
    const label = checkboxLabel(box);
    if (label && !/skip this question/i.test(label)) return true;
    return isVisible(box);
  }

  function looksLikeCityOption(text) {
    const t = cleanLabel(text);
    if (!t || /skip this question/i.test(t)) return false;
    if (/^(yes|no)$/i.test(t)) return false;
    return /,/.test(t) || /\\b(gurugram|gurgaon|noida|bengaluru|bangalore|hyderabad|pune|mumbai|chennai|delhi|haryana|pradesh)\\b/i.test(t);
  }

  function plausibleOption(text, questionText) {
    const t = cleanLabel(text);
    if (!t || /skip this question/i.test(t)) return false;
    if (t.length > 80) return false;
    const q = norm(questionText || '');
    if (q && norm(t) === q) return false;
    if (q && t.includes('?') && t.length > 40) return false;
    return true;
  }

  function discoverAnswerOptions(scope, questionText) {
    const options = [];
    const seen = new Set();
    function add(text, kind) {
      const t = cleanLabel(text);
      if (!t || seen.has(t) || !plausibleOption(t, questionText)) return;
      seen.add(t);
      options.push({ text: t, kind });
    }

    const roots = discoverRoots(scope);
    const cityQuestion = /\\bselect\\b.{0,30}\\b(city|cities)\\b|\\b(city|cities)\\b.{0,30}\\b(residing|relocate)\\b/i.test(questionText || '');

    for (const root of roots) {
      for (const box of root.querySelectorAll('input[type="checkbox"]')) {
        if (!checkboxUsable(box)) continue;
        add(checkboxLabel(box), 'checkbox');
      }

      for (const el of root.querySelectorAll('[role="checkbox"]')) {
        const label = cleanLabel(el.getAttribute('aria-label') || el.innerText);
        if (!label || /skip this question/i.test(label)) continue;
        add(label, 'role-checkbox');
      }

      for (const lbl of root.querySelectorAll('label.ssrc__label, label:has(.truncate)')) {
        const text = cleanLabel(lbl.innerText);
        if (!text || !looksLikeCityOption(text)) continue;
        add(text, 'checkbox');
      }

      for (const lbl of root.querySelectorAll('.singleselect-radiobutton .ssrc__label, .singleselect-radiobutton label')) {
        if (!isVisible(lbl)) continue;
        add(lbl.innerText, 'singleselect-radio');
      }

      for (const radio of root.querySelectorAll(
        '.singleselect-radiobutton input[type="radio"], .ssrc__radio, input[type="radio"]'
      )) {
        if (!isVisible(radio) && !radio.closest('.singleselect-radiobutton')) continue;
        add(radioLabel(radio), 'radio');
      }

      for (const el of root.querySelectorAll('[role="radio"]')) {
        if (!isVisible(el)) continue;
        add(el.getAttribute('aria-label') || el.innerText, 'role-radio');
      }

      const chipEls = root.querySelectorAll('.chatbot_Chip, .chipItem');
      for (const el of chipEls) {
        if (!isVisible(el)) continue;
        const text = cleanLabel(el.innerText);
        const kind = (cityQuestion && looksLikeCityOption(text)) ? 'checkbox' : 'chip';
        add(text, kind);
      }

      for (const el of root.querySelectorAll('.chipsContainer .chatbot_Chip, .chipsContainer .chipItem, .chipMsg .chatbot_Chip, .chipMsg .chipItem')) {
        if (!isVisible(el)) continue;
        const text = cleanLabel(el.innerText);
        const kind = (cityQuestion && looksLikeCityOption(text)) ? 'checkbox' : 'chip';
        add(text, kind);
      }

      for (const el of root.querySelectorAll('.footerInputBoxWrapper .chatbot_Chip, .footerInputBoxWrapper .chipItem')) {
        if (!isVisible(el)) continue;
        const text = cleanLabel(el.innerText);
        const kind = (cityQuestion && looksLikeCityOption(text)) ? 'checkbox' : 'chip';
        add(text, kind);
      }

      for (const el of root.querySelectorAll('.chatbot_InputContainer .chatbot_Chip, .chatbot_InputContainer .chipItem')) {
        if (!isVisible(el)) continue;
        const text = cleanLabel(el.innerText);
        const kind = (cityQuestion && looksLikeCityOption(text)) ? 'checkbox' : 'chip';
        add(text, kind);
      }

      for (const el of root.querySelectorAll('.footerInputBoxWrapper span, .chatbot_InputContainer span')) {
        if (!isVisible(el) || el.children.length > 0) continue;
        const t = cleanLabel(el.innerText);
        if (/^(yes|no)$/i.test(t)) add(t, 'text-option');
      }
    }

    return options;
  }

  function submitChatbotChoice(scope) {
    const roots = discoverRoots(scope);
    const patterns = [/^(submit|save|continue|next|done|confirm)$/i];
    for (const root of roots) {
      for (const btn of root.querySelectorAll(
        'button, input[type="submit"], input[type="button"], [role="button"], .sendMsg, .send'
      )) {
        const text = norm(btn.innerText || btn.value || btn.getAttribute('aria-label'));
        if (!text || btn.disabled) continue;
        if (!patterns.some((p) => p.test(text))) continue;
        btn.click();
        return true;
      }
      const save = root.querySelector(
        '.sendMsgbtn_container .sendMsg:not(.disabled), .sendMsgbtn_container .send:not(.disabled)'
      );
      if (save) {
        save.click();
        return true;
      }
    }
    return false;
  }

  function clickCheckboxAnswers(scope, answers) {
    const wants = (answers || []).map((a) => norm(a)).filter(Boolean);
    if (!wants.length) return { filled: false, reason: 'no-answers' };

    function shouldPick(label) {
      return wants.some((want) => cityOptionMatches(label, want) || optionMatches(label, want));
    }

    let clicked = 0;
    const roots = discoverRoots(scope);
    for (const root of roots) {
      for (const box of root.querySelectorAll('input[type="checkbox"]')) {
        const label = checkboxLabel(box);
        if (!label || /skip this question/i.test(label)) continue;
        if (!shouldPick(label)) continue;
        if (clickCheckbox(box, true)) clicked++;
      }
      for (const el of root.querySelectorAll('[role="checkbox"]')) {
        const label = cleanLabel(el.getAttribute('aria-label') || el.innerText);
        if (!label || /skip this question/i.test(label)) continue;
        if (!shouldPick(label)) continue;
        if (el.getAttribute('aria-checked') !== 'true') el.click();
        clicked++;
      }
      for (const lbl of root.querySelectorAll('label.ssrc__label, label:has(.truncate)')) {
        const label = cleanLabel(lbl.innerText);
        if (!label || /skip this question/i.test(label)) continue;
        if (!shouldPick(label)) continue;
        lbl.click();
        clicked++;
      }
      for (const chip of root.querySelectorAll('.chatbot_Chip, .chipItem')) {
        const label = cleanLabel(chip.innerText);
        if (!label || /skip this question/i.test(label)) continue;
        if (!shouldPick(label)) continue;
        chip.click();
        clicked++;
      }
    }
    if (!clicked) return { filled: false, reason: 'no-checkbox-match' };
    submitChatbotChoice(scope);
    return { filled: true, method: 'checkbox', label: answers.join(', '), count: clicked };
  }

  function clickAnswerOption(scope, answer) {
    const roots = discoverRoots(scope);

    for (const root of roots) {
      for (const chip of [...root.querySelectorAll('.chatbot_Chip, .chipItem')]) {
        const label = cleanLabel(chip.innerText);
        if (!optionMatches(label, answer)) continue;
        chip.click();
        return { filled: true, method: 'chip', label, autoSubmit: true };
      }
    }

    for (const root of roots) {
      for (const radio of root.querySelectorAll(
        '.singleselect-radiobutton input[type="radio"], .ssrc__radio, input[type="radio"]'
      )) {
        const label = radioLabel(radio);
        if (!optionMatches(label, answer)) continue;
        const picked = clickRadio(radio);
        // Naukri singleselect radios need an explicit Save click to advance.
        return { filled: true, method: 'radio', label: picked || label, autoSubmit: true };
      }
    }

    for (const root of roots) {
      for (const el of root.querySelectorAll('[role="radio"]')) {
        const label = cleanLabel(el.getAttribute('aria-label') || el.innerText);
        if (!optionMatches(label, answer)) continue;
        el.click();
        return { filled: true, method: 'radio', label, autoSubmit: true };
      }
    }

    for (const root of roots) {
      for (const el of root.querySelectorAll(
        '.chatbot_InputContainer *, .footerInputBoxWrapper *, .chipsContainer *, .chipMsg *'
      )) {
        const label = cleanLabel(el.innerText);
        if (!/^(yes|no)$/i.test(label) || el.children.length > 0) continue;
        if (!optionMatches(label, answer)) continue;
        el.click();
        return { filled: true, method: 'option', label, autoSubmit: true };
      }
    }

    return null;
  }
"""

_CHATBOT_READY_JS = (
    _CHATBOT_HELPERS_JS
    + """
() => {
  const scope = chatbotScope();
  if (!scope) return false;

  const msgs = [...scope.querySelectorAll('.botItem .botMsg, .botMsg')];
  for (const msg of msgs) {
    const text = (msg.innerText || '').replace(/\\s+/g, ' ').trim();
    if (text.length > 3) return true;
  }
  if (scope.querySelector('.singleselect-radiobutton, .chatbot_Chip, .chipItem, input[type="radio"], [role="radio"], input[type="checkbox"], [role="checkbox"]')) return true;
  if (visibleTextInput(scope)) return true;
  return false;
}
"""
)

_CHATBOT_OPEN_JS = """
() => {
  if (document.querySelector('.chatbot_Overlay.show')) return true;
  const drawer = document.querySelector('.chatbot_Drawer.chatbot_right, .chatbot_Drawer');
  if (drawer && drawer.querySelector('.botItem, .chatbot_DrawerContentWrapper')) return true;
  const container = document.querySelector('#desktopChatBotContainer, ._chatBotContainer');
  if (!container) return false;
  return !!container.querySelector('.botItem, .botMsg, .chatbot_Chip, .chipItem, input[type="radio"], [role="radio"], input[type="checkbox"], [role="checkbox"], div.textArea[contenteditable="true"]');
}
"""

_DISCOVER_JS = (
    _CHATBOT_HELPERS_JS
    + """
() => {
  const scope = chatbotScope();
  if (!scope) return null;

  const botMsgs = [...scope.querySelectorAll('.botItem .botMsg, .botMsg')];
  let question = '';
  for (let i = botMsgs.length - 1; i >= 0; i--) {
    const text = (botMsgs[i].innerText || '').replace(/\\s+/g, ' ').trim();
    if (!text) continue;
    if (text.includes('?') || /\\b(years?|experience|salary|ctc|notice|relocate|residing|willing|confirm|located|select|birth|dob)\\b/i.test(text)) {
      question = text;
      break;
    }
  }
  if (!question && botMsgs.length) {
    question = (botMsgs[botMsgs.length - 1].innerText || '').replace(/\\s+/g, ' ').trim();
  }

  const options = discoverAnswerOptions(scope, question);
  const meaningfulOpts = options.filter((o) => !/skip this question/i.test(o.text || ''));
  const chips = options.map((o) => o.text);
  const kinds = [...new Set(options.map((o) => o.kind))];
  const radioOptions = options.filter((o) => /radio|chip|text-option/i.test(o.kind)).map((o) => o.text);
  const checkboxOptions = options.filter((o) => /checkbox/i.test(o.kind)).map((o) => o.text);
  const chipOptions = options.filter((o) => o.kind === 'chip').map((o) => o.text);
  const hasVisibleInput = !!visibleTextInput(scope);
  const hasSingleSelect = !!scope.querySelector('.singleselect-radiobutton input[type="radio"], .ssrc__radio');
  const hasChoice = meaningfulOpts.length > 0 || hasSingleSelect;
  const hasSkipOnly = options.length > 0 && meaningfulOpts.length === 0;
  const hasCheckbox = checkboxOptions.length > 0 || kinds.some((k) => k.includes('checkbox'));
  return {
    question,
    chips,
    options,
    kinds,
    radioOptions,
    checkboxOptions,
    chipOptions,
    hasInput: hasVisibleInput,
    hasVisibleInput,
    hasSingleSelect,
    hasChoice,
    hasSkipOnly,
    hasCheckbox,
  };
}
"""
)

_FILL_JS = (
    _CHATBOT_HELPERS_JS
    + """
({ answer, answers, allowText, mode }) => {
  const scope = chatbotScope();
  if (!scope) return { filled: false, reason: 'no-drawer' };

  if (mode === 'date' || mode === 'text') {
    const textResult = fillTextInput(scope, answer);
    if (textResult.filled || mode === 'date') return textResult;
  }

  if (mode === 'checkbox') {
    const result = clickCheckboxAnswers(scope, answers || [answer]);
    if (result) return result;
    if (!allowText) return { filled: false, reason: 'choice-only' };
  }

  const clicked = clickAnswerOption(scope, answer);
  if (clicked) {
    if (clicked.autoSubmit) submitChatbotChoice(scope);
    return clicked;
  }

  if (!allowText || hasMeaningfulChoiceUI(scope)) return { filled: false, reason: 'choice-only' };

  return fillTextInput(scope, answer);
}
"""
)


async def chatbot_is_open(page: Page) -> bool:
    try:
        return bool(await page.evaluate(_CHATBOT_OPEN_JS))
    except Exception:
        return False


async def wait_for_chatbot(page: Page, timeout_ms: int = 25000) -> bool:
    """Wait until the side panel has a question, chips, or text input — not just the empty shell."""
    try:
        await page.wait_for_function(_CHATBOT_READY_JS, timeout=timeout_ms)
        return True
    except PlaywrightTimeout:
        return await chatbot_is_open(page)
    except Exception as exc:
        logger.warning("Naukri chatbot wait failed: %s", exc)
        return await chatbot_is_open(page)


async def wait_for_question_advance(
    page: Page,
    previous_question: str = "",
    timeout_ms: int = 20000,
) -> bool:
    """After answering, wait until the question text changes or the panel closes."""
    prev = re.sub(r"\s+", " ", previous_question.strip().lower())
    escaped = prev.replace("\\", "\\\\").replace("'", "\\'")
    ready_js = f"""
() => {{
  const overlay = document.querySelector('.chatbot_Overlay.show');
  const container = document.querySelector('#desktopChatBotContainer, ._chatBotContainer');
  if (!overlay && !container) return true;

  const btn = document.querySelector('#jobs-desc button');
  if (btn) {{
    for (const span of btn.querySelectorAll('span')) {{
      const label = (span.textContent || '').trim().toLowerCase();
      const cls = span.className || '';
      if (!cls.includes('translate-y-full') && /\\bapplied\\b/.test(label) && !/quick apply/.test(label)) {{
        return true;
      }}
    }}
  }}

  const roots = [
    document.querySelector('#desktopChatBotContainer'),
    document.querySelector('._chatBotContainer'),
    document.querySelector('.chatbot_Drawer.chatbot_right'),
    document.querySelector('.chatbot_Drawer'),
  ].filter(Boolean);
  if (!roots.length) return true;
  const root = roots[0];
  const drawer = root.classList?.contains('chatbot_Drawer')
    ? root
    : (root.querySelector('.chatbot_Drawer') || root);
  const scope = drawer || root;
  const prev = '{escaped}';

  if (scope.querySelector('.userItem, .userMsg, li.userItem')) return true;

  const msgs = [...scope.querySelectorAll('.botItem .botMsg, .botMsg')];
  let latest = '';
  for (let i = msgs.length - 1; i >= 0; i--) {{
    const text = (msgs[i].innerText || '').replace(/\\s+/g, ' ').trim();
    if (text.length > 3) {{
      latest = text.toLowerCase();
      break;
    }}
  }}
  if (!latest) return false;
  if (!overlay && !scope.querySelector('.botItem')) return true;
  if (prev && latest !== prev) return true;
  return false;
}}
"""
    try:
        await page.wait_for_function(ready_js, timeout=timeout_ms)
        return True
    except PlaywrightTimeout:
        return not await chatbot_is_open(page)


def _normalize_city(value: str) -> str:
    key = re.sub(r"\s+", " ", value.strip().lower())
    return _CITY_ALIASES.get(key, key)


def _chip_matches(chip_label: str, answer: str) -> bool:
    chip = re.sub(r"\s+", " ", chip_label.strip().lower())
    want = re.sub(r"\s+", " ", answer.strip().lower())
    if not chip or _SKIP_CHIP.search(chip):
        return False
    chip = re.sub(r"\s*\(\d[\d,]*\)\s*$", "", chip)
    want = _normalize_city(want)
    chip_norm = _normalize_city(chip)
    if chip_norm == want or want in chip_norm or chip_norm in want:
        return True
    if chip in ("yes", "no"):
        if chip == "yes" and re.search(r"\byes\b", want) and not re.search(r"\bno\b", want):
            return True
        if chip == "no" and re.search(r"\bno\b", want):
            return True
    chip_num = re.search(r"(\d+)", chip)
    want_num = re.search(r"(\d+)", want)
    if chip_num and want_num and chip_num.group(1) == want_num.group(1):
        return True
    return False


def _value_in_chip_range(value: int, chip: str) -> bool:
    chip_l = chip.lower()
    lt_m = re.search(r"<\s*(\d+)", chip_l)
    if lt_m:
        return value < int(lt_m.group(1))
    range_m = re.search(r"(\d+)\s*[-–]\s*(\d+)", chip_l)
    if range_m:
        return int(range_m.group(1)) <= value <= int(range_m.group(2))
    plus_m = re.search(r"(\d+)\s*\+", chip_l)
    if plus_m:
        return value >= int(plus_m.group(1))
    for m in re.finditer(r"(\d+)", chip_l):
        if int(m.group(1)) == value:
            return True
    return False


_IMMEDIATE_NOTICE_CHIP = re.compile(
    r"immediate|immediately|join\s*immediately|0\s*days?|available\s*now|right\s*away",
    re.I,
)

_SHORT_NOTICE_CHIP = re.compile(r"15\s*days?\s*or\s*less", re.I)


def _answer_implies_immediate(answer: str) -> bool:
    a = answer.lower()
    return any(w in a for w in ("immediate", "immediately", "available", "lwd"))


def _pick_immediate_notice_chip(chips: list[str]) -> str | None:
    for chip in chips:
        if _IMMEDIATE_NOTICE_CHIP.search(chip):
            return chip
    for chip in chips:
        if _SHORT_NOTICE_CHIP.search(chip):
            return chip
    return None


def _pick_notice_period_chip(answer: str, chips: list[str]) -> str | None:
    a = answer.lower()

    if _answer_implies_immediate(answer):
        immediate = _pick_immediate_notice_chip(chips)
        if immediate:
            return immediate

    if "serving" in a:
        for chip in chips:
            if re.search(r"serving", chip, re.I):
                return chip

    month_m = re.search(r"(\d+)\s*month", a)
    if month_m:
        months = month_m.group(1)
        for chip in chips:
            if re.search(rf"\b{months}\s*month", chip, re.I):
                return chip

    for num in re.findall(r"(\d+)", a):
        days = int(num)
        if days > 180:
            continue
        for chip in chips:
            if _value_in_chip_range(days, chip):
                return chip
        if days <= 15:
            for chip in chips:
                if _SHORT_NOTICE_CHIP.search(chip):
                    return chip
        if days <= 30:
            for chip in chips:
                if re.search(r"\b1\s*month", chip, re.I):
                    return chip

    return None


def _pick_best_chip(answer: str, chips: list[str], question: str) -> str | None:
    for chip in chips:
        if _chip_matches(chip, answer):
            return chip

    q = question.lower()

    if "notice" in q:
        picked = _pick_notice_period_chip(answer, chips)
        if picked:
            return picked

    if re.search(r"experience|years?", q):
        ym = re.search(r"(\d+)", answer)
        if ym:
            years = int(ym.group(1))
            for chip in chips:
                if _value_in_chip_range(years, chip):
                    return chip

    if "city" in q or "select" in q:
        for chip in chips:
            if _chip_matches(chip, answer):
                return chip

    return None


def _looks_like_notice_period_question(label: str) -> bool:
    return bool(_NOTICE_PERIOD_QUESTION.search(label))


def _looks_like_city_option(opt: str) -> bool:
    text = opt.strip()
    if not text or _SKIP_CHIP.search(text):
        return False
    if re.fullmatch(r"yes|no", text, re.I):
        return False
    return bool(_CITY_OPTION.search(text)) or "," in text


def _city_token(value: str) -> str:
    token = value.strip().lower().split(",")[0].strip()
    return _normalize_city(token)


def _best_city_match(want: str, options: list[str]) -> str | None:
    want_tok = _city_token(want)
    if not want_tok:
        return None
    for opt in options:
        opt_tok = _city_token(opt)
        if want_tok == opt_tok or want_tok in opt_tok or opt_tok in want_tok:
            return opt
        if want_tok in opt.lower() or opt.lower().startswith(want_tok):
            return opt
    return None


def _city_checkbox_targets(answer: str, options: list[str]) -> list[str]:
    city_opts = [o for o in options if _looks_like_city_option(o)]
    if not city_opts:
        city_opts = [o for o in options if o.strip() and not _SKIP_CHIP.search(o)]
    if not city_opts:
        return []

    raw = answer.strip().lower()
    if raw in ("yes", "y", "true"):
        return city_opts
    if raw in ("no", "n", "false"):
        return []

    targets: list[str] = []
    for part in _parse_multi_answer(answer) or [answer]:
        part = part.strip()
        if not part or re.search(r"\b(in that order|preference)\b", part, re.I):
            continue
        for token in re.split(r"[,;|]", part):
            token = token.strip()
            if not token or len(token) < 3:
                continue
            if re.search(r"\b(in that order|preference|delhi|bangalore|bengaluru)\b", token, re.I):
                # Still try to match delhi/bangalore as cities
                pass
            best = _best_city_match(token, city_opts)
            if best and best not in targets:
                targets.append(best)
    return targets


def _looks_like_city_select_question(label: str) -> bool:
    return bool(_CITY_SELECT_QUESTION.search(label))


def _parse_multi_answer(answer: str) -> list[str]:
    raw = answer.strip()
    if not raw:
        return []
    return [part.strip() for part in re.split(r"[,;|]", raw) if part.strip()]


def _targets_for_answer(answer: str, options: list[str], label: str) -> list[str]:
    if _looks_like_city_select_question(label):
        city_targets = _city_checkbox_targets(answer, options)
        if city_targets:
            return city_targets
        parts = _parse_multi_answer(answer) or [answer.strip()]
        targets: list[str] = []
        for part in parts:
            best = _pick_best_chip(part, options, label) if options else None
            targets.append(best or part)
        return targets

    best = _pick_best_chip(answer, options, label) if options else None
    return [best or answer.strip()]


def _looks_like_yes_no_question(label: str) -> bool:
    return bool(_YES_NO_QUESTION.search(label)) or bool(
        re.search(r"residing in .+\?", label, re.I)
    )


def _is_yes_no_options(options: list[str]) -> bool:
    opts = {o.strip().lower() for o in options if o.strip()}
    return bool(opts) and opts <= {"yes", "no"}


def _is_checkbox_options(raw: dict[str, Any] | None, options: list[str]) -> bool:
    if not raw:
        return False
    if raw.get("hasCheckbox"):
        return True
    kinds = raw.get("kinds") or []
    return any("checkbox" in str(kind) for kind in kinds)


def _choice_only_question(label: str, options: list[str], raw: dict[str, Any] | None = None) -> bool:
    if raw and raw.get("hasVisibleInput") and not options:
        return False
    if raw and raw.get("hasSkipOnly") and raw.get("hasVisibleInput"):
        return False
    if _looks_like_yes_no_question(label):
        return True
    if _looks_like_city_select_question(label):
        return True
    if _looks_like_notice_period_question(label):
        return True
    if raw and raw.get("hasSingleSelect"):
        return True
    meaningful = [o for o in options if o.strip() and not _SKIP_CHIP.search(o)]
    if raw and raw.get("hasChoice") and meaningful:
        return True
    if _is_yes_no_options(options):
        return True
    if _is_checkbox_options(raw, options):
        return True
    if re.search(r"\bselect\b", label, re.I) and options:
        return True
    return False


def _wait_attempts_for(label: str) -> int:
    if _is_date_field(label):
        return 32
    if (
        _looks_like_yes_no_question(label)
        or _looks_like_city_select_question(label)
        or _looks_like_notice_period_question(label)
    ):
        return 28
    return 16


def _is_date_field(label: str) -> bool:
    return bool(_DATE_FIELD.search(label))


def _normalize_dob_answer(answer: str) -> str:
    text = answer.strip()
    if not text:
        return text
    # DD/MM/YYYY or DD-MM-YYYY — keep as-is for Naukri text fields
    m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", text)
    if m:
        d, mo, y = m.groups()
        return f"{int(d):02d}/{int(mo):02d}/{y}"
    return text


async def _fill_text_playwright(page: Page, answer: str) -> bool:
    """Playwright fallback when JS fill cannot find the chatbot text input."""
    scope = page.locator(_CHATBOT_SCOPE).last
    if await scope.count() == 0:
        scope = page.locator("body")

    selectors = (
        'div.textArea[contenteditable="true"]',
        'div.textArea[contenteditable]',
        '[contenteditable="true"]',
        "textarea:visible",
        'input[type="text"]:visible',
        'input[type="date"]:visible',
    )
    for sel in selectors:
        loc = scope.locator(sel)
        if await loc.count() == 0:
            continue
        target = loc.first
        try:
            await target.scroll_into_view_if_needed()
            await target.click(timeout=3000)
            try:
                await target.fill(answer, timeout=3000)
            except PlaywrightTimeout:
                await target.press_sequentially(answer, delay=30)
            for send_sel in (
                ".sendMsgbtn_container .sendMsg:not(.disabled)",
                ".sendMsgbtn_container .send:not(.disabled)",
                ".sendMsg:not(.disabled)",
            ):
                send = scope.locator(send_sel)
                if await send.count() > 0:
                    try:
                        await send.first.click(timeout=3000)
                        return True
                    except PlaywrightTimeout:
                        continue
            await page.keyboard.press("Enter")
            return True
        except PlaywrightTimeout:
            continue
    return False


async def _fill_date_field(page: Page, label: str, answer: str) -> bool:
    """Fill date-of-birth and similar plain-text chatbot fields."""
    answer = _normalize_dob_answer(answer)
    wait_attempts = _wait_attempts_for(label)

    for _ in range(max(3, wait_attempts // 8)):
        result = await page.evaluate(
            _FILL_JS,
            {"answer": answer, "answers": [answer], "allowText": True, "mode": "date"},
        )
        if isinstance(result, dict) and result.get("filled"):
            logger.info("Naukri chatbot: filled date field %r", label[:40])
            return True
        await page.wait_for_timeout(400)

    if await _fill_text_playwright(page, answer):
        logger.info("Naukri chatbot: filled date field via Playwright %r", label[:40])
        return True

    return False


def _dedupe_options(options: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for opt in options:
        text = str(opt).strip()
        if not text or text in seen or _SKIP_CHIP.search(text):
            continue
        seen.add(text)
        out.append(text)
    return out


def _split_discovered_options(
    raw: dict[str, Any] | None,
) -> tuple[list[str], list[str], list[str]]:
    """Return (radio_or_chip_options, checkbox_options, all_chip_options)."""
    if not raw:
        return [], [], []

    radio_opts = _dedupe_options([str(o) for o in (raw.get("radioOptions") or [])])
    checkbox_opts = _dedupe_options([str(o) for o in (raw.get("checkboxOptions") or [])])
    chip_opts = _dedupe_options([str(o) for o in (raw.get("chipOptions") or [])])

    if not radio_opts and not checkbox_opts and not chip_opts:
        structured = raw.get("options") or []
        for item in structured:
            if isinstance(item, dict):
                text = str(item.get("text", "")).strip()
                kind = str(item.get("kind", ""))
            else:
                text = str(item).strip()
                kind = "chip"
            if not text or _SKIP_CHIP.search(text):
                continue
            if "checkbox" in kind:
                checkbox_opts.append(text)
            elif kind == "chip":
                chip_opts.append(text)
            else:
                radio_opts.append(text)
        radio_opts = _dedupe_options(radio_opts)
        checkbox_opts = _dedupe_options(checkbox_opts)
        chip_opts = _dedupe_options(chip_opts)

    choice_opts = chip_opts or radio_opts
    return choice_opts, checkbox_opts, chip_opts


def _infer_naukri_field_kind(
    question: str,
    raw: dict[str, Any] | None,
    choice_opts: list[str],
    checkbox_opts: list[str],
) -> tuple[str, list[str]]:
    """Infer control type and the option list to use for answer resolution/fill."""
    all_opts = _dedupe_options(checkbox_opts + choice_opts)
    city_opts = [o for o in all_opts if _looks_like_city_option(o)]

    if _looks_like_city_select_question(question):
        if len(city_opts) >= 1:
            return "checkbox_group", city_opts
        if len(all_opts) >= 2 and not _is_yes_no_options(all_opts):
            return "checkbox_group", [o for o in all_opts if not _SKIP_CHIP.search(o)]

    if checkbox_opts and (
        len(checkbox_opts) > 1 or _looks_like_city_select_question(question)
    ):
        return "checkbox_group", checkbox_opts

    if choice_opts:
        return "radio", choice_opts

    if raw and raw.get("hasSingleSelect"):
        return "radio", []

    if raw and raw.get("hasCheckbox") and _looks_like_city_select_question(question):
        return "checkbox_group", []

    if raw and raw.get("hasVisibleInput") and not raw.get("hasChoice"):
        return "text", []

    if _is_date_field(question):
        return "text", []

    if _looks_like_city_select_question(question):
        return "checkbox_group", []

    if (
        _looks_like_yes_no_question(question)
        or _looks_like_notice_period_question(question)
        or (raw and raw.get("hasChoice"))
    ):
        return "radio", choice_opts

    return "text", []


def _analyze_chatbot_state(
    raw: dict[str, Any] | None,
    question: str = "",
) -> tuple[str, list[str], dict[str, Any] | None]:
    """Parse discovery payload into (kind, options, raw)."""
    if not raw:
        return "text", [], None
    label = question or str(raw.get("question") or "").strip()
    choice_opts, checkbox_opts, _chip_opts = _split_discovered_options(raw)
    kind, options = _infer_naukri_field_kind(label, raw, choice_opts, checkbox_opts)
    return kind, options, raw


async def _fetch_answer_options(
    page: Page,
    *,
    question: str = "",
    attempts: int = 12,
) -> tuple[str, list[str], dict[str, Any] | None]:
    last_raw: dict[str, Any] | None = None
    best_kind = "text"
    best_options: list[str] = []
    for _ in range(attempts):
        raw = await page.evaluate(_DISCOVER_JS)
        if raw:
            last_raw = raw
            kind, options, _ = _analyze_chatbot_state(raw, question)
            if options:
                return kind, options, raw
            if kind == "checkbox_group" and (
                raw.get("hasCheckbox") or _looks_like_city_select_question(question)
            ):
                pw_opts = await _discover_checkbox_options_playwright(page)
                if pw_opts:
                    return kind, pw_opts, raw
                best_kind, best_options = kind, options
            elif kind == "radio" and (raw.get("hasSingleSelect") or raw.get("hasChoice")):
                best_kind = kind
            elif kind == "text" and raw.get("hasVisibleInput"):
                best_kind, best_options = kind, []
                if not raw.get("hasChoice"):
                    return kind, [], raw
        await page.wait_for_timeout(350)
    if best_options:
        return best_kind, best_options, last_raw
    if last_raw:
        kind, options, _ = _analyze_chatbot_state(last_raw, question)
        if not options and kind == "checkbox_group" and _looks_like_city_select_question(question):
            pw_opts = await _discover_checkbox_options_playwright(page)
            if pw_opts:
                return kind, pw_opts, last_raw
        return kind, options, last_raw
    return "text", [], last_raw


async def _discover_checkbox_options_playwright(page: Page) -> list[str]:
    """Fallback when JS discovery misses async-rendered city checkboxes."""
    scope = page.locator(_CHATBOT_SCOPE).last
    if await scope.count() == 0:
        return []
    options: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        text = re.sub(r"\s+", " ", name.strip())
        if not text or text in seen or _SKIP_CHIP.search(text):
            return
        if not _looks_like_city_option(text):
            return
        seen.add(text)
        options.append(text)

    try:
        boxes = scope.get_by_role("checkbox")
        count = await boxes.count()
        for i in range(count):
            box = boxes.nth(i)
            name = (await box.get_attribute("aria-label") or "").strip()
            if not name:
                try:
                    name = (await box.evaluate(
                        """el => {
                          const id = el.id;
                          if (id) {
                            const lbl = document.querySelector(`label[for="${CSS.escape(id)}"]`);
                            if (lbl) return (lbl.innerText || '').trim();
                          }
                          const wrap = el.closest('label');
                          return wrap ? (wrap.innerText || '').trim() : '';
                        }"""
                    )).strip()
                except Exception:
                    name = ""
            _add(name)

        for sel in ("label.ssrc__label", "label:has(.truncate)", ".chatbot_Chip", ".chipItem"):
            loc = scope.locator(sel)
            count = await loc.count()
            for i in range(count):
                text = re.sub(r"\s+", " ", (await loc.nth(i).inner_text()).strip())
                if _looks_like_city_option(text):
                    _add(text)
    except Exception as exc:
        logger.debug("Playwright checkbox discovery failed: %s", exc)
    return options


async def _click_checkbox_playwright(page: Page, targets: list[str]) -> bool:
    scope = page.locator(_CHATBOT_SCOPE).last
    clicked = False
    for target in targets:
        if not target:
            continue
        tokens = [target, target.split(",")[0].strip()]
        if tokens[-1].lower() in ("gurugram", "gurgaon"):
            tokens.extend(["Gurugram", "Gurgaon"])
        if tokens[-1].lower() == "noida":
            tokens.append("Noida")
        for pattern in dict.fromkeys(t for t in tokens if t):
            box = scope.get_by_role(
                "checkbox", name=re.compile(re.escape(pattern), re.I)
            )
            if await box.count() > 0:
                try:
                    await box.first.click(timeout=5000)
                    clicked = True
                    break
                except PlaywrightTimeout:
                    pass
            label = scope.locator("label.ssrc__label, label, .chatbot_Chip, .chipItem").filter(
                has_text=re.compile(re.escape(pattern), re.I)
            )
            if await label.count() > 0:
                try:
                    await label.first.click(timeout=5000)
                    clicked = True
                    break
                except PlaywrightTimeout:
                    pass
    if clicked:
        await _click_save_if_present(page)
    return clicked


async def _click_save_if_present(page: Page) -> bool:
    scope = page.locator(_CHATBOT_SCOPE).last
    save = scope.locator(
        ".sendMsgbtn_container .sendMsg:not(.disabled), "
        ".sendMsgbtn_container .send:not(.disabled) .sendMsg"
    )
    if await save.count() > 0:
        try:
            await save.first.click(timeout=3000)
            return True
        except PlaywrightTimeout:
            pass
    return False


async def _click_option_playwright(page: Page, target: str) -> bool:
    target = target.strip()
    if not target or len(target) > 120:
        return False
    scope = page.locator(_CHATBOT_SCOPE).last
    try:
        chip_loc = scope.locator(".chatbot_Chip, .chipItem")
        count = await chip_loc.count()
        for i in range(count):
            chip = chip_loc.nth(i)
            label = re.sub(r"\s+", " ", (await chip.inner_text()).strip())
            if not label or _SKIP_CHIP.search(label):
                continue
            if not _chip_matches(label, target):
                continue
            try:
                await chip.scroll_into_view_if_needed()
                await chip.click(timeout=5000)
                return True
            except PlaywrightTimeout:
                try:
                    await chip.click(timeout=5000, force=True)
                    return True
                except PlaywrightTimeout:
                    pass

        for pattern in (target, target.capitalize()):
            if not pattern:
                continue
            radio = scope.get_by_role(
                "radio", name=re.compile(re.escape(pattern), re.I)
            )
            if await radio.count() > 0:
                try:
                    await radio.first.click(timeout=5000)
                    await _click_save_if_present(page)
                    return True
                except PlaywrightTimeout:
                    pass
            label = scope.locator("label.ssrc__label, label").filter(
                has_text=re.compile(re.escape(pattern), re.I)
            )
            if await label.count() > 0:
                try:
                    await label.first.click(timeout=5000)
                    await _click_save_if_present(page)
                    return True
                except PlaywrightTimeout:
                    pass
    except Exception as exc:
        logger.debug("Playwright option click failed for %r: %s", target[:40], exc)
    return False


async def _click_chip(page: Page, chip_label: str) -> bool:
    chip_loc = page.locator(_CHIP_SELECTOR)
    count = await chip_loc.count()
    for i in range(count):
        chip = chip_loc.nth(i)
        label = re.sub(r"\s+", " ", (await chip.inner_text()).strip())
        if not label or _SKIP_CHIP.search(label):
            continue
        if not _chip_matches(label, chip_label):
            continue
        try:
            await chip.scroll_into_view_if_needed()
            await chip.click(timeout=5000)
            return True
        except PlaywrightTimeout:
            try:
                await chip.click(timeout=5000, force=True)
                return True
            except PlaywrightTimeout:
                return False
    return False


def _coerce_chip_answer(question: dict[str, Any], answer: str) -> str:
    """Map saved answers to Yes/No chips when the chatbot expects chips."""
    label = str(question.get("label", "")).lower()
    options = [str(o).strip() for o in (question.get("options") or []) if str(o).strip()]
    a = answer.strip().lower()

    if not options and _looks_like_yes_no_question(label):
        if a in ("yes", "y", "true", "1"):
            return "Yes"
        if a in ("no", "n", "false", "0"):
            return "No"
        if any(city in label for city in ("bengaluru", "bangalore", "hyderabad", "pune", "mumbai", "chennai", "delhi")):
            if a in ("bengaluru", "bangalore", "hyderabad", "pune", "mumbai", "delhi", "ncr", "gurgaon", "noida", "yes"):
                return "Yes"
            if a in ("no", "not"):
                return "No"

    if "military spouse" in label:
        if a in ("no", "n", "false", "0"):
            for opt in options:
                if re.search(r"\bnot\b.*military|never\b", str(opt), re.I):
                    return str(opt)
            return next((o for o in options if o.lower() == "no"), answer)
        if a in ("yes", "y", "true", "1"):
            for opt in options:
                if re.search(r"\bmilitary spouse\b", str(opt), re.I) and not re.search(
                    r"\bnot\b", str(opt), re.I
                ):
                    return str(opt)

    if re.search(r"\b(associated with|previously employed|employee of)\b", label):
        if a in ("no", "n", "false", "0"):
            picked = next(
                (o for o in options if re.search(r"\b(not|never|no)\b", o, re.I)),
                None,
            )
            if picked:
                return picked
        if a in ("yes", "y", "true", "1"):
            picked = next(
                (
                    o
                    for o in options
                    if re.search(r"\byes\b", o, re.I)
                    and not re.search(r"\b(not|never)\b", o, re.I)
                ),
                None,
            )
            if picked:
                return picked

    if not options:
        return answer.strip()

    opts_lower = {o.lower() for o in options}
    if opts_lower <= {"yes", "no"}:
        a = answer.strip().lower()
        label = str(question.get("label", "")).lower()
        if a in ("yes", "y", "true", "1"):
            return next((o for o in options if o.lower() == "yes"), answer)
        if a in ("no", "n", "false", "0"):
            return next((o for o in options if o.lower() == "no"), answer)
        if any(city in label for city in ("bengaluru", "bangalore", "hyderabad", "pune", "mumbai")):
            if a in ("bengaluru", "bangalore", "hyderabad", "pune", "mumbai", "delhi", "ncr", "gurgaon", "noida"):
                return next((o for o in options if o.lower() == "yes"), answer)
    return answer.strip()


def _effective_options(options: list[str], label: str) -> list[str]:
    if options:
        return options
    if _looks_like_notice_period_question(label):
        return [
            "Serving Notice Period",
            "15 days or less",
            "1 month",
            "2 months",
            "3 months",
        ]
    return []


def _is_naukri_chatbot_question(label: str) -> bool:
    """Aurus chatbot often uses short field labels without '?'."""
    if is_plausible_application_question(label):
        return True
    text = re.sub(r"\s+", " ", label.strip())
    return bool(text) and 3 <= len(text) <= 100 and not _SKIP_CHIP.search(text)


async def discover_naukri_chatbot_questions(page: Page) -> list[dict[str, Any]]:
    raw = await page.evaluate(_DISCOVER_JS)
    if not raw:
        return []

    question = str(raw.get("question") or "").strip()
    if not question or is_generic_question_label(question):
        return []
    if not _is_naukri_chatbot_question(question):
        logger.debug("Skipping non-question chatbot text: %s", question[:80])
        return []

    wait_attempts = _wait_attempts_for(question)
    kind, options, raw = await _fetch_answer_options(
        page, question=question, attempts=wait_attempts
    )
    if not options:
        kind, options, _ = _analyze_chatbot_state(raw, question)

    field: dict[str, Any] = {
        "kind": kind,
        "label": question,
        "index": 0,
    }
    if options:
        field["options"] = options

    opt_preview = ", ".join(options[:5])
    if len(options) > 5:
        opt_preview += f" (+{len(options) - 5} more)"
    logger.info(
        "Naukri chatbot question [%s]: %s%s",
        kind,
        question[:70],
        f" — options: {opt_preview}" if options else "",
    )
    return [field]


async def _click_skip_question(page: Page) -> bool:
    js = (
        _CHATBOT_HELPERS_JS
        + """
() => {
  const scope = chatbotScope();
  if (!scope) return false;
  for (const chip of scope.querySelectorAll('.chatbot_Chip, .chipItem')) {
    const t = (chip.innerText || '').replace(/\\s+/g, ' ').trim();
    if (/skip this question/i.test(t)) {
      chip.click();
      return true;
    }
  }
  return false;
}
"""
    )
    try:
        return bool(await page.evaluate(js))
    except Exception:
        return False


def _is_optional_text_field(label: str) -> bool:
    return bool(_OPTIONAL_TEXT_FIELD.search(label))


def _should_skip_text_answer(label: str, answer: str) -> bool:
    a = answer.strip().lower()
    if a in _SKIP_ANSWERS:
        return True
    if _is_optional_text_field(label) and re.fullmatch(r"\d+", a or ""):
        return True
    return False


async def fill_naukri_chatbot_question(
    page: Page,
    question: dict[str, Any],
    answer: str,
) -> bool:
    label = str(question.get("label", ""))
    wait_attempts = _wait_attempts_for(label)
    kind, answer_options, raw = await _fetch_answer_options(
        page, question=label, attempts=wait_attempts
    )
    if not answer_options:
        answer_options = [
            str(c).strip()
            for c in (question.get("options") or [])
            if str(c).strip() and not _SKIP_CHIP.search(str(c))
        ]
    if not kind or kind == "text":
        kind = str(question.get("kind", kind or "text"))

    if _should_skip_text_answer(label, answer):
        if await _click_skip_question(page):
            logger.info("Naukri chatbot: skipped optional field %s", label[:60])
            return await wait_for_question_advance(
                page,
                previous_question=label,
                timeout_ms=15000,
            )

    if _is_date_field(label):
        if await _fill_date_field(page, label, answer):
            return await wait_for_question_advance(
                page,
                previous_question=label,
                timeout_ms=15000,
            )
        logger.warning(
            "Could not fill Naukri chatbot question: %s (date-input)",
            label[:60],
        )
        return False

    if not answer_options:
        answer_options = [
            str(c).strip()
            for c in (question.get("options") or question.get("chips") or [])
            if str(c).strip() and not _SKIP_CHIP.search(str(c))
        ]

    # Re-infer kind from live DOM — discovery snapshot may be stale.
    live_kind, live_options, _ = _analyze_chatbot_state(raw, label)
    if live_options:
        answer_options = live_options
        kind = live_kind
    elif live_kind != "text":
        kind = live_kind

    if _is_optional_text_field(label) or _is_date_field(label) or (raw and raw.get("hasVisibleInput") and raw.get("hasSkipOnly")):
        result = await page.evaluate(
            _FILL_JS,
            {"answer": answer, "answers": [answer], "allowText": True, "mode": "text"},
        )
        if isinstance(result, dict) and result.get("filled"):
            logger.info("Naukri chatbot: filled text field %r", label[:40])
            return await wait_for_question_advance(
                page,
                previous_question=label,
                timeout_ms=15000,
            )

    answer = resolve_fill_answer(
        answer, {**question, "kind": kind, "options": answer_options}
    )
    answer = _coerce_chip_answer({**question, "kind": kind, "options": answer_options}, answer)
    answer_options = _effective_options(answer_options, label)
    choice_only = _choice_only_question(label, answer_options, raw)
    checkbox_mode = kind in ("checkbox", "checkbox_group") or (
        _is_checkbox_options(raw, answer_options) and len(answer_options) > 1
    ) or (
        _looks_like_city_select_question(label) and (answer_options or (raw and raw.get("hasCheckbox")))
    )
    radio_mode = kind == "radio" or (
        not checkbox_mode
        and (
            _looks_like_yes_no_question(label)
            or _looks_like_notice_period_question(label)
            or bool(answer_options)
            or bool(raw and raw.get("hasSingleSelect"))
        )
    )
    targets = _targets_for_answer(answer, answer_options, label)

    if checkbox_mode:
        if not answer_options:
            answer_options = await _discover_checkbox_options_playwright(page)
        targets = _targets_for_answer(answer, answer_options, label)
        if not targets and _looks_like_city_select_question(label) and answer_options:
            targets = _city_checkbox_targets("Yes", answer_options)
        logger.info(
            "Naukri chatbot city targets for %s: %s",
            label[:50],
            ", ".join(targets) if targets else "(none)",
        )
        result = await page.evaluate(
            _FILL_JS,
            {"answer": answer, "answers": targets, "allowText": False, "mode": "checkbox"},
        )
        if isinstance(result, dict) and result.get("filled"):
            logger.info(
                "Naukri chatbot: clicked %s %r",
                result.get("method", "checkbox"),
                str(result.get("label", answer))[:40],
            )
            return await wait_for_question_advance(
                page,
                previous_question=label,
                timeout_ms=15000,
            )
        if await _click_checkbox_playwright(page, targets):
            logger.info("Naukri chatbot: clicked checkbox(es) %r", ", ".join(targets)[:60])
            return await wait_for_question_advance(
                page,
                previous_question=label,
                timeout_ms=15000,
            )
        if choice_only:
            logger.warning(
                "Naukri chatbot: expected checkbox for %s but found no options",
                label[:60],
            )
            return False

    if radio_mode:
        target = targets[0]
        result = await page.evaluate(
            _FILL_JS,
            {"answer": target, "answers": targets, "allowText": False, "mode": "choice"},
        )
        if isinstance(result, dict) and result.get("filled"):
            logger.info(
                "Naukri chatbot: clicked %s %r",
                result.get("method", "option"),
                str(result.get("label", target))[:40],
            )
            return await wait_for_question_advance(
                page,
                previous_question=label,
                timeout_ms=15000,
            )
        if await _click_option_playwright(page, target):
            logger.info("Naukri chatbot: clicked radio %r", target[:40])
            return await wait_for_question_advance(
                page,
                previous_question=label,
                timeout_ms=15000,
            )
        logger.warning(
            "Naukri chatbot: expected radio for %s but could not click %r",
            label[:60],
            target[:40],
        )
        return False

    if answer_options:
        target = targets[0]
        result = await page.evaluate(
            _FILL_JS,
            {"answer": target, "answers": targets, "allowText": False, "mode": "choice"},
        )
        if isinstance(result, dict) and result.get("filled"):
            logger.info(
                "Naukri chatbot: clicked %s %r",
                result.get("method", "option"),
                str(result.get("label", target))[:40],
            )
            return await wait_for_question_advance(
                page,
                previous_question=label,
                timeout_ms=15000,
            )
        if await _click_chip(page, target) or await _click_option_playwright(page, target):
            logger.info("Naukri chatbot: clicked option %r", target[:40])
            return await wait_for_question_advance(
                page,
                previous_question=label,
                timeout_ms=15000,
            )
        logger.warning(
            "Naukri chatbot: no matching option for %r (options: %s)",
            answer[:40],
            ", ".join(answer_options[:6]),
        )
        return False

    if choice_only:
        logger.warning(
            "Naukri chatbot: expected chips/radio/checkbox for %s but found no options",
            label[:60],
        )
        return False

    result = await page.evaluate(
        _FILL_JS,
        {"answer": answer, "answers": [answer], "allowText": True, "mode": "text"},
    )
    if not isinstance(result, dict) or not result.get("filled"):
        logger.warning(
            "Could not fill Naukri chatbot question: %s (%s)",
            label[:60],
            result.get("reason") if isinstance(result, dict) else result,
        )
        return False

    logger.info("Naukri chatbot: filled via %s", result.get("method", "text"))
    return await wait_for_question_advance(
        page,
        previous_question=label,
        timeout_ms=15000,
    )
