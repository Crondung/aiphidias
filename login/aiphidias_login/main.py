"""Login- und Consent-App für Ory Hydra.

Das einzige Stück des Autorisierungs-Aufbaus, das wir selbst schreiben. Es
nimmt Hydras Login- und Consent-Challenges entgegen, schickt den Menschen zu
GitHub, bildet die GitHub-Identität auf einen Account ab und bestätigt die
Challenge bei Hydra.

Dabei erledigt es eine Aufgabe, die eigentlich Hydra gehören müsste: **es
bindet die Token-Audience**. Hydra ignoriert den `resource`-Parameter aus
RFC 8707, deshalb liest diese App ihn aus der ursprünglichen Anfrage und setzt
`grant_access_token_audience` selbst. Ohne diesen Schritt trüge kein Token ein
`aud`, und die Prüfung im Resource Server liefe ins Leere.
"""

import contextlib
import logging
import secrets
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import asyncpg
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .accounts import upsert_account
from .config import Settings, get_settings
from .github import GitHubError, authorize_url, exchange_code
from .hydra import HydraAdmin, HydraError
from .signing import StateError, sign_state, verify_state

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"

# Cookie, dessen Nonce den Callback an denselben Browser bindet.
_NONCE_COOKIE = "aiphidias_login_nonce"

# Was die Scopes für einen Menschen bedeuten. Englisch, weil das Publikum
# international ist - siehe die Sprachregel in CLAUDE.md.
_SCOPE_LABELS = {
    "openid": "Read your AIPHIDIAS identity",
    "offline_access": "Stay connected without asking you to sign in again",
    "posts:read": "Browse the feed and view individual images",
    "posts:write": "Publish images on your behalf",
    "reactions:write": "Like posts",
}


def create_app(settings: Settings | None = None) -> FastAPI:
    """Baut die ASGI-Anwendung der Login-App."""
    effective = settings or get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    @contextlib.asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        application.state.pool = await asyncpg.create_pool(effective.database_url, min_size=1, max_size=5)
        try:
            yield
        finally:
            await application.state.pool.close()

    app = FastAPI(title="AIPHIDIAS Login", version="0.1.0", lifespan=lifespan)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    hydra = HydraAdmin(base_url=effective.hydra_admin_url)

    @app.get("/login")
    async def login(request: Request, login_challenge: str = ""):
        """Nimmt Hydras Login-Challenge entgegen und startet den GitHub-Login.

        Hat Hydra bereits eine Session für diesen Browser (`skip`), wird der
        GitHub-Umweg übersprungen - der Mensch hat sich ja gerade erst
        angemeldet.
        """
        if not login_challenge:
            return _problem(templates, request, "This request is missing a login_challenge.")

        try:
            login_request = await hydra.get_login_request(login_challenge)
            if login_request.get("skip"):
                target = await hydra.accept_login(login_challenge, login_request["subject"])
                return RedirectResponse(target, status_code=302)
        except HydraError as exc:
            return _problem(templates, request, f"The authorization server did not respond as expected: {exc}")

        nonce = secrets.token_urlsafe(16)
        state = sign_state(
            {"challenge": login_challenge, "nonce": nonce},
            effective.github_client_secret,
            effective.login_state_ttl_seconds,
        )
        response = RedirectResponse(
            authorize_url(effective.github_client_id, effective.github_callback_url, state),
            status_code=302,
        )
        # Das Cookie ist das Gegenstück zum Nonce im state: nur wer beide hat,
        # hat den Flow auch gestartet.
        response.set_cookie(
            _NONCE_COOKIE, nonce, max_age=effective.login_state_ttl_seconds,
            httponly=True, secure=request.url.scheme == "https", samesite="lax",
        )
        return response

    @app.get("/auth/github/callback")
    async def github_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        """Verarbeitet GitHubs Antwort und bestätigt den Login bei Hydra."""
        if error:
            return _problem(templates, request, f"GitHub cancelled the sign-in: {error}")
        if not code or not state:
            return _problem(templates, request, "GitHub did not return an authorization code.")

        try:
            payload = verify_state(state, effective.github_client_secret)
        except StateError as exc:
            return _problem(templates, request, f"This sign-in is no longer valid: {exc}")

        if payload.get("nonce") != request.cookies.get(_NONCE_COOKIE):
            return _problem(
                templates, request,
                "This sign-in was started in a different browser. Please start over.",
            )

        try:
            identity = await exchange_code(
                code, effective.github_client_id, effective.github_client_secret,
                effective.github_callback_url,
            )
        except GitHubError as exc:
            return _problem(templates, request, f"GitHub sign-in failed: {exc}")

        account_id = await upsert_account(request.app.state.pool, identity.user_id, identity.login)

        try:
            target = await hydra.accept_login(str(payload["challenge"]), account_id)
        except HydraError as exc:
            return _problem(templates, request, f"The authorization server rejected the sign-in: {exc}")

        response = RedirectResponse(target, status_code=302)
        response.delete_cookie(_NONCE_COOKIE)
        return response

    @app.get("/consent", response_class=HTMLResponse)
    async def consent(request: Request, consent_challenge: str = ""):
        """Zeigt, was der Agent verlangt, und fragt den Menschen."""
        if not consent_challenge:
            return _problem(templates, request, "This request is missing a consent_challenge.")

        try:
            consent_request = await hydra.get_consent_request(consent_challenge)
        except HydraError as exc:
            return _problem(templates, request, f"The authorization server did not respond as expected: {exc}")

        resource = _requested_resource(consent_request)
        if resource is None:
            return _problem(
                templates, request,
                "This request does not name a valid resource. The MCP client must send a "
                "`resource` parameter pointing at this server.",
            )
        if resource not in effective.allowed_resource_set:
            logger.warning("Consent mit fremder Resource abgelehnt: %s", resource)
            target = await hydra.reject_consent(
                consent_challenge, "invalid_target", f"Unknown resource {resource}."
            )
            return RedirectResponse(target, status_code=302)

        # Hat der Mensch dieser Anwendung schon zugestimmt, nicht erneut fragen.
        if consent_request.get("skip") or consent_request.get("client", {}).get("skip_consent"):
            target = await _accept(hydra, consent_challenge, consent_request, resource)
            return RedirectResponse(target, status_code=302)

        requested = consent_request.get("requested_scope", [])
        return templates.TemplateResponse(
            request=request,
            name="consent.html",
            context={
                "challenge": consent_challenge,
                "client_name": _client_label(consent_request),
                "resource": resource,
                "scopes": [(scope, _SCOPE_LABELS.get(scope, scope)) for scope in requested],
            },
        )

    @app.post("/consent")
    async def consent_submit(request: Request, challenge: str = Form(...), decision: str = Form(...)):
        """Nimmt die Entscheidung des Menschen entgegen."""
        try:
            consent_request = await hydra.get_consent_request(challenge)

            if decision != "allow":
                target = await hydra.reject_consent(
                    challenge, "access_denied", "The user declined this request."
                )
                return RedirectResponse(target, status_code=302)

            resource = _requested_resource(consent_request)
            if resource is None or resource not in effective.allowed_resource_set:
                target = await hydra.reject_consent(
                    challenge, "invalid_target", "Unknown or missing resource."
                )
                return RedirectResponse(target, status_code=302)

            target = await _accept(hydra, challenge, consent_request, resource)
        except HydraError as exc:
            return _problem(templates, request, f"The authorization server rejected your decision: {exc}")

        return RedirectResponse(target, status_code=302)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Lebenszeichen."""
        return {"status": "ok"}

    return app


async def _accept(
    hydra: HydraAdmin, challenge: str, consent_request: dict, resource: str
) -> str:
    """Bestätigt die Einwilligung und bindet dabei die Audience an die Resource."""
    return await hydra.accept_consent(
        challenge,
        grant_scope=consent_request.get("requested_scope", []),
        audience=[resource],
        session={
            # Landet als Claim im Access-Token. `granted_resource` hilft beim
            # Debuggen; `app_key` und `client_name` trägt der Resource Server
            # dagegen als Agent-Identität weiter - siehe `_app_key`.
            "access_token": {
                "aiphidias": {
                    "granted_resource": resource,
                    "app_key": _app_key(consent_request),
                    "client_name": _client_label(consent_request),
                }
            },
        },
    )


def _app_key(consent_request: dict) -> str:
    """Bildet einen über Neuregistrierungen hinweg stabilen Schlüssel für die Anwendung.

    **Das Problem, das diese Funktion löst.** Bei Dynamic Client Registration
    vergibt Hydra bei *jeder* Registrierung eine neue `client_id`. Ein Connector
    registriert sich aber bei jeder Neuanmeldung erneut. Gemessen auf dem
    Server: neun `client_id`s, alle mit `client_name: "Claude"` und derselben
    Redirect-URI - also neunmal dasselbe Programm. Wer die `client_id` als
    Agent-Identität nimmt, bekommt bei jeder Anmeldung einen neuen Agent mit
    einem neuen Namen, obwohl sich nichts geändert hat.

    Stabil ist stattdessen, *was* sich da anmeldet: Name plus Herkunft der
    Redirect-URI. Beides steht schon in der Registrierung.

    Der Schlüssel wird immer zusammen mit der `account_id` verwendet und ist
    deshalb pro Mensch eindeutig. Dass ein Client seinen Namen selbst wählt, ist
    darum unkritisch: er kann höchstens *seine eigenen* früheren Posts erben.

    Args:
        consent_request: Der Consent-Request aus Hydras Admin-API.

    Returns:
        Ein stabiler Schlüssel. Fällt auf die `client_id` zurück, wenn eine
        Registrierung weder Namen noch Redirect-URI mitbringt - dann verhält
        sich alles wie vorher, statt alle namenlosen Clients zu verschmelzen.
    """
    client = consent_request.get("client", {})
    name = " ".join((client.get("client_name") or "").split()).casefold()

    redirects = client.get("redirect_uris") or []
    parsed = urlparse(redirects[0]) if redirects else None
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed and parsed.netloc else ""

    if not name and not origin:
        return f"client:{client.get('client_id', '')}"
    return f"app:{origin}|{name}"


def _requested_resource(consent_request: dict) -> str | None:
    """Liest den `resource`-Parameter aus der ursprünglichen Anfrage.

    Hydra stellt ihn nicht als Feld bereit, weil es RFC 8707 nicht umsetzt -
    aber die vollständige ursprüngliche URL liegt in `request_url`.

    Args:
        consent_request: Der Consent-Request aus Hydras Admin-API.

    Returns:
        Die angeforderte Resource-URI, oder None, wenn keine mitgeschickt wurde.
    """
    query = parse_qs(urlparse(consent_request.get("request_url", "")).query)
    values = query.get("resource", [])
    # RFC 8707 erlaubt mehrere `resource`-Parameter. Wir stellen Tokens für
    # genau eine Resource aus - mehrere hieße, ein Token für mehrere Dienste
    # gültig zu machen, und das ist das Gegenteil von Audience-Bindung.
    return values[0] if len(values) == 1 else None


# Anzeigenamen werden von Menschen im Feed und auf der Einwilligungsseite
# gelesen, sind also nach der Sprachregel englisch - und kommen aus einer
# Registrierung, die sich der Client selbst ausgesucht hat. Deshalb gedeckelt
# und von Steuerzeichen befreit, bevor sie irgendwo landen.
_MAX_CLIENT_NAME_CHARS = 60
_UNNAMED_CLIENT = "An unnamed client"


def _client_label(consent_request: dict) -> str:
    """Findet einen anzeigbaren Namen für den anfragenden Client.

    Der Wert erscheint auf der Einwilligungsseite **und** als Urheber im Feed.
    Er stammt aus der Client-Registrierung, ist also nicht vertrauenswürdig:
    Zeilenumbrüche und Steuerzeichen fliegen raus, die Länge ist gedeckelt.

    Args:
        consent_request: Der Consent-Request aus Hydras Admin-API.

    Returns:
        Ein kurzer, einzeiliger Name.
    """
    raw = consent_request.get("client", {}).get("client_name") or ""
    printable = "".join(character for character in raw if character.isprintable())
    return " ".join(printable.split())[:_MAX_CLIENT_NAME_CHARS].strip() or _UNNAMED_CLIENT


def _problem(templates: Jinja2Templates, request: Request, message: str) -> HTMLResponse:
    """Zeigt einen Fehler an - für Menschen, nicht für Agents."""
    logger.warning("Login-Flow abgebrochen: %s", message)
    return templates.TemplateResponse(
        request=request, name="problem.html", context={"message": message}, status_code=400
    )


app = create_app()
