"""Datenbankzugriff: Verbindungspool und Migrationen.

Bewusst kein ORM und kein Alembic. Das Schema ist klein und die Zugriffe sind
handverlesenes SQL; ein ORM wäre hier reine Abstraktion ohne Gegenwert. Was
Alembic beiträgt - Migrationen laufen automatisch, nachvollziehbar und genau
einmal - leistet der Runner unten in dreißig Zeilen, ohne zweite Sprache fürs
Schema.
"""

import logging
from pathlib import Path

import asyncpg

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


async def create_pool(database_url: str) -> asyncpg.Pool:
    """Baut den Verbindungspool auf.

    Args:
        database_url: libpq-URL der Zieldatenbank.

    Returns:
        Ein offener Pool. Der Aufrufer ist fürs Schließen zuständig.

    Raises:
        OSError: Datenbank nicht erreichbar (Aufrufer sollte mit Retry umgehen).
    """
    return await asyncpg.create_pool(database_url, min_size=1, max_size=10)


async def apply_migrations(pool: asyncpg.Pool) -> list[str]:
    """Spielt alle noch nicht angewandten Migrationen in Dateinamen-Reihenfolge ein.

    Jede Migration läuft in ihrer eigenen Transaktion zusammen mit dem Eintrag
    in `schema_migrations`: entweder beides oder nichts. Ein Abbruch mittendrin
    hinterlässt damit keinen Zustand, den niemand mehr einordnen kann.

    Args:
        pool: Offener Verbindungspool.

    Returns:
        Die Versionen, die in diesem Lauf tatsächlich angewandt wurden.

    Raises:
        asyncpg.PostgresError: Fehlerhaftes SQL in einer Migration.
    """
    async with pool.acquire() as conn:
        await conn.execute(_SCHEMA_MIGRATIONS_DDL)
        applied: set[str] = {
            row["version"] for row in await conn.fetch("SELECT version FROM schema_migrations")
        }

        newly_applied: list[str] = []
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = path.stem
            if version in applied:
                continue
            logger.info("Migration wird angewandt: %s", version)
            async with conn.transaction():
                await conn.execute(path.read_text(encoding="utf-8"))
                await conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES ($1)", version
                )
            newly_applied.append(version)

    return newly_applied
