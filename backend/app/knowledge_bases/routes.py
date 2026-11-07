"""Knowledge bases API (REQ-008, REQ-009, REQ-010).

GET /kbs and GET /kbs/{kb_id} answer every signed-in user with the KBs visible to them (all for the admin); the other
routes are for the admin only, with the role checked before the ids and the body (403 for a user, whatever it sends).
A kb_id that is not a UUID, an unknown KB and a KB the user is not assigned to get the same 404 kb_not_found.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response

from app.auth.deps import ApiError, CurrentUser, DbSession, require_admin
from app.auth.routes import UserOut
from app.db.models import User
from app.knowledge_bases.repository import KbNameTaken, KnowledgeBaseRepository
from app.knowledge_bases.schemas import KnowledgeBaseCreateIn, KnowledgeBaseOut, KnowledgeBaseUpdateIn
from app.users.routes import parse_user_id
from app.users.service import user_not_found

Admin = Annotated[User, Depends(require_admin)]
ADMIN_ONLY = [Depends(require_admin)]

router = APIRouter(prefix="/kbs")


def kb_not_found() -> ApiError:
    return ApiError(404, "kb_not_found", "Knowledge base not found")


def kb_name_taken() -> ApiError:
    return ApiError(409, "kb_name_taken", "The knowledge base name is already taken")


def parse_kb_id(kb_id: str) -> uuid.UUID:
    """An id that is not a UUID is answered like an unknown KB (404 kb_not_found), not with a 422."""
    try:
        return uuid.UUID(kb_id)
    except ValueError:
        raise kb_not_found() from None


def repository(session: DbSession) -> KnowledgeBaseRepository:
    return KnowledgeBaseRepository(session)


Repository = Annotated[KnowledgeBaseRepository, Depends(repository)]


async def visible_kb_id(kb_id: str, user: User, kbs: KnowledgeBaseRepository) -> uuid.UUID:
    kb = await kbs.get_for(user, parse_kb_id(kb_id))
    if kb is None:
        raise kb_not_found()
    return kb.id


@router.get("")
async def list_kbs(user: CurrentUser, kbs: Repository) -> list[KnowledgeBaseOut]:
    return [KnowledgeBaseOut.model_validate(kb) for kb in await kbs.list_for(user)]


@router.get("/{kb_id}")
async def get_kb(kb_id: str, user: CurrentUser, kbs: Repository) -> KnowledgeBaseOut:
    kb = await kbs.get_for(user, parse_kb_id(kb_id))
    if kb is None:
        raise kb_not_found()
    return KnowledgeBaseOut.model_validate(kb)


@router.post("", status_code=201, dependencies=ADMIN_ONLY)
async def create_kb(body: KnowledgeBaseCreateIn, kbs: Repository) -> KnowledgeBaseOut:
    try:
        kb = await kbs.create(body.name, body.description)
    except KbNameTaken:
        raise kb_name_taken() from None
    return KnowledgeBaseOut.model_validate(kb)


@router.patch("/{kb_id}", dependencies=ADMIN_ONLY)
async def update_kb(kb_id: str, body: KnowledgeBaseUpdateIn, kbs: Repository) -> KnowledgeBaseOut:
    try:
        kb = await kbs.update(parse_kb_id(kb_id), body.changes())
    except KbNameTaken:
        raise kb_name_taken() from None
    if kb is None:
        raise kb_not_found()
    return KnowledgeBaseOut.model_validate(kb)


@router.delete("/{kb_id}", status_code=204, response_class=Response, dependencies=ADMIN_ONLY)
async def delete_kb(kb_id: str, kbs: Repository) -> None:
    if not await kbs.delete(parse_kb_id(kb_id)):
        raise kb_not_found()


@router.get("/{kb_id}/members", dependencies=ADMIN_ONLY)
async def list_members(kb_id: str, admin: Admin, kbs: Repository) -> list[UserOut]:
    members = await kbs.members(await visible_kb_id(kb_id, admin, kbs))
    return [UserOut.model_validate(user) for user in members]


async def member_ids(
    kb_id: str, user_id: str, admin: User, kbs: KnowledgeBaseRepository
) -> tuple[uuid.UUID, uuid.UUID]:
    """The KB is checked first (kb_not_found), then the user (user_not_found, also for an id that is not a UUID)."""
    found_kb = await visible_kb_id(kb_id, admin, kbs)
    user = await kbs.session.get(User, parse_user_id(user_id))
    if user is None:
        raise user_not_found()
    return found_kb, user.id


@router.put("/{kb_id}/members/{user_id}", status_code=204, response_class=Response, dependencies=ADMIN_ONLY)
async def add_member(kb_id: str, user_id: str, admin: Admin, kbs: Repository) -> None:
    found_kb, found_user = await member_ids(kb_id, user_id, admin, kbs)
    if not await kbs.add_member(found_kb, found_user):
        raise kb_not_found()  # the KB was deleted meanwhile (users are never deleted in phase 1)


@router.delete("/{kb_id}/members/{user_id}", status_code=204, response_class=Response, dependencies=ADMIN_ONLY)
async def remove_member(kb_id: str, user_id: str, admin: Admin, kbs: Repository) -> None:
    found_kb, found_user = await member_ids(kb_id, user_id, admin, kbs)
    await kbs.remove_member(found_kb, found_user)
