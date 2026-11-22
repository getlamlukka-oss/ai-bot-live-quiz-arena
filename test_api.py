"""
Unit & Integration Test Suite for AI Bot Live Quiz Arena (`test_api.py`).

Covers:
  1. JWT Authentication (valid credentials -> JWT, invalid credentials -> 401, protected guard)
  2. Random Quiz Question retrieval (`/api/questions/random`) across all 4 ROV categories
  3. AI Bot answering (`/api/bot/answer`) with timing validation & answer accuracy
  4. Real-time WebSocket stream (`/ws/logs`) emitting `[User]` and `[AI]` logs live
  5. Resilient 3-attempt Retry logic & Sentry exception capture on external failures
  6. Phase 1 Scraper HTML cleanup & 4-category normalization
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from backend.config import DEFAULT_LOCAL_CACHE_PATH, Settings
from backend.database import DatabaseConnectionError, QuizRepository
from backend.retry_utils import execute_with_async_retry, retry_sync
from scraper import match_target_category, strip_html
from server import create_app


@pytest.fixture()
def test_settings() -> Settings:
    """Isolated deterministic settings (no external network calls, zero hardcoded prod secrets)."""
    return Settings(
        app_env="testing",
        host="127.0.0.1",
        port=8080,
        app_username="arena_tester",
        app_password="StrongTestPassword!99",
        jwt_secret_key="unit-test-secret-key-at-least-32-bytes-long-0123456789",
        jwt_algorithm="HS256",
        jwt_expire_minutes=60,
        supabase_url="",
        supabase_key="",
        sentry_dsn="",
        bot_min_think_seconds=0.05,
        bot_max_think_seconds=0.10,
        retry_max_attempts=3,
        retry_min_wait_seconds=0.01,
        retry_max_wait_seconds=0.02,
        local_cache_path=DEFAULT_LOCAL_CACHE_PATH,
    )


@pytest.fixture()
def client(test_settings: Settings) -> TestClient:
    app = create_app(custom_settings=test_settings)
    with TestClient(app) as test_client:
        yield test_client


def _login_and_get_token(client: TestClient, settings: Settings) -> str:
    resp = client.post(
        "/api/auth/login",
        json={"username": settings.app_username, "password": settings.app_password},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


# =============================================================================
# 1. Authentication & JWT Tests
# =============================================================================


def test_login_success_issues_valid_jwt(client: TestClient, test_settings: Settings) -> None:
    response = client.post(
        "/api/auth/login",
        json={
            "username": test_settings.app_username,
            "password": test_settings.app_password,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["username"] == test_settings.app_username
    assert body["expires_in"] == 3600

    # Verify cryptographic signature & required claims
    decoded = jwt.decode(
        body["access_token"],
        test_settings.jwt_secret_key,
        algorithms=[test_settings.jwt_algorithm],
    )
    assert decoded["sub"] == test_settings.app_username
    assert "exp" in decoded and "iat" in decoded


def test_login_invalid_credentials_returns_401(client: TestClient, test_settings: Settings) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": test_settings.app_username, "password": "wrong-password"},
    )
    assert response.status_code == 401


def test_protected_endpoint_rejects_missing_or_bad_token(client: TestClient) -> None:
    assert client.get("/api/questions/random").status_code == 401
    assert (
        client.get(
            "/api/questions/random",
            headers={"Authorization": "Bearer not.a.valid.jwt"},
        ).status_code
        == 401
    )


# =============================================================================
# 2. Quiz Question Retrieval Tests (4 ROV Academy Categories)
# =============================================================================


def test_random_question_returns_full_rov_record(
    client: TestClient, test_settings: Settings
) -> None:
    token = _login_and_get_token(client, test_settings)
    headers = {"Authorization": f"Bearer {token}"}

    resp = client.get("/api/questions/random", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["category"] in {"MOBA101", "แผนที่", "ไอเทม", "ตำแหน่ง"}
    assert len(data["title"]) > 0
    assert len(data["content"]) > 20
    assert set(data["options"].keys()) == {"A", "B", "C", "D"}


@pytest.mark.parametrize("category", ["MOBA101", "แผนที่", "ไอเทม", "ตำแหน่ง"])
def test_random_question_category_filter(
    client: TestClient, test_settings: Settings, category: str
) -> None:
    token = _login_and_get_token(client, test_settings)
    resp = client.get(
        "/api/questions/random",
        params={"category": category},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["category"] == category


# =============================================================================
# 3. AI Bot Answering & Real-Time WebSocket Log Stream Tests
# =============================================================================


def test_ai_bot_answers_correctly_and_streams_ws_logs(
    client: TestClient, test_settings: Settings
) -> None:
    token = _login_and_get_token(client, test_settings)
    headers = {"Authorization": f"Bearer {token}"}

    q_resp = client.get("/api/questions/random", headers=headers)
    assert q_resp.status_code == 200
    question = q_resp.json()

    with client.websocket_connect(f"/ws/logs?token={token}") as ws:
        # Receive initial replay/connected event
        first_evt = ws.receive_json()
        assert "actor" in first_evt and "message" in first_evt

        # Trigger AI Bot Bolt Answer while WebSocket is connected
        ans_resp = client.post(
            "/api/bot/answer",
            json={"question_id": question["id"]},
            headers=headers,
        )
        assert ans_resp.status_code == 200
        answer = ans_resp.json()
        assert answer["question_id"] == question["id"]
        assert answer["selected_option"] in {"A", "B", "C", "D"}
        assert answer["is_correct"] is True
        assert 0.05 <= answer["thinking_seconds"] <= 0.10
        assert len(answer["explanation"]) > 0

        # Drain streamed events and verify [User], [AI] thinking, and [AI] answered logs
        collected = []
        for _ in range(10):
            evt = ws.receive_json()
            collected.append(evt)
            if evt.get("type") == "ai_answered":
                break

        event_types = {e.get("type") for e in collected}
        assert "question_dispatched" in event_types
        assert "ai_thinking" in event_types
        assert "ai_answered" in event_types
        final_evt = next(e for e in collected if e.get("type") == "ai_answered")
        assert f"ตอบข้อ {answer['selected_option']}" in final_evt["message"]


def test_websocket_rejects_invalid_token(client: TestClient) -> None:
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/logs?token=invalid_token") as ws:
            ws.receive_json()


# =============================================================================
# 4. 3x Retry Resilience & Sentry Capture Tests
# =============================================================================


def test_sync_retry_recovers_on_third_attempt() -> None:
    attempts = {"count": 0}

    @retry_sync(
        component="test",
        operation="flaky_op",
        max_attempts=3,
        min_wait=0.01,
        max_wait=0.02,
        retry_exceptions=(ConnectionError,),
    )
    def flaky_call() -> str:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise ConnectionError("temporary network glitch")
        return "recovered"

    assert flaky_call() == "recovered"
    assert attempts["count"] == 3


def test_async_retry_exhausts_3_attempts_and_reports_to_sentry() -> None:
    attempts = {"count": 0}

    async def always_fails() -> None:
        attempts["count"] += 1
        raise httpx.ConnectError("supabase unreachable")

    with patch("backend.retry_utils.capture_error") as mock_sentry_capture:
        with pytest.raises(httpx.ConnectError):
            asyncio.run(
                execute_with_async_retry(
                    always_fails,
                    component="database",
                    operation="supabase_select",
                    max_attempts=3,
                    min_wait=0.01,
                    max_wait=0.02,
                    retry_exceptions=(httpx.HTTPError,),
                )
            )

    assert attempts["count"] == 3
    mock_sentry_capture.assert_called_once()


def test_supabase_repository_retries_3_times_before_raising() -> None:
    cfg = Settings(
        supabase_url="https://demo-project.supabase.co",
        supabase_key="demo-secret-key",
        retry_max_attempts=3,
        retry_min_wait_seconds=0.01,
        retry_max_wait_seconds=0.02,
    )
    repo = QuizRepository(cfg)
    repo._sdk_client = None  # Exercise direct PostgREST HTTP retry path

    call_counter = {"n": 0}

    async def failing_get(self, *args, **kwargs):
        call_counter["n"] += 1
        raise httpx.ConnectTimeout("timeout")

    with patch.object(httpx.AsyncClient, "get", new=failing_get):
        with pytest.raises(DatabaseConnectionError):
            asyncio.run(repo._fetch_supabase_rows())

    assert call_counter["n"] == 3


def test_random_question_returns_503_when_supabase_down_and_no_local_cache(tmp_path) -> None:
    down_settings = Settings(
        app_env="testing",
        app_username="arena_tester",
        app_password="StrongTestPassword!99",
        jwt_secret_key="unit-test-secret-key-at-least-32-bytes-long-0123456789",
        supabase_url="https://unreachable.supabase.co",
        supabase_key="demo-key",
        retry_max_attempts=3,
        retry_min_wait_seconds=0.01,
        retry_max_wait_seconds=0.02,
        local_cache_path=tmp_path / "missing_cache.json",
    )
    app = create_app(custom_settings=down_settings)
    app.state.repository._sdk_client = None

    async def always_timeout(self, *args, **kwargs):
        raise httpx.ConnectTimeout("simulated outage")

    with TestClient(app) as tc:
        token = _login_and_get_token(tc, down_settings)
        with patch.object(httpx.AsyncClient, "get", new=always_timeout):
            resp = tc.get(
                "/api/questions/random",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 503


# =============================================================================
# 5. Phase 1 Scraper Utility Tests
# =============================================================================


def test_scraper_helpers_clean_html_and_match_4_categories() -> None:
    cleaned = strip_html("<p>MOBA <b>101</b><br/>RoV&nbsp;Academy</p>")
    assert cleaned == "MOBA 101 \nRoV\xa0Academy" or "MOBA" in cleaned and "<" not in cleaned
    assert match_target_category("MOBA101") == "MOBA101"
    assert match_target_category("แผนที่ RoV") == "แผนที่"
    assert match_target_category("ไอเทมในเกม") == "ไอเทม"
    assert match_target_category("ตำแหน่งในเกม") == "ตำแหน่ง"
    assert match_target_category("การแข่งขันอีสปอร์ต") is None
