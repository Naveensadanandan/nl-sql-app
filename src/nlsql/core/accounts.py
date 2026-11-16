"""User accounts and login sessions (see docs/ui-and-auth.md).

- Sign-up creates a user and a new organisation (tenant). The username and organisation id are
  stored lowercase. Joining an existing organisation (invites) is not in v0.1.
- Passwords are hashed with scrypt (stdlib `hashlib.scrypt`) and a random 16-byte salt per user.
  The plain password is never stored or logged.
- Log-in returns a random bearer token. Only its SHA-256 is stored, with an expiry, so a leaked
  sessions table cannot be replayed. Log-out deletes the row.
"""

import base64
import hashlib
import hmac
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from psycopg import errors
from psycopg.rows import dict_row

from nlsql.core import app_db
from nlsql.core.config import get_settings

USERNAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,39}$")
# Stricter than normalize_organisation_id: no '-', so ws_<organisation_id> is a plain identifier
# and two organisations can never share a CSV schema.
NEW_ORGANISATION = re.compile(r"^[a-z0-9][a-z0-9_]{1,39}$")
MIN_PASSWORD = 8

_N, _R, _P = 2**14, 8, 1  # scrypt cost: ~16 MB, ~50 ms per hash


@dataclass(frozen=True)
class User:
    user_id: str
    username: str
    organisation_id: str


class AccountError(ValueError):
    """Sign-up / log-in refused. The message is safe to show to the user."""


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    b64 = base64.b64encode
    return f"scrypt${_N}${_R}${_P}${b64(salt).decode()}${b64(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        expected = base64.b64decode(digest)
        got = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except ValueError:
        return False
    return hmac.compare_digest(got, expected)


# Verified against when the username is unknown, so a wrong username takes as long as a wrong
# password (no user enumeration by timing).
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_user(username: str, password: str, organisation_id: str) -> User:
    username, org = username.strip().lower(), organisation_id.strip().lower()
    if not USERNAME.match(username):
        raise AccountError("username: 3-40 chars of a-z, 0-9, '_', '.', '-'")
    if not NEW_ORGANISATION.match(org):
        raise AccountError("organisation: 2-40 chars of a-z, 0-9, '_'")
    if len(password) < MIN_PASSWORD:
        raise AccountError(f"password must be at least {MIN_PASSWORD} characters")
    user = User(user_id=f"u_{uuid.uuid4().hex[:16]}", username=username, organisation_id=org)
    pw_hash = hash_password(password)
    try:
        with app_db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                app_db.q("INSERT INTO {s}.organisations (organisation_id) VALUES (%s)"), (org,)
            )
            cur.execute(
                app_db.q(
                    "INSERT INTO {s}.users (user_id, username, password_hash, organisation_id) "
                    "VALUES (%s, %s, %s, %s)"
                ),
                (user.user_id, username, pw_hash, org),
            )
    except errors.UniqueViolation as e:
        # Same message for both: which one exists is not something to reveal to a stranger.
        raise AccountError("that username or organisation is already taken") from e
    return user


def login(username: str, password: str) -> tuple[str, User]:
    """(bearer token, user). Raises AccountError on a wrong username or password."""
    with app_db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            app_db.q(
                "SELECT user_id, username, password_hash, organisation_id "
                "FROM {s}.users WHERE username = %s"
            ),
            (username.strip().lower(),),
        )
        row = cur.fetchone()
    if not verify_password(password, row["password_hash"] if row else _DUMMY_HASH) or not row:
        raise AccountError("wrong username or password")
    user = User(row["user_id"], row["username"], row["organisation_id"])
    token = secrets.token_urlsafe(32)
    expires = datetime.now(UTC) + timedelta(hours=get_settings().session_ttl_hours)
    with app_db.connection() as conn, conn.cursor() as cur:
        cur.execute(app_db.q("DELETE FROM {s}.sessions WHERE expires_at < now()"))
        cur.execute(
            app_db.q(
                "INSERT INTO {s}.sessions (token_hash, user_id, expires_at) VALUES (%s,%s,%s)"
            ),
            (_token_hash(token), user.user_id, expires),
        )
    return token, user


def user_for_token(token: str) -> User | None:
    """The user a live (unexpired) token belongs to, else None."""
    with app_db.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            app_db.q(
                "SELECT u.user_id, u.username, u.organisation_id "
                "FROM {s}.sessions s JOIN {s}.users u USING (user_id) "
                "WHERE s.token_hash = %s AND s.expires_at > now()"
            ),
            (_token_hash(token),),
        )
        row = cur.fetchone()
    return User(row["user_id"], row["username"], row["organisation_id"]) if row else None


def logout(token: str) -> None:
    with app_db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            app_db.q("DELETE FROM {s}.sessions WHERE token_hash = %s"), (_token_hash(token),)
        )
