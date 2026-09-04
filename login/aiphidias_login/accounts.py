"""Der Mensch hinter dem GitHub-Konto.

Ein Account pro GitHub-Nutzer (docs/auth.md §4). Die Agents darunter entstehen
nicht hier, sondern im Hauptdienst, sobald ein Token mit einer neuen
`client_id` auftaucht - der Login weiß nichts von Agents.
"""

import logging
import secrets

import asyncpg

logger = logging.getLogger(__name__)

# Gleiche Bauart wie `ids.py` im Hauptdienst.
_ID_ENTROPY_BYTES = 12


async def upsert_account(pool: asyncpg.Pool, github_user_id: str, github_login: str) -> str:
    """Findet den Account zu einer GitHub-Identität oder legt ihn an.

    Args:
        pool: Verbindungspool.
        github_user_id: GitHubs numerische ID - der stabile Schlüssel.
        github_login: GitHubs Anzeigename, dient als Vorschlag für das Handle.

    Returns:
        Die `account_id`, die als OAuth-`subject` verwendet wird.

    Raises:
        asyncpg.PostgresError: Datenbankfehler.
    """
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            "SELECT id FROM accounts WHERE github_user_id = $1", github_user_id
        )
        if existing:
            return existing

        account_id = f"ac_{secrets.token_urlsafe(_ID_ENTROPY_BYTES)}"
        # Das Handle muss eindeutig sein; bei Kollision hängt die GitHub-ID an.
        handle = await _free_handle(conn, github_login, github_user_id)

        created = await conn.fetchval(
            """
            INSERT INTO accounts (id, github_user_id, handle) VALUES ($1, $2, $3)
            ON CONFLICT (github_user_id) DO UPDATE SET github_user_id = EXCLUDED.github_user_id
            RETURNING id
            """,
            account_id,
            github_user_id,
            handle,
        )
        logger.info("Account %s für GitHub-Nutzer %s", created, github_login)
        return created


async def _free_handle(conn: asyncpg.Connection, preferred: str, github_user_id: str) -> str:
    """Liefert ein freies Handle, ausgehend vom GitHub-Login."""
    taken = await conn.fetchval("SELECT true FROM accounts WHERE handle = $1", preferred)
    return f"{preferred}-{github_user_id}" if taken else preferred
