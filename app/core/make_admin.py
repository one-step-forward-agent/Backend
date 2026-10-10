"""Give a user the admin dashboard (dayla.tech/dashboard): `python -m app.core.make_admin <email>`.

`--revoke` takes it away. Emails are encrypted, so a plain UPDATE by email does not work; this finds the user
by the email's hash.
"""

import asyncio
import sys

from sqlalchemy import select

from app.core.database import session_factory
from app.core.dataenc import email_lookup
from app.models.models import User


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
