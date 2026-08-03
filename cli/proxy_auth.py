"""Shared proxy-auth policy for CodexProxy client launchers."""

PROXY_NO_AUTH_SENTINEL = "cdx-no-auth"


def proxy_auth_token(auth_token: str) -> str:
    """Return the configured proxy token or the no-auth client marker."""

    return auth_token.strip() or PROXY_NO_AUTH_SENTINEL
