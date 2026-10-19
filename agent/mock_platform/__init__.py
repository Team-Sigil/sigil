"""In-memory mock of the platform API the agent zone calls (gateway + policy engine).

It implements the contract in ``agent_layer.contracts`` with realistic engine
behaviour (fixed-order checks, deny by default, idempotency, Auditor verdicts bound to
the Auditor's credential) so the agent can be tested end to end over real HTTP. It is
not the real engine: no signatures, no database, no chain.
"""

from mock_platform.app import MockPlatform

__all__ = ["MockPlatform"]
