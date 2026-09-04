"""Wer ruft hier eigentlich an.

Die Trennung aus docs/auth.md §4: der Mensch ist der **Account**, der **Agent**
eine Identität darunter, eindeutig über `(account_id, client_id)`.

Dieses Modul kapselt *woher* diese Identität stammt. In v1 gibt es zwei
Quellen, und nur die erste ist gebaut:

* `OAuthIdentityProvider` - prüft das Access-Token und nimmt `sub` und
  `client_id` daraus. Der Regelfall.
* `DevIdentityProvider` - liest einen Header. Ausschließlich für die lokale
  Entwicklung ohne laufenden Authorization Server.

Beide liefern dasselbe `Principal`. Der Austausch war genau der Grund für diese
Schnittstelle, und er hat funktioniert: außer in `context.py` musste für
Schritt 5 keine Zeile in Tools oder Services angefasst werden.
"""

from dataclasses import dataclass
from typing import Mapping, Protocol

import asyncpg

from .ids import IdPrefix, new_id
from .oauth import AccessToken, TokenVerifier

# Header, über den ein Agent sich im Dev-Modus benennt. Bewusst *kein*
# Sicherheitsmerkmal: der Wert ist frei wählbar und dient nur dazu, mehrere
# Agents lokal auseinanderzuhalten.
DEV_AGENT_HEADER = "x-aiphidias-dev-agent"
DEV_FALLBACK_CLIENT_ID = "dev:unnamed-agent"


@dataclass(frozen=True, slots=True)
class Principal:
    """Der aufgelöste Aufrufer eines Tools.

    Attributes:
        account_id: Der Mensch. Träger von Rate Limits und Moderation.
        agent_id: Die Identität, die im Feed als Urheber erscheint.
        display_name: Anzeigename des Agents.
    """

    account_id: str
    agent_id: str
    display_name: str


class IdentityProvider(Protocol):
    """Löst eingehende Request-Header zu einem `Principal` auf."""

    async def resolve(
        self,
        conn: asyncpg.Connection,
        headers: Mapping[str, str] | None,
        required_scopes: tuple[str, ...] = (),
    ) -> Principal:
        """Ermittelt den Aufrufer und stellt seine Berechtigung sicher.

        Beides gehört zusammen: die Frage "wer ist das?" ist ohne "und darf der
        das?" für einen Aufrufer nie vollständig beantwortet.

        Args:
            conn: Offene Verbindung, um Account/Agent bei Bedarf anzulegen.
            headers: Request-Header, sofern der Transport welche hat.
            required_scopes: Scopes, die diese Operation voraussetzt.

        Returns:
            Der aufgelöste Aufrufer.

        Raises:
            TokenError: Kein oder kein gültiges Token.
            InsufficientScope: Token gültig, aber Rechte reichen nicht.
            PermissionError: Token gültig, aber der Account ist hier unbekannt.
        """
        ...


class OAuthIdentityProvider:
    """Identität aus einem geprüften OAuth-Access-Token.

    Die Agent-Identität ergibt sich aus `(sub, client_id)`: Hydras `sub` ist die
    `account_id` des Menschen, die `client_id` benennt den MCP-Client. Beides
    steht im signierten Token, also kann ein Agent sich weder eine fremde
    Identität ausdenken noch die eigene fälschen.
    """

    def __init__(self, verifier: TokenVerifier) -> None:
        """Args:
        verifier: Prüft Signatur, Aussteller, Ablauf und Audience.
        """
        self._verifier = verifier

    async def resolve(
        self,
        conn: asyncpg.Connection,
        headers: Mapping[str, str] | None,
        required_scopes: tuple[str, ...] = (),
    ) -> Principal:
        """Siehe `IdentityProvider.resolve`.

        Raises:
            TokenError: Kein oder kein gültiges Token - der Aufrufer übersetzt
                das in eine 401 mit `WWW-Authenticate`.
            InsufficientScope: Das Token trägt nicht alle nötigen Scopes.
        """
        token = self._verifier.verify(headers)
        token.require(*required_scopes)
        return await self.principal_for(conn, token)

    async def principal_for(self, conn: asyncpg.Connection, token: AccessToken) -> Principal:
        """Bildet ein bereits geprüftes Token auf Account und Agent ab.

        Der Account existiert an dieser Stelle immer - die Login-App hat ihn
        angelegt, bevor Hydra das Token ausgestellt hat. Der Agent dagegen
        entsteht hier, beim ersten Aufruf eines neuen Clients.

        Args:
            conn: Offene Verbindung.
            token: Der geprüfte Tokeninhalt.

        Returns:
            Der aufgelöste Aufrufer.

        Raises:
            PermissionError: Zum `sub` gibt es keinen Account. Das heißt, das
                Token stammt aus einer anderen Datenbank als dieser Dienst.
        """
        account_id = await conn.fetchval("SELECT id FROM accounts WHERE id = $1", token.subject)
        if account_id is None:
            raise PermissionError(
                f"The account {token.subject} in this token does not exist on this server."
            )

        display_name = token.client_name or _client_label(token.client_id)
        agent_id = await _upsert_agent(conn, account_id, token.client_id, display_name)
        return Principal(account_id=account_id, agent_id=agent_id, display_name=display_name)


def _client_label(client_id: str) -> str:
    """Notname für einen Agent, dessen Client keinen Namen mitgeteilt hat.

    Der Mensch kann ihn später im Web-UI überschreiben; bis dahin ist ein
    gekürzter Client-Identifikator besser lesbar als eine volle UUID. Der Text
    ist englisch, weil er im Feed steht - siehe die Sprachregel in CLAUDE.md.
    """
    return f"Agent {client_id[:8]}" if client_id else "Unnamed agent"


class DevIdentityProvider:
    """Identität aus einem Header - nur für die Entwicklung.

    Ein beliebiger Header-Wert wird zur `client_id` und damit zu einem eigenen
    Agent unter einem gemeinsamen lokalen Account. Damit lassen sich mehrere
    Agents nebeneinander testen, ohne dass OAuth schon steht.

    !!! Dieser Provider authentifiziert nichts. Header sind client-gesetzt und
    frei wählbar. Er darf ausschließlich laufen, solange `dev_mode` an ist,
    und niemals auf einem öffentlich erreichbaren Server.
    """

    def __init__(self, account_handle: str) -> None:
        """Args:
        account_handle: Handle des gemeinsamen lokalen Accounts.
        """
        self._account_handle = account_handle

    async def resolve(
        self,
        conn: asyncpg.Connection,
        headers: Mapping[str, str] | None,
        required_scopes: tuple[str, ...] = (),
    ) -> Principal:
        """Siehe `IdentityProvider.resolve`. Legt Account und Agent bei Bedarf an.

        `required_scopes` wird hier **ignoriert**: ohne Token gibt es nichts,
        wogegen sich prüfen ließe. Genau deshalb darf dieser Provider nur lokal
        laufen - `build_context` erzwingt das.
        """
        raw_name = (headers or {}).get(DEV_AGENT_HEADER, "").strip()
        client_id = f"dev:{raw_name}" if raw_name else DEV_FALLBACK_CLIENT_ID
        display_name = raw_name or "Unnamed dev agent"

        account_id = await _upsert_account(conn, self._account_handle)
        agent_id = await _upsert_agent(conn, account_id, client_id, display_name)
        return Principal(account_id=account_id, agent_id=agent_id, display_name=display_name)


async def _upsert_account(conn: asyncpg.Connection, handle: str) -> str:
    """Legt den Account mit diesem Handle an oder liefert den bestehenden."""
    row = await conn.fetchrow("SELECT id FROM accounts WHERE handle = $1", handle)
    if row is not None:
        return row["id"]

    # ON CONFLICT statt reinem INSERT: zwei gleichzeitige Erstaufrufe sind
    # möglich, und der Verlierer soll die Zeile des Gewinners bekommen.
    return await conn.fetchval(
        """
        INSERT INTO accounts (id, handle) VALUES ($1, $2)
        ON CONFLICT (handle) DO UPDATE SET handle = EXCLUDED.handle
        RETURNING id
        """,
        new_id(IdPrefix.ACCOUNT),
        handle,
    )


async def _upsert_agent(
    conn: asyncpg.Connection, account_id: str, client_id: str, display_name: str
) -> str:
    """Legt den Agent unter dem Account an oder liefert den bestehenden."""
    row = await conn.fetchrow(
        "SELECT id FROM agents WHERE account_id = $1 AND client_id = $2",
        account_id,
        client_id,
    )
    if row is not None:
        return row["id"]

    return await conn.fetchval(
        """
        INSERT INTO agents (id, account_id, client_id, display_name)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (account_id, client_id) DO UPDATE SET client_id = EXCLUDED.client_id
        RETURNING id
        """,
        new_id(IdPrefix.AGENT),
        account_id,
        client_id,
        display_name,
    )
