#!/usr/bin/env python3
"""Liest das HFF-Vorlesungsverzeichnis aus und erzeugt ICS-Kalenderdateien.

Pro Kurs entstehen zwei Dateien im Ordner docs/:
  <kurs>.ics          alle Termine
  <kurs>-pflicht.ics  nur Pflichttermine
Dazu eine docs/index.html mit den Links.
"""
import hashlib
import json
import re
import sys
from datetime import date
from html import escape as html_escape
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://campus.hff-muc.de/vvz/downloadVVZ.aspx"
OUT_DIR = Path("docs")
DATE_RE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{2})\b")
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})")

VTIMEZONE = """BEGIN:VTIMEZONE
TZID:Europe/Berlin
BEGIN:DAYLIGHT
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
TZNAME:CEST
DTSTART:19700329T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
TZNAME:CET
DTSTART:19701025T030000
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
END:STANDARD
END:VTIMEZONE"""


def fetch(semester, kurs):
    r = requests.get(
        BASE_URL,
        params={"Semester": semester, "Kurs": kurs},
        timeout=60,
        headers={"User-Agent": "hff-kalender/1.0"},
    )
    r.raise_for_status()
    return r.content


def parse(html):
    """Gibt eine Liste von Terminen (dicts) zurück."""
    soup = BeautifulSoup(html, "html.parser")
    events = {}
    current = None
    for tr in soup.find_all("tr"):
        if tr.find("table"):  # Layout-Zeilen mit verschachtelten Tabellen überspringen
            continue
        tds = tr.find_all(["td", "th"])
        if not tds:
            continue
        texts = [td.get_text(" ", strip=True) for td in tds]

        time_idx = next((i for i, t in enumerate(texts) if TIME_RE.fullmatch(t)), None)
        if time_idx is None:
            # Datumszeile, z. B. "nach oben  Mo, 12.10.26"
            m = DATE_RE.search(" ".join(texts))
            if m and len(tds) <= 3:
                d, mo, y = (int(x) for x in m.groups())
                current = date(2000 + y, mo, d)
            continue
        if current is None:
            continue

        sh, sm, eh, em = (int(x) for x in TIME_RE.fullmatch(texts[time_idx]).groups())
        rest = tds[time_idx + 1:]

        def cell(i):
            return rest[i].get_text(", ", strip=True) if i < len(rest) else ""

        title_td = rest[1] if len(rest) > 1 else None
        link = title_td.find("a") if title_td else None
        ev = {
            "date": current,
            "start": (sh, sm),
            "end": (eh, em),
            "ort": cell(0),
            "title": cell(1),
            "dozent": cell(2),
            "abt": cell(3),
            "status": cell(4),
            "url": link["href"] if link and link.has_attr("href") else "",
        }
        if not ev["title"]:
            continue
        ev["pflicht"] = "pflicht" in ev["status"].lower()
        key = (ev["date"], ev["start"], ev["title"])
        events[key] = ev
    return sorted(events.values(), key=lambda e: (e["date"], e["start"], e["title"]))


def esc(text):
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r", "")
        .replace("\n", "\\n")
    )


def fold(line):
    """ICS-Zeilen auf 75 Byte umbrechen (UTF-8-sicher)."""
    b = line.encode("utf-8")
    parts, limit = [], 75
    while len(b) > limit:
        cut = limit
        while (b[cut] & 0xC0) == 0x80:
            cut -= 1
        parts.append(b[:cut].decode("utf-8"))
        b = b[cut:]
        limit = 74
    parts.append(b.decode("utf-8"))
    return "\r\n ".join(parts)


def build_ics(events, name):
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//hff-kalender//DE",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{esc(name)}",
        "X-WR-TIMEZONE:Europe/Berlin",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]
    lines += VTIMEZONE.split("\n")
    for e in events:
        d = e["date"].strftime("%Y%m%d")
        start = f"{d}T{e['start'][0]:02d}{e['start'][1]:02d}00"
        end = f"{d}T{e['end'][0]:02d}{e['end'][1]:02d}00"
        uid = hashlib.sha1(f"{d}|{start}|{e['title']}".encode()).hexdigest()[:20] + "@hff-kalender"
        desc = []
        if e["dozent"]:
            desc.append(f"Dozent/in: {e['dozent']}")
        if e["status"]:
            desc.append(f"Status: {e['status']}")
        if e["abt"]:
            desc.append(f"Abteilung: {e['abt']}")
        if e["url"]:
            desc.append(e["url"])
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            "DTSTAMP:20260101T000000Z",  # fest, damit sich die Datei nur bei echten Änderungen ändert
            f"DTSTART;TZID=Europe/Berlin:{start}",
            f"DTEND;TZID=Europe/Berlin:{end}",
            f"SUMMARY:{esc(e['title'])}",
            f"LOCATION:{esc(e['ort'])}",
            f"DESCRIPTION:{esc(chr(10).join(desc))}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return ("\r\n".join(fold(l) for l in lines) + "\r\n").encode("utf-8")


def main():
    config = json.loads(Path("config.json").read_text(encoding="utf-8"))
    semester = config["semester"]
    OUT_DIR.mkdir(exist_ok=True)
    index = []

    for kurs in config["kurse"]:
        events = parse(fetch(semester, kurs))
        if not events:
            # Nichts überschreiben, wenn die Seite leer oder umgebaut ist
            sys.exit(f"Keine Termine für {kurs} gefunden – Abbruch, alte Dateien bleiben.")
        slug = kurs.replace("/", "-")
        pflicht = [e for e in events if e["pflicht"]]
        (OUT_DIR / f"{slug}.ics").write_bytes(build_ics(events, f"HFF {kurs} (alle)"))
        (OUT_DIR / f"{slug}-pflicht.ics").write_bytes(build_ics(pflicht, f"HFF {kurs} (Pflicht)"))
        index.append((kurs, slug, len(events), len(pflicht)))
        print(f"{kurs}: {len(events)} Termine, davon {len(pflicht)} Pflicht")

    rows = "\n".join(
        f"<li><b>{html_escape(k)}</b>: "
        f'<a href="{s}.ics">alle ({n})</a> · <a href="{s}-pflicht.ics">nur Pflicht ({p})</a></li>'
        for k, s, n, p in index
    )
    (OUT_DIR / "index.html").write_text(
        "<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
        f"<title>HFF Kalender</title><h1>HFF Kalender {html_escape(semester)}</h1>"
        "<p>Link kopieren und im Kalender als Abo hinzufügen.</p>"
        f"<ul>{rows}</ul>",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
