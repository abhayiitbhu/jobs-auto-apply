from __future__ import annotations

import contextlib
import logging
import re
from typing import Any

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from ..answers.chip_options import is_notice_chip_option, value_in_chip_range
from ..answers.fields import is_numeric_ctc_question
from ..application_questions import (
    enrich_field_for_llm,
    infer_field_input_type,
    is_generic_question_label,
    is_pincode_field,
    is_plausible_application_question,
    normalize_question_label,
    parse_years_numeric_value,
    resolve_fill_answer,
)
from ..config import AppConfig

logger = logging.getLogger("job_apply")


class CannotAnswerTruthfully(Exception):
    """The question's only options would overstate experience the user lacks.

    Raised instead of returning a generic "could not fill" so the caller can
    treat it as an honest skip (queue for manual review) rather than a technical
    apply failure.
    """

    def __init__(self, label: str, reason: str = ""):
        self.label = label
        self.reason = reason
        super().__init__(reason or label)


_SKIP_CHIP = re.compile(r"skip this question", re.I)
_INVALID_OPTION = re.compile(
    r"try\s*again|invalid\s+input|please\s+(?:re)?enter|error|something went wrong",
    re.I,
)
_PAN_VALUE = re.compile(r"^[A-Z]{5}\d{4}[A-Z]$", re.I)

_OPTIONAL_TEXT_FIELD = re.compile(
    r"\b(middle name|first name|last name|maiden name|nick\s*name|nickname)\b",
    re.I,
)

_SKIP_ANSWERS = frozenset({"", "skip", "n/a", "na", "none", "-", "not applicable"})

_YES_NO_QUESTION = re.compile(
    r"\b(are you|do you|will you|can you|have you|did you)\b",
    re.I,
)
_YES_NO_EXPLICIT = re.compile(
    r"\b(have you previously|previously worked|previously employed|"
    r"legally permitted|require sponsorship|identify as a)\b",
    re.I,
)

_CITY_SELECT_QUESTION = re.compile(
    r"\bselect\b.{0,30}\b(city|cities)\b|\b(city|cities)\b.{0,30}\b(residing|relocate)\b",
    re.I,
)

_NOTICE_PERIOD_QUESTION = re.compile(
    r"\b(notice\s*period|how\s+soon|when\s+can\s+you\s+join|can\s+you\s+join|"
    r"join\s+us|available\s+to\s+join|how\s+soon\s+you\s+can\s+join)\b",
    re.I,
)

_F2F_AVAILABILITY_QUESTION = re.compile(
    r"\b(face.?to.?face|f2f|interview on|available for.*interview|walk.?in|" r"attend.*interview)\b",
    re.I,
)

_BLOB_ANSWER = re.compile(
    r"willing_to_relocate\s*:|preferred_location|current\s*:|native\s*:|serving_notice\s*:",
    re.I,
)

_DATE_FIELD = re.compile(
    r"\b(date\s*of\s*birth|dob|d\.o\.b|birth\s*date|last\s*working\s*day|lwd)\b",
    re.I,
)

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

  function visibleDobInputs(scope) {
    for (const root of discoverRoots(scope)) {
      const day = root.querySelector('input.dob__input.day, input[name="day"]');
      const month = root.querySelector('input.dob__input.month, input[name="month"]');
      const year = root.querySelector('input.dob__input.year, input[name="year"]');
      if (day && month && year && isVisible(day)) return { day, month, year };
    }
    return null;
  }

  function parseDobAnswer(answer) {
    const text = String(answer || '').trim();
    const m = text.match(/^(\\d{1,2})[\\/-](\\d{1,2})[\\/-](\\d{4})$/);
    if (!m) return null;
    return {
      day: String(parseInt(m[1], 10)).padStart(2, '0'),
      month: String(parseInt(m[2], 10)).padStart(2, '0'),
      year: m[3],
    };
  }

  function fillDobInput(scope, answer) {
    const parts = parseDobAnswer(answer);
    if (!parts) return { filled: false, reason: 'bad-format' };
    const dob = visibleDobInputs(scope);
    if (!dob) return { filled: false, reason: 'no-dob-input' };
    const entries = [
      [dob.day, parts.day],
      [dob.month, parts.month],
      [dob.year, parts.year],
    ];
    for (const [inp, val] of entries) {
      inp.focus();
      inp.value = val;
      inp.dispatchEvent(new Event('input', { bubbles: true }));
      inp.dispatchEvent(new Event('change', { bubbles: true }));
    }
    if (!clickSendButton(scope)) return { filled: false, reason: 'no-save' };
    return { filled: true, method: 'dob' };
  }

  function inputTextValue(input) {
    if (!input) return '';
    if (input.tagName === 'INPUT' || input.tagName === 'TEXTAREA') return (input.value || '').trim();
    return (input.textContent || input.innerText || '').trim();
  }

  function isInputInAnsweredTurn(input) {
    if (!input) return true;
    if (input.closest('.userItem, li.userItem')) return true;
    const botItem = input.closest('.botItem');
    if (!botItem) return false;
    let node = botItem.nextElementSibling;
    while (node) {
      if (node.matches?.('.userItem, li.userItem')) return true;
      if (node.matches?.('.botItem')) return false;
      node = node.nextElementSibling;
    }
    return false;
  }

  function findLastUnansweredBotItem(scope) {
    const botItems = [...scope.querySelectorAll('.botItem')];
    for (let i = botItems.length - 1; i >= 0; i--) {
      const bot = botItems[i];
      let node = bot.nextElementSibling;
      let answered = false;
      while (node) {
        if (node.matches?.('.userItem, li.userItem')) {
          answered = true;
          break;
        }
        if (node.matches?.('.botItem')) break;
        node = node.nextElementSibling;
      }
      if (!answered) return bot;
    }
    return botItems.length ? botItems[botItems.length - 1] : null;
  }

  function collectTextInputCandidates(scope) {
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
    const candidates = [];
    const seen = new Set();
    for (const root of discoverRoots(scope)) {
      for (const sel of selectors) {
        for (const input of root.querySelectorAll(sel)) {
          if (!isVisible(input)) continue;
          if (input.tagName === 'INPUT' && skipTypes.has((input.type || '').toLowerCase())) continue;
          if (isInputInAnsweredTurn(input)) continue;
          if (seen.has(input)) continue;
          seen.add(input);
          candidates.push(input);
        }
      }
    }
    return candidates;
  }

  function clearInputFully(input) {
    if (!input) return;
    input.focus();
    if (input.tagName === 'INPUT' || input.tagName === 'TEXTAREA') {
      input.value = '';
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
      return;
    }
    try {
      const sel = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(input);
      sel.removeAllRanges();
      sel.addRange(range);
      document.execCommand('delete');
      document.execCommand('selectAll', false, null);
      document.execCommand('delete', false, null);
    } catch (e) {}
    input.innerHTML = '';
    input.textContent = '';
    input.innerText = '';
    input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'deleteContentBackward' }));
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function clearActiveComposers(scope) {
    const candidates = collectTextInputCandidates(scope);
    for (const input of candidates) clearInputFully(input);
    return candidates.length;
  }

  function inputMatchesQuestion(input, questionText) {
    if (!questionText) return false;
    const q = norm(questionText);
    const bits = [];
    const ph = input.getAttribute('data-placeholder') || input.placeholder || '';
    if (ph) bits.push(ph);
    const aria = input.getAttribute('aria-label') || '';
    if (aria) bits.push(aria);
    const name = input.getAttribute('name') || '';
    if (name) bits.push(name);
    let el = input.parentElement;
    for (let i = 0; i < 5 && el; i++, el = el.parentElement) {
      const lbl = el.querySelector(':scope > label, :scope > .label, :scope > span, :scope > p');
      if (lbl) bits.push(lbl.innerText || '');
    }
    const hay = norm(bits.join(' '));
    if (!hay) return false;
    if (/postal|pin\\s*code|pincode|zip/.test(q)) {
      return /postal|pin\\s*code|pincode|zip/.test(hay);
    }
    if (/current location|your location|^location$|native location|where.*located|enter.*location|^city$/.test(q)) {
      return /location|city|based in|where|native/.test(hay);
    }
    if (/last\\s*name|surname/.test(q)) {
      return /last\\s*name|surname|family/.test(hay);
    }
    if (/first\\s*name/.test(q)) {
      return /first\\s*name|given/.test(hay);
    }
    return false;
  }

  function isLocationQuestion(questionText) {
    const q = norm(questionText || '');
    return /current location|your location|^location$|native location|where.*located|enter.*location|^city$/.test(q);
  }

  function isFooterInput(input) {
    return !!input.closest('.footerInputBoxWrapper, .chatbot_InputContainer, .chatbot_SendMessageContainer, #userInput__');
  }

  function visibleTextInput(scope, questionText) {
    if (visibleDobInputs(scope)) return null;
    const candidates = collectTextInputCandidates(scope);
    if (!candidates.length) return null;
    const qText = questionText || '';

    if (qText) {
      for (const input of candidates) {
        if (inputMatchesQuestion(input, qText)) return input;
      }
    }

    const openBot = findLastUnansweredBotItem(scope);
    if (openBot) {
      const inline = candidates.filter((inp) => openBot.contains(inp) && !isFooterInput(inp));
      if (inline.length) return inline[inline.length - 1];
    }

    const footers = candidates.filter(isFooterInput);
    if (footers.length) return footers[footers.length - 1];

    if (qText && isLocationQuestion(qText)) {
      for (const input of candidates) {
        if (isFooterInput(input)) return input;
      }
    }

    for (const input of candidates) {
      if (!inputTextValue(input)) return input;
    }

    return candidates[candidates.length - 1];
  }

  function setTextInputValue(input, answer) {
    if (!input || answer == null) return false;
    const text = String(answer);
    clearInputFully(input);
    input.focus();

    if (input.tagName === 'INPUT' || input.tagName === 'TEXTAREA') {
      input.value = text;
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
      return inputTextValue(input) === text;
    }

    let inserted = false;
    try {
      inserted = document.execCommand('insertText', false, text);
    } catch (e) {}
    if (!inserted) {
      input.textContent = text;
      input.innerText = text;
    }
    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, inputType: 'insertText', data: text }));
    input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: text }));
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    input.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: text.slice(-1) || 'Unidentified' }));

    const got = inputTextValue(input);
    if (got === text || got.replace(/\\s+/g, '') === text.replace(/\\s+/g, '')) return true;

    clearInputFully(input);
    input.focus();
    input.textContent = text;
    input.innerText = text;
    input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: text }));
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    const got2 = inputTextValue(input);
    return got2 === text || got2.replace(/\\s+/g, '') === text.replace(/\\s+/g, '');
  }

  function fillTextInput(scope, answer, questionText) {
    clearActiveComposers(scope);
    const input = visibleTextInput(scope, questionText || '');
    if (!input) return { filled: false, reason: 'no-input' };
    const want = String(answer);
    const before = inputTextValue(input);
    if (!setTextInputValue(input, want)) return { filled: false, reason: 'set-failed', before };
    let got = inputTextValue(input);
    if (got !== want && got.replace(/\\s+/g, '') !== want.replace(/\\s+/g, '')) {
      if (!setTextInputValue(input, want)) {
        return { filled: false, reason: 'verify-failed', before, got };
      }
      got = inputTextValue(input);
    }
    if (got !== want && got.replace(/\\s+/g, '') !== want.replace(/\\s+/g, '')) {
      return { filled: false, reason: 'verify-failed', before, got, want };
    }
    if (!clickSendButton(scope)) return { filled: false, reason: 'no-save' };
    return { filled: true, method: 'text' };
  }

  function clickSendButton(scope) {
    const deadline = Date.now() + 6000;
    while (Date.now() < deadline) {
      for (const root of discoverRoots(scope)) {
        const save = root.querySelector(
          '.sendMsgbtn_container .send:not(.disabled) .sendMsg, ' +
          '.sendMsgbtn_container .send:not(.disabled) .sendMsg, ' +
          '.sendMsgbtn_container .sendMsg:not(.disabled), ' +
          'button.sendMsg:not([disabled])'
        );
        if (save) {
          save.click();
          return true;
        }
      }
    }
    return false;
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

  function isPlainYearNumber(answer) {
    return /^\\d+\\s*(years?)?$/i.test(String(answer || '').trim());
  }

  function rangeBounds(text) {
    const raw = String(text || '').toLowerCase();
    const rangeM = raw.match(/(\\d+)\\s*[-–]\\s*(\\d+)/);
    if (rangeM) return [parseInt(rangeM[1], 10), parseInt(rangeM[2], 10)];
    return null;
  }

  function optionMatches(label, answer) {
    const c = norm(label);
    const want = norm(answer);
    if (!c || /skip this question/i.test(c)) return false;
    if (c === want || c.replace(/\\s+/g, '') === want.replace(/\\s+/g, '')) return true;
    if (c.includes(want) || want.includes(c)) return true;
    if (c === 'yes' && /\\byes\\b/i.test(answer) && !/\\bno\\b/i.test(answer)) return true;
    if (c === 'no' && /\\bno\\b/i.test(answer)) return true;
    const labelRange = rangeBounds(label);
    const answerRange = rangeBounds(answer);
    if (labelRange && answerRange) {
      return labelRange[0] === answerRange[0] && labelRange[1] === answerRange[1];
    }
    if (isPlainYearNumber(answer)) {
      const wantNum = String(answer).match(/(\\d+)/);
      if (wantNum && valueInRange(parseInt(wantNum[1], 10), label)) return true;
    }
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
  if (scope.querySelector('.dob__input, input[name="day"]')) return true;
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
  return !!container.querySelector('.botItem, .botMsg, .chatbot_Chip, .chipItem, input[type="radio"], [role="radio"], input[type="checkbox"], [role="checkbox"], div.textArea[contenteditable="true"], input.dob__input, input[name="day"]');
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
  const hasVisibleInput = !!visibleTextInput(scope) || !!visibleDobInputs(scope);
  const input = visibleTextInput(scope);
  let placeholder = '';
  let inputMode = '';
  if (input) {
    placeholder = input.getAttribute('data-placeholder') || input.placeholder || '';
    if (input.tagName === 'INPUT') {
      inputMode = (input.type || 'text').toLowerCase();
    } else {
      inputMode = 'contenteditable';
    }
  }
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
    hasDobInput: !!visibleDobInputs(scope),
    placeholder,
    inputMode,
  };
}
"""
)

_FILL_JS = (
    _CHATBOT_HELPERS_JS
    + """
({ answer, answers, allowText, mode, question }) => {
  const scope = chatbotScope();
  if (!scope) return { filled: false, reason: 'no-drawer' };

  if (mode === 'date' || mode === 'text') {
    const dobResult = fillDobInput(scope, answer);
    if (dobResult.filled) return dobResult;
    const textResult = fillTextInput(scope, answer, question || '');
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

  return fillTextInput(scope, answer, question || '');
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


async def _chatbot_user_message_count(page: Page) -> int:
    js = (
        _CHATBOT_HELPERS_JS
        + """
() => {
  const scope = chatbotScope();
  if (!scope) return 0;
  return scope.querySelectorAll('.userItem, li.userItem').length;
}
"""
    )
    try:
        return int(await page.evaluate(js))
    except Exception:
        return 0


async def _chatbot_bot_message_count(page: Page) -> int:
    js = (
        _CHATBOT_HELPERS_JS
        + """
() => {
  const scope = chatbotScope();
  if (!scope) return 0;
  return scope.querySelectorAll('.botItem').length;
}
"""
    )
    try:
        return int(await page.evaluate(js))
    except Exception:
        return 0


async def _clear_chatbot_composer(page: Page) -> None:
    """Clear stale text in the active chatbot composer before filling the next answer."""
    js = (
        _CHATBOT_HELPERS_JS
        + """
() => {
  const scope = chatbotScope();
  if (!scope) return 0;
  return clearActiveComposers(scope);
}
"""
    )
    with contextlib.suppress(Exception):
        await page.evaluate(js)


async def wait_for_question_advance(
    page: Page,
    previous_question: str = "",
    timeout_ms: int = 20000,
    *,
    user_msgs_before: int = 0,
    bot_msgs_before: int = 0,
) -> bool:
    """After answering, wait until the bot posts a new message or the panel closes."""
    prev = re.sub(r"\s+", " ", normalize_question_label(previous_question).lower())
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
  const before = {user_msgs_before};
  const botBefore = {bot_msgs_before};

  const userCount = scope.querySelectorAll('.userItem, li.userItem').length;
  if (userCount <= before) return false;

  const botCount = scope.querySelectorAll('.botItem').length;
  if (botBefore > 0 && botCount <= botBefore) return false;

  const msgs = [...scope.querySelectorAll('.botItem .botMsg, .botMsg')];
  let latest = '';
  let latestRaw = '';
  for (let i = msgs.length - 1; i >= 0; i--) {{
    let text = (msgs[i].innerText || '').replace(/\\s+/g, ' ').trim();
    latestRaw = text;
    text = text.replace(/^the input seems invalid\\.?\\s*/i, '').trim();
    if (text.length > 3) {{
      latest = text.toLowerCase();
      break;
    }}
  }}
  if (/input seems invalid/i.test(latestRaw)) return false;
  if (!latest) return false;
  if (!overlay && !scope.querySelector('.botItem')) return true;
  if (!prev) return botCount > botBefore || latest.length > 0;
  if (latest !== prev) return true;
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


def _is_plain_year_number(answer: str) -> bool:
    return bool(re.fullmatch(r"\d+(?:\s*years?)?", answer.strip(), re.I))


def _chip_matches(chip_label: str, answer: str) -> bool:
    chip = re.sub(r"\s+", " ", chip_label.strip().lower())
    want = re.sub(r"\s+", " ", answer.strip().lower())
    if not chip or _SKIP_CHIP.search(chip):
        return False
    chip = re.sub(r"\s*\(\d[\d,]*\)\s*$", "", chip)
    if chip.replace(" ", "") == want.replace(" ", ""):
        return True
    # A purely numeric answer (e.g. "0", "5") must match a chip ONLY by exact value
    # or numeric range — never by substring. Otherwise "0" matches "7 10 years"
    # because the normalized chip "710years" contains a "0" (from "10").
    want_is_numeric = bool(re.fullmatch(r"\d+(?:\.\d+)?", want))
    if not want_is_numeric:
        want_norm = _normalize_city(want)
        chip_norm = _normalize_city(chip)
        if chip_norm == want_norm or want_norm in chip_norm or chip_norm in want_norm:
            return True
    if chip in ("yes", "no"):
        if chip == "yes" and re.search(r"\byes\b", want) and not re.search(r"\bno\b", want):
            return True
        if chip == "no" and re.search(r"\bno\b", want):
            return True
    chip_range = re.search(r"(\d+)\s*[-–]\s*(\d+)", chip)
    want_range = re.search(r"(\d+)\s*[-–]\s*(\d+)", want)
    if chip_range and want_range:
        return chip_range.group(1) == want_range.group(1) and chip_range.group(2) == want_range.group(2)
    if _is_plain_year_number(answer):
        # A duration/notice chip ("2 Months", "15 Days") is never a years-of-
        # experience band. Without this guard "2" (years) wrongly matches the
        # bare number in "2 Months" and clicks a notice-period option.
        if is_notice_chip_option(chip_label):
            return False
        num = re.search(r"(\d+)", want)
        if num and value_in_chip_range(int(num.group(1)), chip_label):
            return True
    return False


_IMMEDIATE_NOTICE_CHIP = re.compile(
    r"immediate|immediately|join\s*immediately|0\s*days?|available\s*now|right\s*away",
    re.I,
)

_SHORT_NOTICE_CHIP = re.compile(r"15\s*days?\s*or\s*less", re.I)


def _answer_implies_immediate(answer: str) -> bool:
    a = answer.lower().strip()
    if re.fullmatch(r"0(?:\s*days?)?", a):
        return True
    return any(w in a for w in ("immediate", "immediately", "available now", "join immediately"))


def _pick_immediate_notice_chip(chips: list[str]) -> str | None:
    for chip in chips:
        if _IMMEDIATE_NOTICE_CHIP.search(chip):
            return chip
    for chip in chips:
        if _SHORT_NOTICE_CHIP.search(chip):
            return chip
    return None


def _pick_notice_period_chip(answer: str, chips: list[str]) -> str | None:
    a = answer.lower().strip()

    if _answer_implies_immediate(answer):
        immediate = _pick_immediate_notice_chip(chips)
        if immediate:
            return immediate

    if re.fullmatch(r"0(?:\s*days?)?", a):
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

    day_m = re.search(r"(\d+)\s*days?", a)
    if day_m:
        days = int(day_m.group(1))
        if days == 0:
            immediate = _pick_immediate_notice_chip(chips)
            if immediate:
                return immediate
        # A 0-day (immediately available) notice must never map to a month chip —
        # that overstates the notice period. Only days >= 1 may round up to months.
        if days >= 1:
            approx_months = max(1, round(days / 30))
            for chip in chips:
                chip_month_m = re.search(r"(\d+)\s*month", chip, re.I)
                if chip_month_m and int(chip_month_m.group(1)) == approx_months:
                    return chip
        if days <= 15:
            for chip in chips:
                if _SHORT_NOTICE_CHIP.search(chip):
                    return chip
        if 1 <= days <= 30:
            for chip in chips:
                if re.search(r"\b1\s*month", chip, re.I):
                    return chip
        if 1 <= days <= 60:
            for chip in chips:
                if re.search(r"\b2\s*month", chip, re.I):
                    return chip
        if 1 <= days <= 90:
            for chip in chips:
                if re.search(r"\b3\s*month", chip, re.I):
                    return chip
        if days >= 1:
            for chip in chips:
                if re.search(r"more than 3 month", chip, re.I):
                    return chip

    for num in re.findall(r"(\d+)", a):
        days = int(num)
        if days == 0:
            immediate = _pick_immediate_notice_chip(chips)
            if immediate:
                return immediate
        if days > 180:
            continue
        for chip in chips:
            if value_in_chip_range(days, chip):
                return chip
        if days <= 15:
            for chip in chips:
                if _SHORT_NOTICE_CHIP.search(chip):
                    return chip
        # A 0-day (immediately available) notice must never map to a month chip —
        # that overstates the notice period. Only days >= 1 may round up to "1 month";
        # an immediate candidate with only month chips is left unselected (manual).
        if 1 <= days <= 30:
            for chip in chips:
                if re.search(r"\b1\s*month", chip, re.I):
                    return chip

    return None


def _pick_best_chip(answer: str, chips: list[str], question: str) -> str | None:
    if "_" in answer and chips:
        matched = _match_underscore_answer(answer, chips)
        if matched:
            return matched

    for chip in chips:
        if _chip_matches(chip, answer):
            return chip

    q = question.lower()

    if _looks_like_notice_period_question(question):
        picked = _pick_notice_period_chip(answer, chips)
        if picked:
            return picked

    if re.search(r"experience|years?", q) and _is_plain_year_number(answer):
        ym = re.search(r"(\d+)", answer)
        if ym:
            years = int(ym.group(1))
            for chip in chips:
                if is_notice_chip_option(chip):
                    continue
                if value_in_chip_range(years, chip):
                    return chip

    if "city" in q or "select" in q:
        for chip in chips:
            if _chip_matches(chip, answer):
                return chip

    return None


def _looks_like_notice_period_question(label: str) -> bool:
    if _F2F_AVAILABILITY_QUESTION.search(label) and not re.search(r"\bnotice\s*period\b", label, re.I):
        return False
    return bool(_NOTICE_PERIOD_QUESTION.search(label))


def _coerce_blob_answer(label: str, answer: str, options: list[str]) -> str:
    """Map structured config/memory blobs to chip labels."""
    a = answer.strip()
    if not a:
        return a
    al = a.lower()
    opts_are_yes_no = options and _is_yes_no_options(options)

    if _BLOB_ANSWER.search(a) or (";" in a and opts_are_yes_no):
        if re.search(r"\b(no|not willing|cannot relocate|won't)\b", al):
            return _yes_no_option_label(options, "no")
        if re.search(r"\byes\b", al):
            return _yes_no_option_label(options, "yes")

    if _PAN_VALUE.match(a):
        if re.search(r"\bpan\b", label, re.I) and opts_are_yes_no:
            return _yes_no_option_label(options, "yes")
        if opts_are_yes_no and re.search(r"\bpan\b", label, re.I):
            return _yes_no_option_label(options, "yes")

    if opts_are_yes_no and len(a) > 20:
        if re.search(r"\b(residing|relocate|living in|located|location)\b", label, re.I):
            if re.search(r"\b(no|not willing|cannot)\b", al):
                return _yes_no_option_label(options, "no")
            if re.search(
                r"\b(current|native|bengaluru|bangalore|hyderabad|pune|mumbai|delhi|gurgaon|gurugram|noida)\b",
                al,
            ):
                return _yes_no_option_label(options, "yes")

    if opts_are_yes_no and re.search(r"\bwhere are you located\b", label, re.I):
        return _yes_no_option_label(options, "yes")

    return a


def _yes_no_option_label(options: list[str], want: str) -> str:
    want_l = want.strip().lower()
    for opt in options:
        if opt.strip().lower() == want_l:
            return opt.strip()
    return "Yes" if want_l == "yes" else "No"


def _upgrade_kind_for_yes_no_options(
    kind: str,
    label: str,
    options: list[str],
    raw: dict[str, Any] | None,
) -> str:
    opts = _filter_meaningful_options(options)
    if not _is_yes_no_options(opts):
        return kind
    if _looks_like_yes_no_question(label) or re.search(
        r"\b(living in|residing|relocate|willing to|have you|do you|experience in)\b",
        label,
        re.I,
    ):
        return "radio"
    if raw and (raw.get("hasChoice") or raw.get("hasSingleSelect")):
        return "radio"
    return kind


def _coerce_years_answer_to_yes_no(label: str, answer: str, options: list[str]) -> str | None:
    """When a years-numeric answer meets a Yes/No re-ask for the same skill."""
    if not _is_yes_no_options(options):
        return None
    if not re.search(r"\b(experience|years?)\b", label, re.I):
        return None
    years = parse_years_numeric_value(answer)
    if years is None and not re.fullmatch(r"\d+(?:\.\d+)?", answer.strip()):
        return None
    if years is None:
        years = float(answer.strip())
    return _yes_no_option_label(options, "yes" if years > 0 else "no")


def _looks_like_multi_chip_question(label: str, options: list[str]) -> bool:
    opts = _filter_meaningful_options(options)
    if len(opts) < 2 or _is_yes_no_options(opts):
        return False
    if _looks_like_years_range_options(opts):
        return False
    norm = label.lower()
    return bool(re.search(r"\b(tools?|technologies|skills?|frameworks?|which|select)\b", norm) or "," in norm)


def _dom_text_input_only(raw: dict[str, Any] | None) -> bool:
    """Chatbot step has a free-text box and no rendered choice controls."""
    if not raw:
        return False
    return bool(
        raw.get("hasVisibleInput")
        and not raw.get("hasChoice")
        and not raw.get("hasSingleSelect")
        and not raw.get("hasCheckbox")
    )


def _is_chatbot_terminal_message(label: str) -> bool:
    text = label.strip().lower()
    return bool(
        re.search(r"thank you for your", text)
        or re.search(r"thanks for (?:your )?(?:response|time)", text)
        or re.search(r"application (?:has been )?submitted", text)
        or re.search(r"successfully (?:applied|submitted)", text)
        or re.search(r"your responses", text)
    )


def _filter_meaningful_options(options: list[str]) -> list[str]:
    return [
        o for o in options if str(o).strip() and not _SKIP_CHIP.search(str(o)) and not _INVALID_OPTION.search(str(o))
    ]


def _looks_like_ctc_chip_options(options: list[str]) -> bool:
    opts = _filter_meaningful_options(options)
    if not opts:
        return False
    return any(re.search(r"\d|lac|lpa|ctc|annum", o, re.I) for o in opts)


def _options_plausible_for_question(label: str, options: list[str]) -> bool:
    opts = _filter_meaningful_options(options)
    if not opts:
        return True
    joined = " ".join(opts).lower()
    norm = label.lower()
    if re.search(r"\bmale\b|\bfemale\b|self-identify", joined):
        if not re.search(r"\b(gender|diversity|identify)\b", norm):
            return False
    if re.search(r"\bhow many\b.*\bexperience\b", norm):
        if any(re.search(r"\bmale\b|\bfemale\b", o, re.I) for o in opts):
            return False
    if is_numeric_ctc_question(label) and not _looks_like_ctc_chip_options(opts):
        return False
    # A location-only option set ("Other City", a single city) on a question that
    # is not about location is a mis-scraped adjacent dropdown, not this
    # question's choices (e.g. "What is your notice period?" -> ["Other City"]).
    from ..answers.location import _is_location_value_question

    is_location_q = _is_location_value_question(label) or _looks_like_city_select_question(label)

    def _location_like(o: str) -> bool:
        return bool(re.search(r"\bother\s+city\b|^other$", o, re.I)) or _looks_like_city_option(o)

    if not is_location_q and not _is_yes_no_options(opts):
        if all(re.search(r"\bother\s+city\b|^other$", o, re.I) for o in opts):
            return False
        if _looks_like_notice_period_question(label) or re.search(r"\bhow many\b.*\bexperience\b", norm):
            if all(_location_like(o) for o in opts):
                return False
    # Notice-period chips (months/days/weeks) on a years-of-experience question
    # are a mis-scraped/desynced control — "2 years" must never map to "2 Months".
    exp_years_q = bool(
        re.search(r"\b(years?|yrs)\b.*\bexperience\b", norm)
        or re.search(r"\bexperience\b.*\b(years?|yrs)\b", norm)
        or re.search(r"\bhow many\b.*\b(years?|experience)\b", norm)
    )
    if exp_years_q and not _looks_like_notice_period_question(label):
        if all(is_notice_chip_option(o) for o in opts):
            return False
    return True


def _is_gender_options(options: list[str]) -> bool:
    """Live options form a gender picker (Male/Female[/Other/…])."""
    opts = [o.strip().lower() for o in _filter_meaningful_options(options)]
    if not opts:
        return False
    if not any(re.fullmatch(r"male|female", o) for o in opts):
        return False
    return all(
        re.fullmatch(
            r"male|female|other|others|transgender|non-?binary|" r"prefer not to (say|disclose).*",
            o,
        )
        for o in opts
    )


def _is_other_city_only_options(options: list[str]) -> bool:
    """Live options are a city/location picker (only "Other City"/city names)."""
    opts = _filter_meaningful_options(options)
    if not opts:
        return False
    if not any(re.search(r"\bother\s*city\b|^other$", o, re.I) for o in opts):
        return False
    return all(re.search(r"\bother\s*city\b|^other$", o, re.I) or _looks_like_city_option(o) for o in opts)


def _looks_like_years_range_options(options: list[str]) -> bool:
    opts = _filter_meaningful_options(options)
    if not opts:
        return False
    return any(re.search(r"\d+\s*[-]\s*\d+|<\s*\d+|>\s*\d+|\+\s*years?|\byears?\b", o, re.I) for o in opts)


def _should_use_text_input_only(
    kind: str,
    label: str,
    answer_options: list[str],
    raw: dict[str, Any] | None,
    input_type: str,
) -> bool:
    opts = _filter_meaningful_options(answer_options)
    if _is_yes_no_options(opts):
        return False
    if _looks_like_yes_no_question(label) and _is_yes_no_options(opts):
        return False
    if _looks_like_notice_period_question(label) and opts and not _dom_text_input_only(raw):
        return False
    if _looks_like_years_range_options(opts):
        return False
    from ..answers.location import _is_location_value_question

    if _is_location_value_question(label) and not _is_yes_no_options(opts):
        return True
    if re.search(r"\bwhere are you located\b", label, re.I) and not _is_yes_no_options(opts):
        return True
    if is_numeric_ctc_question(label) or input_type == "ctc_numeric":
        if not _looks_like_ctc_chip_options(opts):
            return True
        if raw and raw.get("hasVisibleInput"):
            return True
        if kind in ("text", "short_text", "input", "number", "ctc_numeric"):
            return True
        return True
    if _dom_text_input_only(raw):
        return True
    return kind in (
        "text",
        "short_text",
        "input",
        "textarea",
        "years_numeric",
        "number",
    )


async def _chatbot_flow_complete(page: Page) -> bool:
    if not await chatbot_is_open(page):
        return True
    try:
        raw = await page.evaluate(_DISCOVER_JS)
    except Exception:
        return False
    if not raw:
        return False
    question = normalize_question_label(str(raw.get("question") or "").strip())
    return _is_chatbot_terminal_message(question)


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


def _multi_option_targets(answer: str, options: list[str]) -> list[str]:
    """A comma/semicolon-separated answer whose parts match 2+ distinct options is
    a multi-select pick (e.g. 'Flask, FastAPI' -> ['Flask', 'FastAPI']). Returns the
    matched options, or [] when it isn't clearly multi-select. Label-agnostic so it
    works even when the question doesn't contain the word 'experience'."""
    opts = _filter_meaningful_options(options)
    if len(opts) < 2:
        return []
    parts = _parse_multi_answer(answer)
    if len(parts) < 2:
        return []
    matched: list[str] = []
    for part in parts:
        best = _pick_best_chip(part, opts, "")
        if best and best not in matched:
            matched.append(best)
    return matched if len(matched) >= 2 else []


def _targets_for_answer(answer: str, options: list[str], label: str) -> list[str]:
    if _looks_like_skill_checkbox_question(label, options):
        skill_targets = _skill_checkbox_targets(answer, options)
        if skill_targets:
            return skill_targets
        if re.fullmatch(r"\d+(?:\.\d+)?", answer.strip()):
            return []

    if not _looks_like_city_select_question(label):
        multi = _multi_option_targets(answer, options)
        if multi:
            return multi

    if options and "_" in answer:
        matched = _match_underscore_answer(answer, options)
        if matched:
            return [matched]

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
    if _YES_NO_EXPLICIT.search(label):
        return True
    if re.search(r"\b(how many|years?\s+of\s+experience|\d+\+\s+years)\b", label, re.I):
        return False
    if label.strip().endswith("?") and _YES_NO_QUESTION.search(label):
        return True
    return bool(re.search(r"\b(residing in .+\?|relocate|willing to)\b", label, re.I))


def _is_yes_no_options(options: list[str]) -> bool:
    opts = {o.strip().lower() for o in options if o.strip()}
    return bool(opts) and opts <= {"yes", "no"}


def _looks_like_skill_checkbox_question(label: str, options: list[str]) -> bool:
    opts = _filter_meaningful_options(options)
    if len(opts) < 2:
        return False
    if _is_yes_no_options(opts):
        return False
    if all(_looks_like_city_option(o) for o in opts):
        return False
    if _looks_like_years_range_options(opts):
        return False
    norm = label.lower()
    if not re.search(r"\bexperience\b", norm):
        return False
    if re.search(r"\bhow many\b", norm):
        short = sum(1 for o in opts if len(o.strip()) <= 24 and not re.search(r"\d", o))
        return short >= 2
    return False


def _skill_checkbox_targets(answer: str, options: list[str]) -> list[str]:
    wants = _parse_multi_answer(answer)
    if not wants and re.fullmatch(r"\d+(?:\.\d+)?", answer.strip()):
        return []
    if not wants:
        wants = [answer.strip()]
    targets: list[str] = []
    for part in wants:
        best = _pick_best_chip(part, options, "")
        if best:
            targets.append(best)
    return targets


def _match_underscore_answer(answer: str, options: list[str]) -> str | None:
    a = answer.strip().lower().replace("_", " ")
    if not a:
        return None
    for opt in options:
        opt_l = opt.lower()
        if a in opt_l or opt_l in a:
            return opt
    key_tokens = set(re.findall(r"[a-z0-9]+", a))
    if len(key_tokens) < 2:
        return None
    best, best_score = None, 0
    for opt in options:
        opt_tokens = set(re.findall(r"[a-z0-9]+", opt.lower()))
        score = len(key_tokens & opt_tokens)
        if score > best_score:
            best_score, best = score, opt
    return best if best_score >= max(2, len(key_tokens) // 2) else None


def _is_checkbox_options(raw: dict[str, Any] | None, options: list[str]) -> bool:
    if not raw:
        return False
    if raw.get("hasCheckbox"):
        return True
    kinds = raw.get("kinds") or []
    return any("checkbox" in str(kind) for kind in kinds)


def _choice_only_question(label: str, options: list[str], raw: dict[str, Any] | None = None) -> bool:
    if _dom_text_input_only(raw):
        return False
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
    return bool(re.search(r"\bselect\b", label, re.I) and options)


def _wait_attempts_for(label: str, *, has_options: bool = False) -> int:
    if has_options:
        return 3
    if _is_date_field(label):
        return 16
    if (
        _looks_like_yes_no_question(label)
        or _looks_like_city_select_question(label)
        or _looks_like_notice_period_question(label)
    ):
        return 6
    return 6


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


async def _fill_text_playwright(page: Page, answer: str, *, question: str = "") -> bool:
    """Playwright fallback when JS fill cannot find the chatbot text input."""
    await _clear_chatbot_composer(page)
    scope = page.locator(_CHATBOT_SCOPE).last
    if await scope.count() == 0:
        scope = page.locator("body")

    footer_selectors = (
        ".footerInputBoxWrapper div.textArea[contenteditable]",
        "#userInput__ div.textArea[contenteditable]",
        ".chatbot_InputContainer div.textArea[contenteditable]",
        ".chatbot_SendMessageContainer div.textArea[contenteditable]",
    )
    generic_selectors = (
        'div.textArea[contenteditable="true"]',
        "div.textArea[contenteditable]",
        '[contenteditable="true"]',
        "textarea:visible",
        'input[type="text"]:visible',
        'input[type="date"]:visible',
    )
    ordered = footer_selectors + generic_selectors
    for sel in ordered:
        loc = scope.locator(sel)
        count = await loc.count()
        if count == 0:
            continue
        target = loc.last
        try:
            if not await target.is_visible():
                continue
            await target.scroll_into_view_if_needed()
            await target.click(timeout=3000)
            await target.press("Meta+a")
            await target.press("Backspace")
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


def _parse_dob_parts(answer: str) -> tuple[str, str, str] | None:
    text = _normalize_dob_answer(answer)
    match = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", text)
    if not match:
        return None
    day, month, year = match.groups()
    return f"{int(day):02d}", f"{int(month):02d}", year


async def _click_chatbot_save(page: Page) -> bool:
    scope = page.locator(_CHATBOT_SCOPE).last
    if await scope.count() == 0:
        scope = page.locator("body")
    for sel in (
        ".sendMsgbtn_container .send:not(.disabled) .sendMsg",
        ".sendMsgbtn_container .sendMsg:not(.disabled)",
        ".send:not(.disabled) .sendMsg",
    ):
        btn = scope.locator(sel)
        try:
            if await btn.count() == 0:
                continue
            await btn.first.wait_for(state="visible", timeout=2500)
            await btn.first.click(timeout=2000)
            return True
        except PlaywrightTimeout:
            continue
    return False


async def _fill_dob_triplet_playwright(page: Page, answer: str) -> bool:
    parts = _parse_dob_parts(answer)
    if not parts:
        return False
    day_s, month_s, year_s = parts
    scope = page.locator(_CHATBOT_SCOPE).last
    if await scope.count() == 0:
        scope = page.locator("body")

    day_inp = scope.locator('input.dob__input.day, input[name="day"]')
    month_inp = scope.locator('input.dob__input.month, input[name="month"]')
    year_inp = scope.locator('input.dob__input.year, input[name="year"]')
    if await day_inp.count() == 0:
        return False

    for locator, value in (
        (day_inp.first, day_s),
        (month_inp.first, month_s),
        (year_inp.first, year_s),
    ):
        await locator.click(timeout=3000)
        await locator.fill(value)
        await locator.dispatch_event("input")
        await locator.dispatch_event("change")

    return await _click_chatbot_save(page)


async def _fill_date_field(page: Page, label: str, answer: str) -> bool:
    """Fill Naukri DOB triplet (DD / MM / YYYY) or fallback text input."""
    answer = _normalize_dob_answer(answer)

    for _ in range(3):
        result = await page.evaluate(
            _FILL_JS,
            {"answer": answer, "answers": [answer], "allowText": True, "mode": "date"},
        )
        if isinstance(result, dict) and result.get("filled"):
            method = result.get("method", "date")
            logger.info("Naukri chatbot: filled date field %r via %s", label[:40], method)
            return True
        await page.wait_for_timeout(400)

    if await _fill_dob_triplet_playwright(page, answer):
        logger.info("Naukri chatbot: filled date field via Playwright DOB inputs %r", label[:40])
        return True

    if await _fill_text_playwright(page, answer):
        logger.info("Naukri chatbot: filled date field via Playwright text %r", label[:40])
        return True

    return False


def _dedupe_options(options: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for opt in options:
        text = str(opt).strip()
        if not text or text in seen or _SKIP_CHIP.search(text) or _INVALID_OPTION.search(text):
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
    if is_pincode_field(question):
        return "text", []

    all_opts = _dedupe_options(checkbox_opts + choice_opts)

    if _is_yes_no_options(all_opts):
        yes_no = [o for o in all_opts if o.strip().lower() in ("yes", "no")]
        return "radio", yes_no or list(all_opts)

    if checkbox_opts and _is_yes_no_options(checkbox_opts):
        return "radio", checkbox_opts

    if (
        choice_opts
        and raw
        and raw.get("hasSingleSelect")
        and len(choice_opts) >= 2
        and not _looks_like_city_select_question(question)
    ):
        return "radio", choice_opts

    city_opts = [o for o in all_opts if _looks_like_city_option(o)]

    if _looks_like_city_select_question(question):
        if len(city_opts) >= 1:
            return "checkbox_group", city_opts
        if len(all_opts) >= 2 and not _is_yes_no_options(all_opts):
            return "checkbox_group", [o for o in all_opts if not _SKIP_CHIP.search(o)]

    if checkbox_opts and (len(checkbox_opts) > 1 or _looks_like_city_select_question(question)):
        if not _is_yes_no_options(checkbox_opts) and not (raw and raw.get("hasSingleSelect") and choice_opts):
            return "checkbox_group", checkbox_opts

    if choice_opts:
        return "radio", choice_opts

    if raw and raw.get("hasSingleSelect"):
        return "radio", []

    if raw and raw.get("hasCheckbox") and _looks_like_city_select_question(question):
        return "checkbox_group", []

    if raw and raw.get("hasVisibleInput") and not raw.get("hasChoice"):
        if _looks_like_yes_no_question(question) and choice_opts and _is_yes_no_options(choice_opts):
            return "radio", choice_opts
        return "text", []

    if _is_date_field(question) or (raw and raw.get("hasDobInput")):
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
    attempts: int = 8,
    poll_ms: int = 200,
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
            if kind == "checkbox_group" and (raw.get("hasCheckbox") or _looks_like_city_select_question(question)):
                pw_opts = await _discover_checkbox_options_playwright(page)
                if pw_opts:
                    return kind, pw_opts, raw
                best_kind, best_options = kind, options
            elif kind == "radio" and (raw.get("hasSingleSelect") or raw.get("hasChoice")):
                best_kind = kind
            elif kind == "text" and raw.get("hasVisibleInput"):
                choice_opts, _, _ = _split_discovered_options(raw)
                if _looks_like_yes_no_question(question) and choice_opts and _is_yes_no_options(choice_opts):
                    return "radio", choice_opts, raw
                best_kind, best_options = kind, []
                if not raw.get("hasChoice"):
                    return kind, [], raw
        await page.wait_for_timeout(poll_ms)
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
                    name = (
                        await box.evaluate(
                            """el => {
                          const id = el.id;
                          if (id) {
                            const lbl = document.querySelector(`label[for="${CSS.escape(id)}"]`);
                            if (lbl) return (lbl.innerText || '').trim();
                          }
                          const wrap = el.closest('label');
                          return wrap ? (wrap.innerText || '').trim() : '';
                        }"""
                        )
                    ).strip()
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


async def _discover_choice_options_playwright(page: Page) -> list[str]:
    """Last-resort scrape of live chip/radio/checkbox option labels.

    JS discovery occasionally flags a choice control (``hasSingleSelect``) without
    capturing the option *labels* (async render / throttled tab). Reading the live
    elements directly lets us confirm there really are selectable options before
    committing to a fabricated Yes/No target that can never be clicked.
    """
    scope = page.locator(_CHATBOT_SCOPE).last
    if await scope.count() == 0:
        return []
    options: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        text = re.sub(r"\s+", " ", (name or "").strip())
        if not text or text in seen or _SKIP_CHIP.search(text) or _INVALID_OPTION.search(text):
            return
        seen.add(text)
        options.append(text)

    async def _label_text(loc) -> str:
        """inner_text first, then text_content — async-rendered / off-screen chips
        return '' from inner_text but still carry their label in text_content."""
        try:
            text = (await loc.inner_text()).strip()
        except Exception:
            text = ""
        if not text:
            try:
                text = (await loc.text_content() or "").strip()
            except Exception:
                text = ""
        return text

    try:
        chip_loc = scope.locator(".chatbot_Chip, .chipItem")
        for i in range(await chip_loc.count()):
            _add(await _label_text(chip_loc.nth(i)))
        for role in ("radio", "checkbox"):
            boxes = scope.get_by_role(role)
            for i in range(await boxes.count()):
                box = boxes.nth(i)
                name = (await box.get_attribute("aria-label") or "").strip()
                if not name:
                    try:
                        name = (
                            await box.evaluate(
                                """el => {
                                  const id = el.id;
                                  if (id) {
                                    const lbl = document.querySelector(`label[for="${CSS.escape(id)}"]`);
                                    if (lbl) return (lbl.innerText || lbl.textContent || '').trim();
                                  }
                                  const wrap = el.closest('label');
                                  return wrap ? (wrap.innerText || wrap.textContent || '').trim() : '';
                                }"""
                            )
                        ).strip()
                    except Exception:
                        name = ""
                if not name:
                    name = (await box.get_attribute("value") or "").strip()
                _add(name)
        for sel in (
            "label.ssrc__label",
            ".singleselect-radiobutton label",
            ".singleselect-radiobutton .ssrc__label",
            ".ssrc__label",
        ):
            loc = scope.locator(sel)
            for i in range(await loc.count()):
                _add(await _label_text(loc.nth(i)))
    except Exception as exc:
        logger.debug("Playwright choice discovery failed: %s", exc)
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
            box = scope.get_by_role("checkbox", name=re.compile(re.escape(pattern), re.I))
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
        ".sendMsgbtn_container .sendMsg:not(.disabled), .sendMsgbtn_container .send:not(.disabled) .sendMsg"
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
        # The option chips/radios can still be (re)rendering when discovery already
        # reported them (heavy concurrency / throttled tabs). Without this wait the
        # click fast-fails in a few ms because the elements aren't in the live DOM
        # yet — wait for them to attach before attempting to click.
        with contextlib.suppress(Exception):
            await scope.locator(
                ".chatbot_Chip, .chipItem, .singleselect-radiobutton input[type='radio'], "
                ".ssrc__radio, input[type='radio'], [role='radio']"
            ).first.wait_for(state="attached", timeout=4000)

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

        patterns = [target]
        if target[:1].islower():
            patterns.append(target.capitalize())
        if target[:1].isupper():
            patterns.append(target.lower())
        for pattern in dict.fromkeys(p for p in patterns if p):
            radio = scope.get_by_role("radio", name=re.compile(re.escape(pattern), re.I))
            radio_count = await radio.count()
            for i in range(min(radio_count, 4)):
                el = radio.nth(i)
                try:
                    await el.scroll_into_view_if_needed()
                    await el.click(timeout=5000)
                    await _click_save_if_present(page)
                    return True
                except PlaywrightTimeout:
                    try:
                        await el.click(timeout=5000, force=True)
                        await _click_save_if_present(page)
                        return True
                    except PlaywrightTimeout:
                        pass
                try:
                    parent = el.locator("xpath=ancestor::label[1]")
                    if await parent.count() > 0:
                        await parent.first.scroll_into_view_if_needed()
                        await parent.first.click(timeout=5000)
                        await _click_save_if_present(page)
                        return True
                except PlaywrightTimeout:
                    pass
            for sel in (
                ".radio-container",
                ".ssrc__radio",
                ".singleselect-radiobutton",
                "label.ssrc__label",
            ):
                loc = scope.locator(sel).filter(has_text=re.compile(re.escape(pattern), re.I))
                if await loc.count() > 0:
                    try:
                        await loc.first.scroll_into_view_if_needed()
                        await loc.first.click(timeout=5000)
                        await _click_save_if_present(page)
                        return True
                    except PlaywrightTimeout:
                        try:
                            await loc.first.click(timeout=5000, force=True)
                            await _click_save_if_present(page)
                            return True
                        except PlaywrightTimeout:
                            pass
            label = scope.locator("label.ssrc__label, label").filter(has_text=re.compile(re.escape(pattern), re.I))
            if await label.count() > 0:
                try:
                    await label.first.scroll_into_view_if_needed()
                    await label.first.click(timeout=5000)
                    await _click_save_if_present(page)
                    return True
                except PlaywrightTimeout:
                    pass
    except Exception as exc:
        logger.debug("Playwright option click failed for %r: %s", target[:40], exc)
    return False


async def _click_yes_no_playwright(page: Page, want: str, options: list[str] | None = None) -> bool:
    """Dedicated Yes/No chip/radio click — handles YES/NO casing."""
    want_l = want.strip().lower()
    if want_l in ("yes", "y", "true", "1"):
        patterns = ["yes", "YES", "Yes"]
    elif want_l in ("no", "n", "false", "0"):
        patterns = ["no", "NO", "No"]
    else:
        patterns = [want.strip()]
    if options:
        for opt in options:
            if opt.strip().lower() == want_l:
                patterns.insert(0, opt.strip())
    for pattern in dict.fromkeys(p for p in patterns if p):
        if await _click_option_playwright(page, pattern):
            return True
        if await _click_chip(page, pattern):
            return True
    return False


async def _fill_multi_chip_answer(page: Page, answer: str, options: list[str], label: str) -> bool:
    """Click multiple chips for comma-separated tool/skill answers."""
    parts = _parse_multi_answer(answer) or [answer.strip()]
    if len(parts) <= 1 and "," not in answer:
        return False
    clicked_any = False
    for part in parts:
        if not part:
            continue
        best = _pick_best_chip(part, options, label) if options else part
        target = best or part
        if await _click_chip(page, target) or await _click_option_playwright(page, target):
            clicked_any = True
    if clicked_any:
        await _click_save_if_present(page)
    return clicked_any


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


async def _fill_other_city_location(page: Page, city: str, options: list[str], label: str) -> bool:
    """Naukri location prompts that only offer an 'Other City' radio: select it, then
    type the real city into the text input it reveals. Reuses the tested choice + text
    fill paths so it degrades to a normal text fill if no input appears."""
    other = next(
        (o for o in options if re.search(r"\bother\s*city\b|^other$", o.strip(), re.I)),
        None,
    )
    if not other or not city.strip():
        return False
    selected = await page.evaluate(
        _FILL_JS,
        {"answer": other, "answers": [other], "allowText": False, "mode": "choice"},
    )
    if not (isinstance(selected, dict) and selected.get("filled")):
        if not (await _click_chip(page, other) or await _click_option_playwright(page, other)):
            return False
    await page.wait_for_timeout(400)
    typed = await page.evaluate(
        _FILL_JS,
        {
            "answer": city,
            "answers": [city],
            "allowText": True,
            "mode": "text",
            "question": label,
        },
    )
    if isinstance(typed, dict) and typed.get("filled"):
        return True
    return await _fill_text_playwright(page, city, question=label)


_RADIO_SELECTED_JS = """
(targets) => {
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const wants = (targets || []).map(norm).filter(Boolean);
  if (!wants.length) return false;
  const matches = (text) => {
    const t = norm(text);
    if (!t) return false;
    return wants.some((w) => t === w || t.startsWith(w + ' ') || w === t);
  };
  const scopes = [
    document.querySelector('#desktopChatBotContainer'),
    document.querySelector('._chatBotContainer'),
    document.querySelector('.chatbot_Drawer'),
  ].filter(Boolean);
  const scope = scopes[0] || document;

  // Checked radio inputs whose label text matches a desired target.
  for (const input of scope.querySelectorAll('input[type="radio"]')) {
    const on = input.checked || input.getAttribute('aria-checked') === 'true';
    if (!on) continue;
    let text = '';
    if (input.id) {
      const lab = scope.querySelector(`label[for="${input.id}"]`);
      if (lab) text = lab.textContent || '';
    }
    if (!text) {
      const wrap = input.closest('label, .radio-container, .ssrc__radio, .singleselect-radiobutton');
      if (wrap) text = wrap.textContent || '';
    }
    if (matches(text)) return true;
  }

  // Selected/active chips.
  const chipSel = '.chatbot_Chip, .chipItem, [class*="chip" i]';
  for (const chip of scope.querySelectorAll(chipSel)) {
    const cls = (chip.className || '').toLowerCase();
    const selected = /selected|active|checked|chosen/.test(cls)
      || chip.getAttribute('aria-selected') === 'true'
      || chip.getAttribute('aria-checked') === 'true';
    if (selected && matches(chip.textContent)) return true;
  }
  return false;
}
"""


async def _desired_option_already_selected(page: Page, targets: list[str]) -> bool:
    """True when the chatbot already shows one of the desired options as selected.

    Handles the case where the bot re-asks a question we already answered — we
    should advance rather than abandon the whole application.
    """
    clean = [t.strip() for t in targets if t and t.strip()]
    if not clean:
        return False
    try:
        return bool(await page.evaluate(_RADIO_SELECTED_JS, clean))
    except Exception:
        return False


def _coerce_chip_answer(question: dict[str, Any], answer: str) -> str:
    """Map saved answers to Yes/No chips when the chatbot expects chips."""
    label = str(question.get("label", ""))
    label_l = label.lower()
    options = [str(o).strip() for o in (question.get("options") or []) if str(o).strip()]
    answer = _coerce_blob_answer(label, answer, options)
    a = answer.strip().lower()

    if _F2F_AVAILABILITY_QUESTION.search(label) and _is_yes_no_options(options):
        if re.search(r"\b(yes|available|can attend|will attend)\b", a):
            return _yes_no_option_label(options, "yes")
        if re.search(r"\b(no|cannot|not available)\b", a):
            return _yes_no_option_label(options, "no")
        if re.search(r"\d+\s*days?", a):
            return _yes_no_option_label(options, "yes")

    years_yes_no = _coerce_years_answer_to_yes_no(label, answer, options)
    if years_yes_no:
        return years_yes_no

    if _looks_like_notice_period_question(label) and options:
        picked = _pick_notice_period_chip(answer, options)
        if picked:
            return picked

    if not options and _looks_like_yes_no_question(label_l):
        if a in ("yes", "y", "true", "1"):
            return "Yes"
        if a in ("no", "n", "false", "0"):
            return "No"
        if any(city in label for city in ("bengaluru", "bangalore", "hyderabad", "pune", "mumbai", "chennai", "delhi")):
            if a in (
                "bengaluru",
                "bangalore",
                "hyderabad",
                "pune",
                "mumbai",
                "delhi",
                "ncr",
                "gurgaon",
                "noida",
                "yes",
            ):
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
                if re.search(r"\bmilitary spouse\b", str(opt), re.I) and not re.search(r"\bnot\b", str(opt), re.I):
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
                (o for o in options if re.search(r"\byes\b", o, re.I) and not re.search(r"\b(not|never)\b", o, re.I)),
                None,
            )
            if picked:
                return picked

    if not options:
        return answer.strip()

    opts_lower = {o.lower() for o in options}
    if opts_lower <= {"yes", "no"}:
        a = answer.strip().lower()
        label_l = label.lower()
        if a in ("yes", "y", "true", "1"):
            return _yes_no_option_label(options, "yes")
        if a in ("no", "n", "false", "0"):
            return _yes_no_option_label(options, "no")
        if _PAN_VALUE.match(answer.strip()) and re.search(r"\bpan\b", label_l):
            return _yes_no_option_label(options, "yes")
        if _looks_like_yes_no_question(label_l) and re.search(r"\b(residing|relocate|living in|willing to)\b", label_l):
            if re.search(r"\b(no|not willing|cannot relocate|won't)\b", a):
                return next((o for o in options if o.lower() == "no"), "No")
            if re.search(r"\b(current|native)\b", a):
                for city in (
                    "bengaluru",
                    "bangalore",
                    "hyderabad",
                    "pune",
                    "mumbai",
                    "chennai",
                    "delhi",
                    "gurgaon",
                    "gurugram",
                    "noida",
                ):
                    if city in label_l and city in a:
                        return next((o for o in options if o.lower() == "yes"), "Yes")
                return next((o for o in options if o.lower() == "yes"), "Yes")
        if len(answer.strip()) > 24:
            if re.search(r"\byes\b", a) and not re.search(r"\bno\b", a):
                return next((o for o in options if o.lower() == "yes"), "Yes")
            if re.search(r"\bno\b", a):
                return next((o for o in options if o.lower() == "no"), "No")
        if any(city in label_l for city in ("bengaluru", "bangalore", "hyderabad", "pune", "mumbai")) and any(
            city in a
            for city in (
                "bengaluru",
                "bangalore",
                "hyderabad",
                "pune",
                "mumbai",
                "delhi",
                "ncr",
                "gurgaon",
                "gurugram",
                "noida",
            )
        ):
            return next((o for o in options if o.lower() == "yes"), answer)
    return answer.strip()


def _effective_options(
    options: list[str],
    label: str,
    raw: dict[str, Any] | None = None,
) -> list[str]:
    if options:
        return options
    if _dom_text_input_only(raw):
        return []
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


async def discover_naukri_chatbot_questions(
    page: Page,
    *,
    config: AppConfig | None = None,
) -> list[dict[str, Any]]:
    raw = await page.evaluate(_DISCOVER_JS)
    if not raw:
        return []

    question = normalize_question_label(str(raw.get("question") or "").strip())
    if not question or is_generic_question_label(question):
        return []
    if _is_chatbot_terminal_message(question):
        logger.info("Naukri chatbot: terminal screen (%s)", question[:50])
        return []
    if not _is_naukri_chatbot_question(question):
        # A long recruiter statement can still be answerable when the panel renders
        # real, selectable options (e.g. "This role needs 5-9 yrs … [Yes][No]").
        # Keep it only when the DOM exposes *meaningful* scraped option labels — not
        # a bare hasSingleSelect (phantom radio with no labels) and not skip-only
        # chips — so a pure intro statement is still discarded, but a fused
        # statement+choice prompt reaches the normal resolver (incl. the LLM
        # option-picker) instead of getting stuck.
        choice_opts, checkbox_opts, _ = _split_discovered_options(raw)
        meaningful = _filter_meaningful_options(choice_opts + checkbox_opts)
        if not meaningful:
            logger.debug("Skipping non-question chatbot text: %s", question[:80])
            return []
        logger.info(
            "Naukri chatbot: statement with %d selectable option(s) — treating as answerable: %s",
            len(meaningful),
            question[:60],
        )

    poll_ms = 200
    if config is not None:
        poll_ms = config.application.platform_delays.naukri_chip_poll_ms

    wait_attempts = _wait_attempts_for(question)
    kind, options, raw = await _fetch_answer_options(page, question=question, attempts=wait_attempts, poll_ms=poll_ms)
    if not options:
        kind, options, _ = _analyze_chatbot_state(raw, question)

    field: dict[str, Any] = {
        "kind": kind,
        "label": question,
        "index": 0,
        "platform": "naukri",
    }
    if options:
        field["options"] = options
    if raw:
        placeholder = str(raw.get("placeholder") or "").strip()
        if placeholder:
            field["placeholder"] = placeholder
        input_mode = str(raw.get("inputMode") or "").strip()
        if input_mode:
            field["input_mode"] = input_mode
        if raw.get("hasVisibleInput"):
            field["hasVisibleInput"] = bool(raw.get("hasVisibleInput"))
        if raw.get("hasDobInput"):
            field["hasDobInput"] = bool(raw.get("hasDobInput"))
        field["discover_raw"] = raw
    field = enrich_field_for_llm(field)

    opt_preview = ", ".join(options[:5])
    if len(options) > 5:
        opt_preview += f" (+{len(options) - 5} more)"
    logger.info(
        "Naukri chatbot question [%s/%s]: %s%s",
        kind,
        field.get("input_type", kind),
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
    if _looks_like_yes_no_question(label):
        return False
    if _is_optional_text_field(label) and a in ("yes", "no"):
        return True
    return bool(_is_optional_text_field(label) and re.fullmatch(r"\d+", a or ""))


async def _setup_naukri_chatbot_fill(
    page: Page, label: str, config: AppConfig | None
) -> tuple[
    int,
    int,
    int,
    callable,
]:
    await _clear_chatbot_composer(page)
    bot_msgs_before = await _chatbot_bot_message_count(page)
    user_msgs_before = await _chatbot_user_message_count(page)

    async def _advanced(timeout_ms: int = 20000) -> bool:
        return await wait_for_question_advance(
            page,
            previous_question=label,
            timeout_ms=timeout_ms,
            user_msgs_before=user_msgs_before,
            bot_msgs_before=bot_msgs_before,
        )

    poll_ms = 200
    if config is not None:
        poll_ms = config.application.platform_delays.naukri_chip_poll_ms

    return bot_msgs_before, user_msgs_before, poll_ms, _advanced


async def _fetch_naukri_answer_options(
    page: Page,
    label: str,
    question: dict[str, Any],
    poll_ms: int,
) -> tuple[str, list[str], dict[str, Any] | None]:
    pre_opts = [
        str(c).strip() for c in (question.get("options") or []) if str(c).strip() and not _SKIP_CHIP.search(str(c))
    ]
    kind = str(question.get("kind") or "text")
    raw = question.get("discover_raw")
    choice_kinds = {"radio", "checkbox_group", "single_choice", "multi_choice"}
    if pre_opts and kind in choice_kinds:
        answer_options = pre_opts
    else:
        wait_attempts = _wait_attempts_for(label, has_options=bool(pre_opts))
        kind, answer_options, fetched_raw = await _fetch_answer_options(
            page, question=label, attempts=wait_attempts, poll_ms=poll_ms
        )
        if fetched_raw:
            raw = fetched_raw
        if not answer_options:
            answer_options = pre_opts
    if not kind or kind == "text":
        kind = str(question.get("kind", kind or "text"))
    return kind, answer_options, raw


async def _handle_desynced_naukri_question(
    page: Page,
    label: str,
    answer: str,
    answer_options: list[str],
    config: AppConfig | None,
    _advanced: callable,
) -> bool | None:
    """Handle cases where label doesn't match options (gender, location)."""
    if _is_gender_options(answer_options):
        from ..answers.config_answers import gender_answer

        gender = gender_answer(config) if config else None
        if gender:
            gtargets = _targets_for_answer(gender, answer_options, "gender")
            gtarget = gtargets[0] if gtargets else gender
            gres = await page.evaluate(
                _FILL_JS,
                {
                    "answer": gtarget,
                    "answers": gtargets or [gtarget],
                    "allowText": False,
                    "mode": "choice",
                },
            )
            if (isinstance(gres, dict) and gres.get("filled")) or (await _click_option_playwright(page, gtarget)):
                logger.info(
                    "Naukri chatbot: answered desynced gender question with %r (stale label was %r)",
                    gtarget[:20],
                    label[:40],
                )
                return await _advanced()
        raise CannotAnswerTruthfully(label, reason="gender picker present but gender not configured/clickable")
    if _is_other_city_only_options(answer_options):
        from ..answers.config_answers import location_answer as _location_answer_from_config

        city = _location_answer_from_config(config, "current location") if config else None
        if city and await _fill_other_city_location(page, city, answer_options, label):
            logger.info(
                "Naukri chatbot: answered desynced location question (Other City + %r; stale label was %r)",
                city[:30],
                label[:40],
            )
            return await _advanced()
        raise CannotAnswerTruthfully(label, reason="Other City location picker but no city configured")
    return None


def _apply_zero_experience_guard(
    label: str,
    answer: str,
    meaningful_opts: list[str],
    text_input_only: bool,
) -> None:
    """Apply zero-experience guard to prevent overstating experience."""
    from ..answers.location import _is_location_value_question as _is_loc_value_q

    zero_guard_applies = (
        bool(meaningful_opts)
        and not text_input_only
        and not _looks_like_notice_period_question(label)
        and not is_numeric_ctc_question(label)
        and not is_pincode_field(label)
        and not _is_date_field(label)
        and not _is_loc_value_q(label)
    )
    if zero_guard_applies:
        from ..llm_answers import _numeric_answer_value, _option_represents_zero

        if (
            _numeric_answer_value(answer) == 0
            and not any(_option_represents_zero(o) for o in meaningful_opts)
            and not any(_chip_matches(o, answer) for o in meaningful_opts)
        ):
            logger.info(
                "Naukri chatbot: answer '0' (no experience) has no truthful option "
                "for %s (options: %s) — honest skip, not a technical failure",
                label[:50],
                ", ".join(meaningful_opts[:5]),
            )
            raise CannotAnswerTruthfully(label, reason="no zero-experience option (would overstate)")


async def _fill_naukri_checkbox_mode(
    page: Page,
    label: str,
    answer: str,
    answer_options: list[str],
    config: AppConfig | None,
    _advanced: callable,
    raw: dict[str, Any] | None,
) -> bool | None:
    if not answer_options:
        answer_options = await _discover_checkbox_options_playwright(page)
    if not answer_options:
        answer_options = _filter_meaningful_options(await _discover_choice_options_playwright(page))
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
        return await _advanced()
    if await _click_checkbox_playwright(page, targets):
        logger.info("Naukri chatbot: clicked checkbox(es) %r", ", ".join(targets)[:60])
        return await _advanced()
    return None


async def _recover_unfound_choice(
    page: Page,
    label: str,
    answer: str,
    config: AppConfig | None,
    _advanced: callable,
    raw: dict[str, Any] | None,
) -> bool | None:
    """Recover a choice question whose options never scraped (or was misread as a choice).

    JS discovery sometimes flags a choice control (``hasChoice``/``hasSingleSelect``/
    ``hasCheckbox``) whose option labels never materialize in the snapshot — async
    render, a throttled background tab, or a free-text step misclassified as a
    choice. Before hard-failing and queueing the question, read the live DOM one
    more time: click a real option if one now appears, otherwise type into the
    composer when the panel actually exposes one. Returns the advance result on
    success, or ``None`` when nothing was recoverable.
    """
    live_opts = _filter_meaningful_options(await _discover_choice_options_playwright(page))
    if live_opts:
        target = answer.strip()
        if (
            config is not None
            and getattr(config.llm, "enabled", False)
            and not any(_chip_matches(o, answer) for o in live_opts)
        ):
            from ..llm_answers import map_answer_to_option

            mapped = map_answer_to_option(config, question=label, options=live_opts, answer=answer)
            if mapped:
                target = mapped
        targets = _targets_for_answer(target, live_opts, label) or [target]
        target = targets[0]
        logger.info(
            "Naukri chatbot: recovered live options for %s [%s] -> %r",
            label[:50],
            ", ".join(live_opts[:5]),
            target[:40],
        )
        res = await page.evaluate(
            _FILL_JS,
            {"answer": target, "answers": targets, "allowText": False, "mode": "choice"},
        )
        if (
            (isinstance(res, dict) and res.get("filled"))
            or await _click_option_playwright(page, target)
            or await _click_chip(page, target)
        ):
            logger.info("Naukri chatbot: clicked recovered option %r for %s", target[:40], label[:50])
            return await _advanced()

    # No clickable options on the live page. If the panel exposes a text composer,
    # this was a free-text step misread as a choice — type the answer rather than
    # queueing a question the page would actually accept. Open-ended prompts stay
    # queued (we won't fabricate a personal answer just to satisfy the form).
    from ..answers.validation import is_open_ended_describe_question

    if (
        raw
        and raw.get("hasVisibleInput")
        and answer.strip()
        and not is_open_ended_describe_question(label)
        and await _fill_text_playwright(page, answer, question=label)
    ):
        logger.info("Naukri chatbot: filled via text after no options for %s", label[:50])
        return await _advanced()
    return None


async def fill_naukri_chatbot_question(
    page: Page,
    question: dict[str, Any],
    answer: str,
    *,
    config: AppConfig | None = None,
) -> bool:
    label = normalize_question_label(str(question.get("label", "")))
    if _is_chatbot_terminal_message(label):
        logger.info("Naukri chatbot: flow complete (%s)", label[:50])
        return True

    _bot_msgs_before, _user_msgs_before, poll_ms, _advanced = await _setup_naukri_chatbot_fill(page, label, config)
    kind, answer_options, raw = await _fetch_naukri_answer_options(page, label, question, poll_ms)

    if _should_skip_text_answer(label, answer) and await _click_skip_question(page):
        logger.info("Naukri chatbot: skipped optional field %s", label[:60])
        return await _advanced()

    if _is_date_field(label):
        if await _fill_date_field(page, label, answer):
            return await _advanced()
        logger.warning(
            "Could not fill Naukri chatbot question: %s (date-input)",
            label[:60],
        )
        return False

    if is_pincode_field(label):
        result = await page.evaluate(
            _FILL_JS,
            {
                "answer": answer.strip(),
                "answers": [answer.strip()],
                "allowText": True,
                "mode": "text",
                "question": label,
            },
        )
        if isinstance(result, dict) and result.get("filled"):
            logger.info("Naukri chatbot: filled pincode field %r", label[:40])
            return await _advanced()
        if await _fill_text_playwright(page, answer.strip()):
            logger.info("Naukri chatbot: filled pincode via Playwright %r", label[:40])
            return await _advanced()
        logger.warning(
            "Could not fill Naukri chatbot question: %s (pincode)",
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
    if not is_pincode_field(label):
        live_kind, live_options, _ = _analyze_chatbot_state(raw, label)
        if live_options:
            answer_options = live_options
            kind = live_kind
        elif live_kind != "text":
            kind = live_kind

    kind = _upgrade_kind_for_yes_no_options(kind, label, answer_options, raw)

    from ..answers.fields import infer_field_for_question

    # Live DOM shows a real choice control (radio/checkbox/chips), not just the
    # generic "Type message here…" composer that is always present.
    dom_has_choice = bool(
        raw
        and (
            raw.get("hasChoice")
            or raw.get("hasSingleSelect")
            or raw.get("hasCheckbox")
            or raw.get("radioOptions")
            or raw.get("chipOptions")
            or raw.get("chips")
        )
    )
    # A visible composer with no choice control = free-text question. A question
    # worded like "Which … have you worked on?" can look yes/no but actually render
    # as a free-text input; forcing radio there blocks the text fill entirely.
    dom_is_free_text = bool(raw and raw.get("hasVisibleInput")) and not dom_has_choice
    # Open-ended "describe / what experience … and which projects" prompts are free
    # text. If the live panel exposes a text composer, prefer typing over a
    # mis-detected radio (these questions can't be answered by clicking an option).
    from ..answers.validation import is_open_ended_describe_question

    if is_open_ended_describe_question(label) and bool(raw and raw.get("hasVisibleInput")):
        dom_is_free_text = True
        dom_has_choice = False
    inferred = infer_field_for_question(label)
    if inferred.get("kind") == "radio" and not dom_is_free_text:
        inferred_opts = [str(o) for o in inferred.get("options", []) if str(o).strip()]
        if _is_yes_no_options(inferred_opts):
            kind = "radio"
            if not answer_options:
                answer_options = inferred_opts

    if (
        _is_optional_text_field(label)
        or _is_date_field(label)
        or (raw and raw.get("hasVisibleInput") and raw.get("hasSkipOnly"))
    ):
        result = await page.evaluate(
            _FILL_JS,
            {"answer": answer, "answers": [answer], "allowText": True, "mode": "text", "question": label},
        )
        if isinstance(result, dict) and result.get("filled"):
            logger.info("Naukri chatbot: filled text field %r", label[:40])
            return await _advanced()

    answer = resolve_fill_answer(
        answer,
        {
            **question,
            "label": label,
            "kind": kind,
            "options": answer_options,
            "platform": "naukri",
        },
        config=config,
    )
    input_type = infer_field_input_type(label, {**question, "platform": "naukri", "kind": kind})
    from ..answers.config_answers import location_answer as _location_answer_from_config
    from ..answers.location import _is_location_value_question

    if _is_location_value_question(label) and (not answer.strip() or re.fullmatch(r"\d+(?:\.\d+)?", answer.strip())):
        if config:
            fallback = _location_answer_from_config(config, label, question)
            if fallback:
                answer = fallback
        if not answer.strip() or re.fullmatch(r"\d+(?:\.\d+)?", answer.strip()):
            logger.warning(
                "Naukri chatbot: refusing numeric answer %r for location: %s",
                answer[:20],
                label[:60],
            )
            return False
    if input_type == "years_numeric":
        years = parse_years_numeric_value(answer)
        if years is not None:
            answer = str(int(years)) if years == int(years) else str(years)
    answer = _coerce_blob_answer(label, answer, answer_options)
    answer = _coerce_chip_answer({**question, "kind": kind, "options": answer_options}, answer)
    options_were_implausible = False
    if answer_options and not _options_plausible_for_question(label, answer_options):
        desync_result = await _handle_desynced_naukri_question(
            page,
            label,
            answer,
            answer_options,
            config,
            _advanced,
        )
        if desync_result is not None:
            return desync_result
        logger.debug(
            "Naukri chatbot: discarding implausible options for %s: %s",
            label[:40],
            ", ".join(answer_options[:4]),
        )
        answer_options = []
        options_were_implausible = True
    text_input_only = _should_use_text_input_only(kind, label, answer_options, raw, input_type)
    answer_options = _effective_options(answer_options, label, raw)
    choice_only = _choice_only_question(label, answer_options, raw)
    meaningful_opts = _filter_meaningful_options(answer_options)
    # DOM is the source of truth for the *control* type: if the live panel shows a
    # text composer and no real choice control, treat it as free text no matter how
    # the question is worded. Language inference still governs *semantics* (CTC /
    # years / location formatting) but must not turn a text field into a radio.
    if dom_is_free_text and not dom_has_choice:
        answer_options = []
        meaningful_opts = []
        choice_only = False
    # The DOM choice control's options don't belong to this question (e.g.
    # Male/Female on "Total IT Experience?", "Other City" on a notice-period
    # field) — a mis-scraped adjacent dropdown. Treat it as the free-text/number
    # field it really is and type the answer rather than failing on a phantom radio.
    if options_were_implausible:
        answer_options = []
        meaningful_opts = []
        choice_only = False
        text_input_only = True
        dom_is_free_text = True
        dom_has_choice = False
    # When our (already coerced) answer matches none of the real options, ask the
    # LLM to map it onto one of them. This is a fill-time format conversion only —
    # the saved manual answer in user_memory is never changed here.
    if (
        config is not None
        and getattr(config.llm, "enabled", False)
        and meaningful_opts
        and not text_input_only
        and not any(_chip_matches(o, answer) for o in meaningful_opts)
    ):
        from ..llm_answers import map_answer_to_option

        mapped = map_answer_to_option(config, question=label, options=meaningful_opts, answer=answer)
        if mapped:
            logger.info(
                "Naukri chatbot: LLM mapped %r -> option %r (saved answer unchanged)",
                answer[:40],
                mapped[:40],
            )
            answer = mapped

    # Zero-experience guard: when the answer is "0" (no experience) and none of the
    # real options represent zero, refuse to fill this choice question.
    _apply_zero_experience_guard(label, answer, meaningful_opts, text_input_only)

    checkbox_mode = (
        not text_input_only
        and not dom_is_free_text
        and kind != "radio"
        and not (raw and raw.get("hasSingleSelect"))
        and not _is_yes_no_options(meaningful_opts)
        and (
            kind in ("checkbox", "checkbox_group")
            or (
                _is_checkbox_options(raw, answer_options)
                and len(meaningful_opts) > 1
                and (
                    _looks_like_city_select_question(label)
                    or _looks_like_skill_checkbox_question(label, answer_options)
                )
            )
            or (_looks_like_city_select_question(label) and (answer_options or (raw and raw.get("hasCheckbox"))))
            # Answer names 2+ distinct options (e.g. "Flask, FastAPI") — multi-select
            # regardless of how the question is worded. Requires positive checkbox
            # evidence so a comma-style answer never multi-clicks a single-select
            # control; single-select still picks just targets[0] in radio_mode.
            or (_is_checkbox_options(raw, answer_options) and len(_multi_option_targets(answer, meaningful_opts)) >= 2)
        )
    )
    radio_mode = (
        not text_input_only
        and not dom_is_free_text
        and input_type != "ctc_numeric"
        and not is_numeric_ctc_question(label)
        and not (is_numeric_ctc_question(label) and raw and raw.get("hasVisibleInput"))
        and (
            kind == "radio"
            or (
                not checkbox_mode
                and not is_pincode_field(label)
                and (
                    _looks_like_yes_no_question(label)
                    or _looks_like_notice_period_question(label)
                    or (bool(meaningful_opts) and not _is_yes_no_options(meaningful_opts))
                    or bool(raw and raw.get("hasSingleSelect"))
                )
            )
        )
    )
    targets = _targets_for_answer(answer, answer_options, label)

    if checkbox_mode:
        checkbox_result = await _fill_naukri_checkbox_mode(
            page,
            label,
            answer,
            answer_options,
            config,
            _advanced,
            raw,
        )
        if checkbox_result is not None:
            return checkbox_result
        if choice_only:
            recovered = await _recover_unfound_choice(page, label, answer, config, _advanced, raw)
            if recovered is not None:
                return recovered
            if not answer_options:
                raise CannotAnswerTruthfully(
                    label,
                    reason="checkbox group detected but no selectable options on page",
                )
            logger.warning(
                "Naukri chatbot: expected checkbox for %s but found no options",
                label[:60],
            )
            return False

    if radio_mode and not meaningful_opts:
        # Resolved as a single-select but the discovery snapshot captured no
        # option labels. Re-scrape (JS first, then a direct Playwright read of the
        # live chips/radios) before doing anything else.
        _, fresh_opts, fresh_raw = await _fetch_answer_options(page, question=label, attempts=6, poll_ms=poll_ms)
        if fresh_raw:
            raw = fresh_raw
        if fresh_opts:
            answer_options = _filter_meaningful_options(fresh_opts)
            meaningful_opts = answer_options
        if not meaningful_opts:
            pw_opts = await _discover_choice_options_playwright(page)
            if pw_opts:
                answer_options = _filter_meaningful_options(pw_opts)
                meaningful_opts = answer_options
        if not meaningful_opts:
            # Still nothing selectable on the page. The Yes/No options were
            # injected by label inference, not scraped from the DOM, so a
            # fabricated 'No'/'Yes' click would always fail and be logged as a
            # false technical failure. Prefer a real text composer if the live
            # panel exposes one; otherwise skip honestly (not a tech failure).
            if raw and raw.get("hasVisibleInput") and not choice_only:
                logger.info(
                    "Naukri chatbot: single-select resolved but no DOM options for %s — falling back to text input",
                    label[:60],
                )
                radio_mode = False
                answer_options = []
                meaningful_opts = []
                dom_is_free_text = True
                dom_has_choice = False
                kind = "text"
            else:
                raise CannotAnswerTruthfully(
                    label,
                    reason="single-select detected but no selectable options on page",
                )

    if radio_mode:
        targets = _targets_for_answer(answer, answer_options, label)
        answer_is_exact_option = bool(answer_options) and any(
            o.strip().lower() == answer.strip().lower() for o in answer_options
        )
        if (
            targets
            and targets[0].strip().lower() == answer.strip().lower()
            and answer_options
            # Don't re-coerce an answer that is already a literal option (e.g. an
            # LLM-mapped "15 Days or less"); coercion can wrongly bucket it into a
            # different chip ("1 Month") and then fail to click it.
            and not answer_is_exact_option
            and (_looks_like_notice_period_question(label) or _looks_like_yes_no_question(label))
        ):
            coerced = _coerce_chip_answer({**question, "kind": kind, "options": answer_options}, answer)
            if coerced.strip().lower() != answer.strip().lower():
                targets = _targets_for_answer(coerced, answer_options, label)
        target = targets[0] if targets else answer.strip()
        logger.info(
            "Naukri chatbot radio targets for %s: %s (from answer %r)",
            label[:50],
            ", ".join(targets) if targets else "(none)",
            answer[:40],
        )
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
            return await _advanced()
        if await _click_option_playwright(page, target):
            logger.info("Naukri chatbot: clicked radio %r", target[:40])
            return await _advanced()
        if _is_yes_no_options(answer_options) and await _click_yes_no_playwright(page, target, answer_options):
            logger.info("Naukri chatbot: clicked yes/no %r", target[:40])
            return await _advanced()
        if await _desired_option_already_selected(page, [target, *targets]):
            logger.info(
                "Naukri chatbot: %r already selected for %s — advancing",
                target[:40],
                label[:50],
            )
            return await _advanced()
        # All click attempts failed. The options we tried may be label-inferred
        # Yes/No (phantom) rather than the real on-screen choices. Re-scrape the
        # live DOM for the actual options and, when they differ, hand THEM to the
        # LLM and retry — "identify real options -> ask LLM -> click".
        live_opts = _filter_meaningful_options(await _discover_choice_options_playwright(page))
        tried = {o.strip().lower() for o in answer_options}
        if live_opts and {o.strip().lower() for o in live_opts} != tried:
            retry_answer = answer
            if (
                config is not None
                and getattr(config.llm, "enabled", False)
                and not any(_chip_matches(o, answer) for o in live_opts)
            ):
                from ..llm_answers import map_answer_to_option

                mapped = map_answer_to_option(config, question=label, options=live_opts, answer=answer)
                if mapped:
                    logger.info(
                        "Naukri chatbot: LLM mapped %r -> live option %r",
                        answer[:40],
                        mapped[:40],
                    )
                    retry_answer = mapped
            retry_targets = _targets_for_answer(retry_answer, live_opts, label)
            retry_target = retry_targets[0] if retry_targets else retry_answer.strip()
            logger.info(
                "Naukri chatbot: retrying radio for %s with live options [%s] -> %r",
                label[:50],
                ", ".join(live_opts[:5]),
                retry_target[:40],
            )
            res2 = await page.evaluate(
                _FILL_JS,
                {
                    "answer": retry_target,
                    "answers": retry_targets,
                    "allowText": False,
                    "mode": "choice",
                },
            )
            if (isinstance(res2, dict) and res2.get("filled")) or await _click_option_playwright(page, retry_target):
                logger.info(
                    "Naukri chatbot: clicked radio via live re-scrape %r",
                    retry_target[:40],
                )
                return await _advanced()
        # No usable live options, or the live re-scrape matched what we already
        # tried and still won't click. If the prompt is open-ended (mis-detected
        # as a radio) or the panel truly exposes nothing selectable, queue it for
        # manual input instead of logging a false technical failure.
        if is_open_ended_describe_question(label) or not live_opts:
            raise CannotAnswerTruthfully(
                label,
                reason="radio options not selectable on page (open-ended/phantom)",
            )
        if answer_options and any(_chip_matches(o, target) for o in answer_options):
            logger.warning(
                "Naukri chatbot: expected radio for %s but could not click %r",
                label[:60],
                target[:40],
            )
            return False
        logger.info(
            "Naukri chatbot: radio target %r not on page for %s — trying text fill",
            target[:40],
            label[:50],
        )

    elif answer_options:
        target = targets[0]
        if _is_yes_no_options(answer_options):
            coerced = _coerce_chip_answer({**question, "kind": "radio", "options": answer_options}, answer)
            if await _click_yes_no_playwright(page, coerced, answer_options):
                logger.info("Naukri chatbot: clicked yes/no %r", coerced[:40])
                return await _advanced()
        if _looks_like_multi_chip_question(label, answer_options) and "," in answer:
            if await _fill_multi_chip_answer(page, answer, answer_options, label):
                logger.info("Naukri chatbot: clicked multi-chip %r", answer[:40])
                return await _advanced()
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
            return await _advanced()
        if await _click_chip(page, target) or await _click_option_playwright(page, target):
            logger.info("Naukri chatbot: clicked option %r", target[:40])
            return await _advanced()
        from ..answers.location import _is_location_value_question

        if (
            any(re.search(r"\bother\s*city\b|^other$", o.strip(), re.I) for o in answer_options)
            and (_is_location_value_question(label) or _looks_like_city_select_question(label))
            and await _fill_other_city_location(page, answer, answer_options, label)
        ):
            logger.info(
                "Naukri chatbot: selected 'Other City' and typed %r for %s",
                answer[:40],
                label[:50],
            )
            return await _advanced()
        logger.warning(
            "Naukri chatbot: no matching option for %r (options: %s)",
            answer[:40],
            ", ".join(answer_options[:6]),
        )
        if _is_yes_no_options(answer_options) and _looks_like_yes_no_question(label):
            coerced = _coerce_chip_answer({**question, "kind": kind, "options": answer_options}, answer)
            if await _click_yes_no_playwright(page, coerced, answer_options):
                logger.info("Naukri chatbot: clicked yes/no fallback %r", coerced[:40])
                return await _advanced()
        if _looks_like_multi_chip_question(label, answer_options):
            if await _fill_multi_chip_answer(page, answer, answer_options, label):
                logger.info("Naukri chatbot: clicked multi-chip fallback %r", answer[:40])
                return await _advanced()
        if is_numeric_ctc_question(label) or input_type == "ctc_numeric":
            pass  # fall through to text fill
        else:
            return False

    if choice_only:
        if _is_yes_no_options(meaningful_opts):
            coerced = _coerce_chip_answer({**question, "kind": "radio", "options": meaningful_opts}, answer)
            if await _click_yes_no_playwright(page, coerced, meaningful_opts):
                logger.info("Naukri chatbot: clicked yes/no (choice_only) %r", coerced[:40])
                return await _advanced()
        if _looks_like_multi_chip_question(label, meaningful_opts):
            if await _fill_multi_chip_answer(page, answer, meaningful_opts, label):
                logger.info("Naukri chatbot: clicked multi-chip (choice_only) %r", answer[:40])
                return await _advanced()
        recovered = await _recover_unfound_choice(page, label, answer, config, _advanced, raw)
        if recovered is not None:
            return recovered
        logger.warning(
            "Naukri chatbot: expected chips/radio/checkbox for %s but found no options",
            label[:60],
        )
        return False

    allow_text_despite_choices = (
        is_numeric_ctc_question(label)
        or input_type in ("ctc_numeric", "years_numeric")
        or _looks_like_multi_chip_question(label, meaningful_opts)
    )
    if answer_options and kind in ("radio", "checkbox", "checkbox_group") and not dom_is_free_text:
        if not allow_text_despite_choices:
            if await _desired_option_already_selected(
                page, [answer, *_targets_for_answer(answer, answer_options, label)]
            ):
                logger.info(
                    "Naukri chatbot: choice already selected for %s — advancing",
                    label[:50],
                )
                return await _advanced()
            logger.warning(
                "Naukri chatbot: refusing text fill for choice question %s",
                label[:60],
            )
            return False
    if (
        raw
        and answer_options
        and (raw.get("hasChoice") or raw.get("hasSingleSelect"))
        and not allow_text_despite_choices
        and not _is_yes_no_options(meaningful_opts)
    ):
        if await _desired_option_already_selected(page, [answer, *_targets_for_answer(answer, answer_options, label)]):
            logger.info(
                "Naukri chatbot: choice already selected for %s — advancing",
                label[:50],
            )
            return await _advanced()
        logger.warning(
            "Naukri chatbot: refusing text fill — choice UI present for %s",
            label[:60],
        )
        return False

    if is_numeric_ctc_question(label) or input_type == "ctc_numeric":
        if await _fill_text_playwright(page, answer, question=label):
            logger.info("Naukri chatbot: filled CTC via Playwright %r", label[:40])
            return await _advanced()

    result = await page.evaluate(
        _FILL_JS,
        {"answer": answer, "answers": [answer], "allowText": True, "mode": "text", "question": label},
    )
    if not isinstance(result, dict) or not result.get("filled"):
        reason = result.get("reason") if isinstance(result, dict) else result
        if reason == "no-drawer" and await _chatbot_flow_complete(page):
            logger.info("Naukri chatbot: flow complete (drawer closed)")
            return True
        if reason in ("set-failed", "verify-failed", "no-input"):
            if await _chatbot_flow_complete(page):
                logger.info("Naukri chatbot: flow complete after %s", reason)
                return True
            await page.wait_for_timeout(400)
            if await _fill_text_playwright(page, answer, question=label):
                logger.info("Naukri chatbot: filled via Playwright text %r", label[:40])
                return await _advanced()
        if answer_options and await _desired_option_already_selected(
            page, [answer, *_targets_for_answer(answer, answer_options, label)]
        ):
            logger.info(
                "Naukri chatbot: answer already selected for %s — advancing",
                label[:50],
            )
            return await _advanced()
        logger.warning(
            "Could not fill Naukri chatbot question: %s (%s)",
            label[:60],
            reason,
        )
        return False

    logger.info("Naukri chatbot: filled via %s", result.get("method", "text"))
    return await _advanced(timeout_ms=25000)
