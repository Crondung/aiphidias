"""Dünner Client für Hydras Admin-API.

Nur die Aufrufe, die der Login- und Consent-Flow braucht. Bewusst kein
generierter SDK-Client: der wäre um Größenordnungen umfangreicher als das, was
hier gebraucht wird, und würde das Bild verstellen, wie wenig Hydra eigentlich
von uns will.
"""

import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 10.0


class HydraError(RuntimeError):
    """Hydras Admin-API hat einen Fehler gemeldet."""


@dataclass(frozen=True, slots=True)
class HydraAdmin:
    """Zugriff auf die Admin-Endpunkte von Ory Hydra.

    Attributes:
        base_url: Basis-URL des Admin-Ports (niemals öffentlich erreichbar).
    """

    base_url: str

    async def get_login_request(self, challenge: str) -> dict[str, Any]:
        """Holt den Login-Request zu einer Challenge."""
        return await self._request(
            "GET", "/admin/oauth2/auth/requests/login", params={"login_challenge": challenge}
        )

    async def accept_login(self, challenge: str, subject: str, remember_for_seconds: int) -> str:
        """Bestätigt einen Login und liefert die URL, zu der weitergeleitet wird.

        Args:
            challenge: Die Login-Challenge aus Hydras Weiterleitung.
            subject: Stabile Kennung des Menschen - unsere `account_id`.
            remember_for_seconds: Wie lange Hydras Login-Session im Browser
                hält, sodass der nächste Login ohne GitHub-Umweg auskommt.
                Muss größer als 0 sein, siehe unten.

        Returns:
            Die Weiterleitungs-URL zurück zu Hydra.

        Raises:
            HydraError: Hydra hat die Bestätigung abgelehnt.
            ValueError: `remember_for_seconds` ist nicht positiv.
        """
        # Vorsicht, Hydra ist hier uneinheitlich: beim *Login* heißt
        # `remember_for: 0` "Session-Cookie", also weg, sobald der Browser
        # schließt. Beim *Consent* heißt derselbe Wert "für immer". Mit 0 musste
        # jeder Login wieder über GitHub - samt 2FA.
        if remember_for_seconds <= 0:
            raise ValueError("remember_for_seconds must be positive; 0 means a browser-session cookie")
        result = await self._request(
            "PUT",
            "/admin/oauth2/auth/requests/login/accept",
            params={"login_challenge": challenge},
            json={"subject": subject, "remember": True, "remember_for": remember_for_seconds},
        )
        return result["redirect_to"]

    async def get_consent_request(self, challenge: str) -> dict[str, Any]:
        """Holt den Consent-Request zu einer Challenge."""
        return await self._request(
            "GET", "/admin/oauth2/auth/requests/consent", params={"consent_challenge": challenge}
        )

    async def accept_consent(
        self,
        challenge: str,
        *,
        grant_scope: list[str],
        audience: list[str],
        session: dict[str, Any],
        remember: bool = True,
    ) -> str:
        """Bestätigt eine Einwilligung und bindet die Token-Audience.

        `audience` ist hier der entscheidende Parameter: Hydra ignoriert den
        `resource`-Parameter aus RFC 8707, deshalb setzt diese App die Audience
        selbst. Ohne sie trüge das Token kein `aud` und wäre bei jedem Resource
        Server verwendbar, der nicht prüft.

        Args:
            challenge: Die Consent-Challenge.
            grant_scope: Die tatsächlich erteilten Scopes.
            audience: Die Resource-URIs, für die das Token gelten soll.
            session: Zusätzliche Claims für Access- und ID-Token.
            remember: Ob die Einwilligung gespeichert wird.

        Returns:
            Die Weiterleitungs-URL zurück zu Hydra.

        Raises:
            HydraError: Hydra hat die Bestätigung abgelehnt.
        """
        result = await self._request(
            "PUT",
            "/admin/oauth2/auth/requests/consent/accept",
            params={"consent_challenge": challenge},
            json={
                "grant_scope": grant_scope,
                "grant_access_token_audience": audience,
                "session": session,
                "remember": remember,
                "remember_for": 0,
            },
        )
        return result["redirect_to"]

    async def allow_client_audience(self, client: dict[str, Any], audience: list[str]) -> None:
        """Trägt Resource-URIs in die erlaubte Audience eines Clients ein.

        Notwendig, weil `accept_consent` die Audience ins Token schreibt, Hydra
        sie aber beim **Refresh** gegen die Audience des Clients prüft - und
        per DCR registrierte Clients haben dort eine leere Liste. Ohne diesen
        Eintrag gelingt der erste Code-Tausch, jeder Refresh scheitert mit
        "Requested audience ... has not been whitelisted", und der Mensch muss
        sich nach Ablauf des Access-Tokens neu anmelden. Gemessen in
        Produktion, Claude.ai-Connector, 2026-10.

        Idempotent: ist alles schon eingetragen, passiert nichts.

        Args:
            client: Das Client-Objekt aus dem Consent-Request.
            audience: Die Resource-URIs, die der Client anfordern darf.

        Raises:
            HydraError: Hydra hat die Änderung abgelehnt.
        """
        current = client.get("audience") or []
        missing = [uri for uri in audience if uri not in current]
        if not missing:
            return
        await self._request(
            "PATCH",
            f"/admin/clients/{client['client_id']}",
            # `add` auf ein Objektfeld ersetzt einen vorhandenen Wert (RFC 6902
            # §4.1) und funktioniert damit auch, wenn das Feld fehlt.
            json=[{"op": "add", "path": "/audience", "value": current + missing}],
        )
        logger.info("Audience für Client %s ergänzt: %s", client["client_id"], missing)

    async def reject_consent(self, challenge: str, error: str, description: str) -> str:
        """Weist eine Einwilligung ab und liefert die Weiterleitungs-URL."""
        result = await self._request(
            "PUT",
            "/admin/oauth2/auth/requests/consent/reject",
            params={"consent_challenge": challenge},
            json={"error": error, "error_description": description},
        )
        return result["redirect_to"]

    async def _request(
        self, method: str, path: str, **kwargs: Any
    ) -> dict[str, Any]:
        """Führt einen Admin-API-Aufruf aus.

        Raises:
            HydraError: Netzwerkfehler oder Statuscode ab 400.
        """
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            try:
                response = await client.request(method, f"{self.base_url}{path}", **kwargs)
            except httpx.HTTPError as exc:
                raise HydraError(f"Hydra nicht erreichbar: {exc}") from exc

        if response.status_code >= 400:
            logger.error("Hydra %s %s → %s: %s", method, path, response.status_code, response.text[:400])
            raise HydraError(f"Hydra antwortete mit {response.status_code}")
        return response.json()
