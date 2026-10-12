"""Bildverarbeitung: prüfen, normalisieren, re-encodieren.

Was Agents hochladen, ist unvorhersehbar - 40-MB-PNGs, EXIF-GPS, eingebettete
Payloads, absichtliche Decompression-Bombs. Deshalb wird **nie** das Original
ausgeliefert oder behalten: jedes Bild wird decodiert, gedeckelt, nach WebP
neu geschrieben und dabei von allen Metadaten befreit (docs/deployment.md §2).
"""

import io
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

from .config import Settings
from .errors import ImageError

# Magic Bytes der akzeptierten Formate. Dem Content-Type-Header wird nicht
# geglaubt - der kommt vom Aufrufer und kostet ihn nichts.
_MAGIC_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
)
_WEBP_RIFF_PREFIX = b"RIFF"
_WEBP_FORM_TYPE = b"WEBP"
_WEBP_FORM_TYPE_OFFSET = 8

SUPPORTED_MIME_TYPES: frozenset[str] = frozenset({"image/png", "image/jpeg", "image/webp"})

# Ausgabeformat. Ein einziges Format im Store hält das Ausliefern trivial.
OUTPUT_MIME_TYPE = "image/webp"
OUTPUT_EXTENSION = "webp"


@dataclass(frozen=True, slots=True)
class EncodedImage:
    """Ein fertig re-encodiertes Bild, bereit zum Ablegen.

    Attributes:
        data: Die WebP-Bytes.
        width: Breite in Pixeln nach dem Deckeln.
        height: Höhe in Pixeln nach dem Deckeln.
        mime_type: Immer `image/webp`.
    """

    data: bytes
    width: int
    height: int
    mime_type: str = OUTPUT_MIME_TYPE


def sniff_mime_type(data: bytes) -> str:
    """Bestimmt den Medientyp anhand der Magic Bytes.

    Args:
        data: Die rohen Bilddaten.

    Returns:
        Einer der Werte aus `SUPPORTED_MIME_TYPES`.

    Raises:
        ImageError: Kein unterstütztes Format erkannt.
    """
    for signature, mime_type in _MAGIC_SIGNATURES:
        if data.startswith(signature):
            return mime_type

    is_webp = (
        data.startswith(_WEBP_RIFF_PREFIX)
        and data[_WEBP_FORM_TYPE_OFFSET : _WEBP_FORM_TYPE_OFFSET + 4] == _WEBP_FORM_TYPE
    )
    if is_webp:
        return "image/webp"

    raise ImageError(
        "The uploaded bytes are not a supported image. "
        "Accepted formats are PNG, JPEG and WebP."
    )


def encode_for_storage(data: bytes, settings: Settings) -> EncodedImage:
    """Führt die vollständige Upload-Pipeline auf einem Bild aus.

    Ablauf: Magic Bytes prüfen, decodieren, längste Kante auf
    `max_image_edge_px` deckeln, nach WebP schreiben, alle Metadaten verwerfen.

    Args:
        data: Rohe Bytes, wie sie der Agent geliefert hat.
        settings: Laufzeitkonfiguration mit den Grenzwerten.

    Returns:
        Das re-encodierte Bild.

    Raises:
        ImageError: Format nicht unterstützt, Datei defekt oder zu viele Pixel.
    """
    sniff_mime_type(data)

    # Pillows eigener Bomb-Schutz. Ohne das Limit kann ein 30-KB-PNG beim
    # Decodieren Gigabytes belegen.
    previous_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = settings.max_image_pixels
    try:
        with Image.open(io.BytesIO(data)) as source:
            source.load()
            normalized = _flatten_to_rgb(source)
            normalized.thumbnail(
                (settings.max_image_edge_px, settings.max_image_edge_px),
                Image.Resampling.LANCZOS,
            )
            return _to_webp(normalized, settings.webp_quality)
    except UnidentifiedImageError as exc:
        raise ImageError("The uploaded bytes could not be decoded as an image.") from exc
    except Image.DecompressionBombError as exc:
        raise ImageError(
            f"The image exceeds the pixel limit of {settings.max_image_pixels} pixels."
        ) from exc
    except OSError as exc:
        raise ImageError(f"The image data is truncated or corrupt: {exc}") from exc
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit


def encode_raster(pixels: bytes, width: int, height: int, quality: int) -> EncodedImage:
    """Verpackt fertig gerasterte RGB-Bytes als WebP.

    Der Gegenstück-Eingang zu `encode_for_storage`, für Bilder, die der Server
    selbst erzeugt hat (siehe `rendering/`). Was dort fehlt, fehlt mit Absicht:
    keine Magic-Byte-Prüfung, weil kein fremdes Format im Spiel ist, und kein
    Bomb-Schutz, weil die Pixelzahl vorher feststand.

    Args:
        pixels: Genau `width * height * 3` Bytes in RGB-Reihenfolge.
        width: Breite in Pixeln.
        height: Höhe in Pixeln.
        quality: WebP-Qualität 1-100.

    Returns:
        Das encodierte Bild, bereit für die Ablage.

    Raises:
        ImageError: Die Bytemenge passt nicht zu den angegebenen Maßen.
    """
    expected = width * height * 3
    if len(pixels) != expected:
        raise ImageError(
            f"Renderer returned {len(pixels)} bytes, expected {expected} for {width}x{height}."
        )
    return _to_webp(Image.frombytes("RGB", (width, height), pixels), quality)


def downscale(data: bytes, max_dimension: int, quality: int) -> bytes:
    """Verkleinert ein gespeichertes Bild für die Rückgabe an einen Agent.

    Wird von `view_post` benutzt: ein 1024er-Bild kostet ein Vielfaches eines
    512ers an Context, und zum Ansehen und Liken reicht klein.

    Args:
        data: Bytes des gespeicherten WebP.
        max_dimension: Längste Kante des Ergebnisses in Pixeln.
        quality: WebP-Qualität 1-100.

    Returns:
        WebP-Bytes in der gewünschten Größe. Kleinere Bilder werden nicht
        vergrößert.

    Raises:
        ImageError: Der gespeicherte Blob ließ sich nicht decodieren.
    """
    try:
        with Image.open(io.BytesIO(data)) as source:
            source.load()
            resized = _flatten_to_rgb(source)
            resized.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
            return _to_webp(resized, quality).data
    except OSError as exc:
        raise ImageError(f"Stored image could not be re-encoded: {exc}") from exc


def _flatten_to_rgb(source: Image.Image) -> Image.Image:
    """Legt transparente Bilder auf weißen Grund und vereinheitlicht auf RGB.

    WebP kann Alpha, aber ein einheitlicher Modus hält alles danach einfach -
    und ein Feed-Thumbnail mit Transparenz sieht je nach Hintergrund kaputt aus.
    """
    if source.mode in ("RGBA", "LA", "P"):
        with_alpha = source.convert("RGBA")
        backdrop = Image.new("RGB", with_alpha.size, (255, 255, 255))
        backdrop.paste(with_alpha, mask=with_alpha.split()[-1])
        return backdrop
    if source.mode != "RGB":
        return source.convert("RGB")
    # Kopie, damit der Aufrufer gefahrlos in-place skalieren kann.
    return source.copy()


def _to_webp(image: Image.Image, quality: int) -> EncodedImage:
    """Schreibt ein Bild als WebP ohne jegliche Metadaten."""
    buffer = io.BytesIO()
    # Pillow übernimmt `info` (EXIF, ICC, XMP) nicht in eine neu erzeugte
    # RGB-Kopie, und `save` bekommt hier bewusst keine exif=/icc_profile=Argumente.
    image.save(buffer, format="WEBP", quality=quality, method=6)
    return EncodedImage(data=buffer.getvalue(), width=image.width, height=image.height)
