# api/demo_seed.py
"""Identification of seeded demo accounts.

`scripts/seed_demo_data.py` creates fake trader accounts so a demo (or the
first investor walkthrough) does not start from an empty admin panel. Those
accounts are NOT customers: they have fabricated trade history and fabricated
backtests. They must therefore never be counted in a statistic that a prospect
can see, because a counter that includes them overstates the product.

WHY THIS IS A READ-TIME FILTER AND NOT A DATA CLEANUP
-----------------------------------------------------
Deleting or deactivating the seeded rows is a destructive change to a live
database and is the owner's call, not the code's. Everything here works by
EXCLUDING rows from a query, so:
  * no production row is touched,
  * no schema migration is needed,
  * and the rows become visible again the moment they are cleaned up.

HOW DEMO ACCOUNTS ARE RECOGNISED
---------------------------------
Three independent signals, OR-ed together:

1. `admin_notes == "demo_seed_v1"` -- the marker the seed script already
   writes on every user it creates (`scripts/seed_demo_data.py`). This is the
   authoritative signal for rows created by the script.

2. An e-mail address in the RESERVED `example.com` domain (RFC 2606).
   `example.com` is reserved by the IETF for documentation and examples and
   can never be a real customer's address, so any account on it is a test
   account. This catches demo accounts that were created by hand, and it
   catches a future re-seed that forgets the marker.

3. The `demo_inactive` username -- the churned demo persona.

WHAT THIS DOES NOT DO
---------------------
It does not change entitlements, does not lock anyone out and does not affect
any authenticated endpoint. The filter is applied ONLY to public statistics.

The one thing it can do is hide a genuine customer from a PUBLIC COUNTER,
and only if that customer registered on an `@example.com` address. That
domain is reserved by the IETF (RFC 2606) for documentation and examples and
cannot receive mail, so it is not a customer address -- but nothing in the
signup form enforces that, so this is a trade-off and not a guarantee. It
has no effect on what the account can do or pay for.
"""

from sqlalchemy import func, or_

from .models import User

# The marker `scripts/seed_demo_data.py` writes into `users.admin_notes`.
DEMO_SEED_ADMIN_NOTES = "demo_seed_v1"

# RFC 2606 reserved domain: documentation/examples only, never a real customer.
RESERVED_DEMO_EMAIL_DOMAIN = "example.com"

# The churned demo persona created by the seed script.
DEMO_USERNAMES = ("demo_inactive",)


def is_demo_account(
    username: str | None = None,
    email: str | None = None,
    admin_notes: str | None = None,
) -> bool:
    """True when a user row is a seeded demo account rather than a customer.

    Exposed as a plain function so the same rule can be used in Python code
    (and tested) without building a SQL expression.

    The rules here are kept EXACTLY in step with the SQL expression built by
    `not_demo_accounts`, so the two never disagree about a row.
    """
    if admin_notes and admin_notes.strip() == DEMO_SEED_ADMIN_NOTES:
        return True
    if email:
        # Same as the SQL: the address must END with "@example.com". An
        # address with no "@" at all is not a match.
        normalised = email.strip().lower()
        if normalised.endswith(f"@{RESERVED_DEMO_EMAIL_DOMAIN}"):
            return True
    if username and username.strip().lower() in DEMO_USERNAMES:
        return True
    return False


def not_demo_accounts(model=User):
    """SQLAlchemy filter expression selecting rows that are NOT demo accounts.

    `admin_notes` is nullable, so a bare `!=` comparison would silently drop
    every user with a NULL marker. `func.coalesce` keeps NULL rows.
    """
    return ~(
        or_(
            func.coalesce(model.admin_notes, "") == DEMO_SEED_ADMIN_NOTES,
            func.lower(model.email).like(f"%@{RESERVED_DEMO_EMAIL_DOMAIN}"),
            func.lower(model.username).in_(DEMO_USERNAMES),
        )
    )


__all__ = [
    "DEMO_SEED_ADMIN_NOTES",
    "RESERVED_DEMO_EMAIL_DOMAIN",
    "DEMO_USERNAMES",
    "is_demo_account",
    "not_demo_accounts",
]
