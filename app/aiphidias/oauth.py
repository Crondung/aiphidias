"""Token-Prüfung im Resource Server.

Die MCP-Spec (Revision 2026-07-28) verlangt hier drei Dinge als **MUST**:

* Tokens validieren,
* prüfen, dass sie **für diesen Server** ausgestellt wurden (`aud`, RFC 8707),
* niemals fremde Tokens annehmen oder weiterreichen.

Die Prüfung läuft lokal gegen den JWKS des Authorization Servers, ohne Rückfrage
bei Hydra. Das hält den Admin-Port aus dem Anfragepfad heraus und spart pro
MCP-Aufruf einen Roundtrip - der Preis ist, dass ein widerrufenes Token bis zum
Ablauf gültig bleibt. Bei einer Token-Lebensdauer von einer Stunde ist das
vertretbar; wäre es das nicht, wäre Introspection der richtige Weg.
"""

import logging
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.error import URLError

import jwt
from jwt import PyJWKClient, PyJWKClientError

from .config import Settings

logger = logging.getLogger(__name__)

# Wie lange der JWKS zwischengespeichert wird. Schlüsselwechsel schlagen
# entsprechend verzögert durch; PyJWKClient holt bei unbekannter `kid` neu.
_JWKS_CACHE_SECONDS = 600
_ALGORITHMS = ["RS256"]


class TokenError(Exception):
    """Das vorgelegte Token ist unbrauchbar.

    Attributes:
        reason: Kurzform für den `error`-Parameter im `WWW-Authenticate`-Header.
    """

    def __init__(self, message: str, reason: str = "invalid_token") -> None:
        super().__init__(message)
        self.reason = reason


class InsufficientScope(TokenError):
    """Das Token ist gültig, aber der Scope reicht für diese Operation nicht.

    Attributes:
        required: Die Scopes, die die Operation braucht - gehören nach RFC 6750
            in den `scope`-Parameter der Challenge.
    """

    def __init__(self, required: frozenset[str]) -> None:
        needed = " ".join(sorted(required))
        super().__init__(f"This operation requires the scope(s): {needed}.", "insufficient_scope")
        self.required = required


@dataclass(frozen=True, slots=True)
class AccessToken:
    """Der geprüfte Inhalt eines Access-Tokens.

    Attributes:
        subject: Die `account_id` des Menschen (Hydras `sub`).
        client_id: Der registrierte MCP-Client - zusammen mit `subject` ergibt
            das die Agent-Identität aus docs/auth.md §4.
        scopes: Die erteilten Scopes.
        client_name: Anzeigename des Clients, falls im Token enthalten.
    """

    subject: str
    client_id: str
    scopes: frozenset[str]
    client_name: str | None = None

    def require(self, *scopes: str) -> None:
        """Stellt sicher, dass alle genannten Scopes erteilt wurden.

        Raises:
            InsufficientScope: Mindestens einer fehlt. Es werden **alle**
                fehlenden auf einmal gemeldet - die Spec verlangt, nicht
                inkrementell zu challengen.
        """
        missing = frozenset(scopes) - self.scopes
        if missing:
            raise InsufficientScope(frozenset(scopes))


class TokenVerifier:
    """Prüft Bearer-Tokens gegen den JWKS des Authorization Servers."""

    def __init__(self, settings: Settings) -> None:
        """Args:
        settings: Konfiguration mit Issuer und kanonischer Resource-URI.
        """
        self._issuer = settings.oauth_issuer.rstrip("/")
        self._audience = settings.mcp_resource_uri
        self._jwks = PyJWKClient(
            settings.effective_jwks_url, cache_keys=True, lifespan=_JWKS_CACHE_SECONDS
        )

    def verify(self, headers: Mapping[str, str] | None) -> AccessToken:
        """Liest das Bearer-Token aus den Headern und prüft es.

        Args:
            headers: Die Header des eingehenden Requests.

        Returns:
            Der geprüfte Tokeninhalt.

        Raises:
            TokenError: Kein Token, kaputte Signatur, abgelaufen, falscher
                Aussteller oder falsche Audience.
        """
        raw = _bearer_token(headers)
        if raw is None:
            raise TokenError("No access token was provided.", "invalid_request")

        try:
            signing_key = self._jwks.get_signing_key_from_jwt(raw)
            claims: dict[str, Any] = jwt.decode(
                raw,
                signing_key.key,
                algorithms=_ALGORITHMS,
                issuer=self._issuer,
                audience=self._audience,
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
            )
        except jwt.InvalidAudienceError as exc:
            # Der wichtigste Einzelfall: ein gültiges Token für einen *anderen*
            # Dienst. Genau das soll die Audience-Bindung verhindern.
            logger.warning("Token mit fremder Audience abgewiesen: %s", exc)
            raise TokenError(
                f"This token was not issued for {self._audience}. "
                "Request a token with the correct `resource` parameter.",
            ) from exc
        except jwt.PyJWTError as exc:
            raise TokenError(f"The access token is not valid: {exc}") from exc
        except (PyJWKClientError, URLError, OSError) as exc:
            # JWKS nicht erreichbar - kein Tokenfehler, aber der Aufruf scheitert.
            raise TokenError("The authorization server is currently unreachable.", "temporarily_unavailable") from exc

        return AccessToken(
            subject=claims["sub"],
            client_id=claims.get("client_id", ""),
            scopes=frozenset((claims.get("scp") or _scope_list(claims))),
            client_name=_client_name(claims),
        )


def protected_resource_metadata(settings: Settings) -> dict[str, Any]:
    """Baut das Dokument nach RFC 9728.

    MCP-Clients finden darüber den Authorization Server. Das Ausliefern ist ein
    **MUST** der Spec.

    Args:
        settings: Konfiguration mit Resource-URI, Issuer und Scopes.

    Returns:
        Der Inhalt von `/.well-known/oauth-protected-resource`.
    """
    return {
        "resource": settings.mcp_resource_uri,
        "authorization_servers": [settings.oauth_issuer.rstrip("/")],
        "scopes_supported": sorted(settings.supported_scope_set),
        "bearer_methods_supported": ["header"],
        "resource_documentation": f"{settings.public_base_url}/",
    }


def challenge_header(settings: Settings, error: TokenError | None = None) -> str:
    """Baut den `WWW-Authenticate`-Header für 401- und 403-Antworten.

    Der `resource_metadata`-Verweis ist der Einstieg in den gesamten
    OAuth-Ablauf: ein MCP-Client, der ihn sieht, findet von dort aus den
    Authorization Server und startet die Autorisierung selbst.

    Args:
        settings: Konfiguration mit der Metadata-URL.
        error: Der aufgetretene Fehler, falls es einen gab.

    Returns:
        Der vollständige Header-Wert.
    """
    parts = [f'resource_metadata="{settings.protected_resource_metadata_url}"']

    if isinstance(error, InsufficientScope):
        parts.append('error="insufficient_scope"')
        parts.append(f'scope="{" ".join(sorted(error.required))}"')
    else:
        if error is not None and error.reason != "invalid_request":
            parts.append(f'error="{error.reason}"')
        # Ohne Token: die Scopes nennen, die für den Normalbetrieb nötig sind.
        parts.append(f'scope="{" ".join(sorted(settings.supported_scope_set))}"')

    if error is not None:
        parts.append(f'error_description="{_escape(str(error))}"')

    return "Bearer " + ", ".join(parts)


def _bearer_token(headers: Mapping[str, str] | None) -> str | None:
    """Zieht das Token aus dem `Authorization`-Header."""
    if not headers:
        return None
    value = headers.get("authorization") or headers.get("Authorization") or ""
    scheme, _, token = value.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


def _scope_list(claims: dict[str, Any]) -> list[str]:
    """Liest Scopes aus dem `scope`-Claim, der als String kommen kann."""
    scope = claims.get("scope", "")
    return scope.split() if isinstance(scope, str) else list(scope)


def _client_name(claims: dict[str, Any]) -> str | None:
    """Holt den Anzeigenamen des Clients aus den Zusatz-Claims, falls vorhanden."""
    extra = claims.get("ext") or {}
    aiphidias = extra.get("aiphidias") if isinstance(extra, dict) else None
    return aiphidias.get("client_name") if isinstance(aiphidias, dict) else None


def _escape(text: str) -> str:
    """Entschärft Anführungszeichen für die Verwendung im Header."""
    return text.replace('"', "'").replace("\n", " ")
