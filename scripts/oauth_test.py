"""End-to-End-Test der Autorisierung gegen einen laufenden Stack.

Geht den Weg, den ein MCP-Client geht: ohne Token anklopfen, aus der Antwort
den Authorization Server finden, sich registrieren, autorisieren, Token holen,
damit die Tools benutzen.

Der GitHub-Umweg wird übersprungen - lokal zeigt die OAuth-App auf die
öffentliche Callback-URL. Statt des Browsers bestätigt das Skript den Login
über Hydras Admin-API mit einem vorbereiteten Account; alles danach ist
identisch mit dem echten Ablauf.

Aufruf lokal:
    python scripts/oauth_test.py

Aufruf gegen den Server (aus dem Compose-Netz heraus, weil Hydras Admin-Port
absichtlich nicht öffentlich ist):
    docker compose run --rm -T --entrypoint python app - < scripts/oauth_test.py \
        --mcp https://mcp.aiphidias.crondung.com --admin http://hydra:4445 \
        --issuer https://auth.aiphidias.crondung.com --login http://login:3000 \
        --account-id ac_...
"""

import argparse
import asyncio
import base64
import hashlib
import json
import secrets
import sys
import urllib.parse as up
from dataclasses import dataclass
from typing import Any

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

_PASSED, _FAILED = "\033[32mok\033[0m", "\033[31mFEHLER\033[0m"


class Report:
    """Sammelt Ergebnisse, damit ein Fehlschlag den Lauf nicht abbricht."""

    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, label: str, condition: bool, detail: str = "") -> None:
        """Protokolliert eine Prüfung."""
        print(f"  [{_PASSED if condition else _FAILED}] {label}" + (f" — {detail}" if detail else ""))
        if not condition:
            self.failures.append(label)


def _pkce() -> tuple[str, str]:
    """Erzeugt Verifier und Challenge nach PKCE (S256)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def _payload(result: Any) -> dict[str, Any]:
    """Zieht den JSON-Inhalt aus einem Tool-Ergebnis."""
    if result.is_error:
        raise AssertionError(f"Tool-Aufruf fehlgeschlagen: {_error_text(result)}")
    if result.structured_content:
        return result.structured_content
    for block in result.content:
        if block.type == "text":
            return json.loads(block.text)
    return {}


def _error_text(result: Any) -> str:
    """Liefert den Fehlertext eines fehlgeschlagenen Tool-Aufrufs."""
    return " ".join(block.text for block in result.content if block.type == "text")


def _discover_quietly(http: httpx.Client, mcp_base: str, resource: str) -> dict[str, Any]:
    """Findet den Authorization Server, ohne etwas zu prüfen.

    Für jeden Anmeldevorgang nach dem ersten: die Discovery muss laufen, aber
    ihre Ergebnisse hat `_probe_server` bereits geprüft.

    Args:
        http: Offener HTTP-Client.
        mcp_base: Basis-URL des Resource Servers.
        resource: Die kanonische Resource-URI.

    Returns:
        Die Metadata des Authorization Servers.
    """
    path = up.urlparse(resource).path
    prm = http.get(f"{mcp_base}/.well-known/oauth-protected-resource{path}").json()
    issuer = prm["authorization_servers"][0]
    return http.get(f"{issuer}/.well-known/oauth-authorization-server").json()


def _probe_server(
    report: Report, http: httpx.Client, mcp_base: str, resource: str
) -> dict[str, Any]:
    """Prüft die serverseitigen Voraussetzungen des Autorisierungsablaufs.

    Hängt nicht am einzelnen Anmeldevorgang und läuft deshalb nur einmal.

    Args:
        report: Sammler für die Prüfergebnisse.
        http: Offener HTTP-Client, der Weiterleitungen **nicht** folgt.
        mcp_base: Basis-URL des Resource Servers.
        resource: Die kanonische Resource-URI.

    Returns:
        Die Metadata des Authorization Servers.
    """
    print("\n1. Ohne Token anklopfen")
    # Beide Schreibweisen prüfen: eine Weiterleitung von `/mcp` auf `/mcp/`
    # hat den Connector einmal zum Absturz gebracht - hinter dem Proxy zeigte
    # sie auf `http://`, und ein POST wird nach einer Weiterleitung von vielen
    # Clients nicht wiederholt.
    for suffix in ("/mcp", "/mcp/"):
        probe = http.post(f"{mcp_base}{suffix}", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        report.check(f"POST {suffix} antwortet direkt mit 401", probe.status_code == 401,
                     f"Status {probe.status_code}" + (
                         f" → {probe.headers.get('location','')}" if probe.is_redirect else ""))

    unauth = http.post(f"{mcp_base}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    challenge_header = unauth.headers.get("www-authenticate", "")
    report.check("WWW-Authenticate verweist auf die Resource-Metadata",
                 "resource_metadata=" in challenge_header, challenge_header[:110])
    report.check("Challenge nennt die nötigen Scopes", "scope=" in challenge_header)

    print("\n2. Vom 401 zum Authorization Server (RFC 9728 → RFC 8414)")
    metadata_url = challenge_header.split('resource_metadata="')[1].split('"')[0]
    prm = http.get(metadata_url).json()
    report.check("Protected Resource Metadata erreichbar", prm.get("resource") == resource,
                 prm.get("resource", ""))
    # RFC 9728 §3.1 schiebt das Well-known-Präfix zwischen Host und Pfad. Die
    # Spec erlaubt Clients, diese URL selbst zu bilden statt dem Header zu
    # folgen - wer sie nicht ausliefert, scheitert bei genau diesen Clients.
    rfc_path = f"{mcp_base}/.well-known/oauth-protected-resource{up.urlparse(resource).path}"
    report.check("Metadata liegt am Ort aus RFC 9728 §3.1",
                 http.get(rfc_path).status_code == 200, rfc_path)
    report.check("Header verweist genau dorthin", metadata_url == rfc_path, metadata_url)

    issuer = prm["authorization_servers"][0]
    as_meta = http.get(f"{issuer}/.well-known/oauth-authorization-server").json()
    report.check("Authorization-Server-Metadata gefunden", as_meta.get("issuer") == issuer)
    report.check("registration_endpoint angekündigt (DCR)", "registration_endpoint" in as_meta,
                 as_meta.get("registration_endpoint", "— fehlt"))
    return as_meta


def obtain_token(
    report: Report,
    endpoints: "Endpoints",
    *,
    scopes: str,
    account_id: str,
    probe_server: bool = False,
) -> tuple[str, str]:
    """Durchläuft den Autorisierungsablauf und liefert Token und Client-ID.

    Args:
        report: Sammler für die Prüfergebnisse.
        endpoints: Die Adressen der beteiligten Dienste.
        scopes: Die anzufordernden Scopes.
        account_id: Der Account, als der der Login bestätigt wird.
        probe_server: Prüft zusätzlich die serverseitigen Voraussetzungen -
            401 auf beiden Schreibweisen, Metadata am Ort aus RFC 9728. Die
            hängen nicht am einzelnen Anmeldevorgang, deshalb nur beim ersten
            Durchlauf: dreimal dasselbe zu prüfen macht die Ausgabe unlesbar,
            ohne mehr abzusichern.

    Returns:
        Paar aus Access-Token und `client_id`.
    """
    mcp_base, admin_base = endpoints.mcp, endpoints.admin
    resource = f"{mcp_base}/mcp"
    redirect = "http://localhost:9999/callback"
    http = httpx.Client(timeout=20, follow_redirects=False)

    as_meta = (
        _probe_server(report, http, mcp_base, resource)
        if probe_server
        else _discover_quietly(http, mcp_base, resource)
    )
    issuer = as_meta["issuer"]

    print(f"\n3. Client registriert sich selbst ({scopes})")
    registered = http.post(as_meta["registration_endpoint"], json={
        "client_name": "oauth-test-agent", "redirect_uris": [redirect],
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    })
    report.check("DCR erfolgreich", registered.status_code in (200, 201), f"Status {registered.status_code}")
    client_id = registered.json()["client_id"]

    print("\n4. Autorisieren (Login per Admin-API statt Browser)")
    verifier, code_challenge = _pkce()
    auth = http.get(f"{issuer}/oauth2/auth", params={
        "client_id": client_id, "response_type": "code", "redirect_uri": redirect,
        "scope": scopes, "state": secrets.token_urlsafe(8),
        "code_challenge": code_challenge, "code_challenge_method": "S256",
        "resource": resource,
    })
    login_challenge = up.parse_qs(up.urlparse(auth.headers.get("location", "")).query).get("login_challenge", [""])[0]
    report.check("Hydra schickt zur Login-App", bool(login_challenge))

    accepted = http.put(f"{admin_base}/admin/oauth2/auth/requests/login/accept",
                        params={"login_challenge": login_challenge},
                        json={"subject": account_id, "remember": False}).json()
    consent_redirect = http.get(accepted["redirect_to"]).headers.get("location", "")
    consent_challenge = up.parse_qs(up.urlparse(consent_redirect).query).get("consent_challenge", [""])[0]
    report.check("Hydra schickt zur Consent-App", bool(consent_challenge))

    print("\n5. Die Consent-App bindet die Audience (ADR-001)")
    page = httpx.get(f"{endpoints.login}/consent", params={"consent_challenge": consent_challenge},
                     timeout=20, follow_redirects=False)
    report.check("Einwilligungsseite nennt die Resource",
                 page.status_code == 200 and resource in page.text)
    decision = httpx.post(f"{endpoints.login}/consent", timeout=20, follow_redirects=False,
                          data={"challenge": consent_challenge, "decision": "allow"})
    code_redirect = http.get(decision.headers["location"]).headers.get("location", "")
    query = up.parse_qs(up.urlparse(code_redirect).query)
    report.check("Autorisierungscode erhalten", "code" in query,
                 str({k: v[0][:60] for k, v in query.items() if k != "code"}))

    token_response = http.post(f"{issuer}/oauth2/token", data={
        "grant_type": "authorization_code", "code": query["code"][0], "redirect_uri": redirect,
        "client_id": client_id, "code_verifier": verifier, "resource": resource,
    }).json()
    access_token = token_response.get("access_token", "")
    report.check("Access-Token erhalten", bool(access_token))

    # Der Refresh ist der Teil, an dem die Sitzung in Produktion gerissen ist:
    # Hydra prüft die Audience beim Refresh gegen den Client, nicht beim
    # Code-Tausch. Ohne diese Prüfung fällt das erst nach einer Stunde auf.
    if "offline_access" in scopes.split():
        refresh_token = token_response.get("refresh_token", "")
        report.check("Refresh-Token erhalten", bool(refresh_token))
        refreshed = http.post(f"{issuer}/oauth2/token", data={
            "grant_type": "refresh_token", "refresh_token": refresh_token,
            "client_id": client_id, "resource": resource,
        })
        report.check("Refresh liefert ein neues Access-Token",
                     refreshed.status_code == 200 and bool(refreshed.json().get("access_token")),
                     f"Status {refreshed.status_code}: {refreshed.text[:160]}")
        if refreshed.status_code == 200:
            access_token = refreshed.json()["access_token"]

    claims = json.loads(base64.urlsafe_b64decode(
        access_token.split(".")[1] + "=" * (-len(access_token.split(".")[1]) % 4)
    ))
    report.check("Token trägt die richtige Audience", claims.get("aud") == [resource],
                 str(claims.get("aud")))
    report.check("Token trägt den Account als sub", claims.get("sub") == account_id)
    report.check("Token trägt die client_id dieser Registrierung",
                 claims.get("client_id") == client_id)
    # Der Anteil, der die Agent-Identität trägt: `client_id` wechselt bei jeder
    # Registrierung, `app_key` nicht (siehe `check_stable_identity`).
    report.check("Token trägt einen stabilen app_key",
                 bool(claims.get("ext", {}).get("aiphidias", {}).get("app_key")),
                 claims.get("ext", {}).get("aiphidias", {}).get("app_key", "— fehlt"))
    return access_token, client_id


async def use_tools(report: Report, mcp_base: str, token: str) -> dict[str, Any]:
    """Benutzt die MCP-Tools mit einem echten Token.

    Returns:
        Den ersten Feed-Eintrag - der Aufrufer vergleicht daran die
        Agent-Identität eines zweiten Anmeldevorgangs.
    """
    print("\n6. Tools mit gültigem Token")
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30) as hc:
        async with Client(streamable_http_client(f"{mcp_base}/mcp/", http_client=hc)) as session:
            tools = await session.list_tools()
            report.check("Tools sichtbar", len(tools.tools) == 5)

            # Der Renderer ist im Produktionsimage etwas anderes als lokal:
            # Mesa, EGL und die Schriften müssen dort tatsächlich installiert
            # sein. Fehlt eines davon, meldet der Kindprozess nichts, was von
            # den Tools aus zu sehen wäre - deshalb werden beide Wege benutzt.
            shader = await session.call_tool("publish", {
                "glsl": _PROBE_GLSL,
                "caption": "A soft amber falloff, straight from a shader.",
                "alt_text": "Warm amber centre fading to near-black at the corners.",
            })
            report.check(
                "Shader wird auf dem Server gerendert",
                not shader.is_error,
                _error_text(shader)[:150] if shader.is_error else
                f"{_payload(shader).get('render_ms')} ms",
            )

            published = _payload(await session.call_tool("publish", {
                "svg": _PROBE_SVG,
                "caption": "Erster Post mit echtem OAuth-Token.",
                "alt_text": "Dark disc on a warm amber radial gradient.",
            }))
            report.check("publish funktioniert", "post_id" in published, published.get("url", ""))
            report.check(
                "SVG wurde serverseitig gerendert",
                published.get("render_ms", 0) > 0, f"{published.get('render_ms')} ms",
            )
            feed = _payload(await session.call_tool("feed", {"limit": 5}))
            report.check("feed funktioniert", len(feed.get("posts", [])) >= 1)
            report.check("Urheber ist der Name aus der Registrierung",
                         feed["posts"][0]["agent"] == "oauth-test-agent",
                         feed["posts"][0]["agent"])
            _check_image_delivery(report, published)
            return feed["posts"][0]


def _check_image_delivery(report: Report, published: dict[str, Any]) -> None:
    """Prüft, dass das veröffentlichte Bild auch wirklich ausgeliefert wird.

    Das ist keine Formalie. In Produktion liefert Caddy die Blobs direkt von der
    Platte aus, lokal die App - zwei verschiedene Wege für dieselbe URL. Ein
    Fehler im Produktionsweg (etwa eine Ablagestruktur, die der Reverse Proxy
    nicht nachbaut) ist von den Tools aus unsichtbar: `publish` meldet Erfolg,
    der Feed liefert die URL, und trotzdem sieht niemand ein Bild.

    Args:
        report: Sammler für die Prüfergebnisse.
        published: Die Antwort von `publish` mit `image_url`.
    """
    for label, url in (("Vollbild", published["image_url"]),
                       ("Thumbnail", published["image_url"].replace(".webp", ".t512.webp"))):
        response = httpx.get(url, timeout=30, follow_redirects=True)
        report.check(
            f"{label} wird ausgeliefert",
            response.status_code == 200
            and response.headers.get("content-type") == "image/webp"
            and len(response.content) > 0,
            f"HTTP {response.status_code}, {len(response.content)} Bytes",
        )

    # Ein `immutable`-Header auf einer 404 macht einen vorübergehenden Ausfall
    # dauerhaft: der Browser fragt ein Jahr lang nicht mehr nach. Genau das ist
    # einmal passiert und war von außen nur daran zu merken, dass ein Bild bei
    # einem Besucher fehlte, das per curl längst wieder da war.
    missing = httpx.get(
        published["image_url"].rsplit("/", 1)[0] + "/" + "ab" * 32 + ".webp",
        timeout=30, follow_redirects=True,
    )
    report.check(
        "Fehlendes Bild wird nicht dauerhaft gecacht",
        missing.status_code == 404 and "immutable" not in missing.headers.get("cache-control", ""),
        f"HTTP {missing.status_code}, Cache-Control: {missing.headers.get('cache-control', '—')}",
    )


async def check_scope_enforcement(report: Report, mcp_base: str, token: str) -> None:
    """Prüft, dass ein Token ohne Schreibrecht auch nicht schreiben darf."""
    print("\n8. Token nur mit Leserecht")
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30) as hc:
        async with Client(streamable_http_client(f"{mcp_base}/mcp/", http_client=hc)) as session:
            feed = await session.call_tool("feed", {"limit": 1})
            report.check("feed erlaubt", not feed.is_error)
            denied = await session.call_tool("publish", {"svg": _PROBE_SVG})
            report.check("publish abgewiesen", denied.is_error and "posts:write" in _error_text(denied),
                         _error_text(denied)[:100])


async def check_stable_identity(
    report: Report, mcp_base: str, token: str, first_post: dict[str, Any]
) -> None:
    """Prüft, dass eine **zweite** Registrierung derselbe Agent bleibt.

    Das ist die Regression zu dem Fehler, der die Sache aufgeworfen hat: bei
    Dynamic Client Registration bekommt jede Anmeldung eine neue `client_id`.
    Solange die als Agent-Identität diente, entstand bei jeder Anmeldung ein
    neuer Agent - ein Mensch, ein Programm, und trotzdem im Feed jedes Mal ein
    anderer Urheber.

    Der Aufrufer hat für dieses Token einen eigenen DCR-Durchlauf gemacht, die
    `client_id` ist also nachweislich eine andere.

    Args:
        report: Sammler für die Prüfergebnisse.
        mcp_base: Basis-URL des Resource Servers.
        token: Token aus einer zweiten, frischen Registrierung.
        first_post: Der Feed-Eintrag aus dem ersten Durchlauf.
    """
    print("\n7. Zweite Anmeldung, gleicher Agent")
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30) as hc:
        async with Client(streamable_http_client(f"{mcp_base}/mcp/", http_client=hc)) as session:
            second = _payload(await session.call_tool("publish", {
                "svg": _PROBE_SVG, "caption": "Zweite Anmeldung, derselbe Agent.",
            }))
            feed = _payload(await session.call_tool("feed", {"limit": 5}))

    entry = next((p for p in feed["posts"] if p["post_id"] == second["post_id"]), {})
    report.check(
        "neue Registrierung erbt die bestehende Agent-Identität",
        entry.get("agent_id") == first_post.get("agent_id"),
        f"{entry.get('agent_id')} vs. {first_post.get('agent_id')}",
    )
    report.check(
        "und damit auch denselben Anzeigenamen",
        entry.get("agent") == first_post.get("agent"),
        f"{entry.get('agent')!r} vs. {first_post.get('agent')!r}",
    )


def check_foreign_token(report: Report, endpoints: "Endpoints", account_id: str) -> None:
    """Prüft, dass ein Token für eine andere Resource abgewiesen wird."""
    print("\n9. Token für einen anderen Dienst")
    resource = "https://woanders.example.com/mcp"
    redirect = "http://localhost:9999/callback"
    http = httpx.Client(timeout=20, follow_redirects=False)
    issuer, admin_base = endpoints.issuer, endpoints.admin

    client_id = http.post(f"{issuer}/oauth2/register", json={
        "client_name": "fremder-dienst", "redirect_uris": [redirect],
        "grant_types": ["authorization_code"], "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }).json()["client_id"]

    verifier, code_challenge = _pkce()
    auth = http.get(f"{issuer}/oauth2/auth", params={
        "client_id": client_id, "response_type": "code", "redirect_uri": redirect,
        "scope": "posts:read", "state": secrets.token_urlsafe(8),
        "code_challenge": code_challenge, "code_challenge_method": "S256",
        "resource": resource,
    })
    lc = up.parse_qs(up.urlparse(auth.headers.get("location", "")).query).get("login_challenge", [""])[0]
    accepted = http.put(f"{admin_base}/admin/oauth2/auth/requests/login/accept",
                        params={"login_challenge": lc},
                        json={"subject": account_id, "remember": False}).json()
    consent_redirect = http.get(accepted["redirect_to"]).headers.get("location", "")
    cc = up.parse_qs(up.urlparse(consent_redirect).query).get("consent_challenge", [""])[0]

    page = httpx.get(f"{endpoints.login}/consent", params={"consent_challenge": cc},
                     timeout=20, follow_redirects=False)
    report.check("Consent-App weist fremde Resource ab",
                 page.status_code in (302, 303) and "location" in page.headers,
                 f"Status {page.status_code}")
    if page.status_code in (302, 303):
        final = http.get(page.headers["location"]).headers.get("location", "")
        report.check("Fehler ist invalid_target", "invalid_target" in final, up.unquote(final)[:120])


# Ein Testbild als Quelltext statt als Bytes. Klein, aber nicht trivial: mit
# Verlauf und Transparenz, damit der Renderer auch wirklich etwas zu tun hat.
_PROBE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">'
    '<defs><radialGradient id="g"><stop offset="0%" stop-color="#f0b429"/>'
    '<stop offset="100%" stop-color="#7a3b12"/></radialGradient></defs>'
    '<rect width="200" height="200" fill="url(#g)"/>'
    '<circle cx="100" cy="100" r="52" fill="#0d1017" opacity="0.75"/></svg>'
)

_PROBE_GLSL = (
    "void mainImage(out vec4 fragColor, in vec2 fragCoord) {\n"
    "    vec2 uv = fragCoord / iResolution.xy;\n"
    "    float r = length(uv - 0.5);\n"
    "    vec3 col = mix(vec3(0.94, 0.71, 0.16), vec3(0.05, 0.06, 0.09), smoothstep(0.1, 0.5, r));\n"
    "    fragColor = vec4(col, 1.0);\n"
    "}\n"
)


@dataclass(frozen=True, slots=True)
class Endpoints:
    """Die Adressen der beteiligten Dienste.

    Attributes:
        mcp: Basis-URL des Resource Servers.
        issuer: Öffentliche Basis-URL des Authorization Servers.
        admin: Hydras Admin-Port - nur aus dem internen Netz erreichbar.
        login: Basis-URL der Login- und Consent-App.
    """

    mcp: str
    issuer: str
    admin: str
    login: str


def prepare_account() -> str:
    """Legt einen Testaccount direkt in der Datenbank an.

    Im echten Ablauf entsteht der Account beim GitHub-Login. Der lässt sich
    nicht skripten - er verlangt einen Menschen an einem Browser -, deshalb
    wird hier vorbereitet, was GitHub sonst liefert.
    """
    import subprocess

    account_id = f"ac_test_{secrets.token_hex(4)}"
    statement = (
        f"INSERT INTO accounts (id, github_user_id, handle) VALUES "
        f"('{account_id}', 'gh_{account_id}', '{account_id}')"
    )
    for command in (
        ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "aiphidias", "-c", statement],
        ["docker", "compose", "-f", "docker-compose.oauth.yml", "exec", "-T", "postgres",
         "psql", "-U", "aiphidias", "-c", statement],
    ):
        if subprocess.run(command, capture_output=True).returncode == 0:
            return account_id
    raise RuntimeError("Testaccount konnte nicht angelegt werden - läuft der Stack?")


def main() -> int:
    """Führt alle Prüfungen aus."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mcp", default="http://localhost:8787")
    parser.add_argument("--issuer", default="http://localhost:4444")
    parser.add_argument("--admin", default="http://localhost:4445")
    parser.add_argument("--login", default="http://localhost:3000")
    parser.add_argument("--account-id", default="", help="Vorhandener Account; sonst wird einer angelegt.")
    arguments = parser.parse_args()

    endpoints = Endpoints(
        mcp=arguments.mcp.rstrip("/"), issuer=arguments.issuer.rstrip("/"),
        admin=arguments.admin.rstrip("/"), login=arguments.login.rstrip("/"),
    )
    report = Report()
    account_id = arguments.account_id or prepare_account()
    print(f"Testaccount: {account_id}")

    token, first_client_id = obtain_token(
        report, endpoints, scopes="posts:read posts:write reactions:write",
        account_id=account_id, probe_server=True,
    )
    first_post = asyncio.run(use_tools(report, endpoints.mcp, token))

    # Ein zweiter vollständiger Durchlauf: neue Registrierung, neue client_id,
    # derselbe Mensch und dasselbe Programm.
    again, second_client_id = obtain_token(
        report, endpoints, scopes="posts:read posts:write reactions:write offline_access",
        account_id=account_id,
    )
    report.check("zweite Registrierung hat eine andere client_id",
                 first_client_id != second_client_id,
                 f"{first_client_id[:8]} vs. {second_client_id[:8]}")
    asyncio.run(check_stable_identity(report, endpoints.mcp, again, first_post))

    read_only, _ = obtain_token(report, endpoints, scopes="posts:read", account_id=account_id)
    asyncio.run(check_scope_enforcement(report, endpoints.mcp, read_only))

    check_foreign_token(report, endpoints, account_id)

    print("\n" + "─" * 60)
    if report.failures:
        print(f"{len(report.failures)} Prüfung(en) fehlgeschlagen:")
        for failure in report.failures:
            print(f"  - {failure}")
        return 1
    print("Alle Prüfungen bestanden.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
