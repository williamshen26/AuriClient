"""Thin integration exceptions."""

from __future__ import annotations


class SaaSRequestError(Exception):
    """Raised when the SaaS endpoint returns an invalid response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class ToolExecutionError(Exception):
    """Raised when a local tool call cannot be executed."""
