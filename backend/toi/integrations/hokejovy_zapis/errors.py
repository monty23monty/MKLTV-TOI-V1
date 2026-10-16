"""Structured provider failures safe to show in the operator import flow."""

from typing import Any


class ProviderError(ValueError):
    def __init__(
        self, message: str, code: str = "provider_error", status_code: int = 502,
        *, cached_preview: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.cached_preview = cached_preview

    def as_dict(self) -> dict[str, Any]:
        result = {"provider": "hokejovy-zapis", "code": self.code, "message": self.message}
        if self.cached_preview is not None:
            result["cached_preview"] = self.cached_preview
        return result
