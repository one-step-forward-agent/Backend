"""Export rated assistant answers for analysis: python -m app.core.ratings_export > ratings.csv

Messages are encrypted at rest, so the export decrypts them with the configured keys. Each row is
one rated answer with the user's request that preceded it. Handle the file as personal data.
"""

import asyncio
import csv
import sys

from sqlalchemy import select

from app.core.database import session_factory
from app.models.models import ConversationMessage


async def export(out=sys.stdout) -> int:
    writer = csv.writer(out)
    writer.writerow(["message_id", "user_id", "created_at", "rated_at", "rating", "kind", "request", "answer"])
    count = 0
    async with session_factory() as session:
        rated = list(await session.scalars(select(ConversationMessage).where(ConversationMessage.rating.is_not(None)).order_by(ConversationMessage.id)))
        for message in rated:
            request = await session.scalar(
                select(ConversationMessage)
                .where(ConversationMessage.user_id == message.user_id, ConversationMessage.role == "user", ConversationMessage.id < message.id)
                .order_by(ConversationMessage.id.desc())
                .limit(1)
            )
            kind = (message.reply or {}).get("kind") if isinstance(message.reply, dict) else None
            writer.writerow([message.id, message.user_id, message.created_at.isoformat(), message.rated_at.isoformat() if message.rated_at else "", message.rating, kind or "", request.content if request else "", message.content])
            count += 1
    return count


if __name__ == "__main__":
    print(f"exported {asyncio.run(export())} rated answers", file=sys.stderr)
