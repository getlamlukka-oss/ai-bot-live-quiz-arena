"""
PHASE 1: ROV Academy Data Acquisition Scraper (`scraper.py`).

Uses Playwright to automate ROV Academy (`https://academy.rov.in.th`) login with
credentials loaded strictly from `.env` (`ROV_USERNAME`, `ROV_PASSWORD`), pauses via
`page.pause()` so the operator can solve any Garena Captcha manually, then iterates
over all 4 required categories:
  1. MOBA101
  2. แผนที่ (แผนที่ RoV)
  3. ไอเทม (ไอเทมในเกม)
  4. ตำแหน่ง (ตำแหน่งในเกม)

Extracts clean `Title` and `Content` (plus quiz options when available) and persists
records into Supabase table `quiz_questions` (see `supabase_schema.sql`) with:
  - 3x exponential-backoff retry on Playwright & Supabase external operations
  - Comprehensive `try-except` + Sentry SDK exception reporting
  - Automatic local JSON backup (`data/quiz_questions.json`)

Usage:
  python scraper.py                 # Interactive Playwright mode (pauses at Captcha)
  python scraper.py --api-only      # Fetch public chapter content via ROV Academy API
"""

from __future__ import annotations

import argparse
import asyncio
import html
import logging
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

import httpx
from playwright.sync_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from backend.config import Settings, get_settings
from backend.database import QuizRepository
from backend.monitoring import capture_error, configure_logging, init_sentry
from backend.retry_utils import retry_sync

logger = configure_logging()

# Canonical mapping between ROV Academy topic names and our 4 target DB categories
TARGET_CATEGORIES: List[Tuple[str, Tuple[str, ...]]] = [
    ("MOBA101", ("moba101", "moba 101")),
    ("แผนที่", ("แผนที่", "map")),
    ("ไอเทม", ("ไอเทม", "item")),
    ("ตำแหน่ง", ("ตำแหน่ง", "role", "position")),
]

LOGIN_SELECTORS = {
    "username": (
        "input[name='account'], input[autocomplete='username'], "
        "input[placeholder*='Account' i], input[placeholder*='Username' i], "
        "input[placeholder*='Garena' i], input[type='text'], #username"
    ),
    "password": "input[type='password'], input[name='password'], #password",
    "submit": (
        "button[type='submit'], #login-btn, "
        "button:has-text('Login'), button:has-text('Log in'), button:has-text('เข้าสู่ระบบ')"
    ),
}


def strip_html(raw: Optional[str]) -> str:
    """Convert raw ROV Academy HTML fragments into readable plain text."""
    if not raw:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", raw, flags=re.IGNORECASE)
    text = re.sub(r"</(p|li|div|h[1-6])>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def match_target_category(topic_title: str) -> Optional[str]:
    """Map an ROV Academy topic heading onto one of our 4 required categories."""
    normalized = topic_title.strip().lower()
    for canonical, keywords in TARGET_CATEGORIES:
        if any(keyword in normalized for keyword in keywords):
            return canonical
    return None


def build_quiz_record(
    *,
    chapter_id: int,
    category: str,
    title: str,
    content: str,
    source_url: str,
    existing_quiz_lookup: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Construct a normalized `quiz_questions` row preserving extracted `title` & `content`.

    If verified multiple-choice options already exist for this chapter key, preserves
    them; otherwise synthesizes structured A/B/C/D options directly from the chapter.
    """
    external_key = f"rov_academy_ch_{chapter_id}"
    if existing_quiz_lookup and external_key in existing_quiz_lookup:
        cached = dict(existing_quiz_lookup[external_key])
        cached["category"] = category
        cached["title"] = title
        cached["content"] = content[:1800]
        cached["source_url"] = source_url
        return cached

    # Fallback option generation when scraping a brand-new ROV Academy chapter
    snippet = content.split("\n")[0][:120].strip() if content else title
    return {
        "external_key": external_key,
        "category": category,
        "title": title,
        "content": content[:1800],
        "question_text": f"ข้อใดสรุปสาระสำคัญของบทเรียน '{title}' ในหมวด {category} ได้ถูกต้องที่สุด?",
        "options": {
            "A": snippet or f"หลักการพื้นฐานของ {title}",
            "B": "ระบบแลกเปลี่ยนสกินและคูปองในร้านค้าล็อบบี้",
            "C": "วิธีปรับตั้งค่าเฟรมเรตและคุณภาพกราฟิกในเกม",
            "D": "กติกาการสมัครแข่งขันอีสปอร์ตระดับภูมิภาค",
        },
        "correct_option": "A",
        "explanation": f"อ้างอิงจากบทเรียน RoV Academy หมวด {category} หัวข้อ {title}",
        "source_url": source_url,
    }


class ROVAcademyScraper:
    """Playwright + API hybrid scraper for ROV Academy with 3x retry & Sentry protection."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.repository = QuizRepository(settings)

    def _navigate_with_retry(self, page: Page, url: str) -> None:
        """Navigate Playwright page with 3x exponential-backoff retry."""

        @retry_sync(
            component="playwright_scraper",
            operation="page_goto",
            max_attempts=self.settings.retry_max_attempts,
            min_wait=self.settings.retry_min_wait_seconds,
            max_wait=self.settings.retry_max_wait_seconds,
            retry_exceptions=(PlaywrightTimeoutError, PlaywrightError, RuntimeError, OSError),
        )
        def _go() -> None:
            logger.info("Navigating to %s ...", url)
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)

        _go()

    def _fill_login_form_with_retry(self, page: Page) -> None:
        """Locate and fill Garena/ROV login credentials sourced from `.env` with 3x retry."""
        if not self.settings.rov_username or not self.settings.rov_password:
            raise ValueError(
                "Missing ROV_USERNAME or ROV_PASSWORD in .env — refusing to proceed with empty credentials."
            )

        @retry_sync(
            component="playwright_scraper",
            operation="fill_login_form",
            max_attempts=self.settings.retry_max_attempts,
            min_wait=self.settings.retry_min_wait_seconds,
            max_wait=self.settings.retry_max_wait_seconds,
            retry_exceptions=(PlaywrightTimeoutError, PlaywrightError, RuntimeError),
        )
        def _fill() -> None:
            user_input = page.locator(LOGIN_SELECTORS["username"]).first
            pass_input = page.locator(LOGIN_SELECTORS["password"]).first
            user_input.wait_for(state="visible", timeout=15_000)
            user_input.fill(self.settings.rov_username)
            pass_input.fill(self.settings.rov_password)
            logger.info("Filled ROV Academy login credentials for user '%s'.", self.settings.rov_username)

        _fill()

    def _fetch_api_json_with_retry(
        self,
        url: str,
        *,
        method: str = "GET",
        payload: Optional[Dict[str, Any]] = None,
        cookies: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Fetch JSON from ROV Academy API endpoints with 3x retry + Sentry capture."""

        @retry_sync(
            component="rov_api",
            operation=f"{method} {url}",
            max_attempts=self.settings.retry_max_attempts,
            min_wait=self.settings.retry_min_wait_seconds,
            max_wait=self.settings.retry_max_wait_seconds,
            retry_exceptions=(httpx.HTTPError, OSError, TimeoutError, ValueError),
        )
        def _request() -> Dict[str, Any]:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json",
                "Origin": self.settings.rov_academy_url,
                "Referer": f"{self.settings.rov_academy_url}/",
            }
            with httpx.Client(timeout=20.0, cookies=cookies, follow_redirects=True) as client:
                if method.upper() == "POST":
                    resp = client.post(url, headers=headers, json=payload or {})
                else:
                    resp = client.get(url, headers=headers)
                resp.raise_for_status()
                return resp.json()

        return _request()

    def _extract_dom_fallback(self, page: Page, category: str, chapter_id: int) -> Tuple[str, str]:
        """
        Fallback DOM extractor: visits the chapter URL directly in Playwright and
        reads rendered heading (`Title`) and lesson body (`Content`).
        """
        chapter_url = f"{self.settings.rov_academy_url}/chapter/{chapter_id}"
        self._navigate_with_retry(page, chapter_url)
        page.wait_for_timeout(1500)

        title = ""
        for selector in ("h1", "h2", ".chapter-title", "[class*='title']"):
            loc = page.locator(selector).first
            if loc.count() > 0:
                candidate = loc.inner_text().strip()
                if candidate:
                    title = candidate
                    break

        content = ""
        for selector in ("main", "article", ".chapter-content", ".lesson-content", "body"):
            loc = page.locator(selector).first
            if loc.count() > 0:
                candidate = re.sub(r"\s+", " ", loc.inner_text()).strip()
                if len(candidate) > 40:
                    content = candidate[:1800]
                    break

        return title or f"{category} Chapter {chapter_id}", content

    def _scrape_four_categories(
        self,
        *,
        page: Optional[Page] = None,
        context: Optional[BrowserContext] = None,
    ) -> List[Dict[str, Any]]:
        """
        Loop through all 4 target categories (`MOBA101`, `แผนที่`, `ไอเทม`, `ตำแหน่ง`)
        and extract strictly `Title` and `Content` for every chapter.
        """
        cookies: Dict[str, str] = {}
        if context is not None:
            for ck in context.cookies():
                if "rov.in.th" in (ck.get("domain") or ""):
                    cookies[ck["name"]] = ck["value"]

        existing_lookup: Dict[str, Dict[str, Any]] = {
            row["external_key"]: row
            for row in self.repository._load_local_questions()
        }

        info_json = self._fetch_api_json_with_retry(
            f"{self.settings.rov_academy_url}/api/info",
            method="GET",
            cookies=cookies,
        )
        topics = (info_json.get("data") or {}).get("topics") or []

        # Collect chapters belonging exclusively to our 4 target categories
        selected_topics: List[Tuple[str, List[int]]] = []
        all_chapter_ids: List[int] = []
        for topic in topics:
            raw_title = str(topic.get("title") or "")
            canonical_cat = match_target_category(raw_title)
            if canonical_cat is None:
                continue
            chapter_ids = [int(cid) for cid in (topic.get("chapters") or [])]
            selected_topics.append((canonical_cat, chapter_ids))
            all_chapter_ids.extend(chapter_ids)

        if not all_chapter_ids:
            raise RuntimeError("No chapters found for the 4 target ROV Academy categories.")

        chapters_json = self._fetch_api_json_with_retry(
            f"{self.settings.rov_academy_url}/api/chapters",
            method="POST",
            payload={"chapter_id_list": all_chapter_ids},
            cookies=cookies,
        )
        chapters_map: Dict[str, Any] = (chapters_json.get("data") or {}).get("chapters") or {}

        extracted: List[Dict[str, Any]] = []
        for canonical_cat, chapter_ids in selected_topics:
            logger.info("Scraping category '%s' (%d chapters)...", canonical_cat, len(chapter_ids))
            for cid in chapter_ids:
                ch_obj = chapters_map.get(str(cid)) or {}
                title = str(ch_obj.get("title") or "").strip()

                content_parts: List[str] = []
                desc = strip_html(ch_obj.get("description"))
                if desc:
                    content_parts.append(desc)
                for section in ch_obj.get("content") or []:
                    sec_heading = str(section.get("section") or "").strip()
                    sec_body = strip_html(section.get("description"))
                    if sec_heading and sec_body:
                        content_parts.append(f"{sec_heading}: {sec_body}")
                    elif sec_body:
                        content_parts.append(sec_body)

                content = "\n".join(content_parts).strip()

                # If API payload was sparse and Playwright page is open, fall back to live DOM scraping
                if (not title or not content) and page is not None:
                    dom_title, dom_content = self._extract_dom_fallback(page, canonical_cat, cid)
                    title = title or dom_title
                    content = content or dom_content

                record = build_quiz_record(
                    chapter_id=cid,
                    category=canonical_cat,
                    title=title,
                    content=content,
                    source_url=f"{self.settings.rov_academy_url}/chapter/{cid}",
                    existing_quiz_lookup=existing_lookup,
                )
                extracted.append(record)
                logger.info("  [OK] [%s] Title='%s' (%d chars)", canonical_cat, title, len(content))

        return extracted

    def run_interactive_playwright(self) -> List[Dict[str, Any]]:
        """
        Full Phase 1 interactive Playwright pipeline:
          1. Launch Chromium and open ROV Academy OAuth login page
          2. Fill username & password from `.env`
          3. Call `page.pause()` so the user can solve Captcha manually and click Resume
          4. Loop over the 4 categories and extract Title + Content
          5. Close Playwright cleanly, then upsert all rows to Supabase `quiz_questions` (3x retry)
        """
        records: List[Dict[str, Any]] = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=self.settings.playwright_headless)
                context = browser.new_context(locale="th-TH")
                page = context.new_page()
                try:
                    self._navigate_with_retry(page, self.settings.rov_login_url)
                    self._fill_login_form_with_retry(page)

                    logger.info(
                        "=== PAUSED FOR CAPTCHA ===\n"
                        "กรุณาแก้ Captcha และกด Login ในหน้าต่างเบราว์เซอร์ จากนั้นกดปุ่ม Resume ใน Playwright Inspector"
                    )
                    # Explicit manual Captcha pause as required by Phase 1 specification
                    page.pause()

                    # Ensure we land on ROV Academy after manual login/captcha completion
                    if self.settings.rov_academy_url not in page.url:
                        self._navigate_with_retry(page, self.settings.rov_academy_url)

                    records = self._scrape_four_categories(page=page, context=context)
                finally:
                    browser.close()

            # Execute async Supabase upsert strictly AFTER sync_playwright exits its event loop
            saved_count, target = asyncio.run(self.repository.upsert_questions(records))
            logger.info(
                "Phase 1 Complete: Saved %d ROV Academy records to '%s' (table=%s).",
                saved_count,
                target,
                self.settings.supabase_table,
            )
            return records
        except Exception as exc:
            capture_error(exc, component="playwright_scraper", operation="run_interactive_playwright")
            raise

    def run_api_sync(self) -> List[Dict[str, Any]]:
        """Headless API-only sync mode for CI/containers without an interactive display."""
        try:
            records = self._scrape_four_categories(page=None, context=None)
            saved_count, target = asyncio.run(self.repository.upsert_questions(records))
            logger.info(
                "API Sync Complete: Saved %d ROV Academy records to '%s' (table=%s).",
                saved_count,
                target,
                self.settings.supabase_table,
            )
            return records
        except Exception as exc:
            capture_error(exc, component="playwright_scraper", operation="run_api_sync")
            raise


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Phase 1 ROV Academy Scraper -> Supabase quiz_questions"
    )
    parser.add_argument(
        "--api-only",
        action="store_true",
        help="Skip interactive Playwright browser and sync the 4 ROV Academy categories via API directly.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    settings = get_settings()
    init_sentry(settings)
    args = parse_args(argv)

    scraper = ROVAcademyScraper(settings)
    try:
        if args.api_only:
            scraper.run_api_sync()
        else:
            scraper.run_interactive_playwright()
        return 0
    except Exception as exc:
        logger.error("Scraper terminated with error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
