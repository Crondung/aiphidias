"""Fragment-Shader rendern - headless, auf der CPU, nach Shadertoy-Konvention.

Der Agent liefert **nur** `mainImage`. Version, Uniforms und `main` kommen von
uns. Das hat zwei Gründe: es hält die Pipeline in unserer Hand, und es ist die
Form, die Modelle am besten beherrschen - Shadertoy-Shader sind in großer Zahl
öffentlich, ein Shader mit selbstgewähltem Rahmen ist es nicht.

Gerendert wird mit Mesas llvmpipe über EGL, also ohne Grafikkarte und ohne
X-Server. Auf dem Server gemessen, ein 1024er-Frame: 465 ms bei kaltem
Shader-Cache, 170 ms bei warmem. Der Unterschied ist der LLVM-Durchlauf, der
den Shader in Maschinencode übersetzt - das Zeichnen selbst ist der kleinere
Teil. Praktisch heißt das: wer erst `preview` aufruft und dann `publish`,
zahlt die Übersetzung nur einmal.

Das Zeitlimit im Aufrufer ist nicht optional. Die Kosten wachsen linear mit den
Schleifendurchläufen pro Pixel - auf dem Server bei 1024² kosten 64 Iterationen
0,36 s und 16.384 Iterationen 9,0 s, eine unbegrenzte Schleife läuft für immer.
Ein laufender Draw-Call lässt sich weder per Signal noch per Thread abbrechen;
nur der Prozess kann sterben.

Dieses Modul wird ausschließlich vom Kindprozess importiert (`worker.py`).
"""

import re

# Vollflächiges Dreieck statt Quad: eine Primitive weniger, kein Naht in der
# Diagonale, und die Interpolation deckt den ganzen Viewport ab.
VERTEX_SHADER = """\
#version 330 core
in vec2 in_position;
void main() { gl_Position = vec4(in_position, 0.0, 1.0); }
"""

# Was der Shader vorfindet. Absichtlich klein gehalten - jedes Uniform hier ist
# eines, das wir für immer unterstützen müssen.
_PROLOGUE_LINES = (
    "#version 330 core",
    "out vec4 aiphidias_fragment;",
    "uniform vec3 iResolution;",
    "uniform float iTime;",
)
_EPILOGUE = """
void main() {
    vec4 color;
    mainImage(color, gl_FragCoord.xy);
    aiphidias_fragment = vec4(color.rgb, 1.0);
}
"""

# Uniform-Namen, die wir setzen, sofern der Shader sie überhaupt benutzt.
UNIFORM_NAMES = ("iResolution", "iTime")

ENTRY_POINT = "mainImage"

# Direktiven, die den Rahmen aushebeln würden. `#version` ein zweites Mal ist
# ohnehin ein Compilerfehler, `#extension` schaltet Sprachumfang zu, auf den
# wir uns nicht festlegen wollen.
_FORBIDDEN_DIRECTIVES = ("#version", "#extension")

# Mesa meldet Fehler als `0:<zeile>(<spalte>)`. Die Zeile zählt die
# zusammengesetzte Quelle, der Agent kennt aber nur seine eigene.
_ERROR_POSITION = re.compile(r"\b0:(\d+)\((\d+)\)")


def validate_source(source: str) -> None:
    """Prüft den Shader, bevor ein Prozess dafür gestartet wird.

    Die Meldungen beschreiben nicht nur den Fehler, sondern die richtige Form -
    ein Agent, der `void main()` geschrieben hat, soll aus der Antwort direkt
    ableiten können, wie es stattdessen aussieht.

    Args:
        source: Der Shader-Quelltext des Agents.

    Raises:
        ValueError: Der Shader kann in diesem Rahmen nicht laufen.
    """
    for directive in _FORBIDDEN_DIRECTIVES:
        if re.search(rf"^\s*{re.escape(directive)}\b", source, re.MULTILINE):
            raise ValueError(
                f"Remove the `{directive}` directive. The server supplies the shader "
                "preamble (GLSL 3.30 core, plus the `iResolution` and `iTime` uniforms); "
                "your code only provides the `mainImage` function."
            )

    if re.search(r"^\s*(void|int)\s+main\s*\(", source, re.MULTILINE):
        raise ValueError(
            "Do not define `main`. The server does that and calls your `mainImage` "
            "from it. Rename your entry point to "
            "`void mainImage(out vec4 fragColor, in vec2 fragCoord)`."
        )

    if not re.search(rf"\b{ENTRY_POINT}\s*\(", source):
        raise ValueError(
            "The shader has no `mainImage` function. Write it in the Shadertoy form: "
            "`void mainImage(out vec4 fragColor, in vec2 fragCoord) { ... }`. "
            "Available uniforms are `vec3 iResolution` and `float iTime`."
        )


def assemble(source: str) -> str:
    """Setzt den vollständigen Fragment-Shader aus Rahmen und Agent-Code zusammen."""
    return "\n".join(_PROLOGUE_LINES) + "\n" + source + _EPILOGUE


def translate_compile_error(message: str) -> str:
    """Rechnet Zeilennummern des Compilers auf den Quelltext des Agents um.

    Ohne das nennt jede Meldung eine Zeile, die im Quelltext des Agents etwas
    ganz anderes ist - falsche Zeilennummern sind schlimmer als gar keine,
    weil der Agent daraufhin die falsche Stelle ändert.

    Args:
        message: Der Originaltext des GLSL-Compilers.

    Returns:
        Dieselbe Meldung mit Positionen relativ zum Agent-Quelltext. Fehler im
        Rahmen (negative Zeilen) bleiben unverändert und behalten ihren
        Originaltext, damit sie nicht als Fehler des Agents erscheinen.
    """
    offset = len(_PROLOGUE_LINES)

    def shift(match: re.Match[str]) -> str:
        line = int(match.group(1)) - offset
        return match.group(0) if line < 1 else f"line {line}, column {match.group(2)}"

    return _ERROR_POSITION.sub(shift, message)


def render_glsl(source: str, width: int, height: int, time_seconds: float) -> bytes:
    """Rendert einen Fragment-Shader zu rohen RGB-Bytes.

    Args:
        source: Der `mainImage`-Quelltext des Agents.
        width: Zielbreite in Pixeln.
        height: Zielhöhe in Pixeln.
        time_seconds: Wert für das `iTime`-Uniform. Ein Shader ist als
            zeitabhängige Funktion geschrieben; welchen Moment wir festhalten,
            gehört dem Agent.

    Returns:
        `width * height * 3` Bytes in RGB-Reihenfolge, oben-links beginnend.

    Raises:
        ValueError: Shader lehnt der Compiler ab oder der Kontext kommt nicht
            zustande. Der Text geht wörtlich an den Agent.
    """
    # Erst hier importieren: der Elternprozess soll weder moderngl noch numpy
    # laden, er rendert nie selbst.
    import moderngl
    import numpy as np

    validate_source(source)

    try:
        context = moderngl.create_context(standalone=True, backend="egl")
    except Exception as exc:  # pragma: no cover - fehlende GL-Umgebung ist ein Betriebsfehler
        raise RuntimeError(f"No headless GL context available: {exc}") from exc

    try:
        try:
            program = context.program(
                vertex_shader=VERTEX_SHADER, fragment_shader=assemble(source)
            )
        except Exception as exc:
            raise ValueError(translate_compile_error(str(exc))) from exc

        # Unbenutzte Uniforms entfernt der Compiler restlos - sie dann zu setzen
        # wirft einen KeyError. Ein Shader ohne `iTime` ist völlig legitim.
        for name, value in (
            ("iResolution", (float(width), float(height), 1.0)),
            ("iTime", float(time_seconds)),
        ):
            if name in program:
                program[name].value = value

        corners = np.array([-1.0, -1.0, 3.0, -1.0, -1.0, 3.0], dtype="f4")
        vertex_buffer = context.buffer(corners.tobytes())
        vertex_array = context.vertex_array(program, [(vertex_buffer, "2f", "in_position")])

        framebuffer = context.simple_framebuffer((width, height))
        framebuffer.use()
        vertex_array.render(moderngl.TRIANGLES)
        pixels = framebuffer.read(components=3)
    finally:
        context.release()

    # OpenGL legt den Ursprung unten links, Bildformate oben links. Zeilenweise
    # umdrehen ist billiger als ein zweiter Renderpass mit gespiegelter Matrix.
    stride = width * 3
    return b"".join(pixels[start : start + stride] for start in range((height - 1) * stride, -1, -stride))
