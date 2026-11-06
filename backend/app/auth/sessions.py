"""Server-side sessions in Redis (REQ-005): key session:<token>, TTL renewed on each request."""

import json
import re
import secrets
import uuid
from dataclasses import dataclass

from redis.asyncio import Redis

_TOKEN = re.compile(r"^[A-Za-z0-9_-]{20,128}$")


@dataclass(frozen=True)
class SessionData:
    user_id: uuid.UUID
    epoch: int


class SessionStore:
    def __init__(self, redis: Redis, idle_timeout_seconds: int) -> None:
        self._redis = redis
        self._ttl = idle_timeout_seconds

    @staticmethod
    def _key(token: str) -> str:
        return f"session:{token}"

    async def create(self, user_id: uuid.UUID, epoch: int) -> str:
        token = secrets.token_urlsafe(32)
        value = json.dumps({"user_id": str(user_id), "epoch": epoch})
        await self._redis.set(self._key(token), value, ex=self._ttl)
        return token

    async def get(self, token: str) -> SessionData | None:
        """The session of the token, or None; reading it renews the idle timeout."""
        if not _TOKEN.match(token):
            return None
        raw = await self._redis.getex(self._key(token), ex=self._ttl)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            return SessionData(user_id=uuid.UUID(data["user_id"]), epoch=int(data["epoch"]))
        except (ValueError, KeyError, TypeError):
            return None

    async def delete(self, token: str) -> None:
        if _TOKEN.match(token):
            await self._redis.delete(self._key(token))
