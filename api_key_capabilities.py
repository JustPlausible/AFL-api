"""Consumer API-key capability names and validation helpers."""

STANDARD_READ = "standard-read"
ADVANCED_READ = "advanced-read"

# This allow-list is intentionally separate from persistence. Adding a future
# consumer read capability requires no schema change, but still requires an
# explicit application decision before operators can grant it.
SUPPORTED_CAPABILITIES = frozenset({STANDARD_READ, ADVANCED_READ})


def validate_capability(capability: str) -> str:
    value = capability.strip().lower()
    if value not in SUPPORTED_CAPABILITIES:
        supported = ", ".join(sorted(SUPPORTED_CAPABILITIES))
        raise ValueError(f"Invalid capability '{capability}'. Supported capabilities: {supported}")
    return value
