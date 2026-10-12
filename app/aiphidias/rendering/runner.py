"""Die Seite des Renderns, die im Serverprozess läuft.

Hier passiert alles außer dem Rendern selbst: Quelltext prüfen, einen
Renderplatz belegen, den Kindprozess starten, das Zeitlimit durchsetzen und
das Ergebnis nach WebP schreiben.

Zwei Grenzen sind tragend und keine Vorsichtsmaßnahme:

**Das Zeitlimit.** Gemessen skaliert ein Shader linear mit den
Schleifendurchläufen pro Pixel: bei 1024² kosten 64 Iterationen 0,36 s, 16.384
kosten 9,0 s, eine unbegrenzte Schleife läuft für immer. Durchsetzbar ist das
nur von außen, deshalb der eigene Prozess.

**Die Serialisierung.** llvmpipe rechnet auf allen Kernen. Zwei gleichzeitige
Läufe auf einem kleinen Server machen beide langsam, statt einen schnell -
und der Server beantwortet nebenher noch Anfragen. Ein Platz, eine Warteschlange
mit Zeitlimit, und wer nicht drankommt, bekommt eine Meldung, die zum
Wiederholen einlädt statt zu einem Fehlversuch.
"""

import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ..config import Settings
from ..errors import ImageError, RenderCapacityError, RenderError, RenderTimeout
from ..imaging import EncodedImage, encode_raster

logger = logging.getLogger(__name__)

# Modulpfad des Kindprozesses. Als `-m` gestartet, damit die relativen Importe
# im Paket funktionieren.
_WORKER_MODULE = "aiphidias.rendering.worker"

# Wurzel, unter der `aiphidias` liegt - explizit gesetzt, damit der Kindprozess
# nicht davon abhängt, aus welchem Verzeichnis der Server gestartet wurde.
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent

# Die vollständige Umgebung des Kindprozesses - sie **ersetzt** die des Servers,
# sie ergänzt sie nicht. Das ist Absicht: `DATABASE_URL`, `HMAC_SECRET` und die
# übrigen Secrets haben in einem Prozess, der fremden Quelltext ausführt, nichts
# zu suchen. Wer hier etwas braucht, trägt es ein.
#
# `LIBGL_ALWAYS_SOFTWARE` und `EGL_PLATFORM=surfaceless`: ohne beides sucht Mesa
# eine Grafikkarte und einen X-Server, findet keins von beidem und bricht ab.
_RENDER_ENVIRONMENT = {
    "LIBGL_ALWAYS_SOFTWARE": "1",
    "EGL_PLATFORM": "surfaceless",
    # Ohne das legt Mesa seinen Shader-Cache unter $HOME an - das hier gesetzt
    # zu lassen ist billiger, als $HOME durchzureichen.
    "MESA_SHADER_CACHE_DIR": "/tmp/mesa-cache",
    "PYTHONPATH": str(_PACKAGE_ROOT),
    "PYTHONUNBUFFERED": "1",
    "PATH": "/usr/local/bin:/usr/bin:/bin",
}


class RenderKind(StrEnum):
    """Die Quelltextarten, aus denen ein Bild entstehen kann."""

    SVG = "svg"
    GLSL = "glsl"


@dataclass(frozen=True, slots=True)
class RenderResult:
    """Ein fertig gerendertes und encodiertes Bild.

    Attributes:
        image: Das WebP samt Maßen, bereit für die Ablage.
        render_ms: Reine Renderzeit im Kindprozess, ohne Prozessstart und ohne
            WebP-Encoding. Geht als Kennzahl an den Agent zurück, damit er
            merkt, wenn sein Shader teuer wird.
    """

    image: EncodedImage
    render_ms: float


class Renderer:
    """Verwandelt Quelltext in ein gespeichertes Bildformat."""

    def __init__(self, settings: Settings) -> None:
        """Args:
        settings: Grenzwerte für Quelltextlänge, Zeitlimit und Parallelität.
        """
        self._settings = settings
        self._slots = asyncio.Semaphore(settings.render_concurrency)

    async def render(
        self,
        kind: RenderKind,
        source: str,
        *,
        edge_px: int,
        time_seconds: float = 0.0,
    ) -> RenderResult:
        """Rendert Quelltext zu einem WebP.

        Args:
            kind: SVG oder GLSL.
            source: Der Quelltext des Agents.
            edge_px: Längste Kante des Ergebnisses. Ein Shader wird quadratisch
                gerendert; ein SVG behält die Proportion seiner `viewBox` und
                passt sich nur in diesen Rahmen ein.
            time_seconds: Wert für `iTime`; bei SVG ohne Bedeutung.

        Returns:
            Das encodierte Bild samt gemessener Renderzeit.

        Raises:
            RenderError: Quelltext leer, zu lang oder vom Renderer abgelehnt.
            RenderTimeout: Zeitlimit überschritten, Prozess abgeschossen.
            RenderCapacityError: Kein Renderplatz frei geworden.
            ImageError: Der Renderer lieferte eine unpassende Bytemenge.
        """
        self._check_source(kind, source)
        request = {
            "kind": str(kind),
            "source": source,
            "width": edge_px,
            "height": edge_px,
            "time": time_seconds,
            "font_dir": str(self._settings.render_font_dir),
            "memory_limit_bytes": self._settings.render_memory_limit_bytes,
        }

        async with self._reserved_slot():
            header, payload = await self._run_worker(request)

        image = encode_raster(
            payload, header["width"], header["height"], self._settings.webp_quality
        )
        logger.info(
            "%s gerendert: %dx%d in %.1f ms, %d Bytes WebP",
            kind,
            header["width"],
            header["height"],
            header["render_ms"],
            len(image.data),
        )
        return RenderResult(image=image, render_ms=header["render_ms"])

    def _check_source(self, kind: RenderKind, source: str) -> None:
        """Weist unbrauchbaren Quelltext ab, bevor ein Prozess dafür startet.

        Raises:
            RenderError: Quelltext leer oder über der Längengrenze.
        """
        if not source or not source.strip():
            raise RenderError(f"The `{kind}` source is empty.")

        limit = (
            self._settings.max_svg_source_chars
            if kind is RenderKind.SVG
            else self._settings.max_glsl_source_chars
        )
        if len(source) > limit:
            raise RenderError(
                f"The `{kind}` source is {len(source)} characters, the limit is {limit}. "
                "Shorten it - a complete piece usually needs far less than that."
            )

    def _reserved_slot(self):
        """Belegt einen Renderplatz, mit Wartezeitlimit.

        Returns:
            Ein async-Kontextmanager, der den Platz wieder freigibt.
        """
        return _Slot(self._slots, self._settings.render_queue_wait_seconds)

    async def _run_worker(self, request: dict) -> tuple[dict, bytes]:
        """Startet den Kindprozess und wertet seine Antwort aus.

        Returns:
            Paar aus Antwortkopf und rohen RGB-Bytes.

        Raises:
            RenderError: Der Renderer hat den Quelltext abgelehnt.
            RenderTimeout: Zeitlimit überschritten.
            ImageError: Antwort des Kindprozesses unlesbar oder unvollständig.
        """
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            _WORKER_MODULE,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_RENDER_ENVIRONMENT,
            cwd=str(_PACKAGE_ROOT),
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(json.dumps(request).encode()),
                timeout=self._settings.render_timeout_seconds,
            )
        except TimeoutError:
            # `kill`, nicht `terminate`: der Prozess steckt im Zweifel in einem
            # Draw-Call und kommt gar nicht dazu, ein SIGTERM zu bearbeiten.
            process.kill()
            await process.wait()
            seconds = self._settings.render_timeout_seconds
            logger.warning("Renderlauf nach %.0f s abgebrochen (%s)", seconds, request["kind"])
            raise RenderTimeout(
                f"Rendering took longer than {seconds:.0f} seconds and was cancelled. "
                "Reduce the work per pixel - fewer loop iterations, fewer noise octaves, "
                "or a smaller `max_dimension`."
            ) from None

        return self._parse_response(stdout, stderr, process.returncode)

    def _parse_response(self, stdout: bytes, stderr: bytes, returncode: int | None) -> tuple[dict, bytes]:
        """Zerlegt die Antwort in Kopf und Nutzlast.

        Raises:
            RenderError: Fachliche Ablehnung durch den Renderer.
            ImageError: Protokollbruch - das ist ein Serverfehler, kein Eingabefehler.
        """
        header_bytes, separator, payload = stdout.partition(b"\n")
        if not separator and not header_bytes:
            detail = stderr.decode("utf-8", "replace").strip()[-500:]
            logger.error("Renderprozess ohne Antwort (rc=%s): %s", returncode, detail)
            raise ImageError("The renderer produced no output. This is a server-side problem.")

        try:
            header = json.loads(header_bytes)
        except json.JSONDecodeError as exc:
            logger.error("Renderprozess mit unlesbarem Kopf (rc=%s): %r", returncode, header_bytes[:200])
            raise ImageError("The renderer returned an unreadable response.") from exc

        if not header.get("ok"):
            message = header.get("error", "The renderer rejected the source.")
            if header.get("fatal"):
                # Kein Fehler des Agents: die Renderumgebung fehlt oder ist kaputt.
                logger.error("Renderumgebung nicht verfügbar: %s", message)
                raise ImageError("The renderer is unavailable. This is a server-side problem.")
            raise RenderError(message)

        expected = header["byte_size"]
        if len(payload) != expected:
            logger.error("Renderprozess lieferte %d statt %d Bytes", len(payload), expected)
            raise ImageError("The renderer returned a truncated image.")

        return header, payload


class _Slot:
    """Belegt einen Renderplatz oder gibt nach der Wartezeit auf.

    Ein eigener Kontextmanager statt `async with semaphore`, weil das Warten
    ein Zeitlimit braucht: ohne das sammelt sich bei einem Ansturm eine
    Schlange, deren hintere Hälfte auf Aufrufe antwortet, die der Client längst
    aufgegeben hat.
    """

    def __init__(self, semaphore: asyncio.Semaphore, wait_seconds: float) -> None:
        self._semaphore = semaphore
        self._wait_seconds = wait_seconds

    async def __aenter__(self) -> None:
        """Raises:
        RenderCapacityError: Innerhalb der Wartezeit wurde kein Platz frei.
        """
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=self._wait_seconds)
        except TimeoutError:
            logger.warning("Renderanfrage abgewiesen: kein Platz nach %.0f s", self._wait_seconds)
            raise RenderCapacityError(
                "The renderer is busy right now. Wait a few seconds and call again - "
                "your source is fine, the server just had no free slot."
            ) from None

    async def __aexit__(self, *_exc_info: object) -> None:
        self._semaphore.release()
