import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class DomainError(Exception):
    """Base for domain exceptions. Subclasses set `code` and the HTTP status the gateway returns."""

    code: str = "BAD_REQUEST"
    status: int = 400
    message: str = "Request could not be processed."

    def __init__(self, message: str | None = None) -> None:
        if message is not None:
            self.message = message
        super().__init__(self.message)


class NotFoundError(DomainError):
    code = "NOT_FOUND"
    status = 404
    message = "Not found."


class UnauthorizedError(DomainError):
    code = "UNAUTHORIZED"
    status = 401
    message = "Authentication required."


class ForbiddenError(DomainError):
    code = "FORBIDDEN"
    status = 403
    message = "Not allowed."


class ConflictError(DomainError):
    code = "CONFLICT"
    status = 409
    message = "Conflicts with the current state."


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    """Error envelope for every failed request (SPEC §7.2)."""

    error: ErrorDetail


def error_response(status: int, code: str, message: str) -> JSONResponse:
    body = ErrorResponse(error=ErrorDetail(code=code, message=message))
    return JSONResponse(status_code=status, content=body.model_dump())


_HTTP_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    413: "PAYLOAD_TOO_LARGE",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMITED",
}


def install_error_handlers(app: FastAPI) -> None:
    """Every error leaves as the §8.1 envelope; never a stack trace or raw input (SEC-19)."""

    @app.exception_handler(DomainError)
    async def handle_domain_error(_: Request, exc: DomainError) -> JSONResponse:
        return error_response(exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Only field locations are returned; echoing input values could leak data back.
        fields = sorted({".".join(str(p) for p in err["loc"]) for err in exc.errors()})
        return error_response(422, "VALIDATION_ERROR", f"Invalid fields: {', '.join(fields)}")

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_CODES.get(exc.status_code, "ERROR")
        message = exc.detail if isinstance(exc.detail, str) else code
        return error_response(exc.status_code, code, message)

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error")
        return error_response(500, "INTERNAL_ERROR", "Internal server error.")
