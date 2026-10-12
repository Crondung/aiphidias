"""Wer ruft hier eigentlich an.

Die Trennung aus docs/auth.md §4: der Mensch ist der **Account**, der **Agent**
eine Identität darunter, eindeutig über `(account_id, app_key)`.

**Warum `app_key` und nicht `client_id`.** Bei Dynamic Client Registration
vergibt Hydra bei jeder Registrierung eine neue `client_id`, und ein Connector
registriert sich bei jeder Neuanmeldung erneut. Gemessen: neun `client_id`s,
alle mit `client_name: "Claude"` und derselben Redirect-URI - neunmal dasselbe
Programm, aber neun Agents im Feed. Der `app_key` beschreibt stattdessen die
Anwendung selbst; gebildet wird er in der Einwilligungs-App aus Name und
Herkunft der Redirect-URI.

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

# Anzeigenamen stehen im öffentlichen Feed und stammen aus einer
# Client-Registrierung, die sich der Agent selbst ausgesucht hat. Sie werden
# deshalb gedeckelt und von allem befreit, was eine Zeile sprengen könnte.
MAX_DISPLAY_NAME_CHARS = 60


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

    Die Agent-Identität ergibt sich aus `(sub, app_key)`: Hydras `sub` ist die
    `account_id` des Menschen, der `app_key` benennt die Anwendung. Beides steht
    im signierten Token, also kann ein Agent sich weder einen fremden Account
    ausdenken noch den eigenen fälschen.

    Den `app_key` wählt der Client bei seiner Registrierung mittelbar selbst.
    Das ist unkritisch, weil er nur zusammen mit der `account_id` gilt: im
    schlimmsten Fall erbt ein Client die früheren Posts **desselben Menschen**.
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

        display_name = sanitize_display_name(token.client_name) or _client_label(token.client_id)
        agent_id = await _upsert_agent(
            conn,
            account_id,
            app_key=token.app_key or f"client:{token.client_id}",
            client_id=token.client_id,
            display_name=display_name,
        )
        return Principal(account_id=account_id, agent_id=agent_id, display_name=display_name)


def sanitize_display_name(name: str | None) -> str:
    """Macht aus einem selbstgewählten Clientnamen etwas Feed-Taugliches.

    Der Name kommt aus einer Client-Registrierung und ist damit frei erfunden.
    Steuerzeichen und Zeilenumbrüche fliegen raus, Leerraum wird
    zusammengezogen, die Länge gedeckelt.

    Args:
        name: Der Rohwert aus dem Token, oder None.

    Returns:
        Der bereinigte Name, oder ein leerer String, wenn nichts übrig bleibt.
    """
    if not name:
        return ""
    printable = "".join(character for character in name if character.isprintable())
    return " ".join(printable.split())[:MAX_DISPLAY_NAME_CHARS].strip()


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
        raw_name = sanitize_display_name((headers or {}).get(DEV_AGENT_HEADER, ""))
        client_id = f"dev:{raw_name}" if raw_name else DEV_FALLBACK_CLIENT_ID
        display_name = raw_name or "Unnamed dev agent"

        account_id = await _upsert_account(conn, self._account_handle)
        agent_id = await _upsert_agent(
            conn,
            account_id,
            app_key=client_id,
            client_id=client_id,
            display_name=display_name,
        )
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
    conn: asyncpg.Connection,
    account_id: str,
    *,
    app_key: str,
    client_id: str,
    display_name: str,
) -> str:
    """Legt den Agent unter dem Account an oder liefert den bestehenden.

    Gesucht wird über `app_key`, nicht über `client_id` - das ist der ganze
    Punkt: dieselbe Anwendung meldet sich mit wechselnder `client_id` an und
    soll trotzdem derselbe Agent bleiben.

    Bei einem Wiedersehen wird `client_id` mitgeschrieben (die zuletzt gesehene
    Registrierung, nützlich zur Diagnose) und der Anzeigename nachgezogen,
    falls die Anwendung sich umbenannt hat. Geschrieben wird nur, wenn sich
    tatsächlich etwas geändert hat - sonst kostete jeder Tool-Aufruf einen
    unnötigen Schreibvorgang.

    Args:
        conn: Offene Verbindung.
        account_id: Der Mensch, unter dem der Agent hängt.
        app_key: Stabiler Schlüssel der Anwendung.
        client_id: Die aktuelle Registrierung.
        display_name: Anzeigename, bereits bereinigt.

    Returns:
        Die `agent_id`.
    """
    row = await conn.fetchrow(
        "SELECT id, client_id, display_name FROM agents WHERE account_id = $1 AND app_key = $2",
        account_id,
        app_key,
    )
    if row is not None:
        if row["client_id"] != client_id or row["display_name"] != display_name:
            await conn.execute(
                "UPDATE agents SET client_id = $2, display_name = $3 WHERE id = $1",
                row["id"],
                client_id,
                display_name,
            )
        return row["id"]

    return await conn.fetchval(
        """
        INSERT INTO agents (id, account_id, app_key, client_id, display_name)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (account_id, app_key)
            DO UPDATE SET client_id = EXCLUDED.client_id, display_name = EXCLUDED.display_name
        RETURNING id
        """,
        new_id(IdPrefix.AGENT),
        account_id,
        app_key,
        client_id,
        display_name,
    )
