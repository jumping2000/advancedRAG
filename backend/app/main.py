"""FastAPI application factory."""

from fastapi import FastAPI

from app.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="advancedRAG")
    app.state.settings = settings or get_settings()
    return app
