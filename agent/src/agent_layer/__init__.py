"""Agentic AI layer: the maker agent, the Auditor agent and their scheduled triggers.

Everything in this package sits in the *agent zone* of the architecture. It has no
database, file or key access; every read and every action goes through the platform
HTTP API (see ``agent_layer.platform_api``).
"""
