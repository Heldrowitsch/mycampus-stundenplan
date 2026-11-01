"""
Der eigentliche Konnektor: ein kleiner MCP-Server.

MCP (Model Context Protocol) ist die "Steckdose", über die Claude fremde Werkzeuge
benutzen kann. Jede Funktion mit @server.tool() darüber wird für Claude zu einem
Werkzeug. Den Text in den dreifachen Anführungszeichen liest Claude, um zu
entscheiden, wann es das Werkzeug benutzt. Deshalb ist er so genau formuliert.

Es gibt zwei Betriebsarten:
- Auf deinem PC startet Claude Code diese Datei selbst (über stdin/stdout).
- Im Internet (für claude.ai, Handy, Browser) läuft sie als Webserver, sobald die
  Umgebungsvariable GEHEIMER_PFAD gesetzt ist. Siehe LIESMICH.md.
"""

import os
from datetime import datetime, timedelta

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

import stundenplan as sp

server = MCPServer(
    name="mycampus",
    instructions="Liest den persönlichen Stundenplan des Nutzers aus MyCampus der HSWT (Campus Triesdorf).",
)


def _mit_hinweis(text, hinweis):
    """Hängt einen Hinweis (z. B. 'MyCampus nicht erreichbar') vorne an."""
    return f"{hinweis}\n\n{text}" if hinweis else text


@server.tool()
def stundenplan_tag(datum: str = "heute") -> str:
    """Zeigt alle Vorlesungen und Termine eines Tages.
    datum: 'heute', 'morgen', 'übermorgen', '2026-11-03' oder '03.11.2026'."""
    try:
        tag = sp.datum_lesen(datum)
        liste, hinweis = sp.termine(tag, tag)
    except sp.StundenplanFehler as fehler:
        return str(fehler)

    kopf = f"{sp.WOCHENTAGE[tag.weekday()]}, {tag:%d.%m.%Y}"
    if not liste:
        return _mit_hinweis(f"{kopf}: keine Termine.", hinweis)
    zeilen = [sp.termin_als_text(t) for t in liste]
    return _mit_hinweis(kopf + "\n" + "\n".join(zeilen), hinweis)


@server.tool()
def stundenplan_woche(datum: str = "heute") -> str:
    """Zeigt den Stundenplan der ganzen Woche (Montag bis Sonntag), in der `datum` liegt.
    Für 'nächste Woche' ein Datum aus der nächsten Woche angeben."""
    try:
        tag = sp.datum_lesen(datum)
        montag = tag - timedelta(days=tag.weekday())
        liste, hinweis = sp.termine(montag, montag + timedelta(days=6))
    except sp.StundenplanFehler as fehler:
        return str(fehler)

    zeilen = [f"Woche vom {montag:%d.%m.} bis {montag + timedelta(days=6):%d.%m.%Y}"]
    for i in range(7):
        t_tag = montag + timedelta(days=i)
        an_dem_tag = [t for t in liste if t["beginn"].date() == t_tag]
        if not an_dem_tag and i >= 5:
            continue  # leere Wochenendtage weglassen
        zeilen.append(f"\n{sp.WOCHENTAGE[i]}, {t_tag:%d.%m.}")
        if an_dem_tag:
            zeilen += ["  " + sp.termin_als_text(t) for t in an_dem_tag]
        else:
            zeilen.append("  frei")
    return _mit_hinweis("\n".join(zeilen), hinweis)


@server.tool()
def naechste_termine(anzahl: int = 5) -> str:
    """Zeigt die nächsten anstehenden Vorlesungen ab jetzt (Standard: 5)."""
    try:
        heute = sp.heute_in_deutschland()
        liste, hinweis = sp.termine(heute, heute + timedelta(days=60))
    except sp.StundenplanFehler as fehler:
        return str(fehler)

    jetzt = datetime.now(sp.ZEITZONE)
    kommende = [t for t in liste if (t["ende"] or t["beginn"]) > jetzt][:anzahl]
    if not kommende:
        return _mit_hinweis("In den nächsten 60 Tagen stehen keine Termine an.", hinweis)
    return _mit_hinweis("\n".join(sp.termin_als_text(t, mit_datum=True) for t in kommende), hinweis)


@server.tool()
def veranstaltung_suchen(suchbegriff: str, tage_voraus: int = 60) -> str:
    """Sucht kommende Termine, deren Titel, Raum oder Beschreibung den Suchbegriff enthält,
    z. B. 'Pflanzenbau' oder einen Dozentennamen. Groß-/Kleinschreibung ist egal."""
    try:
        heute = sp.heute_in_deutschland()
        liste, hinweis = sp.termine(heute, heute + timedelta(days=tage_voraus))
    except sp.StundenplanFehler as fehler:
        return str(fehler)

    s = suchbegriff.lower()
    treffer = [t for t in liste if s in (t["titel"] + t["ort"] + t["beschreibung"]).lower()]
    if not treffer:
        return _mit_hinweis(f"Keine Termine mit '{suchbegriff}' in den nächsten {tage_voraus} Tagen.", hinweis)
    return _mit_hinweis("\n".join(sp.termin_als_text(t, mit_datum=True) for t in treffer), hinweis)


@server.tool()
def stundenplan_aenderungen() -> str:
    """Prüft, ob sich der Stundenplan seit dem letzten Nachsehen geändert hat
    (neue, verschobene oder ausgefallene Termine in den nächsten 60 Tagen).
    Gut für die morgendliche Frage 'Hat sich was geändert?'."""
    try:
        neu, weggefallen, erster_aufruf, hinweis = sp.aenderungen()
    except sp.StundenplanFehler as fehler:
        return str(fehler)

    if erster_aufruf:
        return _mit_hinweis("Erster Aufruf: Der aktuelle Stand wurde gemerkt. "
                            "Ab dem nächsten Mal werden Änderungen angezeigt.", hinweis)
    if not neu and not weggefallen:
        return _mit_hinweis("Keine Änderungen seit dem letzten Nachsehen.", hinweis)

    zeilen = []
    if neu:
        zeilen.append("Neu oder geändert:")
        zeilen += ["  + " + sp.termin_als_text(t, mit_datum=True) for t in neu]
    if weggefallen:
        zeilen.append("Weggefallen oder verschoben (alter Stand):")
        # Format des Schlüssels: Datum Beginn|Ende|Titel|Ort
        for s in weggefallen:
            beginn, ende, titel, ort = s.split("|", 3)
            zeilen.append(f"  - {beginn}–{ende}  {titel}" + (f"  ({ort})" if ort else ""))
    return _mit_hinweis("\n".join(zeilen), hinweis)


if __name__ == "__main__":
    geheimer_pfad = os.environ.get("GEHEIMER_PFAD")
    if not geheimer_pfad:
        server.run()  # auf dem PC: Kommunikation über stdin/stdout mit Claude Code
    else:
        # Im Internet: Webserver. Die Adresse enthält einen langen Zufallstext,
        # damit nur jemand mit genau diesem Link deinen Stundenplan abrufen kann.
        # Der Schutz vor "DNS-Rebinding" ist nur für Server auf dem eigenen PC gedacht
        # und würde hier die Anfragen von claude.ai blockieren, darum aus.
        server.run(
            "streamable-http",
            host="0.0.0.0",
            port=int(os.environ.get("PORT", 7860)),
            streamable_http_path=f"/{geheimer_pfad}/mcp",
            stateless_http=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )
