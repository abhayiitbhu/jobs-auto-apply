from __future__ import annotations

import re
from typing import Any

from playwright.async_api import Page

from ..page_load import ensure_page_ready
from .labels import COVER_NOTE_HINT, is_generic_question_label

async def discover_questions(page: Page) -> list[dict[str, Any]]:
    """Find mandatory application fields beyond the cover-note textarea."""
    await ensure_page_ready(page, for_form=True)
    fields: list[dict[str, Any]] = []
    container = page.locator('[role="dialog"]').last
    if await container.count() == 0:
        container = page.locator("body")

    textareas = container.locator("textarea:visible")
    for i in range(await textareas.count()):
        el = textareas.nth(i)
        label = await _label_for(page, el)
        placeholder = (await el.get_attribute("placeholder")) or ""
        if COVER_NOTE_HINT.search(label + placeholder):
            continue
        resolved = label or placeholder
        if is_generic_question_label(resolved):
            continue
        fields.append({"kind": "textarea", "label": resolved, "index": i})

    inputs = container.locator('input[type="text"]:visible, input:not([type]):visible')
    for i in range(await inputs.count()):
        el = inputs.nth(i)
        label = await _label_for(page, el)
        placeholder = (await el.get_attribute("placeholder")) or ""
        if not label and not placeholder:
            continue
        resolved = label or placeholder
        if is_generic_question_label(resolved):
            continue
        fields.append({"kind": "input", "label": resolved, "index": i})

    selects = container.locator("select:visible")
    for i in range(await selects.count()):
        el = selects.nth(i)
        label = await _label_for(page, el)
        fields.append({"kind": "select", "label": label or f"Question {i+1}", "index": i})

    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for f in fields:
        lab = f["label"].strip()
        if not lab or lab in seen:
            continue
        seen.add(lab)
        unique.append(f)
    return unique



async def _label_for(page: Page, el) -> str:
    el_id = await el.get_attribute("id")
    if el_id:
        label = page.locator(f'label[for="{el_id}"]')
        if await label.count() > 0:
            return (await label.first.inner_text()).strip()
    aria = await el.get_attribute("aria-label")
    return (aria or "").strip()



async def fill_questions(page: Page, answers: dict[str, str]) -> None:
    await ensure_page_ready(page, for_form=True)
    container = page.locator('[role="dialog"]').last
    if await container.count() == 0:
        container = page.locator("body")

    for question, answer in answers.items():
        if not answer:
            continue
        label = container.locator("label").filter(has_text=re.compile(re.escape(question[:40]), re.I))
        if await label.count() > 0:
            for_id = await label.first.get_attribute("for")
            if for_id:
                target = container.locator(f"#{for_id}")
                if await target.count() > 0:
                    await target.fill(answer)
                    continue
        field = container.locator(
            f'textarea[placeholder*="{question[:20]}" i], input[placeholder*="{question[:20]}" i]'
        )
        if await field.count() > 0:
            await field.first.fill(answer)