"""Course entry point: uvicorn app_final:create_app --factory."""

from app.main import create_app

__all__ = ["create_app"]
