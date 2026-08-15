"""
Authentication for Discourse.

Supports three modes (checked in order):
  1. API key — sets Api-Key / Api-Username headers. Fastest, no redirects.
  2. Session cookie — uses a _t cookie extracted from your browser. Reliable fallback.
  3. OIDC/Keycloak — automated browser-style login flow via form POST.
"""

import sys
import warnings
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from discourse_explorer.config import ConfigError, RuntimeConfig


class AuthError(RuntimeError):
    """Raised when an authenticated session cannot be established."""


def _cookie_domain(base_url: str) -> str:
    """Return the bare hostname for cookie-domain matching.

    `requests` strips the port from the request URL host before matching
    cookies; if we set domain="localhost:3000" the cookie never attaches.
    """
    host = urlparse(base_url).hostname
    if not host:
        raise ConfigError(f"Could not parse hostname from URL: {base_url!r}")
    return host


def _warn_if_cleartext(base_url: str) -> None:
    if urlparse(base_url).scheme == "http":
        warnings.warn(
            f"Sending credentials over plain HTTP to {base_url!r}. "
            "Cookies and Api-Key headers traverse the wire in cleartext. "
            "Use HTTPS in production.",
            stacklevel=3,
        )


def get_session(base_url: str, rc: RuntimeConfig) -> requests.Session:
    """Return an authenticated Discourse session."""
    if not base_url:
        raise ConfigError(
            "Discourse URL is required (positional arg or DISCOURSE_URL in <data-dir>/config/.env)"
        )

    _warn_if_cleartext(base_url)

    if rc.discourse_api_key:
        return _session_from_api_key(base_url, rc)
    if rc.discourse_cookie:
        return _session_from_cookie(base_url, rc)
    if rc.discourse_username and rc.discourse_password:
        return _session_from_oidc(base_url, rc)

    raise ConfigError(
        "No authentication configured. Set one of the following in <data-dir>/config/.env:\n"
        "  DISCOURSE_API_KEY            — API key (preferred)\n"
        "  DISCOURSE_COOKIE             — browser session cookie (_t value)\n"
        "  DISCOURSE_USERNAME/PASSWORD  — OIDC/Keycloak login"
    )


def _verify_session(session: requests.Session, base_url: str, label: str) -> requests.Session:
    """Try multiple endpoints to verify the session works. Raises on failure."""
    last_status: int | None = None
    for verify_path in ("/latest.json", "/categories.json", "/session/current.json"):
        verify_resp = session.get(f"{base_url}{verify_path}")
        last_status = verify_resp.status_code
        if verify_resp.status_code == 200:
            if verify_path == "/session/current.json":
                user_data = verify_resp.json()
                username = user_data.get("current_user", {}).get("username", "unknown")
                print(f"Authenticated as: {username} ({label})")
            else:
                print(f"Authenticated via {label} (verified with {verify_path})")
            return session
        if verify_resp.status_code == 403:
            raise AuthError(f"{label} authentication rejected (403).")

    cookie_names = [c.name for c in session.cookies]
    raise AuthError(
        f"{label} authentication failed — could not access any endpoint. "
        f"Cookies: {cookie_names}, last status: {last_status}"
    )


# ---------------------------------------------------------------------------
# Auth method 1: API key
# ---------------------------------------------------------------------------

def _session_from_api_key(base_url: str, rc: RuntimeConfig) -> requests.Session:
    """Authenticate using a Discourse API key."""
    api_user = rc.discourse_api_username or rc.discourse_username
    if not api_user:
        raise ConfigError(
            "DISCOURSE_API_USERNAME (or DISCOURSE_USERNAME) must be set when using an API key."
        )

    session = requests.Session()
    session.headers.update({
        "User-Agent": "DiscourseExplorerBot/1.0",
        "Api-Key": rc.discourse_api_key,
        "Api-Username": api_user,
    })

    print(f"Authenticating with API key (user: {api_user})...")
    return _verify_session(session, base_url, "API key")


# ---------------------------------------------------------------------------
# Auth method 2: Browser session cookie
# ---------------------------------------------------------------------------

def _session_from_cookie(base_url: str, rc: RuntimeConfig) -> requests.Session:
    """Authenticate using a _t session cookie extracted from a browser."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "DiscourseExplorerBot/1.0",
    })
    session.cookies.set("_t", rc.discourse_cookie, domain=_cookie_domain(base_url))

    print("Authenticating with browser cookie...")
    return _verify_session(session, base_url, "cookie")


# ---------------------------------------------------------------------------
# Auth method 3: OIDC/Keycloak
# ---------------------------------------------------------------------------

def _session_from_oidc(base_url: str, rc: RuntimeConfig) -> requests.Session:
    """Authenticate via OIDC/Keycloak browser-style login flow."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "DiscourseExplorerBot/1.0",
    })

    print("Authenticating via OIDC...")
    resp = session.get(base_url, allow_redirects=True)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    login_form = soup.find("form", id="kc-form-login")
    if login_form is None:
        login_form = soup.find("form")
        if login_form is None:
            raise AuthError(f"Could not find login form on IdP page (URL: {resp.url}).")

    action_url = login_form.get("action")
    if not action_url:
        raise AuthError("Login form has no action URL.")

    form_data = {}
    for hidden_input in login_form.find_all("input", type="hidden"):
        name = hidden_input.get("name")
        value = hidden_input.get("value", "")
        if name:
            form_data[name] = value

    form_data["username"] = rc.discourse_username
    form_data["password"] = rc.discourse_password

    print("Submitting credentials...")
    login_resp = session.post(action_url, data=form_data, allow_redirects=True)
    login_resp.raise_for_status()

    if base_url not in login_resp.url and "login" in login_resp.url.lower():
        raise AuthError(
            f"Login failed — check your username and password. Landed on: {login_resp.url}"
        )

    cookie_names = [c.name for c in session.cookies]
    has_session = "_t" in cookie_names or "_forum_session" in cookie_names
    if not has_session:
        print(f"Warning: No session cookie found (got: {cookie_names})")
        print("  The OIDC callback may require a real browser.")
        print("  Trying anyway, but if this fails use DISCOURSE_COOKIE instead.")

    return _verify_session(session, base_url, "OIDC")


def get_csrf_token(session: requests.Session, base_url: str) -> str:
    """Fetch the CSRF token from Discourse (needed for some write operations)."""
    resp = session.get(f"{base_url}/session/csrf.json")
    resp.raise_for_status()
    return resp.json()["csrf"]


if __name__ == "__main__":
    from discourse_explorer.config import bootstrap
    try:
        rc = bootstrap(None)
        s = get_session(rc.discourse_url, rc)
    except (ConfigError, AuthError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    print("Session cookies:", [c.name for c in s.cookies])
