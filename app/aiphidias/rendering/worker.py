"""Der Kindprozess, in dem tatsächlich gerendert wird.

Aufgerufen als `python -m aiphidias.rendering.worker`. Liest eine
JSON-Anforderung von stdin und schreibt eine JSON-Kopfzeile plus rohe
RGB-Bytes nach stdout.

**Warum überhaupt ein eigener Prozess.** Weder resvg noch ein GL-Draw lassen
sich von innen abbrechen: kein Signal-Handler läuft, solange llvmpipe rechnet,
und ein Thread lässt sich in Python ohnehin nicht töten. Ein Zeitlimit ist nur
durchsetzbar, wenn ein Prozess existiert, den man abschießen kann. Alles
Weitere - kein geteilter Speicher, kein kaputter GL-Zustand im Server, ein
Absturz kostet nur diesen Lauf - kommt gratis dazu.

Das Protokoll ist bewusst binär statt base64: bei 1024x1024 sind das drei
Megabyte, und die durch eine Textkodierung zu schicken wäre genau der Fehler,
den dieses ganze Feature vermeidet.
"""

import json
import os
import resource
import sys
import time

# Formatkennung im Kopf. Steigt, wenn sich das Protokoll ändert - Eltern- und
# Kindprozess stammen immer aus demselben Image, ein Versionsvergleich wäre
# also Zierde. Der Wert dient der Diagnose in Logs.
PROTOCOL_VERSION = 1

SUPPORTED_KINDS = ("svg", "glsl")


def _protected_stdout() -> int:
    """Legt stdout beiseite und leitet alles Weitere auf stderr um.

    Mesa und andere Bibliotheken schreiben gelegentlich unaufgefordert nach
    stdout. Eine einzige solche Zeile würde die Kopfzeile des Protokolls
    unlesbar machen, und zwar nur gelegentlich und nur auf manchen Systemen -
    also genau die Sorte Fehler, die man erst in Produktion findet.

    Returns:
        Ein Duplikat des ursprünglichen stdout-Deskriptors.
    """
    preserved = os.dup(1)
    os.dup2(2, 1)
    return preserved


def _apply_memory_limit(limit_bytes: int) -> None:
    """Deckelt den Adressraum des Prozesses, sofern ein Limit gesetzt ist.

    Fängt ab, was das Zeitlimit nicht abfängt: ein SVG, das durch tief
    verschachtelte Gruppen viel Speicher belegt, bevor es überhaupt lange
    rechnet. `0` schaltet die Deckelung ab.

    **Gilt nur für den SVG-Pfad, und das ist keine Nachlässigkeit.** llvmpipe
    reserviert beim JIT-Übersetzen des Shaders große Bereiche des *virtuellen*
    Adressraums - genau dessen, was `RLIMIT_AS` zählt. Scheitert eine solche
    Reservierung, fängt LLVM das nicht ab, sondern stirbt mit SIGSEGV. Gemessen
    im amd64-Produktionsimage: mit 2-GB-Limit `rc=-11` und keine Ausgabe, ohne
    Limit derselbe Shader in 1,2 s. Auf arm64 fiel es nicht auf - der Grund,
    das Image vor dem Deployen für die Zielarchitektur zu bauen.

    Im GLSL-Pfad hat die Deckelung ohnehin nichts zu bewachen: der Framebuffer
    hat eine von uns vorgegebene Größe, GLSL kennt keine dynamische
    Speicheranforderung, und der Quelltext ist auf 16 KB begrenzt. Was bleibt,
    deckt das Zeitlimit ab.
    """
    if limit_bytes > 0:
        resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))


def _render(request: dict) -> tuple[bytes, int, int]:
    """Führt den angeforderten Renderlauf aus.

    Args:
        request: Die dekodierte Anforderung des Elternprozesses.

    Returns:
        Tripel aus RGB-Bytes, Breite und Höhe.

    Raises:
        ValueError: Unbekannte Art oder unbrauchbarer Quelltext.
    """
    kind = request["kind"]
    width = int(request["width"])
    height = int(request["height"])

    if kind == "svg":
        from .svg import render_svg

        _apply_memory_limit(int(request.get("memory_limit_bytes", 0)))

        # Die tatsächlichen Maße kommen vom Renderer zurück: ein SVG behält
        # seine Proportion, `width`/`height` sind nur der Rahmen dafür.
        return render_svg(request["source"], width, height, request.get("font_dir"))

    if kind == "glsl":
        from .glsl import render_glsl

        pixels = render_glsl(request["source"], width, height, float(request.get("time", 0.0)))
        return pixels, width, height

    raise ValueError(f"Unknown render kind {kind!r}, expected one of {SUPPORTED_KINDS}.")


def main() -> int:
    """Liest die Anforderung, rendert, schreibt das Ergebnis.

    Returns:
        Exit-Code. `0` auch bei fachlichen Fehlern - die stehen im Kopf und
        sind für den Agent bestimmt, nicht für den Prozessstatus. Nur ein
        kaputtes Protokoll führt zu einem Code ungleich null.
    """
    channel = _protected_stdout()
    try:
        request = json.loads(sys.stdin.buffer.read() or b"{}")
    except (json.JSONDecodeError, ValueError) as exc:
        os.write(channel, json.dumps({"ok": False, "error": f"bad request: {exc}"}).encode() + b"\n")
        return 2

    started = time.perf_counter()
    try:
        pixels, width, height = _render(request)
    except (ValueError, RuntimeError, MemoryError) as exc:
        # `MemoryError` heißt: das Adressraumlimit hat gegriffen. Für den Agent
        # ist das eine Eigenschaft seines Quelltextes, keine Serverstörung.
        message = "The renderer ran out of memory." if isinstance(exc, MemoryError) else str(exc)
        payload = {"ok": False, "error": message, "fatal": isinstance(exc, RuntimeError)}
        os.write(channel, json.dumps(payload).encode() + b"\n")
        return 0

    header = {
        "ok": True,
        "protocol": PROTOCOL_VERSION,
        "width": width,
        "height": height,
        "byte_size": len(pixels),
        "render_ms": round((time.perf_counter() - started) * 1000, 1),
    }
    os.write(channel, json.dumps(header).encode() + b"\n")
    with os.fdopen(channel, "wb") as sink:
        sink.write(pixels)
    return 0


if __name__ == "__main__":
    sys.exit(main())
