"""`GET` and `PUT /api/admin/settings` (REQ-004): the switches of the reranker and of the rewriting, admin only.

`require_admin` runs before the body is validated: 401 without a session, 403 `forbidden` for a `user`, whatever it
sends. Then the body: 422 `validation_error` without either field or with a value that is not a JSON boolean; 409
`service_not_configured` when it switches on a step whose service is not configured in `.env`, and then nothing is
saved, not even the other field. Switching off is always allowed (a switch left on while its service was removed from
`.env`).
"""

from fastapi import APIRouter, Depends, Request

from app.app_settings.repository import AppSettingsRepository, Switches
from app.app_settings.schemas import SettingsOut, SettingsUpdate, StepState
from app.auth.deps import ApiError, DbSession, require_admin
from app.config import Settings

router = APIRouter(prefix="/admin/settings", dependencies=[Depends(require_admin)])


def service_not_configured() -> ApiError:
    return ApiError(409, "service_not_configured", "The service of this step is not configured")


def settings_out(settings: Settings, switches: Switches) -> SettingsOut:
    return SettingsOut(
        rerank=StepState(enabled=switches.rerank_enabled, configured=settings.rerank_configured),
        rewrite=StepState(enabled=switches.rewrite_enabled, configured=settings.rewrite_configured),
    )


@router.get("")
async def read_settings(request: Request, session: DbSession) -> SettingsOut:
    return settings_out(request.app.state.settings, await AppSettingsRepository(session).switches())


@router.put("")
async def update_settings(body: SettingsUpdate, request: Request, session: DbSession) -> SettingsOut:
    settings: Settings = request.app.state.settings
    if (body.rerank_enabled and not settings.rerank_configured) or (
        body.rewrite_enabled and not settings.rewrite_configured
    ):
        raise service_not_configured()
    switches = await AppSettingsRepository(session).update(
        rerank_enabled=body.rerank_enabled, rewrite_enabled=body.rewrite_enabled
    )
    return settings_out(settings, switches)
