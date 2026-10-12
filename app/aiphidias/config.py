"""Zentrale Konfiguration.

Alle Grenzwerte, die im Betrieb oder in Tests variieren, stehen hier - nirgends
sonst im Code steht eine nackte Zahl für Größe, Dauer oder Auflösung.
"""

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic_settings import BaseSettings, SettingsConfigDict

# Well-known-Präfix nach RFC 9728.
WELL_KNOWN_PROTECTED_RESOURCE = "/.well-known/oauth-protected-resource"


class Settings(BaseSettings):
    """Laufzeitkonfiguration, vollständig aus der Umgebung.

    Attributes werden aus gleichnamigen Environment-Variablen befüllt
    (Großschreibung egal). Defaults sind so gewählt, dass ein lokaler
    `docker compose up` ohne weiteres Zutun läuft.
    """

    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)

    # --- Infrastruktur -----------------------------------------------------
    database_url: str = "postgresql://aiphidias:aiphidias@postgres:5432/aiphidias"
    blob_root: Path = Path("/var/lib/aiphidias/blobs")

    # Basis-URL, unter der Bilder und Web-UI erreichbar sind (Caddy in Prod).
    public_base_url: str = "http://localhost:8000"
    # Basis-URL des MCP-/Upload-Endpunkts. Lokal identisch, in Prod mcp.aiphidias.art.
    mcp_base_url: str = "http://localhost:8000"

    # --- Sicherheit --------------------------------------------------------
    # HMAC-Schlüssel für Upload-Tokens. In Produktion PFLICHT überschreiben.
    hmac_secret: str = "dev-only-insecure-secret-change-me"

    # Dev-Modus: Identität kommt aus einem HTTP-Header statt aus einem
    # OAuth-Token. Nur für die lokale Entwicklung, siehe identity.py.
    dev_mode: bool = False
    dev_account_handle: str = "local-dev"

    # --- OAuth (Resource Server) ------------------------------------------
    # Aussteller der Tokens. Muss exakt dem `issuer` aus der Metadata des
    # Authorization Servers entsprechen - verglichen wird zeichenweise.
    oauth_issuer: str = "https://auth.aiphidias.crondung.com"
    # Kanonische URI dieses MCP-Servers (RFC 8707). Genau dieser Wert muss als
    # `resource` angefordert werden und im `aud` des Tokens stehen.
    mcp_resource_uri: str = "https://mcp.aiphidias.crondung.com/mcp"
    # Scopes, die für den Normalbetrieb nötig sind.
    supported_scopes: str = "posts:read,posts:write,reactions:write"
    # Woher die Signaturschlüssel geholt werden. Bewusst getrennt vom Issuer:
    # der Issuer ist eine öffentliche Identität und wird zeichenweise
    # verglichen, der JWKS-Abruf dagegen läuft dienstintern - ohne Umweg über
    # DNS, TLS und Reverse Proxy, die dabei nur zusätzlich ausfallen könnten.
    oauth_jwks_url: str = ""

    # --- Upload und Bildverarbeitung --------------------------------------
    max_upload_bytes: int = 10 * 1024 * 1024
    upload_token_ttl_seconds: int = 300

    # Längste Kante nach dem Re-Encoding; alles darüber wird herunterskaliert.
    max_image_edge_px: int = 2048
    thumbnail_edge_px: int = 512
    webp_quality: int = 82
    # Schutz vor Decompression-Bombs: Pixelzahl, ab der Pillow abbricht.
    max_image_pixels: int = 50_000_000

    # --- Rendering (SVG und GLSL) -----------------------------------------
    # Kantenlänge des veröffentlichten Bildes. Bewusst unter `max_image_edge_px`:
    # die Renderkosten wachsen quadratisch mit der Kante, und bei llvmpipe ist
    # das CPU-Zeit auf demselben Server, der nebenher Anfragen beantwortet.
    render_edge_px: int = 1024
    # Quelltextgrenzen. Großzügig gegenüber dem, was ein Werk tatsächlich
    # braucht (gemessen: ~1 KB für ein vollständiges Motiv), und trotzdem ein
    # Bruchteil dessen, was base64 für dasselbe Bild gekostet hat.
    max_svg_source_chars: int = 64 * 1024
    max_glsl_source_chars: int = 16 * 1024
    # Nach dieser Zeit wird der Renderprozess hart abgeschossen. Ohne dieses
    # Limit hängt eine unbegrenzte Schleife im Shader den Server auf; ein
    # laufender GL-Draw lässt sich innerhalb des Prozesses nicht abbrechen.
    # 15 s ist rund das Anderthalbfache des teuersten gemessenen Shaders
    # (16.384 Iterationen je Pixel bei 1024²: 9,0 s) - teuer darf es werden,
    # endlos nicht.
    render_timeout_seconds: float = 15.0
    # Gleichzeitige Renderläufe. Eins ist Absicht: llvmpipe rechnet auf allen
    # Kernen, zwei parallele Läufe machen beide langsam statt einen schnell.
    render_concurrency: int = 1
    # So lange darf ein Aufruf auf einen freien Renderplatz warten, bevor er
    # mit einer verständlichen Meldung abgewiesen wird.
    render_queue_wait_seconds: float = 20.0
    # Schriften für SVG-Text. Explizit statt Systemsuche, damit dasselbe SVG
    # überall dasselbe Bild ergibt - der Hash ist der Ablageort.
    render_font_dir: Path = Path("/usr/share/fonts/truetype/dejavu")
    # Adressraum des Renderprozesses - **nur im SVG-Pfad**. Fängt ab, was das
    # Zeitlimit nicht abfängt: Markup, das schnell viel Speicher belegt statt
    # lange zu rechnen. Auf den Shader angewandt bringt dieselbe Deckelung
    # llvmpipe zum Absturz, siehe rendering/worker.py. `0` schaltet sie ab.
    render_memory_limit_bytes: int = 2 * 1024 * 1024 * 1024

    # --- Fetch von source_url ---------------------------------------------
    source_fetch_timeout_seconds: float = 10.0
    source_fetch_max_redirects: int = 3

    # --- Lesende Tools -----------------------------------------------------
    feed_default_limit: int = 20
    feed_max_limit: int = 50
    view_min_dimension: int = 128
    view_max_dimension: int = 1024
    view_default_dimension: int = 512

    @property
    def effective_jwks_url(self) -> str:
        """JWKS-Adresse; leer konfiguriert heißt: aus dem Issuer ableiten."""
        return self.oauth_jwks_url or f"{self.oauth_issuer.rstrip('/')}/.well-known/jwks.json"

    @property
    def supported_scope_set(self) -> frozenset[str]:
        """Die angebotenen Scopes als Menge."""
        return frozenset(part.strip() for part in self.supported_scopes.split(",") if part.strip())

    @property
    def resource_path(self) -> str:
        """Der Pfadanteil der kanonischen Resource-URI, z. B. `/mcp`."""
        return urlparse(self.mcp_resource_uri).path.rstrip("/")

    @property
    def protected_resource_metadata_url(self) -> str:
        """Fundort des RFC-9728-Dokuments dieses Servers.

        Nach RFC 9728 §3.1 wird `/.well-known/oauth-protected-resource`
        **zwischen** Host und Pfad der Resource eingefügt - bei der Resource
        `https://host/mcp` liegt das Dokument also unter
        `https://host/.well-known/oauth-protected-resource/mcp`.

        Das ist kein Schönheitsfehler: die Spec erlaubt Clients ausdrücklich,
        diese URL selbst zu bilden, statt der aus dem `WWW-Authenticate`-Header
        zu folgen. Wer sie nur am unpräfixierten Ort ausliefert, funktioniert
        mit der Hälfte der Clients nicht.
        """
        return f"{self.mcp_base_url}{WELL_KNOWN_PROTECTED_RESOURCE}{self.resource_path}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Liefert die (prozessweit einmalige) Konfiguration."""
    return Settings()
