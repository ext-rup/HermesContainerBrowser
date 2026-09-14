"""Hermes user plugin: Local Sidecar browser provider.

Registers the ``local-sidecar`` browser backend so the built-in ``browser_*``
tools drive a locally hosted, isolated Chromium (with uBlock Origin Lite) instead
of any cloud provider. See ``provider.py`` for configuration.
"""

from __future__ import annotations

from .provider import LocalSidecarProvider


def register(ctx) -> None:
    ctx.register_browser_provider(LocalSidecarProvider())
