import asyncio
import json
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

from playwright.async_api import async_playwright


# Ekantipur scraper
# - Uses resilient selectors (CSS/semantic HTML) rather than absolute XPaths.
# - Extracts:
#   1) Top 5 Entertainment headlines from the section grid (`h2 a`), then fetches
#      each article page for `og:image` (thumbnail) + author/category meta.
#   2) Cartoon of the Day from the homepage carousel:
#      active Swiper slide -> `img[data-src]` for image URL, and `alt` for cartoonist name.
BASE_URL = "https://ekantipur.com"

def _normalize_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    url = url.strip()
    if not url:
        return None
    if url.startswith("//"):
        return "https:" + url
    return url


async def _safe_get_attr(locator, attr: str, timeout_ms: int = 5000) -> Optional[str]:
    try:
        return await locator.get_attribute(attr, timeout=timeout_ms)
    except Exception:
        return None


async def _safe_get_text(locator, timeout_ms: int = 5000) -> Optional[str]:
    try:
        text = await locator.text_content(timeout=timeout_ms)
        if text is None:
            return None
        text = text.strip()
        return text if text else None
    except Exception:
        return None


async def _get_meta_content(page, css_selector: str) -> Optional[str]:
    try:
        return await page.evaluate(
            """(sel) => {
              const el = document.querySelector(sel);
              return (el && (el.content || el.textContent)) ? (el.content || el.textContent).trim() : null;
            }""",
            css_selector,
        )
    except Exception:
        return None


async def _get_page_author_and_category_from_article(page) -> tuple[Optional[str], Optional[str]]:
    author = (
        await _get_meta_content(page, 'meta[name="author"]')
        or await _get_meta_content(page, 'meta[property="article:author"]')
        or await _get_meta_content(page, 'meta[name="Article:Author"]')
    )

    category = (
        await _get_meta_content(page, 'meta[property="article:section"]')
        or await _get_meta_content(page, 'meta[name="section"]')
        or await _get_meta_content(page, 'meta[name="Section"]')
    )

    return author, category

def _clean_title(title: str) -> str:
    if not title:
        return ""
    title = title.replace("‘", "").replace("’", "").strip()
    return re.sub(r"\s+", " ", title)


async def _extract_cartoon_of_the_day_from_homepage(page) -> dict[str, Optional[str]]:
    # Cartoon carousel (swiper) is on the homepage.
    # Prefer attribute/class-based CSS selectors instead of absolute XPaths.
    slider = page.locator("[class*='cartoon-slider']").first
    await slider.wait_for(state="attached", timeout=10000)
    await page.wait_for_timeout(2500)

    # Swiper marks the active slide with `swiper-slide-active`.
    active_slide = page.locator("[class*='cartoon-slider'] [class*='swiper-slide-active']").first
    if (await active_slide.count()) == 0:
        active_slide = page.locator("[class*='cartoon-slider'] [class*='swiper-slide']").first

    slide = active_slide.first
    slide_img = slide.locator("img").first

    image_url = (
        await _safe_get_attr(slide_img, "data-src", timeout_ms=7000)
        or await _safe_get_attr(slide_img, "src", timeout_ms=7000)
    )
    image_url = _normalize_url(image_url)

    alt = await _safe_get_attr(slide_img, "alt", timeout_ms=7000)
    alt = alt.strip() if alt else None

    # Observed alt: "कान्तिपुर दैनिकमा आज प्रकाशीत अविनको कार्टुन"
    author = None
    if alt:
        # Prefer capturing the cartoonist name right before "को कार्ट/कार्टुन".
        # Observed pattern: "... प्रकाशित <NAME>को कार्टुन"
        m = re.search(r"([\u0900-\u097F]+)को\s*कार्ट", alt)
        if not m:
            m = re.search(r"([\u0900-\u097F]+)को\s*कार्टुन", alt)
        if not m:
            # Fallback: best-effort extraction after the "प्रकाश..." word.
            m = re.search(r"प्रकाश\w*\s*(.+?)को\s*कार्ट", alt)
        if m and m.group(1):
            author = m.group(1).strip()

    # Title/caption isn't exposed as separate text; use alt as best-effort.
    return {"title": alt, "image_url": image_url, "author": author}


async def run(output_path: Path) -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1280, "height": 720})
        page = await context.new_page()

        await page.goto(BASE_URL, wait_until="domcontentloaded")

        # Cartoon of the Day from homepage (before navigating away).
        try:
            await page.wait_for_timeout(2000)
            await page.evaluate("window.scrollTo(0, 2000)")
            await page.wait_for_timeout(1500)
            cartoon_of_the_day = await _extract_cartoon_of_the_day_from_homepage(page)
        except Exception:
            cartoon_of_the_day = {"title": None, "image_url": None, "author": None}

        # Navigate to Entertainment section.
        # Avoid fragile absolute XPaths; instead click via text/URL patterns.
        clicked = False
        for sel in [
            "a:has-text('मनो रञ्जन')",
            "a:has-text('मनोरञ्जन')",
            "a:has-text('Entertainment')",
            "a[href*='/entertainment/']",
        ]:
            loc = page.locator(sel).first
            try:
                await loc.wait_for(state="visible", timeout=6000)
                await loc.scroll_into_view_if_needed(timeout=6000)
                await loc.click(timeout=6000)
                clicked = True
                break
            except Exception:
                continue

        if not clicked:
            # Still write an output with empty fields rather than crashing hard.
            data = {"entertainment_news": [], "cartoon_of_the_day": {"title": None, "image_url": None, "author": None}}
            output_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            await context.close()
            await browser.close()
            return

        await page.wait_for_load_state("domcontentloaded")
        # Some pages load asynchronously; a short wait helps for the content to render.
        await page.wait_for_timeout(2000)

        # Top 5 entertainment news from article pages.
        # The article grid uses semantic headings (`h2`) with anchor links.
        # We then filter links by `/entertainment/` to ensure we only pick this section.
        h2a = page.locator("h2 a")
        total_links = await h2a.count()

        picked: list[tuple[str, str]] = []
        for i in range(min(total_links, 25)):
            link = h2a.nth(i)
            href = await _safe_get_attr(link, "href", timeout_ms=2000)
            if not href or "/entertainment/" not in href:
                continue
            title = await _safe_get_text(link, timeout_ms=2000) or ""
            title = _clean_title(title)
            if not title:
                continue
            picked.append((title, urljoin(BASE_URL, href)))
            if len(picked) >= 5:
                break

        entertainment_news: list[dict[str, Any]] = []
        for title, article_url in picked:
            article_page = await context.new_page()
            try:
                await article_page.goto(article_url, wait_until="domcontentloaded")
                await article_page.wait_for_timeout(1500)

                image_url = (
                    await _get_meta_content(article_page, 'meta[property="og:image"]')
                    or await _get_meta_content(article_page, 'meta[name="twitter:image"]')
                )
                image_url = _normalize_url(image_url)

                author, category = await _get_page_author_and_category_from_article(article_page)

                entertainment_news.append(
                    {
                        "title": title,
                        "image_url": image_url,
                        "category": category or "मनो रञ्जन",
                        "author": author if author else None,
                    }
                )
            finally:
                await article_page.close()

        data = {"entertainment_news": entertainment_news, "cartoon_of_the_day": cartoon_of_the_day}

        output_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        await context.close()
        await browser.close()


def main() -> None:
    project_root = Path(__file__).resolve().parent
    output_path = project_root / "output.json"
    asyncio.run(run(output_path))


if __name__ == "__main__":
    main()

