"""Password hashing with Argon2id (REQ-006). Plain passwords and hashes are never logged or returned."""

from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

MIN_PASSWORD_LENGTH = 12

_hasher = PasswordHasher()  # Argon2id with the library's recommended parameters


def hash_password(plain: str) -> str:
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, plain)
    except (VerificationError, InvalidHashError):
        return False


@lru_cache
def dummy_hash() -> str:
    """A hash to verify against when the user does not exist, so that both cases take the same time."""
    return hash_password("no user has this password")
