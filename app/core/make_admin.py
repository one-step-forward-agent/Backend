"""Give a user the admin dashboard (dayla.tech/dashboard): `python -m app.core.make_admin <email>`.

`--revoke` takes it away. Emails are encrypted, so a plain UPDATE by email does not work; this finds the user
by the email's hash. Where no shell is at hand (Amvera), the ADMIN_EMAILS variable does the same on every start.
"""

import asyncio
import logging
import sys

from sqlalchemy import select

from app.core.database import session_factory
from app.core.dataenc import email_lookup
from app.models.models import User

logger = logging.getLogger(__name__)


async def grant_listed(emails: tuple[str, ...]) -> None:
    """ADMIN_EMAILS: only accounts that already exist. One not registered yet is not reserved for whoever signs up
    with that email first: it becomes admin at the first start after it exists."""
    if not emails:
        return
    async with session_factory() as session:
        for email in emails:
            user = await session.scalar(select(User).where(User.email_hash.in_(email_lookup(email))))
            if user:
                user.is_admin = True
            else:
                logger.warning("ADMIN_EMAILS: no account with %s yet", email)
        await session.commit()


async def main(email: str, admin: bool) -> int:
    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.email_hash.in_(email_lookup(email))))
        if not user:
            print(f"No user with the email {email}", file=sys.stderr)
            return 1
        user.is_admin = admin
        await session.commit()
        print(f"{email}: is_admin = {admin}")
        return 0


if __name__ == "__main__":
    args = [arg for arg in sys.argv[1:] if arg != "--revoke"]
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(main(args[0], "--revoke" not in sys.argv)))
