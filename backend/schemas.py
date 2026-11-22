"""Pydantic v2 request and response schemas for AI Bot Live Quiz Arena."""

from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

OptionKey = Literal["A", "B", "C", "D"]


class LoginRequest(BaseModel):
    """Credentials payload for POST /api/auth/login."""

    username: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=1, max_length=256)


class TokenResponse(BaseModel):
    """JWT access token response returned after successful login."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    username: str


class UserProfileResponse(BaseModel):
    """Authenticated user metadata returned by GET /api/auth/me."""

    username: str
    role: str = "arena_operator"


class QuizQuestionResponse(BaseModel):
    """Single quiz question returned by GET /api/questions/random."""

    id: int
    external_key: str
    category: str
    title: str
    content: str
    question_text: str
    options: Dict[OptionKey, str]
    source_url: str
    data_source: str = Field(
        default="supabase",
        description="Origin of the question ('supabase' or 'local_rov_cache')",
    )


class QuestionListResponse(BaseModel):
    """Summary list & category distribution of stored questions."""

    total: int
    categories: Dict[str, int]
    data_source: str
    questions: List[QuizQuestionResponse]


class BotAnswerRequest(BaseModel):
    """Request payload when the user clicks the Bolt Button to dispatch a question to the AI Bot."""

    question_id: int = Field(..., ge=1)


class BotAnswerResponse(BaseModel):
    """Result produced by the AI Bot after thinking for 2-4 seconds."""

    question_id: int
    category: str
    title: str
    selected_option: OptionKey
    selected_text: str
    is_correct: bool
    confidence: float = Field(..., ge=0.0, le=1.0)
    thinking_seconds: float
    explanation: str
    answered_at: str


class HealthResponse(BaseModel):
    """System health check payload for Docker/Kubernetes probes and UI status badge."""

    status: str
    data_source: str
    supabase_configured: bool
    sentry_enabled: bool
    questions_available: int
    active_ws_connections: int
