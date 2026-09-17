"""cli_provider_api — authenticated OpenAI-compatible API over the core."""

from .app import create_app

__all__ = ["create_app"]
