"""GitHub als Identity Provider - und ausschließlich als solcher.

GitHub beantwortet genau eine Frage: *welcher Mensch ist das?* Es stellt keine
Tokens für AIPHIDIAS aus, und ein GitHub-Token wird nie an den MCP-Server
weitergereicht. Das ist der Unterschied zwischen einem Identity Provider und
einem Authorization Server, und die Vermischung der beiden wäre genau der
Confused Deputy, vor dem die MCP-Spec warnt.
"""

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_API_URL = "https://api.github.com/user"

# Nur die Identität, sonst nichts. `read:user` reicht dafür; `user:email` wäre
# schon mehr, als wir brauchen.
SCOPE = "read:user"

_TIMEOUT_SECONDS = 15.0


class GitHubError(RuntimeError):
    """Der GitHub-Login ist fehlgeschlagen."""


@dataclass(frozen=True, slots=True)
class GitHubIdentity:
    """Die Antwort auf 'welcher Mensch ist das?'.

    Attributes:
        user_id: GitHubs numerische ID. Stabil, auch wenn der Nutzer sich
            umbenennt - deshalb ist sie und nicht der Login der Schlüssel.
        login: Der Anzeigename bei GitHub.
    """

    user_id: str
    login: str


def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Baut die URL, zu der der Browser für den GitHub-Login geschickt wird."""
    query = httpx.QueryParams(
        {"client_id": client_id, "redirect_uri": redirect_uri, "scope": SCOPE, "state": state}
    )
    return f"{AUTHORIZE_URL}?{query}"


async def exchange_code(code: str, client_id: str, client_secret: str, redirect_uri: str) -> GitHubIdentity:
    """Tauscht den Autorisierungscode gegen die GitHub-Identität.

    Das dabei erhaltene GitHub-Token wird nur für den einen `/user`-Aufruf
    benutzt und danach verworfen. Es wird nirgends gespeichert.

    Args:
        code: Der Code aus GitHubs Callback.
        client_id: Client-ID der GitHub-OAuth-App.
        client_secret: Zugehöriges Secret.
        redirect_uri: Dieselbe Callback-URL wie beim Start des Flows.

    Returns:
        Die Identität des angemeldeten Menschen.

    Raises:
        GitHubError: Code ungültig, GitHub nicht erreichbar oder Antwort unbrauchbar.
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        try:
            token_response = await client.post(
                TOKEN_URL,
                headers={"Accept": "application/json"},
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
            )
            payload = token_response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise GitHubError(f"GitHub-Token-Austausch fehlgeschlagen: {exc}") from exc

        access_token = payload.get("access_token")
        if not access_token:
            raise GitHubError(f"GitHub lieferte kein Token: {payload.get('error_description', payload)}")

        try:
            user_response = await client.get(
                USER_API_URL,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
            )
            user = user_response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise GitHubError(f"GitHub-Nutzerabfrage fehlgeschlagen: {exc}") from exc

    if "id" not in user:
        raise GitHubError("GitHub lieferte keine Nutzer-ID.")

    return GitHubIdentity(user_id=str(user["id"]), login=user.get("login") or f"user-{user['id']}")
