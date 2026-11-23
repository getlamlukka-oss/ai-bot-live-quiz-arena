"""
AI Bot Live Quiz Arena — FastAPI Backend Server (Port 8080).

Endpoints:
  - POST /api/auth/login         Authenticate credentials from `.env` and issue HS256 JWT
  - GET  /api/auth/me            Verify active JWT token
  - GET  /api/health             Service health, Supabase status, Sentry status, WS count
  - GET  /api/questions/random   Fetch a random ROV Academy question (Supabase + 3x retry)
  - GET  /api/questions          List all questions & category counts
  - POST /api/bot/answer         Dispatch question to AI Bot (2-4s thinking + real-time logs)
  - WS   /ws/logs?token=<jwt>    Real-time bidirectional log stream
  - GET  /api/events/stream      Real-time Server-Sent Events (SSE) fallback stream
  - GET  /                       Serves the Dark Glassmorphism Frontend UI
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Dict, Optional

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from backend.ai_bot import QuizAIBot
from backend.auth import create_access_token, decode_access_token, get_current_user, verify_credentials
from backend.config import FRONTEND_DIR, Settings, get_settings
from backend.database import DatabaseConnectionError, QuizRepository
from backend.monitoring import capture_error, configure_logging, init_sentry
from backend.schemas import (
    BotAnswerRequest,
    BotAnswerResponse,
    HealthResponse,
    LoginRequest,
    QuestionListResponse,
    QuizQuestionResponse,
    TokenResponse,
    UserProfileResponse,
)
from backend.ws_manager import RealtimeEventHub

logger = configure_logging()


def create_app(custom_settings: Optional[Settings] = None) -> FastAPI:
    """
    Application factory that wires Settings, Sentry, Supabase Repository,
    WebSocket/SSE Event Hub, and AI Bot cleanly for both production and pytest.
    """
    settings = custom_settings or get_settings()
    sentry_enabled = init_sentry(settings)
    repository = QuizRepository(settings)
    event_hub = RealtimeEventHub()
    ai_bot = QuizAIBot(settings=settings, event_hub=event_hub)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
        logger.info(
            "Starting AI Bot Live Quiz Arena on %s:%d (data_source=%s, sentry=%s)",
            settings.host,
            settings.port,
            repository.active_source,
            sentry_enabled,
        )
        yield
        logger.info("Shutting down AI Bot Live Quiz Arena cleanly.")

    app = FastAPI(
        title="AI Bot Live Quiz Arena API",
        description="Full-Stack Real-time AI Quiz Arena backed by ROV Academy, Supabase, Sentry & WebSockets.",
        version="1.0.0",
        lifespan=lifespan,
    )

    # Expose initialized singletons on app.state for dependency injection & unit testing
    app.state.settings = settings
    app.state.sentry_enabled = sentry_enabled
    app.state.repository = repository
    app.state.event_hub = event_hub
    app.state.ai_bot = ai_bot

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials="*" not in settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # -------------------------------------------------------------------------
    # Global Sentry Exception Guard
    # -------------------------------------------------------------------------
    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        capture_error(
            exc,
            component="fastapi",
            operation=f"{request.method} {request.url.path}",
            extra={"client": request.client.host if request.client else "unknown"},
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal server error captured and logged for investigation."},
        )

    # Dependency resolvers bound to this app instance (supports isolated test overrides)
    def resolve_settings() -> Settings:
        return app.state.settings

    def resolve_repository() -> QuizRepository:
        return app.state.repository

    def resolve_hub() -> RealtimeEventHub:
        return app.state.event_hub

    def resolve_bot() -> QuizAIBot:
        return app.state.ai_bot

    def resolve_user(
        current_user: str = Depends(get_current_user),
    ) -> str:
        return current_user

    # Override default get_settings when custom_settings is passed (e.g., pytest)
    if custom_settings is not None:
        app.dependency_overrides[get_settings] = lambda: custom_settings

    # -------------------------------------------------------------------------
    # Health & Diagnostics
    # -------------------------------------------------------------------------
    @app.get("/api/health", response_model=HealthResponse, tags=["System"])
    async def health_check(
        repo: QuizRepository = Depends(resolve_repository),
        hub: RealtimeEventHub = Depends(resolve_hub),
        cfg: Settings = Depends(resolve_settings),
    ) -> HealthResponse:
        try:
            rows, source = await repo.list_questions()
            return HealthResponse(
                status="ok",
                data_source=source,
                supabase_configured=cfg.has_supabase_config,
                sentry_enabled=bool(app.state.sentry_enabled),
                questions_available=len(rows),
                active_ws_connections=hub.active_ws_count,
            )
        except Exception as exc:
            capture_error(exc, component="database", operation="health_check")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Database health check failed.",
            ) from exc

    # -------------------------------------------------------------------------
    # Authentication (JWT)
    # -------------------------------------------------------------------------
    @app.post("/api/auth/login", response_model=TokenResponse, tags=["Auth"])
    async def login(
        payload: LoginRequest,
        cfg: Settings = Depends(resolve_settings),
        hub: RealtimeEventHub = Depends(resolve_hub),
    ) -> TokenResponse:
        try:
            if not verify_credentials(payload.username, payload.password, cfg):
                await hub.emit_log(
                    actor="System",
                    message=f"ปฏิเสธการเข้าสู่ระบบของบัญชี '{payload.username}' (รหัสผ่านไม่ถูกต้อง)",
                    event_type="auth_failed",
                    level="warning",
                )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง (Invalid username or password)",
                    headers={"WWW-Authenticate": "Bearer"},
                )

            token, expires_in = create_access_token(payload.username, cfg)
            await hub.emit_log(
                actor="System",
                message=f"ผู้ใช้ '{payload.username}' เข้าสู่ระบบและออก JWT Token สำเร็จ",
                event_type="auth_success",
                level="success",
                meta={"username": payload.username, "expires_in": expires_in},
            )
            return TokenResponse(
                access_token=token,
                token_type="bearer",
                expires_in=expires_in,
                username=payload.username,
            )
        except HTTPException:
            raise
        except Exception as exc:
            capture_error(exc, component="auth", operation="login", extra={"username": payload.username})
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Authentication service error.",
            ) from exc

    @app.get("/api/auth/me", response_model=UserProfileResponse, tags=["Auth"])
    async def get_me(username: str = Depends(resolve_user)) -> UserProfileResponse:
        return UserProfileResponse(username=username)

    # -------------------------------------------------------------------------
    # Quiz Question Bank (Supabase + 3x Retry)
    # -------------------------------------------------------------------------
    @app.get("/api/questions/random", response_model=QuizQuestionResponse, tags=["Quiz"])
    async def get_random_question(
        category: Optional[str] = Query(
            default=None,
            description=(
                "Optional category filter: MOBA101, แผนที่, ไอเทม, ตำแหน่ง, "
                "อีสปอร์ต, ทริกตีป้อม, เทคนิคฮีโร่, Garena"
            ),
        ),
        exclude_id: Optional[int] = Query(
            default=None,
            ge=1,
            description="Previous question ID to avoid immediate consecutive repeats",
        ),
        username: str = Depends(resolve_user),
        repo: QuizRepository = Depends(resolve_repository),
        hub: RealtimeEventHub = Depends(resolve_hub),
    ) -> QuizQuestionResponse:
        try:
            question, source = await repo.get_random_question(
                category=category,
                exclude_id=exclude_id,
            )
            if question is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"ไม่พบคำถามในหมวด '{category or 'ทั้งหมด'}' ในฐานข้อมูล",
                )

            await hub.emit_log(
                actor="System",
                message=f"ดึงคำถามสุ่ม #{question['id']} [{question['category']}] {question['title']} (จาก {source})",
                event_type="question_loaded",
                level="info",
                meta={"question_id": question["id"], "source": source, "requested_by": username},
            )
            return QuizQuestionResponse(
                id=question["id"],
                external_key=question["external_key"],
                category=question["category"],
                title=question["title"],
                content=question["content"],
                question_text=question["question_text"],
                options=question["options"],
                source_url=question["source_url"],
                data_source=source,
            )
        except HTTPException:
            raise
        except DatabaseConnectionError as exc:
            capture_error(exc, component="database", operation="get_random_question")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="ไม่สามารถเชื่อมต่อฐานข้อมูล Supabase ได้หลังจากลองใหม่ 3 ครั้ง",
            ) from exc
        except Exception as exc:
            capture_error(exc, component="database", operation="get_random_question")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="เกิดข้อผิดพลาดขณะดึงคำถาม",
            ) from exc

    @app.get("/api/questions", response_model=QuestionListResponse, tags=["Quiz"])
    async def list_questions(
        category: Optional[str] = Query(default=None),
        _username: str = Depends(resolve_user),
        repo: QuizRepository = Depends(resolve_repository),
    ) -> QuestionListResponse:
        try:
            rows, source = await repo.list_questions(category=category)
            counts: Dict[str, int] = {}
            for row in rows:
                cat = row["category"]
                counts[cat] = counts.get(cat, 0) + 1

            return QuestionListResponse(
                total=len(rows),
                categories=counts,
                data_source=source,
                questions=[
                    QuizQuestionResponse(
                        id=r["id"],
                        external_key=r["external_key"],
                        category=r["category"],
                        title=r["title"],
                        content=r["content"],
                        question_text=r["question_text"],
                        options=r["options"],
                        source_url=r["source_url"],
                        data_source=source,
                    )
                    for r in rows
                ],
            )
        except Exception as exc:
            capture_error(exc, component="database", operation="list_questions")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="ไม่สามารถโหลดรายการคำถามได้",
            ) from exc

    # -------------------------------------------------------------------------
    # AI Bot Answer Endpoint
    # -------------------------------------------------------------------------
    @app.post("/api/bot/answer", response_model=BotAnswerResponse, tags=["AI Bot"])
    async def trigger_bot_answer(
        payload: BotAnswerRequest,
        username: str = Depends(resolve_user),
        repo: QuizRepository = Depends(resolve_repository),
        bot: QuizAIBot = Depends(resolve_bot),
    ) -> BotAnswerResponse:
        try:
            question, _source = await repo.get_question_by_id(payload.question_id)
            if question is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"ไม่พบคำถามรหัส #{payload.question_id}",
                )
            return await bot.answer_question(question=question, requested_by=username)
        except HTTPException:
            raise
        except Exception as exc:
            capture_error(
                exc,
                component="ai_bot",
                operation="trigger_bot_answer",
                extra={"question_id": payload.question_id, "username": username},
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="AI Bot ไม่สามารถประมวลผลคำตอบได้ในขณะนี้",
            ) from exc

    # -------------------------------------------------------------------------
    # Real-time Streaming: WebSocket (Primary) + SSE (Fallback)
    # -------------------------------------------------------------------------
    @app.websocket("/ws/logs")
    async def websocket_logs(
        websocket: WebSocket,
        token: str = Query(default=""),
    ) -> None:
        """
        JWT-authenticated WebSocket endpoint streaming live Arena events.

        Browser WebSockets cannot send custom `Authorization` headers, so the JWT
        is verified via the `token` query parameter before accepting the connection.
        """
        cfg: Settings = app.state.settings
        hub: RealtimeEventHub = app.state.event_hub
        try:
            if not token:
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Missing JWT token")
                return
            claims = decode_access_token(token, cfg)
            username = str(claims.get("sub") or "").strip()
            if not username:
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid JWT subject")
                return
        except HTTPException:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid or expired JWT")
            return
        except Exception as exc:
            capture_error(exc, component="websocket", operation="ws_auth")
            await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
            return

        try:
            await hub.connect_ws(websocket, username=username)
            while True:
                # Keepalive listener: respond to client heartbeats and allow interactive pings
                incoming = await websocket.receive_text()
                if incoming.strip().lower() == "ping":
                    await websocket.send_json({"type": "pong", "actor": "System", "message": "pong"})
        except WebSocketDisconnect:
            await hub.disconnect_ws(websocket)
            logger.info("WebSocket client '%s' disconnected cleanly.", username)
        except Exception as exc:
            capture_error(
                exc,
                component="websocket",
                operation="ws_loop",
                extra={"username": username},
            )
            await hub.disconnect_ws(websocket)

    @app.get("/api/events/stream", tags=["Realtime"])
    async def sse_event_stream(
        request: Request,
        token: str = Query(..., description="JWT access token for EventSource auth"),
        cfg: Settings = Depends(resolve_settings),
        hub: RealtimeEventHub = Depends(resolve_hub),
    ) -> StreamingResponse:
        """Server-Sent Events (SSE) real-time fallback stream if WebSockets are blocked."""
        claims = decode_access_token(token, cfg)
        username = str(claims.get("sub") or "").strip()
        if not username:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token payload missing subject claim.",
            )
        queue = hub.subscribe_sse()

        async def _generator() -> AsyncGenerator[str, None]:
            try:
                for past_event in hub.recent_history[-15:]:
                    yield hub.format_sse_frame(past_event)

                welcome = {
                    "type": "sse_connected",
                    "actor": "System",
                    "level": "success",
                    "message": f"เชื่อมต่อ SSE Real-time สำเร็จ (ผู้ใช้: {username})",
                    "formatted": f"[System] เชื่อมต่อ SSE Real-time สำเร็จ (ผู้ใช้: {username})",
                }
                yield hub.format_sse_frame(welcome)
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=20.0)
                        yield hub.format_sse_frame(event)
                    except asyncio.TimeoutError:
                        # SSE keepalive comment frame prevents idle proxy disconnects
                        yield ": keepalive\n\n"
            except Exception as exc:
                capture_error(exc, component="sse", operation="stream_generator")
            finally:
                hub.unsubscribe_sse(queue)

        return StreamingResponse(
            _generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # -------------------------------------------------------------------------
    # Frontend Static Assets Serving (Single-Server Full-Stack Convenience)
    # -------------------------------------------------------------------------
    if FRONTEND_DIR.exists():
        @app.get("/", include_in_schema=False)
        async def serve_index() -> FileResponse:
            return FileResponse(FRONTEND_DIR / "index.html")

        app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="frontend_static")

        @app.get("/style.css", include_in_schema=False)
        async def serve_css() -> FileResponse:
            return FileResponse(FRONTEND_DIR / "style.css", media_type="text/css")

        @app.get("/app.js", include_in_schema=False)
        async def serve_js() -> FileResponse:
            return FileResponse(FRONTEND_DIR / "app.js", media_type="application/javascript")

    return app


app = create_app()


if __name__ == "__main__":
    cfg = get_settings()
    uvicorn.run(
        "server:app",
        host=cfg.host,
        port=cfg.port,
        reload=False,
        log_level="info",
    )
