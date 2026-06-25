"""Wait for SPA pages to settle and scroll lazy-loaded content into view."""

from __future__ import annotations

import logging

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

logger = logging.getLogger("job_apply")

_WAIT_DOM_STABLE_JS = """
async () => {
  const delay = (ms) => new Promise((r) => setTimeout(r, ms));
  let last = -1;
  let stable = 0;
  const maxRounds = 50;
  const needStable = 4;
  for (let i = 0; i < maxRounds; i++) {
    await delay(150);
    const h = Math.max(
      document.body?.scrollHeight || 0,
      document.documentElement?.scrollHeight || 0
    );
    if (h === last) {
      stable++;
      if (stable >= needStable) return { stable: true, height: h };
    } else {
      stable = 0;
      last = h;
    }
  }
  return { stable: false, height: last };
}
"""

_EXPAND_LAZY_JS = """
async ({ rounds, pauseMs }) => {
  const delay = (ms) => new Promise((r) => setTimeout(r, ms));
  let lastHeight = -1;
  for (let r = 0; r < rounds; r++) {
    window.scrollBy(0, Math.max(350, window.innerHeight * 0.8));
    const nodes = [document.documentElement, document.body, ...document.querySelectorAll('*')];
    for (const node of nodes) {
      try {
        const style = window.getComputedStyle(node);
        if (!/(auto|scroll)/.test(style.overflowY)) continue;
        if (node.scrollHeight <= node.clientHeight + 20) continue;
        node.scrollTop = node.scrollHeight;
      } catch (e) {}
    }
    await delay(pauseMs);
    const h = document.body?.scrollHeight || 0;
    if (h === lastHeight && r > 2) break;
    lastHeight = h;
  }
  window.scrollTo(0, 0);
  await delay(100);
}
"""

_SCROLL_FORM_ROOTS_JS = """
async (pauseMs) => {
  const delay = (ms) => new Promise((r) => setTimeout(r, ms));
  const visible = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) return false;
    const s = window.getComputedStyle(el);
    return s.display !== "none" && s.visibility !== "hidden" && s.opacity !== "0";
  };
  const scrollable = (el) => {
    try {
      const s = window.getComputedStyle(el);
      return /(auto|scroll)/.test(s.overflowY) && el.scrollHeight > el.clientHeight + 8;
    } catch (e) {
      return false;
    }
  };
  const roots = new Set([document.documentElement, document.body]);
  const selectors = [
    '[role="dialog"]',
    "form",
    "main",
    '[class*="modal" i]',
    '[class*="screening" i]',
    '[class*="application" i]',
    '[class*="job-apply" i]',
    '[id*="screening" i]',
    '[class*="chatbot" i]',
  ];
  for (const sel of selectors) {
    for (const el of document.querySelectorAll(sel)) {
      if (visible(el)) roots.add(el);
    }
  }
  for (const root of roots) {
    let last = -1;
    for (let i = 0; i < 24; i++) {
      root.scrollTop = root.scrollHeight;
      await delay(pauseMs);
      if (root.scrollTop === last) break;
      last = root.scrollTop;
    }
  }
}
"""

_REVEAL_FOOTER_JS = """
async ({ rounds, pauseMs }) => {
  const delay = (ms) => new Promise((r) => setTimeout(r, ms));
  const scrollContainers = () => {
    const nodes = [document.documentElement, document.body, ...document.querySelectorAll('*')];
    for (const node of nodes) {
      try {
        const style = window.getComputedStyle(node);
        if (!/(auto|scroll)/.test(style.overflowY)) continue;
        if (node.scrollHeight <= node.clientHeight + 20) continue;
        node.scrollTop = node.scrollHeight;
      } catch (e) {}
    }
  };
  for (let i = 0; i < rounds; i++) {
    window.scrollBy(0, Math.max(280, window.innerHeight * 0.7));
    scrollContainers();
    await delay(pauseMs);
  }
  window.scrollTo(0, document.body.scrollHeight);
  scrollContainers();
}
"""

_SCROLL_ACTION_BUTTONS_JS = """
async () => {
  const delay = (ms) => new Promise((r) => setTimeout(r, ms));
  const patterns = [
    /submit\\s*application/i,
    /^submit$/i,
    /^next$/i,
    /^save\\s*&?\\s*next$/i,
    /^continue$/i,
    /^confirm$/i,
    /^finish$/i,
    /^proceed$/i,
    /^apply$/i,
    /^done$/i,
  ];
  const visible = (el) => {
    if (!el || el.disabled) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) return false;
    const s = window.getComputedStyle(el);
    return s.display !== "none" && s.visibility !== "hidden" && s.opacity !== "0";
  };
  const textOf = (el) =>
    (el.value || el.innerText || el.textContent || "").trim().replace(/\\s+/g, " ");
  const inViewport = (el) => {
    const r = el.getBoundingClientRect();
    return r.top < window.innerHeight - 2 && r.bottom > 2 && r.width > 4 && r.height > 4;
  };
  const candidates = () =>
    [...document.querySelectorAll(
      'button, input[type="submit"], input[type="button"], a[role="button"], [class*="btn-next" i], [class*="btn-submit" i]'
    )].filter(visible);

  for (let round = 0; round < 16; round++) {
    for (const el of candidates()) {
      const text = textOf(el);
      if (!text || text.length > 56) continue;
      if (!patterns.some((p) => p.test(text))) continue;
      if (inViewport(el)) return { found: true, text };
      el.scrollIntoView({ block: "center", behavior: "instant" });
      await delay(120);
      if (inViewport(el)) return { found: true, text };
    }
    window.scrollBy(0, Math.max(250, window.innerHeight * 0.55));
    const nodes = document.querySelectorAll('[role="dialog"], form, main');
    for (const node of nodes) {
      try {
        node.scrollTop = node.scrollHeight;
      } catch (e) {}
    }
    await delay(120);
  }
  return { found: false };
}
"""


async def wait_for_dom_stable(page: Page, *, max_wait_ms: int = 8_000) -> bool:
    """Wait until document height stops changing (SPA render finished)."""
    rounds = max(8, min(50, max_wait_ms // 150))
    js = _WAIT_DOM_STABLE_JS.replace("maxRounds = 50", f"maxRounds = {rounds}")
    try:
        result = await page.evaluate(js)
        return bool(result and result.get("stable"))
    except Exception as exc:
        logger.debug("wait_for_dom_stable failed: %s", exc)
        return False


async def wait_for_page_settled(
    page: Page,
    *,
    load_timeout_ms: int = 45_000,
    network_idle_ms: int = 8_000,
    extra_ms: int = 1_200,
    skip_network_idle: bool = False,
) -> None:
    """Wait past domcontentloaded — load + short networkidle + render buffer."""
    try:
        await page.wait_for_load_state("load", timeout=load_timeout_ms)
    except PlaywrightTimeout:
        logger.debug("Page load event not received within %dms", load_timeout_ms)
    if not skip_network_idle and network_idle_ms > 0:
        try:
            await page.wait_for_load_state("networkidle", timeout=network_idle_ms)
        except PlaywrightTimeout:
            pass
    if extra_ms > 0:
        await page.wait_for_timeout(extra_ms)


async def expand_lazy_content(
    page: Page,
    *,
    rounds: int = 10,
    pause_ms: int = 200,
) -> None:
    """Scroll the page to trigger lazy-loaded sections, then return to top."""
    try:
        await page.evaluate(_EXPAND_LAZY_JS, {"rounds": rounds, "pauseMs": pause_ms})
    except Exception as exc:
        logger.debug("expand_lazy_content failed: %s", exc)
    await page.wait_for_timeout(200)


async def scroll_lazy_page(page: Page, *, rounds: int = 14, pause_ms: int = 280) -> None:
    """Incremental window scroll to trigger infinite-scroll / lazy lists."""
    await expand_lazy_content(page, rounds=rounds, pause_ms=pause_ms)


async def scroll_form_roots(page: Page, *, pause_ms: int = 100) -> None:
    """Scroll nested form/dialog containers so all fields and footers render."""
    try:
        await page.evaluate(_SCROLL_FORM_ROOTS_JS, pause_ms)
    except Exception as exc:
        logger.debug("scroll_form_roots failed: %s", exc)
    await page.wait_for_timeout(150)


async def scroll_all_containers(page: Page) -> None:
    """Scroll nested overflow containers (modals, screening forms, side panels)."""
    await scroll_form_roots(page, pause_ms=80)


async def reveal_footer_actions(page: Page, *, for_form: bool = False) -> None:
    """Expose sticky footer Next/Submit buttons hidden below the fold."""
    rounds = 5 if for_form else 3
    pause = 140 if for_form else 120
    try:
        await page.evaluate(_REVEAL_FOOTER_JS, {"rounds": rounds, "pauseMs": pause})
    except Exception as exc:
        logger.debug("reveal_footer_actions failed: %s", exc)
    await page.wait_for_timeout(250 if for_form else 200)


async def scroll_action_buttons_into_view(page: Page) -> bool:
    """Scroll until a Next/Submit-style control is in the viewport."""
    try:
        result = await page.evaluate(_SCROLL_ACTION_BUTTONS_JS)
        return bool(result and result.get("found"))
    except Exception as exc:
        logger.debug("scroll_action_buttons_into_view failed: %s", exc)
        return False


async def ensure_page_ready(
    page: Page,
    *,
    for_form: bool = False,
    quick: bool = False,
) -> None:
    """Fully render a programmatically opened page before form fill or button clicks.

    SPAs often stop rendering after domcontentloaded; this waits for layout stability,
    expands lazy sections, scrolls form containers, and reveals footer actions.
    """
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15_000)
    except PlaywrightTimeout:
        pass

    dom_ms = 4_000 if quick else (6_000 if for_form else 10_000)
    await wait_for_dom_stable(page, max_wait_ms=dom_ms)

    if for_form:
        await expand_lazy_content(page, rounds=5 if quick else 8, pause_ms=160)
        await scroll_form_roots(page, pause_ms=100)
        await reveal_footer_actions(page, for_form=True)
        found = await scroll_action_buttons_into_view(page)
        if not found:
            await reveal_footer_actions(page, for_form=True)
            await scroll_action_buttons_into_view(page)
    else:
        await expand_lazy_content(page, rounds=8, pause_ms=220)
        await scroll_all_containers(page)


async def goto_settled(
    page: Page,
    url: str,
    *,
    timeout_ms: int = 90_000,
    scroll: bool = True,
    quick: bool = False,
) -> None:
    """Navigate and wait for lazy SPAs to fully render."""
    if quick:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        await wait_for_page_settled(page, network_idle_ms=2_000, extra_ms=400)
        await wait_for_dom_stable(page, max_wait_ms=4_000)
        if scroll:
            await expand_lazy_content(page, rounds=4, pause_ms=150)
            await scroll_all_containers(page)
        return
    await page.goto(url, wait_until="load", timeout=timeout_ms)
    await wait_for_page_settled(page, network_idle_ms=5_000)
    await wait_for_dom_stable(page, max_wait_ms=10_000)
    if scroll:
        await expand_lazy_content(page, rounds=10, pause_ms=200)
        await scroll_all_containers(page)


async def prepare_interactive_page(page: Page, *, fast: bool = False) -> None:
    """Call before clicking form buttons or filling fields on an already-open page."""
    await ensure_page_ready(page, for_form=True, quick=fast)
