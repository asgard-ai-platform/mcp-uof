"""
UOF MCP Server — Authentication package.

Two providers coexist behind a common ABC:

- TokenAuthProvider  — RSA-encrypted account/password → SOAP GetToken → bearer token
                       (works against UOF deployments with PublicAPI module installed)
- SessionAuthProvider — Login.aspx form post → ASPXFORMSAUTH cookie + storage state
                       (works against UOF deployments without PublicAPI; web-only)

There is no user-selectable mode: which mechanism (SOAP/token vs web/session) a tool uses is
a per-tool decision baked into ops.router. Both providers coexist and are built lazily from the
same identity (one process = one UOF_ACCOUNT). See docs/architecture.md.
"""

from .base import (
    AuthMode,
    AuthProvider,
    get_provider,
    get_session_provider,
    get_token_provider,
    require_auth,
)
from .token import TokenAuthProvider

# Kept for backwards compatibility with existing imports that did `from .auth import get_token`.
# Returns the SOAP identity's bearer token; tools/mechanisms should prefer their own provider.
def get_token() -> str:
    return get_token_provider().fetch_token()


def rsa_encrypt(public_key_base64: str, plaintext: str) -> str:
    """Compatibility shim — RSA-encrypt with the token provider's helper.

    Only meaningful in token mode (session mode never RSA-encrypts). Kept so existing
    callers/tests that did `from mcp_uof.auth import rsa_encrypt` keep working after the
    auth package split.
    """
    from .token import _rsa_encrypt
    return _rsa_encrypt(public_key_base64, plaintext)


def read_credentials():
    """Compatibility shim used by domains/system/tools.py."""
    provider = get_provider()
    if not hasattr(provider, "read_credentials"):
        return None
    return provider.read_credentials()


def credentials_file() -> str:
    """Compatibility shim — returns the active provider's persistence file."""
    return get_provider().credentials_file()


__all__ = [
    "AuthMode",
    "AuthProvider",
    "TokenAuthProvider",
    "credentials_file",
    "get_provider",
    "get_session_provider",
    "get_token",
    "get_token_provider",
    "read_credentials",
    "require_auth",
    "rsa_encrypt",
]
