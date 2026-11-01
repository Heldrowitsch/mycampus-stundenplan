"""
Lädt deinen Stundenplan aus MyCampus (HSWT) und bringt ihn in eine einfache Form.

So funktioniert es:
MyCampus (HISinOne) bietet für den persönlichen Stundenplan einen Kalender-Abo-Link
an (iCalendar, Dateiendung .ics). Hinter diesem Link steht eine Textdatei mit allen
Terminen. Wir laden diese Datei, lesen die Termine aus und geben sie als einfache
Python-Listen zurück. Dein Passwort wird dafür NICHT gebraucht.

Hier passiert nur Laden und Auswerten. Die Werkzeuge für Claude stehen in server.py.
"""

import json
import os
import subprocess
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import icalendar
import recurring_ical_events

ORDNER = Path(__file__).parent
LINK_DATEI = ORDNER / "kalender_link.txt"   # hier steht dein persönlicher Abo-Link

# Auf dem Internet-Server liegt der Link nicht in einer Datei, sondern in einer
# "Umgebungsvariable" (einem geheimen Wert, den man beim Hoster einträgt).
LINK_VARIABLE = "KALENDER_LINK"

# Wo cache.ics und letzter_stand.json gespeichert werden. Auf dem PC: dieser Ordner.
DATEN_ORDNER = Path(os.environ.get("DATEN_ORDNER", ORDNER))
CACHE_DATEI = DATEN_ORDNER / "cache.ics"          # letzte heruntergeladene Version
STAND_DATEI = DATEN_ORDNER / "letzter_stand.json" # Merkliste für die Änderungserkennung

ZEITZONE = ZoneInfo("Europe/Berlin")

# Wie lange (in Sekunden) eine heruntergeladene Version wiederverwendet wird,
# damit MyCampus nicht bei jeder einzelnen Frage neu abgefragt wird.
CACHE_DAUER = 10 * 60

# Termine mit diesen Kategorien werden ausgeblendet. MyCampus trägt Praktika als
# ganztägige Platzhalter für jede Woche ein, die echten Gruppentermine stehen woanders.
# Zum Wieder-Einblenden einfach "Praktikum" aus der Liste löschen.
AUSBLENDEN_KATEGORIEN = ["Praktikum"]

WOCHENTAGE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


class StundenplanFehler(Exception):
    """Fehler mit einer verständlichen Meldung für dich bzw. für Claude."""


# ---------------------------------------------------------------------------
# 1. Kalenderdatei holen
# ---------------------------------------------------------------------------

def _lies_link():
    """Liest den Abo-Link aus der Umgebungsvariable KALENDER_LINK oder aus kalender_link.txt."""
    if os.environ.get(LINK_VARIABLE):
        return os.environ[LINK_VARIABLE].strip()
    if not LINK_DATEI.exists():
        raise StundenplanFehler(
            "Die Datei kalender_link.txt fehlt. Bitte den Kalender-Abo-Link aus MyCampus "
            "dort einfügen (siehe LIESMICH.md)."
        )
    link = LINK_DATEI.read_text(encoding="utf-8").strip()
    if not link.startswith("http"):
        raise StundenplanFehler(
            "In kalender_link.txt steht noch kein gültiger Link (er muss mit https:// beginnen)."
        )
    return link


def _lade_ics():
    """
    Gibt den Inhalt der Kalenderdatei zurück, plus einen Hinweis-Text (oder "").

    Reihenfolge:
    1. Ist die gespeicherte Kopie jünger als CACHE_DAUER, wird sie direkt benutzt.
    2. Sonst wird neu aus MyCampus geladen und die Kopie überschrieben.
    3. Klappt das Laden nicht (kein Internet, MyCampus down), wird die alte Kopie benutzt.
    """
    if CACHE_DATEI.exists() and time.time() - CACHE_DATEI.stat().st_mtime < CACHE_DAUER:
        return CACHE_DATEI.read_bytes(), ""

    link = _lies_link()
    try:
        # Geladen wird mit curl, dem Download-Programm, das bei Windows dabei ist.
        # Pythons eigenes urllib bekommt von MyCampus nur Fehler 503, curl klappt.
        # --fail: bei Fehlern (z. B. 503) abbrechen statt eine Fehlerseite zu speichern
        ergebnis = subprocess.run(
            ["curl", "--silent", "--show-error", "--fail", "--max-time", "20",
             "--user-agent", "MyCampus-Konnektor/1.0", link],
            capture_output=True,
        )
        if ergebnis.returncode != 0:
            raise StundenplanFehler(ergebnis.stderr.decode("utf-8", "replace").strip())
        inhalt = ergebnis.stdout
    except Exception as fehler:
        if CACHE_DATEI.exists():
            alter = datetime.fromtimestamp(CACHE_DATEI.stat().st_mtime, ZEITZONE)
            hinweis = (f"Achtung: MyCampus war nicht erreichbar ({fehler}). "
                       f"Gezeigt wird der Stand vom {alter:%d.%m.%Y %H:%M} Uhr.")
            return CACHE_DATEI.read_bytes(), hinweis
        raise StundenplanFehler(f"MyCampus war nicht erreichbar: {fehler}")

    # Eine echte Kalenderdatei beginnt immer mit BEGIN:VCALENDAR.
    # Kommt etwas anderes zurück (z. B. eine Login-Seite), ist der Link abgelaufen.
    if b"BEGIN:VCALENDAR" not in inhalt[:500]:
        raise StundenplanFehler(
            "MyCampus hat keine Kalenderdatei geliefert. Vermutlich ist der Abo-Link "
            "abgelaufen oder falsch. Bitte in MyCampus einen neuen Link erzeugen."
        )

    CACHE_DATEI.write_bytes(inhalt)
    return inhalt, ""


# ---------------------------------------------------------------------------
# 2. Termine auslesen
# ---------------------------------------------------------------------------

def _als_datetime(wert):
    """Macht aus einem Kalenderwert eine Uhrzeit in deutscher Zeit."""
    if isinstance(wert, datetime):
        if wert.tzinfo is None:
            return wert.replace(tzinfo=ZEITZONE)
        return wert.astimezone(ZEITZONE)
    # Ganztägige Termine haben nur ein Datum, keine Uhrzeit
    return datetime(wert.year, wert.month, wert.day, tzinfo=ZEITZONE)


def _kategorien(ereignis):
    """Gibt die Kategorien eines Termins als Liste von Texten zurück, z. B. ["Praktikum"]."""
    wert = ereignis.get("CATEGORIES")
    if wert is None:
        return []
    if not isinstance(wert, list):  # mehrere CATEGORIES-Zeilen kommen als Liste
        wert = [wert]
    return [str(k) for eintrag in wert for k in eintrag.cats]


def termine(von, bis):
    """
    Gibt alle Termine zwischen den Tagen `von` und `bis` (beide einschließlich) zurück,
    sortiert nach Beginn, plus einen Hinweis-Text.

    Jeder Termin ist ein einfaches Wörterbuch (dict) mit:
    titel, beginn, ende, ort, beschreibung
    """
    inhalt, hinweis = _lade_ics()
    kalender = icalendar.Calendar.from_ical(inhalt)

    # recurring_ical_events löst auch Serientermine ("jeden Dienstag") in Einzeltermine auf
    ereignisse = recurring_ical_events.of(kalender).between(von, bis + timedelta(days=1))

    liste = []
    for e in ereignisse:
        if any(k in AUSBLENDEN_KATEGORIEN for k in _kategorien(e)):
            continue
        liste.append({
            "titel": str(e.get("SUMMARY", "(ohne Titel)")).strip(),
            "beginn": _als_datetime(e.get("DTSTART").dt),
            "ende": _als_datetime(e.get("DTEND").dt) if e.get("DTEND") else None,
            "ort": str(e.get("LOCATION", "")).strip(),
            "beschreibung": str(e.get("DESCRIPTION", "")).strip(),
        })
    liste.sort(key=lambda t: t["beginn"])
    return liste, hinweis


# ---------------------------------------------------------------------------
# 3. Änderungen seit dem letzten Nachsehen erkennen
# ---------------------------------------------------------------------------

def _schluessel(t):
    """Ein Text, der einen Termin eindeutig beschreibt. Ändert sich etwas, ändert sich der Text."""
    ende = f"{t['ende']:%H:%M}" if t["ende"] else "-"
    return f"{t['beginn']:%Y-%m-%d %H:%M}|{ende}|{t['titel']}|{t['ort']}"


def aenderungen(tage_voraus=60):
    """
    Vergleicht die kommenden Termine mit dem Stand beim letzten Aufruf.

    Gibt (neu, weggefallen, erster_aufruf, hinweis) zurück.
    Danach wird der aktuelle Stand als neuer Vergleichsstand gespeichert.
    """
    heute = heute_in_deutschland()
    aktuell, hinweis = termine(heute, heute + timedelta(days=tage_voraus))
    aktuell_nach_schluessel = {_schluessel(t): t for t in aktuell}

    erster_aufruf = not STAND_DATEI.exists()
    alt = set() if erster_aufruf else set(json.loads(STAND_DATEI.read_text(encoding="utf-8")))

    # Vergangene Termine aus dem alten Stand ignorieren, die sind ja keine "Änderung"
    alt = {s for s in alt if s[:10] >= heute.isoformat()}

    neu = [aktuell_nach_schluessel[s] for s in aktuell_nach_schluessel if s not in alt]
    weggefallen = sorted(s for s in alt if s not in aktuell_nach_schluessel)

    STAND_DATEI.write_text(json.dumps(sorted(aktuell_nach_schluessel), ensure_ascii=False, indent=1),
                           encoding="utf-8")
    if erster_aufruf:
        neu = []
    return neu, weggefallen, erster_aufruf, hinweis


# ---------------------------------------------------------------------------
# 4. Hilfen für Datum und Ausgabe
# ---------------------------------------------------------------------------

def heute_in_deutschland():
    """Das heutige Datum in deutscher Zeit (Server im Internet laufen oft auf UTC)."""
    return datetime.now(ZEITZONE).date()


def datum_lesen(text):
    """Versteht 'heute', 'morgen', 'übermorgen', '2026-11-03' und '03.11.2026'."""
    text = (text or "heute").strip().lower()
    heute = heute_in_deutschland()
    if text in ("heute", ""):
        return heute
    if text == "morgen":
        return heute + timedelta(days=1)
    if text in ("übermorgen", "uebermorgen"):
        return heute + timedelta(days=2)
    for muster in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(text, muster).date()
        except ValueError:
            pass
    raise StundenplanFehler(f"Datum '{text}' nicht verstanden. Bitte z. B. 2026-11-03 oder 03.11.2026.")


def termin_als_text(t, mit_datum=False):
    """Eine Zeile pro Termin, z. B. '08:00–09:30  Pflanzenbau  (Raum H12)'."""
    zeit = f"{t['beginn']:%H:%M}"
    if t["ende"]:
        zeit += f"–{t['ende']:%H:%M}"
    if mit_datum:
        zeit = f"{WOCHENTAGE[t['beginn'].weekday()][:2]} {t['beginn']:%d.%m.} {zeit}"
    zeile = f"{zeit}  {t['titel']}"
    if t["ort"]:
        zeile += f"  ({t['ort']})"
    return zeile
