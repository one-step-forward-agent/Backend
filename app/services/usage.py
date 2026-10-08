"""Token accounting for language model requests: every GigaChat call is stored in llm_usage.

The user is taken from a context variable set when the request is authenticated (or when a background job
works for a user), so the model client does not need a database session or a user passed in."""

import logging
from contextvars import ContextVar

from app.core.database import session_factory
from app.models.models import LlmUsage

logger = logging.getLogger(__name__)

current_user_id: ContextVar[int | None] = ContextVar("llm_usage_user_id", default=None)


def _tokens(usage: dict, key: str) -> int:
    value = usage.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


async def record(purpose: str, model: str | None, usage: dict | None, duration_ms: int | None = None) -> None:
    """Store one request's token counts; accounting never breaks the answer itself."""
    usage = usage if isinstance(usage, dict) else {}
    try:
        async with session_factory() as session:
            session.add(
                LlmUsage(
                    user_id=current_user_id.get(),
                    purpose=purpose[:64],
                    model=str(model or "")[:64],
                    prompt_tokens=_tokens(usage, "prompt_tokens"),
                    completion_tokens=_tokens(usage, "completion_tokens"),
                    precached_prompt_tokens=_tokens(usage, "precached_prompt_tokens"),
                    total_tokens=_tokens(usage, "total_tokens"),
                    duration_ms=duration_ms,
                )
            )
            await session.commit()
    except Exception:
        logger.exception("Could not record language model token usage")
