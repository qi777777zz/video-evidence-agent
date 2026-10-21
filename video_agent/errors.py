"""Stable business errors shared by transports and application services."""


class BusinessError(Exception):
    code = "INTERNAL_ERROR"
    status_code = 500

    def __init__(self, message: str, *, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.message = message
        self.headers = headers or {}


class InvalidRequest(BusinessError):
    code, status_code = "INVALID_REQUEST", 400


class NotFound(BusinessError):
    code, status_code = "NOT_FOUND", 404


class Conflict(BusinessError):
    code, status_code = "CONFLICT", 409


class Gone(BusinessError):
    code, status_code = "GONE", 410


class TooLarge(BusinessError):
    code, status_code = "PAYLOAD_TOO_LARGE", 413


class RateLimited(BusinessError):
    code, status_code = "RATE_LIMITED", 429


class DependencyUnavailable(BusinessError):
    code, status_code = "DEPENDENCY_UNAVAILABLE", 503

