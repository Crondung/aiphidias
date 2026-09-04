"""Die Grenze, an der ein Aufruf ohne gültiges Token endet.

Ein MCP-Client startet den OAuth-Ablauf, wenn er auf **HTTP 401** mit einem
`WWW-Authenticate`-Header trifft. Ein Fehler innerhalb eines Tools reicht dafür
nicht - der wäre eine erfolgreiche HTTP-Antwort mit einem Fehler darin, und der
Client hätte keinen Anlass, sich zu autorisieren.

Deshalb sitzt die Prüfung vor dem MCP-Transport und nicht in den Tools.
"""

import logging

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import Settings
from .oauth import InsufficientScope, TokenError, TokenVerifier, challenge_header

logger = logging.getLogger(__name__)


class RequireAccessToken:
    """ASGI-Middleware, die jeden Aufruf gegen ein Bearer-Token prüft.

    Die Tools prüfen das Token anschließend ein zweites Mal, wenn sie den
    Aufrufer auflösen. Das ist Absicht: der Prüfaufwand ist eine
    RSA-Signaturprüfung gegen einen zwischengespeicherten Schlüssel, und der
    Preis dafür ist niedriger als ein über den Session-Manager
    durchgereichter Zustand, der irgendwann nicht mehr zum Request passt.
    """

    def __init__(self, app: ASGIApp, settings: Settings, verifier: TokenVerifier) -> None:
        """Args:
        app: Die geschützte Anwendung (der MCP-Transport).
        settings: Konfiguration für die Challenge-Header.
        verifier: Prüft die Tokens.
        """
        self._app = app
        self._settings = settings
        self._verifier = verifier

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Lässt den Aufruf durch oder beantwortet ihn mit 401 bzw. 403."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }

        try:
            self._verifier.verify(headers)
        except TokenError as error:
            await self._reject(error, scope, receive, send)
            return

        await self._app(scope, receive, send)

    async def _reject(self, error: TokenError, scope: Scope, receive: Receive, send: Send) -> None:
        """Beantwortet einen abgelehnten Aufruf nach RFC 6750."""
        # 403 nur bei zu wenig Rechten; alles andere ist 401, damit der Client
        # den Autorisierungsablauf beginnt statt aufzugeben.
        status = 403 if isinstance(error, InsufficientScope) else 401
        logger.info("Aufruf abgewiesen (%s): %s", status, error)

        response = JSONResponse(
            {"error": error.reason, "error_description": str(error)},
            status_code=status,
            headers={"WWW-Authenticate": challenge_header(self._settings, error)},
        )
        await response(scope, receive, send)
