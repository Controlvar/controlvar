"""
Public types for the Controlvar OpenFeature Provider.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ProviderConfig:
    """Configuration for :class:`~controlvar_openfeature.ExperimentationProvider`.

    Convenience holder for applications that build the provider from settings::

        cfg = ProviderConfig(api_key=..., base_url=...)
        provider = ExperimentationProvider(**vars(cfg))
    """

    api_key: str
    """API key used in the X-API-Key header."""

    base_url: str = "http://localhost:8000"
    """Backend origin; the SDK appends ``/api/v1/...``."""

    cache_ttl: float = 300
    """Seconds a successful evaluation is reused per user + flag. Defaults to 5 minutes."""

    timeout: float = 10
    """HTTP request timeout in seconds."""
