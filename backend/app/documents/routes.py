"""Documents API of a knowledge base, for the admin only (skeleton: no route yet)."""

from fastapi import APIRouter, Depends

from app.auth.deps import require_admin

router = APIRouter(prefix="/kbs/{kb_id}/documents", dependencies=[Depends(require_admin)])
