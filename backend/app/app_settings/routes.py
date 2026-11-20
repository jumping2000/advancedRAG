"""`GET` and `PUT /api/admin/settings` (REQ-004). Skeleton of T-01: both answer 501."""

from fastapi import APIRouter

from app.auth.deps import ApiError

router = APIRouter(prefix="/admin/settings")


def not_implemented() -> ApiError:
    return ApiError(501, "not_implemented", "Not implemented")


@router.get("")
async def read_settings() -> None:
    raise not_implemented()


@router.put("")
async def update_settings() -> None:
    raise not_implemented()
