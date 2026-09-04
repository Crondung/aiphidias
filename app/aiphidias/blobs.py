"""Content-addressed Blob-Store auf dem Dateisystem.

Layout nach docs/deployment.md §2:

    <blob_root>/ab/cd/abcd1234….webp          Vollbild
    <blob_root>/ab/cd/abcd1234….t512.webp     Thumbnail

Zwei Ebenen Fan-out, damit kein Verzeichnis Millionen Einträge bekommt. Weil
der Pfad aus dem Hash des Inhalts folgt, ist Deduplizierung gratis und der
Inhalt unter einer URL unveränderlich - das erlaubt `immutable` beim Caching.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .errors import NotFoundError
from .imaging import OUTPUT_EXTENSION

# Zwei Ebenen a zwei Hex-Zeichen: 256 Verzeichnisse je Ebene.
_FANOUT_DEPTH = 2
_FANOUT_WIDTH = 2

_THUMBNAIL_SUFFIX = "t512"


def filename_for(sha256: str, *, thumbnail: bool = False) -> str:
    """Baut den Dateinamen eines Blobs.

    Args:
        sha256: Hex-Hash des re-encodierten Bildes.
        thumbnail: True für die verkleinerte Variante.

    Returns:
        Dateiname der Form `<hash>.webp` bzw. `<hash>.t512.webp`.
    """
    suffix = f".{_THUMBNAIL_SUFFIX}" if thumbnail else ""
    return f"{sha256}{suffix}.{OUTPUT_EXTENSION}"


def parse_filename(filename: str) -> tuple[str, bool] | None:
    """Zerlegt einen Blob-Dateinamen wieder in Hash und Variante.

    Args:
        filename: Der Dateiname aus der URL.

    Returns:
        Paar aus Hash und Thumbnail-Flag, oder None, wenn der Name nicht dem
        erwarteten Muster entspricht. Ein None-Ergebnis ist die einzige Stelle,
        an der Pfad-Traversal abgefangen werden muss - danach steht fest, dass
        nur Hex-Zeichen in den Pfad gehen.
    """
    stem, separator, extension = filename.rpartition(".")
    if not separator or extension != OUTPUT_EXTENSION:
        return None

    thumbnail = stem.endswith(f".{_THUMBNAIL_SUFFIX}")
    digest = stem[: -len(_THUMBNAIL_SUFFIX) - 1] if thumbnail else stem

    is_hex_digest = len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
    return (digest, thumbnail) if is_hex_digest else None


def sha256_hex(data: bytes) -> str:
    """Berechnet den SHA-256 der Bytes als Kleinbuchstaben-Hex."""
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True, slots=True)
class BlobStore:
    """Ablage der Bilddateien unterhalb eines Wurzelverzeichnisses.

    Attributes:
        root: Wurzelverzeichnis, in dem die Fan-out-Struktur liegt.
    """

    root: Path

    def path_for(self, sha256: str, *, thumbnail: bool = False) -> Path:
        """Liefert den Ablagepfad zu einem Hash.

        Args:
            sha256: Hex-Hash des re-encodierten Bildes.
            thumbnail: True für die verkleinerte Variante.

        Returns:
            Absoluter Pfad. Die Datei muss nicht existieren.
        """
        segments = [
            sha256[i * _FANOUT_WIDTH : (i + 1) * _FANOUT_WIDTH] for i in range(_FANOUT_DEPTH)
        ]
        return self.root.joinpath(*segments, filename_for(sha256, thumbnail=thumbnail))

    def exists(self, sha256: str) -> bool:
        """Sagt, ob zu diesem Hash schon ein Vollbild liegt."""
        return self.path_for(sha256).is_file()

    def write(self, sha256: str, data: bytes, *, thumbnail: bool = False) -> Path:
        """Schreibt einen Blob, sofern er noch nicht existiert.

        Der Schreibvorgang läuft über eine temporäre Datei mit
        anschließendem `replace`, damit unter dem endgültigen Pfad nie ein
        halb geschriebenes Bild sichtbar wird.

        Args:
            sha256: Hash des Inhalts - bestimmt den Pfad.
            data: Die zu schreibenden Bytes.
            thumbnail: True für die verkleinerte Variante.

        Returns:
            Der Pfad, unter dem der Blob nun liegt.

        Raises:
            OSError: Schreiben fehlgeschlagen (Platte voll, Rechte).
        """
        target = self.path_for(sha256, thumbnail=thumbnail)
        if target.is_file():
            return target

        target.parent.mkdir(parents=True, exist_ok=True)
        # Eindeutiger Temp-Name, damit zwei gleichzeitige Uploads desselben
        # Bildes sich nicht gegenseitig die halbe Datei überschreiben.
        staging = target.with_suffix(f".{id(data):x}.part")
        staging.write_bytes(data)
        staging.replace(target)
        return target

    def read(self, sha256: str, *, thumbnail: bool = False) -> bytes:
        """Liest einen Blob.

        Args:
            sha256: Hash des gesuchten Bildes.
            thumbnail: True für die verkleinerte Variante.

        Returns:
            Die Bytes der Datei.

        Raises:
            NotFoundError: Zu diesem Hash liegt keine Datei.
        """
        path = self.path_for(sha256, thumbnail=thumbnail)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise NotFoundError(f"No stored image for hash {sha256}.") from exc
