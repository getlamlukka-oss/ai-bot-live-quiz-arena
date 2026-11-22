"""
Real-time WebSocket ConnectionManager & Server-Sent Events (SSE) Event Broker.

Broadcasts structured live Arena telemetry (`[User]`, `[AI]`, `[System]`, `[Error]`)
to all connected browser consoles simultaneously, isolating per-client send errors
with `try-except` + Sentry reporting so one dropped socket never disrupts others.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Set

from fastapi import WebSocket

from backend.monitoring import capture_error

logger = logging.getLogger("quiz_arena.realtime")


class RealtimeEventHub:
    """Thread/task-safe real-time hub supporting both WebSocket and SSE subscribers."""

    def __init__(self, history_limit: int = 80) -> None:
        self._ws_connections: Set[WebSocket] = set()
        self._sse_queues: Set[asyncio.Queue[Dict[str, Any]]] = set()
        self._history: Deque[Dict[str, Any]] = deque(maxlen=history_limit)
        self._lock = asyncio.Lock()

    @property
    def active_ws_count(self) -> int:
        return len(self._ws_connections)

    @property
    def recent_history(self) -> List[Dict[str, Any]]:
        return list(self._history)

    async def connect_ws(self, websocket: WebSocket, username: str) -> None:
        """Accept a WebSocket handshake, register the client, and replay recent logs."""
        try:
            await websocket.accept()
            async with self._lock:
                self._ws_connections.add(websocket)
                history_snapshot = list(self._history)

            # Replay recent events so reconnecting clients restore live console context
            for past_event in history_snapshot[-15:]:
                await websocket.send_json(past_event)

            await self.emit_log(
                actor="System",
                message=f"เชื่อมต่อ WebSocket Real-time สำเร็จ (ผู้ใช้: {username})",
                event_type="ws_connected",
                level="success",
                meta={"username": username, "active_clients": self.active_ws_count},
            )
        except Exception as exc:
            capture_error(
                exc,
                component="websocket",
                operation="connect_ws",
                extra={"username": username},
            )
            await self.disconnect_ws(websocket)
            raise

    async def disconnect_ws(self, websocket: WebSocket) -> None:
        """Safely remove a disconnected WebSocket client without raising."""
        async with self._lock:
            self._ws_connections.discard(websocket)

    def subscribe_sse(self) -> asyncio.Queue[Dict[str, Any]]:
        """Register an SSE subscriber queue for EventSource clients."""
        queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(maxsize=100)
        self._sse_queues.add(queue)
        return queue

    def unsubscribe_sse(self, queue: asyncio.Queue[Dict[str, Any]]) -> None:
        """Remove an SSE subscriber queue cleanly on disconnect."""
        self._sse_queues.discard(queue)

    async def emit_log(
        self,
        *,
        actor: str,
        message: str,
        event_type: str = "log",
        level: str = "info",
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Build a standardized real-time log event and broadcast to WebSocket + SSE clients.

        Formatted text follows `[Actor] message` (e.g. `[User] ส่งคำถาม`,
        `[AI] กำลังประมวลผล...`, `[AI] ตอบข้อ B`).
        """
        timestamp = datetime.now(timezone.utc).isoformat()
        event: Dict[str, Any] = {
            "type": event_type,
            "actor": actor,
            "level": level,
            "message": message,
            "formatted": f"[{actor}] {message}",
            "timestamp": timestamp,
            "meta": meta or {},
        }
        await self.broadcast(event)
        return event

    async def broadcast(self, event: Dict[str, Any]) -> None:
        """Fan out a JSON event to every active WebSocket & SSE subscriber."""
        async with self._lock:
            self._history.append(event)
            ws_snapshot = list(self._ws_connections)
            sse_snapshot = list(self._sse_queues)

        stale_sockets: List[WebSocket] = []
        for ws in ws_snapshot:
            try:
                await ws.send_json(event)
            except Exception as exc:
                capture_error(
                    exc,
                    component="websocket",
                    operation="broadcast_send",
                    extra={"event_type": event.get("type")},
                )
                stale_sockets.append(ws)

        if stale_sockets:
            async with self._lock:
                for ws in stale_sockets:
                    self._ws_connections.discard(ws)

        for queue in sse_snapshot:
            try:
                if queue.full():
                    queue.get_nowait()
                queue.put_nowait(event)
            except Exception as exc:
                capture_error(exc, component="sse", operation="enqueue_event")

    @staticmethod
    def format_sse_frame(event: Dict[str, Any]) -> str:
        """Serialize an event dictionary into standard Server-Sent Events wire format."""
        return f"event: {event.get('type', 'log')}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
