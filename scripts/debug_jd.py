#!/usr/bin/env python3
"""Debug Wellfound JD extraction for a single job URL."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobs_auto_apply.config import load_config
from jobs_auto_apply.browser import wellfound_session
from jobs_auto_apply.wellfound.modal import extract_wellfound_job_page


async def main(url: str) -> None:
    config = load_config(Path("config.yaml"))
    async with wellfound_session(config) as (_, _ctx, page):
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)
        diag = await page.evaluate(
            """() => {
                const allP = [...document.querySelectorAll('p')];
                const descEls = [...document.querySelectorAll('[class*="escription"], [class*="Description"], [data-test*="escription"]')];
                const roots = allP.map(p => {
                    let el = p;
                    let path = [];
                    for (let i = 0; i < 5 && el; i++) {
                        path.push(el.tagName + (el.className ? '.' + String(el.className).split(' ')[0] : ''));
                        el = el.parentElement;
                    }
                    return { len: p.innerText.trim().length, text: p.innerText.trim().slice(0, 80), path: path.join(' < ') };
                }).filter(x => x.len > 40).slice(0, 8);
                return {
                    finalUrl: location.href,
                    title: document.title,
                    mainExists: !!document.querySelector('main'),
                    mainLen: document.querySelector('main')?.innerText?.length || 0,
                    pCount: allP.length,
                    pRoots: roots,
                    descBlocks: descEls.slice(0, 5).map(el => ({
                        tag: el.tagName,
                        cls: String(el.className).slice(0, 80),
                        len: el.innerText.length,
                        pCount: el.querySelectorAll('p').length,
                    })),
                    bodyLen: document.body.innerText.length,
                    bodyHead: document.body.innerText.slice(0, 600),
                };
            }"""
        )
        print("DIAG:", diag)
        info = await extract_wellfound_job_page(page)
        print("JD_LEN:", len(info.jd))
        print("JD_HEAD:", info.jd[:500] if info.jd else "(empty)")


if __name__ == "__main__":
    u = sys.argv[1] if len(sys.argv) > 1 else "https://wellfound.com/jobs/3711413-backend-developer-node-js"
    asyncio.run(main(u))
