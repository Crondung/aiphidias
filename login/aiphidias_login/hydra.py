"""Dünner Client für Hydras Admin-API.

Nur die fünf Aufrufe, die der Login- und Consent-Flow braucht. Bewusst kein
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

    async def accept_login(self, challenge: str, subject: str, remember: bool = True) -> str:
        """Bestätigt einen Login und liefert die URL, zu der weitergeleitet wird.

        Args:
            challenge: Die Login-Challenge aus Hydras Weiterleitung.
            subject: Stabile Kennung des Menschen - unsere `account_id`.
            remember: Ob Hydra eine Session setzen soll, die den nächsten Login
                ohne GitHub-Umweg erlaubt.

        Returns:
            Die Weiterleitungs-URL zurück zu Hydra.

        Raises:
            HydraError: Hydra hat die Bestätigung abgelehnt.
        """
        # `remember_for: 0` heißt bei Hydra "bis die Session endet", nicht "gar nicht".
        result = await self._request(
            "PUT",
            "/admin/oauth2/auth/requests/login/accept",
            params={"login_challenge": challenge},
            json={"subject": subject, "remember": remember, "remember_for": 0},
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
