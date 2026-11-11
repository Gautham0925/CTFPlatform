"""Provider-independent external lab integration (Phase 11).

Adapters implement labs.base.LabProvider. Core Arena code only talks to that
interface; no provider is hard-coded into business logic.
"""


def make_provider(row):
    from CTFd.plugins.arena.labs.crypto import decrypt

    if row.adapter == "mock":
        from CTFd.plugins.arena.labs.mock import MockProvider

        return MockProvider(row.config or {})
    if row.adapter == "generic_http":
        from CTFd.plugins.arena.labs.http import GenericHTTPProvider

        return GenericHTTPProvider(
            row.base_url, decrypt(row.credentials_enc), row.config or {}
        )
    raise ValueError(f"unknown lab adapter {row.adapter!r}")
