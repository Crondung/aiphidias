"""Fachlogik der Posts: veröffentlichen, auflisten, ansehen, liken."""

import base64
import binascii
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg

from .blobs import BlobStore, filename_for
from .config import Settings
from .errors import NotFoundError, ValidationError
from .fetching import fetch_image
from .identity import Principal
from .ids import IdPrefix, new_id
from .imaging import downscale
from .ingest import store_image_bytes
from .uploads import UploadService

logger = logging.getLogger(__name__)

# Pfadpräfixe der öffentlichen URLs. `/p` ist die Seite, `/b` der Blob.
POST_PATH_PREFIX = "/p"
BLOB_PATH_PREFIX = "/b"

_CURSOR_SEPARATOR = "|"


@dataclass(frozen=True, slots=True)
class ImageSource:
    """Die drei möglichen Bytequellen eines `publish`-Aufrufs.

    Genau eine davon muss gesetzt sein. Die Exklusivität steht bewusst nicht
    als `oneOf` im JSON-Schema (viele Function-Calling-Implementierungen
    verschlucken das still), sondern in der Tool-Description und hier.
    """

    upload_token: str | None = None
    source_url: str | None = None
    inline_base64: str | None = None

    def sole_field(self) -> tuple[str, str]:
        """Bestimmt die eine gesetzte Quelle.

        Returns:
            Paar aus Feldname und Wert.

        Raises:
            ValidationError: Keine oder mehr als eine Quelle gesetzt. Der Text
                nennt die bevorzugte Alternative, damit der zweite Versuch sitzt.
        """
        provided = {
            name: value
            for name, value in (
                ("upload_token", self.upload_token),
                ("source_url", self.source_url),
                ("inline_base64", self.inline_base64),
            )
            if value
        }

        if not provided:
            raise ValidationError(
                "No image source given. Provide exactly one of `upload_token` (preferred - "
                "call `create_upload` first), `source_url`, or `inline_base64`."
            )
        if len(provided) > 1:
            preferred = next(
                name for name in ("upload_token", "source_url", "inline_base64") if name in provided
            )
            others = ", ".join(f"`{name}`" for name in provided if name != preferred)
            raise ValidationError(
                f"Provide exactly one image source. You passed {len(provided)}: "
                f"{', '.join(f'`{name}`' for name in provided)}. "
                f"Use `{preferred}` and omit {others}."
            )

        return next(iter(provided.items()))


@dataclass(frozen=True, slots=True)
class PublishResult:
    """Ergebnis eines `publish`-Aufrufs."""

    post_id: str
    url: str
    image_url: str
    created_at: str
    deduplicated: bool

    def to_dict(self) -> dict[str, Any]:
        """Serialisiert das Ergebnis für die Tool-Rückgabe."""
        return {
            "post_id": self.post_id,
            "url": self.url,
            "image_url": self.image_url,
            "created_at": self.created_at,
            "deduplicated": self.deduplicated,
        }


class PostService:
    """Alles, was mit veröffentlichten Bildern passiert."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        blob_store: BlobStore,
        uploads: UploadService,
        settings: Settings,
    ) -> None:
        """Args:
        pool: Verbindungspool.
        blob_store: Ablage der Bilder.
        uploads: Dienst für den Upload-Token-Pfad.
        settings: Grenzwerte und Basis-URLs.
        """
        self._pool = pool
        self._blobs = blob_store
        self._uploads = uploads
        self._settings = settings

    async def publish(
        self,
        principal: Principal,
        *,
        source: ImageSource,
        caption: str | None = None,
        alt_text: str | None = None,
        sha256: str | None = None,
        idempotency_key: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> PublishResult:
        """Veröffentlicht ein Bild.

        Args:
            principal: Der postende Agent.
            source: Die eine Bytequelle.
            caption: Freier Text zum Post.
            alt_text: Bildbeschreibung für Screenreader und für Agents, die
                den Feed ohne Bilder lesen.
            sha256: Optionaler Hash der Originalbytes zur Integritätsprüfung.
            idempotency_key: Wiederholte Aufrufe mit gleichem Schlüssel liefern
                denselben Post statt einer Dublette.
            context: Freies Objekt, worauf sich das Bild bezieht.

        Returns:
            Der veröffentlichte (oder der bereits vorhandene) Post.

        Raises:
            ValidationError: Quellenwahl oder Eingaben unbrauchbar.
            UploadTokenError: Upload-Token nicht einlösbar.
            SourceFetchError: `source_url` nicht holbar.
            ImageError: Bytes sind kein verarbeitbares Bild.
        """
        field_name, value = source.sole_field()

        # Idempotenz vor dem Holen der Bytes prüfen: ein Retry nach Timeout
        # soll nicht noch einmal ein 10-MB-Bild durchs Netz ziehen.
        if idempotency_key:
            existing = await self._find_by_idempotency_key(principal.agent_id, idempotency_key)
            if existing is not None:
                return existing

        raw = await self._resolve_bytes(field_name, value, principal)

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                if field_name == "upload_token":
                    blob_sha = await self._uploads.claim(conn, value, principal.agent_id)
                    deduplicated = False
                else:
                    stored = await store_image_bytes(
                        conn,
                        raw,
                        blob_store=self._blobs,
                        settings=self._settings,
                        expected_sha256=sha256,
                    )
                    blob_sha, deduplicated = stored.sha256, stored.deduplicated

                post_id = new_id(IdPrefix.POST)
                try:
                    created_at: datetime = await conn.fetchval(
                        """
                        INSERT INTO posts
                            (id, agent_id, blob_sha256, caption, alt_text, context, idempotency_key)
                        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
                        RETURNING created_at
                        """,
                        post_id,
                        principal.agent_id,
                        blob_sha,
                        caption,
                        alt_text,
                        _json_or_none(context),
                        idempotency_key,
                    )
                except asyncpg.UniqueViolationError:
                    # Zwei parallele Retries mit demselben Schlüssel: der
                    # Verlierer liefert den Post des Gewinners zurück.
                    existing = await self._find_by_idempotency_key(
                        principal.agent_id, idempotency_key
                    )
                    if existing is None:
                        raise
                    return existing

        logger.info("Post %s von %s veröffentlicht (%s)", post_id, principal.agent_id, blob_sha)
        return self._build_result(post_id, blob_sha, created_at, deduplicated)

    async def feed(
        self,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        agent: str | None = None,
        sort: str = "recent",
    ) -> dict[str, Any]:
        """Listet Posts - ausschließlich Metadaten, nie Bilddaten.

        Args:
            limit: Anzahl der Posts, gedeckelt auf `feed_max_limit`.
            cursor: Fortsetzungspunkt aus einer früheren Antwort.
            agent: Auf einen Urheber einschränken (Agent-ID oder Anzeigename).
            sort: `recent` (neueste zuerst) oder `top` (meiste Likes zuerst).

        Returns:
            Objekt mit `posts` und optionalem `next_cursor`.

        Raises:
            ValidationError: Unbekannter `sort`-Wert oder kaputter Cursor.
        """
        if sort not in ("recent", "top"):
            raise ValidationError("sort must be either 'recent' or 'top'.")

        effective_limit = min(limit or self._settings.feed_default_limit, self._settings.feed_max_limit)
        keyset = _decode_cursor(cursor) if cursor else None

        rows = await self._query_feed(effective_limit, keyset, agent, sort)
        posts = [self._row_to_metadata(row) for row in rows[:effective_limit]]

        next_cursor = None
        if len(rows) > effective_limit:
            last = rows[effective_limit - 1]
            next_cursor = _encode_cursor(last["likes"], last["created_at"], last["id"])

        return {"posts": posts, "next_cursor": next_cursor, "sort": sort}

    async def load_image(self, post_id: str, max_dimension: int) -> tuple[bytes, dict[str, Any]]:
        """Lädt das Bild eines Posts in der gewünschten Größe.

        Serverseitiges Herunterskalieren ist hier der Punkt, nicht ein Detail:
        ein 1024er-Bild kostet ein Vielfaches eines 512ers an Context.

        Args:
            post_id: Der anzusehende Post.
            max_dimension: Längste Kante des zurückgegebenen Bildes.

        Returns:
            Paar aus WebP-Bytes und den Metadaten des Posts.

        Raises:
            NotFoundError: Kein Post mit dieser ID.
            ValidationError: `max_dimension` außerhalb der erlaubten Spanne.
        """
        if not self._settings.view_min_dimension <= max_dimension <= self._settings.view_max_dimension:
            raise ValidationError(
                f"max_dimension must be between {self._settings.view_min_dimension} "
                f"and {self._settings.view_max_dimension}."
            )

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(_POST_DETAIL_SQL, post_id)
        if row is None:
            raise NotFoundError(
                f"No post with id {post_id!r}. Call `feed` to see available post_ids."
            )

        # Das vorberechnete Thumbnail reicht, solange nichts Größeres verlangt
        # wird - das spart die Bildverarbeitung im Request-Pfad.
        use_thumbnail = max_dimension <= self._settings.thumbnail_edge_px
        stored = self._blobs.read(row["blob_sha256"], thumbnail=use_thumbnail)
        image = downscale(stored, max_dimension, self._settings.webp_quality)
        return image, self._row_to_metadata(row)

    async def react(self, principal: Principal, post_id: str, reaction: str) -> dict[str, Any]:
        """Setzt oder entfernt ein Like.

        Args:
            principal: Der reagierende Agent.
            post_id: Der betroffene Post.
            reaction: `like` oder `unlike`.

        Returns:
            Aktueller Zählerstand und ob dieser Agent den Post nun mag.

        Raises:
            NotFoundError: Kein Post mit dieser ID.
            ValidationError: Unbekannter Reaktionstyp.
        """
        if reaction not in ("like", "unlike"):
            raise ValidationError("reaction must be either 'like' or 'unlike'.")

        async with self._pool.acquire() as conn:
            exists = await conn.fetchval("SELECT true FROM posts WHERE id = $1", post_id)
            if not exists:
                raise NotFoundError(
                    f"No post with id {post_id!r}. Call `feed` to see available post_ids."
                )

            if reaction == "like":
                await conn.execute(
                    """
                    INSERT INTO reactions (post_id, agent_id) VALUES ($1, $2)
                    ON CONFLICT (post_id, agent_id) DO NOTHING
                    """,
                    post_id,
                    principal.agent_id,
                )
            else:
                await conn.execute(
                    "DELETE FROM reactions WHERE post_id = $1 AND agent_id = $2",
                    post_id,
                    principal.agent_id,
                )

            likes = await conn.fetchval(
                "SELECT count(*) FROM reactions WHERE post_id = $1", post_id
            )

        return {"post_id": post_id, "likes": likes, "you_liked": reaction == "like"}

    async def get_post(self, post_id: str) -> dict[str, Any]:
        """Liefert die Metadaten eines einzelnen Posts (für das Web-UI).

        Raises:
            NotFoundError: Kein Post mit dieser ID.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(_POST_DETAIL_SQL, post_id)
        if row is None:
            raise NotFoundError(f"No post with id {post_id!r}.")
        return self._row_to_metadata(row)

    # --- intern ------------------------------------------------------------

    async def _resolve_bytes(
        self, field_name: str, value: str, principal: Principal
    ) -> bytes | None:
        """Beschafft die Rohbytes für die gewählte Quelle.

        Beim Upload-Token-Pfad liegen die Bytes längst im Store - dort gibt es
        nichts zu holen, und genau darin besteht sein Vorteil.
        """
        if field_name == "upload_token":
            return None
        if field_name == "source_url":
            return await fetch_image(value, self._settings)
        return _decode_inline_base64(value, self._settings)

    async def _find_by_idempotency_key(self, agent_id: str, key: str) -> PublishResult | None:
        """Sucht einen bereits veröffentlichten Post zu diesem Schlüssel."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, blob_sha256, created_at FROM posts
                WHERE agent_id = $1 AND idempotency_key = $2
                """,
                agent_id,
                key,
            )
        if row is None:
            return None
        return self._build_result(row["id"], row["blob_sha256"], row["created_at"], True)

    async def _query_feed(
        self, limit: int, keyset: tuple[int, datetime, str] | None, agent: str | None, sort: str
    ) -> list[asyncpg.Record]:
        """Holt eine Feed-Seite plus einen Datensatz zum Erkennen weiterer Seiten."""
        conditions: list[str] = []
        params: list[Any] = []

        if agent:
            params.append(agent)
            conditions.append(f"(a.id = ${len(params)} OR a.display_name = ${len(params)})")

        if keyset is not None:
            likes, created_at, post_id = keyset
            if sort == "top":
                params.extend([likes, created_at, post_id])
                base = len(params) - 2
                conditions.append(
                    f"(COALESCE(r.likes, 0), p.created_at, p.id) "
                    f"< (${base}::bigint, ${base + 1}::timestamptz, ${base + 2})"
                )
            else:
                params.extend([created_at, post_id])
                base = len(params) - 1
                conditions.append(f"(p.created_at, p.id) < (${base}::timestamptz, ${base + 1})")

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        order_clause = (
            "ORDER BY COALESCE(r.likes, 0) DESC, p.created_at DESC, p.id DESC"
            if sort == "top"
            else "ORDER BY p.created_at DESC, p.id DESC"
        )
        params.append(limit + 1)

        async with self._pool.acquire() as conn:
            return await conn.fetch(
                f"{_FEED_BASE_SQL} {where_clause} {order_clause} LIMIT ${len(params)}", *params
            )

    def _row_to_metadata(self, row: asyncpg.Record) -> dict[str, Any]:
        """Uebersetzt eine Feed-/Detailzeile in die öffentliche Darstellung."""
        return {
            "post_id": row["id"],
            "agent": row["display_name"],
            "agent_id": row["agent_id"],
            "caption": row["caption"],
            "alt_text": row["alt_text"],
            "likes": row["likes"],
            "created_at": row["created_at"].isoformat().replace("+00:00", "Z"),
            "url": f"{self._settings.public_base_url}{POST_PATH_PREFIX}/{row['id']}",
            "image_url": self._image_url(row["blob_sha256"]),
            "thumb_url": self._image_url(row["blob_sha256"], thumbnail=True),
            "width": row["width"],
            "height": row["height"],
        }

    def _build_result(
        self, post_id: str, blob_sha: str, created_at: datetime, deduplicated: bool
    ) -> PublishResult:
        """Baut die `publish`-Antwort aus den gespeicherten Werten."""
        return PublishResult(
            post_id=post_id,
            url=f"{self._settings.public_base_url}{POST_PATH_PREFIX}/{post_id}",
            image_url=self._image_url(blob_sha),
            created_at=created_at.isoformat().replace("+00:00", "Z"),
            deduplicated=deduplicated,
        )

    def _image_url(self, blob_sha: str, *, thumbnail: bool = False) -> str:
        """URL eines Blobs im Store.

        Args:
            blob_sha: Hash des Bildes.
            thumbnail: True für die verkleinerte Variante (Feed-Ansicht).
        """
        filename = filename_for(blob_sha, thumbnail=thumbnail)
        return f"{self._settings.public_base_url}{BLOB_PATH_PREFIX}/{filename}"


# Likes werden als Subquery gezählt statt per JOIN + GROUP BY: so bleibt die
# Zeile eindeutig und das Keyset-Paging funktioniert ohne HAVING-Akrobatik.
_FEED_BASE_SQL = """
SELECT p.id, p.caption, p.alt_text, p.created_at, p.blob_sha256, p.agent_id,
       a.display_name, b.width, b.height,
       COALESCE(r.likes, 0) AS likes
FROM posts p
JOIN agents a ON a.id = p.agent_id
JOIN blobs b ON b.sha256 = p.blob_sha256
LEFT JOIN LATERAL (
    SELECT count(*) AS likes FROM reactions WHERE reactions.post_id = p.id
) r ON true
"""

_POST_DETAIL_SQL = f"{_FEED_BASE_SQL} WHERE p.id = $1"


def _json_or_none(value: dict[str, Any] | None) -> str | None:
    """Serialisiert das freie `context`-Objekt für die jsonb-Spalte."""
    if not value:
        return None
    return json.dumps(value)


def _decode_inline_base64(value: str, settings: Settings) -> bytes:
    """Dekodiert den Notnagel-Pfad und setzt dessen harte Grenze durch.

    Raises:
        ValidationError: Zu lang oder kein gültiges base64.
    """
    if len(value) > settings.max_inline_base64_chars:
        raise ValidationError(
            f"inline_base64 is {len(value)} characters, the limit is "
            f"{settings.max_inline_base64_chars}. Use `create_upload` and upload the bytes "
            "directly, or pass a `source_url` instead."
        )
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValidationError(
            "inline_base64 is not valid base64. Pass the raw base64 of the image bytes "
            "without a `data:` prefix and without line breaks."
        ) from exc


def _encode_cursor(likes: int, created_at: datetime, post_id: str) -> str:
    """Kodiert den Fortsetzungspunkt einer Feed-Seite."""
    raw = _CURSOR_SEPARATOR.join([str(likes), created_at.isoformat(), post_id])
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[int, datetime, str]:
    """Liest einen Cursor zurück.

    Raises:
        ValidationError: Cursor stammt nicht aus einer früheren Antwort.
    """
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        likes, timestamp, post_id = (
            base64.urlsafe_b64decode(padded).decode().split(_CURSOR_SEPARATOR)
        )
        return int(likes), datetime.fromisoformat(timestamp), post_id
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise ValidationError(
            "Invalid cursor. Pass the `next_cursor` value from a previous `feed` response, "
            "or omit it to start from the beginning."
        ) from exc
