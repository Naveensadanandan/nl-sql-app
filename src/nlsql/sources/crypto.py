"""Fernet encryption for user database URLs (they hold a password).

The key is settings.fernet_key (NLSQL_FERNET_KEY). Generate one with:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
Losing the key makes every stored connection unreadable (users re-enter them); changing it
needs a re-encrypt. Never log the plain or the encrypted value.
"""

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from nlsql.core.config import get_settings


class CryptoUnavailable(RuntimeError):
    """No (valid) NLSQL_FERNET_KEY; database connections cannot be stored or used."""


@lru_cache
def _fernet() -> Fernet:
    key = get_settings().fernet_key
    if key is None or not key.get_secret_value().strip():
        raise CryptoUnavailable("NLSQL_FERNET_KEY is not set")
    try:
        return Fernet(key.get_secret_value().strip().encode())
    except ValueError as e:
        raise CryptoUnavailable("NLSQL_FERNET_KEY is not a valid Fernet key") from e


def available() -> bool:
    try:
        _fernet()
    except CryptoUnavailable:
        return False
    return True


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise CryptoUnavailable("stored connection cannot be decrypted with this key") from e
