"""
Supabase database repository for `quiz_questions` with 3x retry & Sentry protection.

Architecture:
- When `SUPABASE_URL` and `SUPABASE_KEY` are configured:
  1. Uses the official `supabase` Python client (`create_client`) when installed, OR
  2. Talks directly to the Supabase PostgREST REST API (`/rest/v1/quiz_questions`)
     via `httpx.AsyncClient` — identical wire protocol, zero native C/Rust wheel
     barriers on ARM64 environments.
  All network calls pass through `execute_with_async_retry` (3 attempts with
  exponential backoff) and report terminal failures to Sentry.
- When Supabase credentials are not configured (fresh local clone / unit tests):
  Automatically loads the verified 4-category ROV Academy dataset from
  `data/quiz_questions.json` so the full stack works end-to-end out of the box.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from backend.config import Settings
from backend.monitoring import capture_error
from backend.retry_utils import execute_with_async_retry

logger = logging.getLogger("quiz_arena.database")

VALID_CATEGORIES = ("MOBA101", "แผนที่", "ไอเทม", "ตำแหน่ง")


class DatabaseConnectionError(RuntimeError):
    """Raised when Supabase remains unreachable after all retry attempts."""


class QuizRepository:
    """Data-access layer for reading and upserting `quiz_questions` records."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._sdk_client: Any = None
        self._init_optional_sdk_client()

    def _init_optional_sdk_client(self) -> None:
        """Initialize official `supabase` SDK if available and configured."""
        if not self.settings.has_supabase_config:
            return
        try:
            from supabase import create_client  # type: ignore[import-not-found]

            self._sdk_client = create_client(
                self.settings.supabase_url,
                self.settings.supabase_key,
            )
            logger.info("Initialized official supabase-py client for %s", self.settings.supabase_url)
        except ImportError:
            # PostgREST HTTP client via httpx handles Supabase identically without SDK
            logger.info("supabase-py not installed; using direct Supabase PostgREST HTTPS API.")
        except Exception as exc:
            capture_error(
                exc,
                component="database",
                operation="init_supabase_sdk",
                extra={"url": self.settings.supabase_url},
            )

    @property
    def active_source(self) -> str:
        """Human-readable indicator of the currently configured persistence backend."""
        return "supabase" if self.settings.has_supabase_config else "local_rov_cache"

    def _rest_headers(self, *, prefer: Optional[str] = None) -> Dict[str, str]:
        headers = {
            "apikey": self.settings.supabase_key,
            "Authorization": f"Bearer {self.settings.supabase_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        return headers

    def _table_url(self) -> str:
        return f"{self.settings.supabase_url}/rest/v1/{self.settings.supabase_table}"

    @staticmethod
    def _normalize_row(row: Dict[str, Any], fallback_id: int = 1) -> Dict[str, Any]:
        """Ensure consistent dictionary shape regardless of Supabase vs local cache origin."""
        raw_options = row.get("options") or {}
        if isinstance(raw_options, str):
            try:
                raw_options = json.loads(raw_options)
            except json.JSONDecodeError:
                raw_options = {}

        options: Dict[str, str] = {}
        for key in ("A", "B", "C", "D"):
            val = raw_options.get(key)
            if val is not None:
                options[key] = str(val)

        if len(options) < 4:
            # Guarantee all 4 choices A-D exist if raw scraped rows only stored title/content
            title = str(row.get("title") or "หัวข้อ RoV Academy")
            category = str(row.get("category") or "MOBA101")
            defaults = {
                "A": f"เนื้อหาหลักเกี่ยวกับ {title} ในหมวด {category}",
                "B": "ระบบแลกคูปองและร้านค้าสกินพิเศษ",
                "C": "การตั้งค่ากราฟิกและเสียงภายในเกม",
                "D": "ระบบแชทเสียงและรายชื่อเพื่อนในล็อบบี้",
            }
            for key in ("A", "B", "C", "D"):
                options.setdefault(key, defaults[key])

        correct = str(row.get("correct_option") or "A").strip().upper()
        if correct not in ("A", "B", "C", "D"):
            correct = "A"

        return {
            "id": int(row.get("id") or fallback_id),
            "external_key": str(row.get("external_key") or f"question_{fallback_id}"),
            "category": str(row.get("category") or "MOBA101"),
            "title": str(row.get("title") or "").strip(),
            "content": str(row.get("content") or "").strip(),
            "question_text": str(
                row.get("question_text") or f"ข้อใดอธิบายเนื้อหาเรื่อง '{row.get('title', '')}' ได้ถูกต้องที่สุด?"
            ).strip(),
            "options": options,
            "correct_option": correct,
            "explanation": str(row.get("explanation") or "").strip(),
            "source_url": str(row.get("source_url") or self.settings.rov_academy_url),
        }

    def _load_local_questions(self) -> List[Dict[str, Any]]:
        """Read local ROV Academy question dataset from JSON cache."""
        cache_path: Path = self.settings.local_cache_path
        if not cache_path.exists():
            logger.warning("Local question cache missing at %s", cache_path)
            return []
        try:
            raw_list = json.loads(cache_path.read_text(encoding="utf-8"))
            if not isinstance(raw_list, list):
                return []
            return [self._normalize_row(item, idx) for idx, item in enumerate(raw_list, start=1)]
        except Exception as exc:
            capture_error(
                exc,
                component="database",
                operation="load_local_cache",
                extra={"path": str(cache_path)},
            )
            return []

    def save_local_questions(self, rows: List[Dict[str, Any]]) -> Path:
        """Persist scraped questions locally with guaranteed unique sequential IDs."""
        cache_path: Path = self.settings.local_cache_path
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        normalized: List[Dict[str, Any]] = []
        seen_ids: set[int] = set()
        for idx, row in enumerate(rows, start=1):
            item = self._normalize_row(row, idx)
            if item["id"] in seen_ids:
                item["id"] = max(seen_ids, default=0) + 1
            seen_ids.add(item["id"])
            normalized.append(item)
        cache_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
        return cache_path

    async def _fetch_supabase_rows(self, category: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Fetch rows from Supabase with 3x exponential-backoff retry.

        Uses official `supabase-py` in a worker thread when available, otherwise calls
        Supabase PostgREST directly via `httpx.AsyncClient`. Catches `Exception` in
        the retry boundary so both `httpx.HTTPError` and `postgrest.exceptions.APIError`
        are retried 3 times and reported to Sentry on exhaustion.
        """

        async def _attempt() -> List[Dict[str, Any]]:
            if self._sdk_client is not None:
                def _run_sdk_query() -> List[Dict[str, Any]]:
                    query = self._sdk_client.table(self.settings.supabase_table).select("*")
                    if category:
                        query = query.eq("category", category)
                    response = query.execute()
                    return list(response.data or [])

                raw_rows = await asyncio.to_thread(_run_sdk_query)
                return [self._normalize_row(r, i) for i, r in enumerate(raw_rows, start=1)]

            params: Dict[str, str] = {"select": "*", "order": "id.asc"}
            if category:
                params["category"] = f"eq.{category}"

            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(
                    self._table_url(),
                    headers=self._rest_headers(),
                    params=params,
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, list):
                    raise DatabaseConnectionError(f"Unexpected Supabase payload: {type(payload).__name__}")
                return [self._normalize_row(r, i) for i, r in enumerate(payload, start=1)]

        try:
            return await execute_with_async_retry(
                _attempt,
                component="database",
                operation="supabase_select_questions",
                max_attempts=self.settings.retry_max_attempts,
                min_wait=self.settings.retry_min_wait_seconds,
                max_wait=self.settings.retry_max_wait_seconds,
                retry_exceptions=(Exception,),
            )
        except Exception as exc:
            raise DatabaseConnectionError(
                f"Failed to query Supabase table '{self.settings.supabase_table}' after "
                f"{self.settings.retry_max_attempts} attempts: {exc}"
            ) from exc

    async def upsert_questions(self, rows: List[Dict[str, Any]]) -> Tuple[int, str]:
        """
        Upsert scraped quiz questions into Supabase `quiz_questions` (3x retry) and mirror locally.

        Returns `(saved_count, target_source)`.
        """
        if not rows:
            return 0, self.active_source

        # Always keep local mirror synchronized for offline resilience
        self.save_local_questions(rows)

        if not self.settings.has_supabase_config:
            logger.info(
                "Supabase credentials not configured; saved %d questions to local cache (%s).",
                len(rows),
                self.settings.local_cache_path,
            )
            return len(rows), "local_rov_cache"

        # Strip synthetic local `id` so Supabase identity sequence manages primary keys
        db_payload = [
            {
                "external_key": r["external_key"],
                "category": r["category"],
                "title": r["title"],
                "content": r["content"],
                "question_text": r["question_text"],
                "options": r["options"],
                "correct_option": r["correct_option"],
                "explanation": r.get("explanation", ""),
                "source_url": r.get("source_url", self.settings.rov_academy_url),
            }
            for r in rows
        ]

        async def _attempt_upsert() -> int:
            if self._sdk_client is not None:
                def _run_sdk_upsert() -> int:
                    res = (
                        self._sdk_client.table(self.settings.supabase_table)
                        .upsert(db_payload, on_conflict="external_key")
                        .execute()
                    )
                    return len(res.data or db_payload)

                return await asyncio.to_thread(_run_sdk_upsert)

            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    self._table_url(),
                    headers=self._rest_headers(
                        prefer="resolution=merge-duplicates,return=representation"
                    ),
                    params={"on_conflict": "external_key"},
                    json=db_payload,
                )
                resp.raise_for_status()
                body = resp.json()
                return len(body) if isinstance(body, list) else len(db_payload)

        try:
            count = await execute_with_async_retry(
                _attempt_upsert,
                component="database",
                operation="supabase_upsert_questions",
                max_attempts=self.settings.retry_max_attempts,
                min_wait=self.settings.retry_min_wait_seconds,
                max_wait=self.settings.retry_max_wait_seconds,
                retry_exceptions=(Exception,),
            )
            return count, "supabase"
        except Exception as exc:
            raise DatabaseConnectionError(
                f"Failed to upsert into Supabase after {self.settings.retry_max_attempts} attempts: {exc}"
            ) from exc

    async def list_questions(self, category: Optional[str] = None) -> Tuple[List[Dict[str, Any]], str]:
        """
        Return all questions (optionally filtered by category) along with the active data source.

        If Supabase is configured but unreachable after 3 retries, falls back to the
        local ROV Academy cache when populated; if the local cache is also unavailable,
        re-raises `DatabaseConnectionError` so the API cleanly returns HTTP 503.
        """
        supabase_error: Optional[DatabaseConnectionError] = None
        if self.settings.has_supabase_config:
            try:
                rows = await self._fetch_supabase_rows(category=category)
                if rows:
                    return rows, "supabase"
            except DatabaseConnectionError as exc:
                supabase_error = exc
                logger.warning("Supabase unavailable after 3 retries; checking local cache: %s", exc)

        local_rows = self._load_local_questions()
        if category:
            local_rows = [r for r in local_rows if r["category"] == category]
        if not local_rows and supabase_error is not None:
            raise supabase_error
        return local_rows, "local_rov_cache"

    async def get_random_question(
        self,
        *,
        category: Optional[str] = None,
        exclude_id: Optional[int] = None,
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        """Fetch a random question from Supabase (or local fallback), avoiding immediate repeats."""
        rows, source = await self.list_questions(category=category)
        if not rows:
            return None, source

        pool = [r for r in rows if r["id"] != exclude_id] if exclude_id is not None and len(rows) > 1 else rows
        return random.choice(pool), source

    async def get_question_by_id(self, question_id: int) -> Tuple[Optional[Dict[str, Any]], str]:
        """Look up a specific question by numeric ID."""
        if self.settings.has_supabase_config:
            try:
                async def _attempt_by_id() -> Optional[Dict[str, Any]]:
                    if self._sdk_client is not None:
                        def _sdk_get() -> Optional[Dict[str, Any]]:
                            res = (
                                self._sdk_client.table(self.settings.supabase_table)
                                .select("*")
                                .eq("id", question_id)
                                .limit(1)
                                .execute()
                            )
                            return res.data[0] if res.data else None

                        row = await asyncio.to_thread(_sdk_get)
                        return self._normalize_row(row, question_id) if row else None

                    async with httpx.AsyncClient(timeout=15.0) as client:
                        resp = await client.get(
                            self._table_url(),
                            headers=self._rest_headers(),
                            params={"select": "*", "id": f"eq.{question_id}", "limit": "1"},
                        )
                        resp.raise_for_status()
                        data = resp.json()
                        if isinstance(data, list) and data:
                            return self._normalize_row(data[0], question_id)
                        return None

                found = await execute_with_async_retry(
                    _attempt_by_id,
                    component="database",
                    operation="supabase_get_question_by_id",
                    max_attempts=self.settings.retry_max_attempts,
                    min_wait=self.settings.retry_min_wait_seconds,
                    max_wait=self.settings.retry_max_wait_seconds,
                    retry_exceptions=(Exception,),
                )
                if found is not None:
                    return found, "supabase"
            except Exception as exc:
                logger.warning("Supabase get_question_by_id failed after 3 retries: %s", exc)

        local_rows = self._load_local_questions()
        for row in local_rows:
            if row["id"] == question_id:
                return row, "local_rov_cache"
        return None, "local_rov_cache"
