"""Public errors contain no raw request bodies or secret values."""

from typing import Any

from pydantic import BaseModel


class AppError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details: Any = None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        super().__init__(code)


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str
    details: list[dict] | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody
