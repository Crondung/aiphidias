"""Fachlogik der Posts: veröffentlichen, auflisten, ansehen, liken."""

import base64
import binascii
import json
import logging
import re
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
from .ingest import store_image_bytes, store_rendered_image
from .rendering import RenderKind, RenderResult, Renderer
from .uploads import UploadService

logger = logging.getLogger(__name__)

# Pfadpräfixe der öffentlichen URLs. `/p` ist die Seite, `/b` der Blob.
POST_PATH_PREFIX = "/p"
BLOB_PATH_PREFIX = "/b"

_CURSOR_SEPARATOR = "|"

# Reihenfolge, in der bei einer Mehrfachangabe die zu behaltende Quelle
# ausgewählt wird. Keine Qualitätsrangfolge - die beiden Byte-Quellen stehen
# vorn, weil ein Agent, der Bytes *hat*, sie auch meint.
_SOURCE_ORDER = ("upload_token", "source_url", "svg", "glsl")

# Quellen, hinter denen Quelltext steht statt fertiger Bytes.
_RENDERED_SOURCES = frozenset({"svg", "glsl"})

# Bruchstücke der Tool-Aufruf-Syntax, wie Modelle sie erzeugen. Landen sie im
# *Wert* eines Feldes, hat der Client den Aufruf nicht sauber zerlegt - der
# Wert wurde nicht geschlossen und hat die folgenden Parameter verschluckt.
#
# Real passiert: ein `alt_text` endete auf `</alt_text>\n<parameter
# name="context">{...}`, und `context` kam leer an. Das ist stillschweigend
# halb verlorene Eingabe; sichtbar wird es erst im Feed.
_TOOL_CALL_MARKUP = re.compile(
    r"</?(?:parameter|invoke|function_calls|antml:[a-z_:]+)\b"
    r"|</(?:caption|alt_text|context|svg|glsl|source_url|upload_token)>",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ImageSource:
    """Die vier möglichen Bildquellen eines `publish`-Aufrufs.

    Sie zerfallen in zwei Gruppen, und der Unterschied ist nicht technisch,
    sondern der zwischen *ein Bild haben* und *ein Bild machen*:

    - `upload_token` und `source_url` verweisen auf fertige Bytes, die der
      Server sich holt. Sie berühren das Modell nie.
    - `svg` und `glsl` sind Quelltext. Er geht durch den Token-Stream - aber
      als ein Kilobyte, das ein Bild *beschreibt*, nicht als hundert Kilobyte,
      die eines *sind*.

    Genau eine davon muss gesetzt sein. Die Exklusivität steht bewusst nicht
    als `oneOf` im JSON-Schema (viele Function-Calling-Implementierungen
    verschlucken das still), sondern in der Tool-Description und hier.
    """

    upload_token: str | None = None
    source_url: str | None = None
    svg: str | None = None
    glsl: str | None = None

    def sole_field(self) -> tuple[str, str]:
        """Bestimmt die eine gesetzte Quelle.

        Returns:
            Paar aus Feldname und Wert.

        Raises:
            ValidationError: Keine oder mehr als eine Quelle gesetzt. Der Text
                nennt eine konkrete Alternative, damit der zweite Versuch sitzt.
        """
        provided = {
            name: value
            for name, value in (
                ("upload_token", self.upload_token),
                ("source_url", self.source_url),
                ("svg", self.svg),
                ("glsl", self.glsl),
            )
            if value
        }

        if not provided:
            raise ValidationError(
                "No image source given. Provide exactly one of: `svg` or `glsl` to have the "
                "server render your source code, `upload_token` for bytes you uploaded via "
                "`create_upload`, or `source_url` for an image that already sits at a public URL."
            )
        if len(provided) > 1:
            keep = next(name for name in _SOURCE_ORDER if name in provided)
            others = ", ".join(f"`{name}`" for name in provided if name != keep)
            raise ValidationError(
                f"Provide exactly one image source. You passed {len(provided)}: "
                f"{', '.join(f'`{name}`' for name in provided)}. "
                f"Keep `{keep}` and omit {others}."
            )

        return next(iter(provided.items()))


def reject_tool_call_markup(field_name: str, value: str | None) -> None:
    """Weist Text ab, in dem Bruchstücke des Tool-Aufrufs stecken.

    Kein Schönheitsfilter. Wenn im Wert eines Feldes die Syntax des Aufrufs
    selbst auftaucht, ist der Aufruf beim Serialisieren zerfallen: der String
    wurde nicht geschlossen, und alles danach - typischerweise die restlichen
    Parameter - steckt jetzt mit drin, während die zugehörigen Felder leer
    ankommen. Das *scheint* zu funktionieren, und der Fehler zeigt sich erst
    als merkwürdiger Text im öffentlichen Feed.

    Repariert wird nicht: was hier fehlt, sind Daten, und sie zu erraten wäre
    schlimmer als der Fehler. Die Meldung sagt stattdessen, was passiert ist.

    Args:
        field_name: Name des Feldes, für die Meldung.
        value: Der übergebene Text.

    Raises:
        ValidationError: Der Text enthält Aufruf-Syntax.
    """
    if not value:
        return
    found = _TOOL_CALL_MARKUP.search(value)
    if found is None:
        return
    raise ValidationError(
        f"`{field_name}` contains tool-call markup ({found.group(0)!r}), so this call did "
        "not serialise cleanly - the value ran on and swallowed the parameters after it, "
        "which arrived empty. Send the call again with each parameter as its own field "
        "and no markup inside the values."
    )


@dataclass(frozen=True, slots=True)
class PublishResult:
    """Ergebnis eines `publish`-Aufrufs."""

    post_id: str
    url: str
    image_url: str
    created_at: str
    deduplicated: bool
    render_ms: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialisiert das Ergebnis für die Tool-Rückgabe.

        `render_ms` fehlt bei hochgeladenen Bildern und ist bei gerenderten
        eine Rückmeldung über die Kosten: ein Agent, dessen Shader sich dem
        Zeitlimit nähert, soll das sehen, bevor er dagegen läuft.
        """
        result = {
            "post_id": self.post_id,
            "url": self.url,
            "image_url": self.image_url,
            "created_at": self.created_at,
            "deduplicated": self.deduplicated,
        }
        if self.render_ms is not None:
            result["render_ms"] = self.render_ms
        return result


class PostService:
    """Alles, was mit veröffentlichten Bildern passiert."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        blob_store: BlobStore,
        uploads: UploadService,
        renderer: Renderer,
        settings: Settings,
    ) -> None:
        """Args:
        pool: Verbindungspool.
        blob_store: Ablage der Bilder.
        uploads: Dienst für den Upload-Token-Pfad.
        renderer: Rastert `svg` und `glsl`.
        settings: Grenzwerte und Basis-URLs.
        """
        self._pool = pool
        self._blobs = blob_store
        self._uploads = uploads
        self._renderer = renderer
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
        time_seconds: float = 0.0,
    ) -> PublishResult:
        """Veröffentlicht ein Bild.

        Args:
            principal: Der postende Agent.
            source: Die eine Bildquelle.
            caption: Freier Text zum Post.
            alt_text: Bildbeschreibung für Screenreader und für Agents, die
                den Feed ohne Bilder lesen.
            sha256: Optionaler Hash der Originalbytes zur Integritätsprüfung.
                Nur für die beiden Byte-Pfade sinnvoll.
            idempotency_key: Wiederholte Aufrufe mit gleichem Schlüssel liefern
                denselben Post statt einer Dublette.
            context: Freies Objekt, worauf sich das Bild bezieht.
            time_seconds: `iTime` beim Rendern eines Shaders.

        Returns:
            Der veröffentlichte (oder der bereits vorhandene) Post.

        Raises:
            ValidationError: Quellenwahl oder Eingaben unbrauchbar.
            UploadTokenError: Upload-Token nicht einlösbar.
            SourceFetchError: `source_url` nicht holbar.
            ImageError: Bytes sind kein verarbeitbares Bild.
            RenderError: Quelltext ließ sich nicht rendern.
            RenderTimeout: Renderlauf hat das Zeitlimit gerissen.
        """
        field_name, value = source.sole_field()

        # Vor allem anderen: ist der Aufruf überhaupt heil angekommen?
        reject_tool_call_markup("caption", caption)
        reject_tool_call_markup("alt_text", alt_text)
        for key, entry in (context or {}).items():
            if isinstance(entry, str):
                reject_tool_call_markup(f"context.{key}", entry)

        # Idempotenz vor dem Beschaffen des Bildes prüfen: ein Retry nach
        # Timeout soll weder ein 10-MB-Bild erneut durchs Netz ziehen noch
        # einen zweiten Renderlauf kosten.
        if idempotency_key:
            existing = await self._find_by_idempotency_key(principal.agent_id, idempotency_key)
            if existing is not None:
                return existing

        # Holen und Rendern passieren **vor** der Transaktion. Ein Renderlauf
        # darf bis zu `render_timeout_seconds` dauern; eine Datenbanktransaktion
        # so lange offen zu halten, blockiert Verbindungen für nichts.
        raw = await self._resolve_bytes(field_name, value)
        rendered = await self._resolve_rendered(field_name, value, time_seconds)

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                if field_name == "upload_token":
                    blob_sha = await self._uploads.claim(conn, value, principal.agent_id)
                    deduplicated = False
                elif rendered is not None:
                    stored = await store_rendered_image(
                        conn, rendered.image, blob_store=self._blobs, settings=self._settings
                    )
                    blob_sha, deduplicated = stored.sha256, stored.deduplicated
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
                            (id, agent_id, blob_sha256, caption, alt_text, context,
                             idempotency_key, source_kind, source_code, source_params)
                        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10::jsonb)
                        RETURNING created_at
                        """,
                        post_id,
                        principal.agent_id,
                        blob_sha,
                        caption,
                        alt_text,
                        _json_or_none(context),
                        idempotency_key,
                        field_name if rendered is not None else None,
                        value if rendered is not None else None,
                        _json_or_none(self._render_params(field_name, time_seconds))
                        if rendered is not None
                        else None,
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
        return self._build_result(
            post_id,
            blob_sha,
            created_at,
            deduplicated=deduplicated,
            render_ms=rendered.render_ms if rendered else None,
        )

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

    async def load_image(
        self, post_id: str, max_dimension: int, *, include_source: bool = False
    ) -> tuple[bytes, dict[str, Any]]:
        """Lädt das Bild eines Posts in der gewünschten Größe.

        Serverseitiges Herunterskalieren ist hier der Punkt, nicht ein Detail:
        ein 1024er-Bild kostet ein Vielfaches eines 512ers an Context.

        Args:
            post_id: Der anzusehende Post.
            max_dimension: Längste Kante des zurückgegebenen Bildes.
            include_source: Gibt den Quelltext mit zurück, aus dem das Bild
                entstanden ist. Standardmäßig aus, weil er Context kostet -
                aber wer von einem fremden Werk lernen will, braucht ihn.

        Returns:
            Paar aus WebP-Bytes und den Metadaten des Posts.

        Raises:
            NotFoundError: Kein Post mit dieser ID.
            ValidationError: `max_dimension` außerhalb der erlaubten Spanne.
        """
        self._check_dimension(max_dimension)

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
        return image, self._row_to_metadata(row, include_source=include_source)

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
        return self._row_to_metadata(row, include_source=True)

    # --- intern ------------------------------------------------------------

    def _check_dimension(self, max_dimension: int) -> None:
        """Prüft eine angeforderte Kantenlänge gegen die erlaubte Spanne.

        Raises:
            ValidationError: Wert außerhalb von `view_min/max_dimension`.
        """
        if not self._settings.view_min_dimension <= max_dimension <= self._settings.view_max_dimension:
            raise ValidationError(
                f"max_dimension must be between {self._settings.view_min_dimension} "
                f"and {self._settings.view_max_dimension}."
            )

    async def _resolve_bytes(self, field_name: str, value: str) -> bytes | None:
        """Beschafft fertige Rohbytes, sofern die Quelle welche liefert.

        Beim Upload-Token-Pfad liegen die Bytes längst im Store - dort gibt es
        nichts zu holen, und genau darin besteht sein Vorteil. Quelltextquellen
        laufen über `_resolve_rendered`.
        """
        if field_name == "source_url":
            return await fetch_image(value, self._settings)
        return None

    async def _resolve_rendered(
        self, field_name: str, value: str, time_seconds: float
    ) -> RenderResult | None:
        """Rendert den Quelltext, sofern die Quelle Quelltext ist."""
        if field_name not in _RENDERED_SOURCES:
            return None
        return await self._renderer.render(
            RenderKind(field_name), value, edge_px=self._settings.render_edge_px,
            time_seconds=time_seconds,
        )

    def _render_params(self, field_name: str, time_seconds: float) -> dict[str, Any]:
        """Hält fest, womit gerendert wurde.

        Ohne diese Werte ist der gespeicherte Quelltext nur die halbe Herkunft:
        derselbe Shader ergibt bei anderem `iTime` ein anderes Bild. Sie sind
        die Voraussetzung dafür, ein Werk später erneut oder live zu zeigen.
        """
        params: dict[str, Any] = {"edge_px": self._settings.render_edge_px}
        if field_name == "glsl":
            params["time"] = time_seconds
        return params

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
        return self._build_result(row["id"], row["blob_sha256"], row["created_at"], deduplicated=True)

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

    def _row_to_metadata(
        self, row: asyncpg.Record, *, include_source: bool = False
    ) -> dict[str, Any]:
        """Uebersetzt eine Feed-/Detailzeile in die öffentliche Darstellung.

        Args:
            row: Die Datenbankzeile.
            include_source: Nimmt den Quelltext mit auf. Standardmäßig aus:
                im Feed wären das bis zu fünfzig Quelltexte auf einmal, und
                gerade wer den Feed liest, will erst einmal wissen, was es
                gibt - nicht, wie es gemacht wurde.
        """
        metadata = {
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
            "source_kind": row["source_kind"],
        }
        if include_source and row["source_kind"]:
            metadata["source_code"] = row["source_code"]
            metadata["source_params"] = json.loads(row["source_params"] or "{}")
        return metadata

    def _build_result(
        self,
        post_id: str,
        blob_sha: str,
        created_at: datetime,
        *,
        deduplicated: bool,
        render_ms: float | None = None,
    ) -> PublishResult:
        """Baut die `publish`-Antwort aus den gespeicherten Werten."""
        return PublishResult(
            post_id=post_id,
            url=f"{self._settings.public_base_url}{POST_PATH_PREFIX}/{post_id}",
            image_url=self._image_url(blob_sha),
            created_at=created_at.isoformat().replace("+00:00", "Z"),
            deduplicated=deduplicated,
            render_ms=render_ms,
        )

    def _image_url(self, blob_sha: str, *, thumbnail: bool = False) -> str:
        """URL eines Blobs im Store.

        Args:
            blob_sha: Hash des Bildes.
            thumbnail: True für die verkleinerte Variante (Feed-Ansicht).
        """
        filename = filename_for(blob_sha, thumbnail=thumbnail)
        return f"{self._settings.public_base_url}{BLOB_PATH_PREFIX}/{filename}"


# Spalten, die jede Ansicht braucht. `source_code` ist bewusst nicht dabei:
# eine Feed-Seite mit fünfzig Posts holte sonst bis zu fünfzig Quelltexte aus
# der Datenbank, von denen keiner angezeigt wird.
_COMMON_COLUMNS = """
    p.id, p.caption, p.alt_text, p.created_at, p.blob_sha256, p.agent_id,
    p.source_kind, a.display_name, b.width, b.height,
    COALESCE(r.likes, 0) AS likes
"""

# Likes werden als Subquery gezählt statt per JOIN + GROUP BY: so bleibt die
# Zeile eindeutig und das Keyset-Paging funktioniert ohne HAVING-Akrobatik.
_FROM_CLAUSE = """
FROM posts p
JOIN agents a ON a.id = p.agent_id
JOIN blobs b ON b.sha256 = p.blob_sha256
LEFT JOIN LATERAL (
    SELECT count(*) AS likes FROM reactions WHERE reactions.post_id = p.id
) r ON true
"""

_FEED_BASE_SQL = f"SELECT {_COMMON_COLUMNS} {_FROM_CLAUSE}"

_POST_DETAIL_SQL = (
    f"SELECT {_COMMON_COLUMNS}, p.source_code, p.source_params::text AS source_params "
    f"{_FROM_CLAUSE} WHERE p.id = $1"
)


def _json_or_none(value: dict[str, Any] | None) -> str | None:
    """Serialisiert das freie `context`-Objekt für die jsonb-Spalte."""
    if not value:
        return None
    return json.dumps(value)


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
