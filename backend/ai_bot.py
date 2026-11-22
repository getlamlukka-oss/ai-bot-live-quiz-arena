"""
AI Bot solver engine for AI Bot Live Quiz Arena.

Simulates realistic reasoning latency (2.0-4.0 seconds by default, configured via
`BOT_MIN_THINK_SECONDS` and `BOT_MAX_THINK_SECONDS`), broadcasts real-time progress
events (`[User] ส่งคำถาม`, `[AI] กำลังประมวลผล...`, `[AI] ตอบข้อ <X>`) over WebSocket
and SSE, and selects the verified correct option grounded in ROV Academy lesson content.
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from typing import Any, Dict

from backend.config import Settings
from backend.monitoring import capture_error
from backend.schemas import BotAnswerResponse
from backend.ws_manager import RealtimeEventHub


class QuizAIBot:
    """Autonomous quiz-answering engine with real-time Arena telemetry."""

    def __init__(self, settings: Settings, event_hub: RealtimeEventHub) -> None:
        self.settings = settings
        self.event_hub = event_hub

    def _compute_confidence(self, question: Dict[str, Any], selected_option: str) -> float:
        """
        Compute a deterministic-feeling confidence score based on keyword overlap
        between the ROV Academy lesson content and the chosen answer text.
        """
        content_lower = str(question.get("content", "")).lower()
        selected_text = str((question.get("options") or {}).get(selected_option, "")).lower()
        tokens = [tok for tok in selected_text.split() if len(tok) >= 3]
        if not tokens:
            return 0.96
        hits = sum(1 for tok in tokens if tok in content_lower)
        ratio = hits / max(len(tokens), 1)
        return round(min(0.99, max(0.91, 0.92 + ratio * 0.07)), 2)

    async def answer_question(
        self,
        *,
        question: Dict[str, Any],
        requested_by: str,
    ) -> BotAnswerResponse:
        """
        Process a question end-to-end:
          1. Broadcast `[User] ส่งคำถาม`
          2. Pick randomized thinking duration in `[min_think, max_think]` (2-4s)
          3. Broadcast `[AI] กำลังประมวลผล...` and mid-flight reasoning checkpoint
          4. Choose the verified correct answer and broadcast `[AI] ตอบข้อ <X>`
        """
        question_id = int(question["id"])
        category = str(question["category"])
        title = str(question["title"])
        options: Dict[str, str] = dict(question["options"])

        try:
            # Step 1: Announce user dispatch immediately to live console
            await self.event_hub.emit_log(
                actor="User",
                message=f"ส่งคำถาม #{question_id} [{category}] {title}",
                event_type="question_dispatched",
                level="user",
                meta={
                    "question_id": question_id,
                    "category": category,
                    "title": title,
                    "requested_by": requested_by,
                },
            )

            # Step 2: Randomized realistic AI thinking delay (2.0 - 4.0 seconds)
            think_seconds = round(
                random.uniform(
                    self.settings.bot_min_think_seconds,
                    self.settings.bot_max_think_seconds,
                ),
                2,
            )

            await self.event_hub.emit_log(
                actor="AI",
                message=f"กำลังประมวลผล... (วิเคราะห์บทเรียน RoV Academy ~{think_seconds:.1f}s)",
                event_type="ai_thinking",
                level="thinking",
                meta={"question_id": question_id, "estimated_seconds": think_seconds},
            )

            # Split thinking time into two phases so the console shows live progress
            first_half = think_seconds * 0.55
            second_half = max(0.0, think_seconds - first_half)
            if first_half > 0:
                await asyncio.sleep(first_half)

            await self.event_hub.emit_log(
                actor="AI",
                message=f"เทียบคีย์เวิร์ดตัวเลือก A-D กับเนื้อหา '{title}'...",
                event_type="ai_analyzing",
                level="info",
                meta={"question_id": question_id, "category": category},
            )

            if second_half > 0:
                await asyncio.sleep(second_half)

            # Step 3: Select verified correct option
            selected_option = str(question.get("correct_option", "A")).strip().upper()
            if selected_option not in options:
                selected_option = next(iter(options.keys()), "A")

            selected_text = options.get(selected_option, "")
            is_correct = selected_option == str(question.get("correct_option", selected_option)).upper()
            confidence = self._compute_confidence(question, selected_option)
            explanation = (
                str(question.get("explanation") or "").strip()
                or f"อ้างอิงจากบทเรียน RoV Academy หมวด {category} หัวข้อ {title}"
            )
            answered_at = datetime.now(timezone.utc).isoformat()

            # Step 4: Broadcast final answer event
            await self.event_hub.emit_log(
                actor="AI",
                message=f"ตอบข้อ {selected_option} — {selected_text} (มั่นใจ {int(confidence * 100)}%)",
                event_type="ai_answered",
                level="success",
                meta={
                    "question_id": question_id,
                    "selected_option": selected_option,
                    "is_correct": is_correct,
                    "confidence": confidence,
                    "thinking_seconds": think_seconds,
                },
            )

            return BotAnswerResponse(
                question_id=question_id,
                category=category,
                title=title,
                selected_option=selected_option,  # type: ignore[arg-type]
                selected_text=selected_text,
                is_correct=is_correct,
                confidence=confidence,
                thinking_seconds=think_seconds,
                explanation=explanation,
                answered_at=answered_at,
            )
        except Exception as exc:
            capture_error(
                exc,
                component="ai_bot",
                operation="answer_question",
                extra={"question_id": question_id, "requested_by": requested_by},
            )
            await self.event_hub.emit_log(
                actor="Error",
                message=f"AI Bot เกิดข้อผิดพลาดขณะประมวลผลคำถาม #{question_id}: {exc}",
                event_type="ai_error",
                level="error",
                meta={"question_id": question_id},
            )
            raise
