"""Local WebUI application API; HTTP is a caller, never a safety authority."""

from code_slayer.api.app import create_app

__all__ = ["create_app"]
