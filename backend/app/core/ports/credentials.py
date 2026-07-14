from typing import Protocol


class CredentialBrokerPort(Protocol):
    """Boundary for resolving credentials by profile without exposing secrets."""
