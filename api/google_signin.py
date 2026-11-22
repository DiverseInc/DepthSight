# api/google_signin.py
"""Server-side single-use enforcement for the Google sign-in attempt.

ONE Google redirect must produce exactly ONE session.

The client sends the one-time value it generated for the attempt (the OAuth
`state`) together with the credential. This module CONSUMES that value
exactly once: the first attempt inserts it, and a second attempt with the
same value is refused.

Why this lives in the DATABASE and not in the process memory
------------------------------------------------------------
The API is served by gunicorn with FIVE workers (Dockerfile:32). An
in-process set of consumed values would be per worker, so two workers would
each accept the same credential and mint two sessions -- the exact failure
this module exists to prevent. A UNIQUE constraint on the consumed value is
what makes "consume once" atomic across workers, across processes and
across restarts.

The credential is also consumed, by its sha256 digest. That covers a client
that sends no one-time value at all (the PWA popup flow) and it makes a
REPLAYED credential refuse to mint a second session, which is a credential
that is still valid but has already been exchanged.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Iterable, List

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .models import GoogleSignInAttempt

# How long a consumed value is remembered.
#
# The Google ID token is valid for one hour, so a credential older than this
# cannot be exchanged for a session anyway -- the verification fails first.
# 24 hours is a wide margin over that, and it keeps the table small.
CONSUMED_VALUE_TTL = timedelta(hours=24)

# The prefixes keep the two kinds of consumed value apart, so a one-time
# value can never collide with a credential digest.
NONCE_PREFIX = "nonce:"
CREDENTIAL_PREFIX = "cred:"


def token_digest(token: str) -> str:
    """A stable identifier for a token that is safe to write to a log.

    The token is a BEARER credential. Neither the token nor any prefix of it
    may be written to a log, an audit log, analytics or an error report --
    a prefix is enough to correlate a leak. A truncated sha256 hex digest
    identifies the attempt for correlation and cannot be used to
    authenticate.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def credential_value(digest: str) -> str:
    """The consumed value that stands for a credential."""
    return f"{CREDENTIAL_PREFIX}{digest}"


def nonce_value(nonce: str) -> str:
    """The consumed value that stands for a client-generated one-time value."""
    return f"{NONCE_PREFIX}{nonce}"


async def _prune_expired(db: AsyncSession) -> None:
    """Forget the values that are too old to matter."""
    cutoff = datetime.now(timezone.utc) - CONSUMED_VALUE_TTL
    await db.execute(
        delete(GoogleSignInAttempt).where(GoogleSignInAttempt.created_at < cutoff)
    )


async def consume_once(db: AsyncSession, values: Iterable[str]) -> bool:
    """Consume each of `values` exactly once.

    Returns True when every value was consumed BY THIS CALL, and False when
    at least one of them had already been consumed -- that is, this attempt
    is a REPLAY.

    The INSERT is what makes this atomic. A check-then-insert would have a
    window between the two statements in which two workers could both see
    "not consumed yet" and both proceed; the UNIQUE constraint makes the
    second insert fail instead.
    """
    unique_values: List[str] = [v for v in dict.fromkeys(values) if v]
    if not unique_values:
        return True

    await _prune_expired(db)
    for value in unique_values:
        db.add(GoogleSignInAttempt(value=value))
    try:
        await db.commit()
    except IntegrityError:
        # One of the values was already consumed. This attempt is a replay.
        await db.rollback()
        return False
    return True
