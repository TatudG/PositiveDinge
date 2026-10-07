#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PositiveDinge — holt Nachrichten, behaelt nur die guten, schreibt index.html.

Nur Standardbibliothek. Kein pip install, kein Bundler, kein Server.

    python3 build.py                 # normaler Build
    python3 build.py --frisch        # alles neu holen, Cache ignorieren
    python3 build.py --offline       # nie ins Netz (Flugmodus-Test)
    python3 build.py --nur-cc-bilder # keine Pressefotos, nur freie Bilder
    python3 build.py --strict        # Exit 1, wenn eine Quelle fehlt

Zur Arbeitsweise des Filters — das Wichtigste zuerst:

Eine reine Sperrliste reicht NICHT. Gegen die echten Schlagzeilen getestet
liessen 78-94 % der allgemeinen Nachrichten eine Sperrliste passieren, darunter
"Messerangriff an Schule - sieben Verletzte". Der Grund: die meisten
Nachrichten sind neutral, nicht negativ-verschluesselt.

Deshalb ist der Filter umgekehrt gebaut. Ein Positivsignal ist PFLICHT, die
Sperrliste ist nur noch Sicherheitsnetz:

    Stufe A  Fehlschlag-Phrasen   "Rettung gescheitert" -> sofort raus
    Stufe B  harte Sperre         Gewalt/Katastrophe -> raus
                                  (mit Negations-Waechter und Entschaerfern)
    Stufe C  Punktwertung         Objektregeln, Positiv x2 im Titel,
                                  Kontext-Abzug, Themen-Bonus
    Schwelle kuratierte Quellen >= 0, allgemeine Quellen >= 4

Wortlisten koennen nicht garantieren, dass nie eine unpassende Meldung
erscheint. Deshalb schreibt jeder Build ein Audit-Log nach
.cache/filter-report.json und liest Korrekturen aus config/overrides.json.
"""

import argparse
import difflib
import hashlib
import html
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

BASIS = Path(__file__).resolve().parent
CONFIG = BASIS / "config"
CACHE = BASIS / ".cache"
BILDER_CC = BASIS / "img" / "cc"
# Pressefotos liegen bewusst in img/presse/ und stehen in .gitignore, nicht in
# img/cc/. Der Grund ist rechtlich, nicht technisch: img/cc/ wird mit der Seite
# veroeffentlicht, und Artikelbilder von Nachrichtenverlagen sind
# urheberrechtlich geschuetzt. Laegen sie in img/cc/, waeren sie ueber die
# Veroeffentlichung abrufbar, selbst wenn keine einzige Karte sie einbindet —
# ein Verzeichnis auf einem Webserver ist lesbar, auch die ungenutzten Dateien.
# Getrennt gehalten kann dieser Fehler gar nicht erst passieren.
BILDER_PRESSE = BASIS / "img" / "presse"
VORLAGE = BASIS / "vorlage.html"
ZIEL = BASIS / "index.html"

UA = "PositiveDinge/1.0 (privater, statischer Nachrichtenleser; lokaler Build)"
OPENVERSE = "https://api.openverse.org/v1/images/"
MAX_ARTIKEL = 48
TTL_FEED = 30 * 60
TTL_OPENVERSE = 7 * 24 * 3600

# Negations-Waechter: steht im Fenster davor eines dieser Woerter, ist ein
# Sperrtreffer kein Sperrtreffer mehr.
NEGATION = re.compile(
    r"\b(ohne|kein|keine|keinen|keiner|keines|nicht|nie|niemals|"
    r"no|not|without|zero|free of|noch nicht)\b"
)
NEG_FENSTER = 45

THEMEN = [
    ("Tiere & Natur", r"tier|artenschutz|naturschutz|wildlife|species|wolf|bär|luchs|"
                      r"vogel|wal|delfin|elefant|tiger|panda|biene|insekt|renaturier|"
                      r"aufforstung|reforest|rewild|wildtier|zoo|brutpaare|population"),
    ("Gesundheit", r"heilung|geheilt|cure|krebs|cancer|therapie|therapy|impfstoff|vaccine|"
                   r"klinik|krankenhaus|arzt|ärztin|patient|gesundheit|health|medizin|"
                   r"virus|bakterien|bacteria|genesung|recovery|vorsorge|screening"),
    ("Wissenschaft & Technik", r"forschung|research|studie|study|durchbruch|breakthrough|"
                               r"entdeck|discover|technik|technolog|physik|chemie|nobelpreis|"
                               r"innovation|erfind|invent|robot|künstliche intelligenz|"
                               r"raumfahrt|space|nasa|esa|teleskop|telescope|archäolog"),
    ("Umwelt & Klima", r"klima|climate|solar|windkraft|wind power|renewable|erneuerbar|"
                       r"emission|recycling|recycelt|nachhaltig|sustainab|umwelt|environment|"
                       r"energiewende|co2|ökostrom|wasserkraft|geothermie"),
    ("Sport", r"\bsport|siegt|sieg\b|\bwin\b|\bwins\b|victory|meister|champion|olymp|"
              r"medaille|medal|rekord|record|liga|league|mannschaft|team|turnier|"
              r"marathon|fußball|football|tennis|radsport|cycling"),
    ("Kultur", r"kultur|culture|musik|music|kunst|\bart\b|film|kino|theater|oper|"
               r"buch|\bbook\b|literatur|literature|festival|museum|ausstellung|"
               r"preisverleihung|award|grammy|oscar|literaturpreis"),
    ("Wirtschaft & Arbeit", r"wirtschaft|econom|unternehmen|company|firma|jobs|arbeitsplatz|"
                            r"invest|umsatz|gewinn|börse|markt|startup|gründung|"
                            r"handwerk|export|auftragseingang"),
]

# Englische Stimmungsmotive fuer die Bildsuche. Bewusst Woerter, die in den
# Titeln freier Bilder tatsaechlich vorkommen — "wellbeing" oder "craftsmanship"
# stehen in kaum einem CC-Titel, "sunrise" und "sunflower" dagegen haeufig.
# Stimmungsmotive je Thema. Englische Begriffe, weil auf Openverse fast nur
# englisch betitelte Bilder liegen — eine deutsche Suche findet nichts und die
# Relevanzpruefung wuerde jeden Treffer verwerfen.
#
# Mehrere Motive je Thema, und zwar in dieser Reihenfolge abgearbeitet: jedes
# Motiv liefert rund acht Treffer, erst danach greift das naechste. Zusammen mit
# der Sperre gegen bereits verwendete Bilder sorgt das fuer Abwechslung, ohne
# dass man fuer jeden Artikel eine eigene Suche braucht (Openverse erlaubt ohne
# Schluessel nur 20 Anfragen pro Minute).
BILD_MOOD = {
    "Tiere & Natur": ("sunflower meadow nature", "forest path sunlight",
                      "birds wildlife green"),
    "Gesundheit": ("sunrise hope wellness", "healthy food vegetables",
                   "yoga meditation calm"),
    "Wissenschaft & Technik": ("science laboratory research", "books library study",
                               "technology innovation blue"),
    "Umwelt & Klima": ("solar panel wind turbine", "green forest aerial",
                       "bicycle city green"),
    "Sport": ("sunrise marathon runners", "football pitch green grass",
              "swimming water blue"),
    "Kultur": ("music festival concert", "art gallery painting",
               "theatre stage light"),
    "Wirtschaft & Arbeit": ("craftsman workshop wood", "market stall vegetables",
                            "office teamwork bright"),
    "Gesellschaft": ("sunflower volunteers community", "children playing happy",
                     "hands together teamwork"),
}
MOOD_ERSATZ = ("good news sunrise", "colorful flowers bright")


# --------------------------------------------------------------------------
# Textwerkzeuge
# --------------------------------------------------------------------------

def falte(text):
    """Kleinschreibung mit casefold plus Vereinheitlichung der Satzzeichen.

    Zwei Gruende, warum das mehr als .lower() ist:

    1. casefold macht aus 'ß' ein 'ss'. Die Staemme in config/ werden beim
       Laden ebenso gefaltet, also trifft "GROSSBRAND" auf den Stamm
       "großbrand" — mit .lower() waere das ein Fehlschlag.
    2. Feeds benutzen typografische Apostrophe: "What We're Reading" steht in
       Wahrheit als "What We’re Reading" im Feed. Ohne diesen Austausch wuerde
       die Serien-Liste nie greifen.
    """
    text = (text or "").casefold()
    for zeichen in ("’", "‘", "‚", "‛"):
        text = text.replace(zeichen, "'")
    for zeichen in ("“", "”", "„", "‟"):
        text = text.replace(zeichen, '"')
    text = text.replace("–", "-").replace("—", "-").replace("‑", "-")
    return text


def nur_text(roh):
    """HTML zu Klartext: Entitaeten aufloesen, Tags entfernen, Weissraum baendigen."""
    if not roh:
        return ""
    text = html.unescape(roh)
    text = re.sub(r"<script\b.*?</script>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)            # doppelt kodierte Entitaeten
    text = text.replace("­", "")     # weiche Trennstriche
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# Verlagshaus-Rauschen, das in fast jedem WordPress-Feed im Beschreibungstext
# steht. Muss vor dem Bewerten weg — nicht nur der Optik wegen:
# "The post ... appeared first on Good News" enthaelt zufaellig "good news",
# ein Positivsignal aus positiv-en.txt, und hebt den Score jedes GNN-Artikels
# um einen Punkt. Das verfaelscht die Schwellenpruefung.
BOILERPLATE = [
    re.compile(r"the post\s+.{0,220}?\s+appeared first on\s+.{0,90}?\.", re.I | re.S),
    re.compile(r"der beitrag\s+.{0,220}?\s+erschien zuerst auf\s+.{0,90}?\.", re.I | re.S),
    re.compile(r"der artikel\s+.{0,220}?\s+erschien zuerst auf\s+.{0,90}?\.", re.I | re.S),
    re.compile(r"\bthis (article|post) (was )?(first )?(published|appeared)[^.]{0,90}\.", re.I),
    re.compile(r"\bby the\s+[\wäöüß\s]{0,60}?editorial team\b", re.I),
    re.compile(r"^\s*(by|von)\s+[A-ZÄÖÜ][\w.\-]+\s+[A-ZÄÖÜ][\w.\-]+\s*[-–—:|]\s*"),
    re.compile(r"^\s*(read more|weiterlesen|lesen sie mehr|mehr dazu)\s*[:.]?\s*", re.I),
    re.compile(r"\b(read more|weiterlesen|lesen sie mehr)\b\s*[:.]?", re.I),
    re.compile(r"\b(advertisement|anzeige|werbung|sponsored content)\b", re.I),
    re.compile(r"\bshare (this|the) (article|story)\b", re.I),
    re.compile(r"^\s*(foto|bild|photo|image|credit)s?\s*:\s*\S+\s*", re.I),
]


def saubre_text(text):
    """Verlagsrauschen aus einem Beschreibungstext entfernen.

    Laeuft VOR der Bewertung und vor dem Kuerzen. Erst danach werden Teaser
    gebaut und Punkte gezaehlt.
    """
    if not text:
        return ""
    for muster in BOILERPLATE:
        text = muster.sub(" ", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"^[\s\-–—.,;:]+", "", text)
    return text.strip()


def kuerze(text, grenze):
    if len(text) <= grenze:
        return text
    schnitt = text[:grenze]
    # an der letzten Wortgrenze trennen, damit kein Wort zerreisst
    lücke = schnitt.rfind(" ")
    if lücke > grenze * 0.6:
        schnitt = schnitt[:lücke]
    return schnitt.rstrip(" ,;:–—-") + " …"


# --------------------------------------------------------------------------
# Konfiguration
# --------------------------------------------------------------------------

def lies_liste(pfad):
    """Liest eine Wortlistendatei.

    Rueckgabe: Liste von (art, wert) mit art == "stamm" oder "regex".
    Kommentare (#) und Leerzeilen fliegen raus.
    """
    eintraege = []
    if not pfad.exists():
        return eintraege
    for zeile in pfad.read_text(encoding="utf-8").splitlines():
        zeile = zeile.strip()
        if not zeile or zeile.startswith("#"):
            continue
        if zeile.lower().startswith("re:"):
            eintraege.append(("regex", falte(zeile[3:].strip())))
        else:
            eintraege.append(("stamm", falte(zeile)))
    return eintraege


def lade_config():
    cfg = {}

    for name in ("hart", "positiv", "kontext"):
        liste = []
        for sprache in ("de", "en"):
            liste += lies_liste(CONFIG / ("%s-%s.txt" % (name, sprache)))
        cfg[name] = liste

    cfg["fehlschlag"] = lies_liste(CONFIG / "fehlschlag.txt")
    cfg["entschaerfer"] = lies_liste(CONFIG / "entschaerfer.txt")
    cfg["serien"] = lies_liste(CONFIG / "serien.txt")

    stopp = []
    for sprache in ("de", "en"):
        stopp += [z.strip().casefold()
                  for z in (CONFIG / ("stoppwoerter-%s.txt" % sprache))
                  .read_text(encoding="utf-8").split()
                  if z.strip()]
    cfg["stopp"] = set(stopp)

    quellen = json.loads((CONFIG / "sources.json").read_text(encoding="utf-8"))
    cfg["quellen"] = [q for q in quellen["quellen"] if q.get("aktiv", True)]
    cfg["max_pro_quelle"] = quellen.get("max_pro_quelle", 6)
    cfg["schwelle_kuratiert"] = quellen.get("schwelle_kuratiert", 0)
    cfg["schwelle_allgemein"] = quellen.get("schwelle_allgemein", 4)

    regeln = json.loads((CONFIG / "objekt-regeln.json").read_text(encoding="utf-8"))
    cfg["objekt"] = [
        {"name": r["name"], "muster": re.compile(falte(r["muster"])), "punkte": r["punkte"]}
        for r in regeln["regeln"]
    ]

    ueb = json.loads((CONFIG / "overrides.json").read_text(encoding="utf-8"))
    cfg["force_drop"] = [u for u in ueb.get("force_drop", [])]
    cfg["force_keep"] = [u for u in ueb.get("force_keep", [])]

    return cfg


# --------------------------------------------------------------------------
# Netz und Cache
# --------------------------------------------------------------------------

def kodiere_url(url):
    """Nicht-ASCII in einer URL prozentkodieren.

    Feeds liefern Bildadressen wie ".../Logo-3000-×-3000.jpg". urllib bricht
    daran mit "'ascii' codec can't encode character" ab — die Adresse muss
    vorher kodiert werden.
    """
    if not url:
        return url
    teile = urllib.parse.urlsplit(url)
    pfad = urllib.parse.quote(teile.path, safe="/%:@&=+$,;~()*!'[]")
    frage = urllib.parse.quote(teile.query, safe="=&%:@+$,;~()*!'[]/?")
    return urllib.parse.urlunsplit((teile.scheme, teile.netloc, pfad, frage, ""))


def cache_pfad(unterordner, schluessel, endung):
    ordner = CACHE / unterordner
    ordner.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha1(schluessel.encode("utf-8")).hexdigest()[:20]
    return ordner / (h + endung)


def cache_frisch(pfad, ttl):
    if not pfad.exists():
        return False
    return (datetime.now().timestamp() - pfad.stat().st_mtime) < ttl


def hole(url, pfad, ttl, args, kopf=None):
    """Holt eine URL, mit Cache und Stale-Fallback.

    Reihenfolge: --frisch umgeht den Cache; --offline geht nie ins Netz.
    Schlaegt der Abruf fehl, wird die letzte gecachte Antwort verwendet — das
    ist der Grund, warum ein alter Cache besser ist als ein Abbruch.
    """
    if not args.frisch and cache_frisch(pfad, ttl):
        return pfad.read_bytes()

    if args.offline:
        return pfad.read_bytes() if pfad.exists() else None

    kopfzeilen = {"User-Agent": UA, "Accept": "*/*"}
    if kopf:
        kopfzeilen.update(kopf)
    try:
        anfrage = urllib.request.Request(kodiere_url(url), headers=kopfzeilen)
        with urllib.request.urlopen(anfrage, timeout=20) as antwort:
            daten = antwort.read()
        if not daten:
            raise ValueError("leere Antwort")
        schreib_atomar(pfad, daten)
        return daten
    except Exception as fehler:
        if pfad.exists():
            warnung("%s — %s (nutze letzten Cache)" % (url, fehler))
            return pfad.read_bytes()
        warnung("%s — %s" % (url, fehler))
        return None


def schreib_atomar(pfad, daten):
    pfad.parent.mkdir(parents=True, exist_ok=True)
    tmp = pfad.with_suffix(pfad.suffix + ".tmp")
    tmp.write_bytes(daten)
    os.replace(tmp, pfad)


def warnung(text):
    print("  ! %s" % text, file=sys.stderr)


def info(text):
    print(text)


# --------------------------------------------------------------------------
# Feed lesen
# --------------------------------------------------------------------------

def dekodiere(daten):
    """Bytes zu Text, mit Ruecksicht auf die XML-Deklaration.

    ntv und Spiegel liefern teils kein UTF-8. Ohne diesen Schritt wird aus
    jedem Umlaut ein Fragezeichen.
    """
    kopf = daten[:400].decode("ascii", "ignore")
    treffer = re.search(r'encoding=["\']([\w-]+)["\']', kopf)
    kodierung = treffer.group(1) if treffer else "utf-8"
    try:
        return daten.decode(kodierung)
    except (LookupError, UnicodeDecodeError):
        return daten.decode("utf-8", "replace")


def repariere_xml(text):
    """Macht kaputtes Feed-XML parsebar.

    Der haeufigste Fehler sind unescapte '&' in Links und Titeln. ElementTree
    hat kein recover — ein einziger Fehler kostet sonst den ganzen Feed.
    """
    text = text.lstrip("﻿")
    # Steuerzeichen ausser Tab, Zeilenumbruch, Wagenruecklauf
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    # '&' das nicht schon eine Entitaet einleitet
    text = re.sub(r"&(?!(?:#\d+|#x[0-9a-fA-F]+|[a-zA-Z][\w.-]*);)", "&amp;", text)
    return text


def notfall_parser(text):
    """Regex-Rueckfall, wenn ElementTree komplett aufgibt.

    Ohne das kostet ein einziges kaputtes Element den ganzen Feed. Hier kommt
    wenigstens das Wesentliche heraus.
    """
    bloecke = re.findall(r"<item\b.*?</item>", text, re.S) or \
              re.findall(r"<entry\b.*?</entry>", text, re.S)

    def feld(block, name):
        m = re.search(r"<%s\b[^>]*>(.*?)</%s>" % (name, name), block, re.S)
        return m.group(1) if m else ""

    ergebnis = []
    for block in bloecke:
        link = feld(block, "link")
        if not link:
            m = re.search(r'<link\b[^>]*href="([^"]+)"', block)
            link = m.group(1) if m else ""
        ergebnis.append({
            "titel": nur_text(feld(block, "title")),
            "link": nur_text(link),
            "content_encoded": feld(block, "content:encoded") or feld(block, "content"),
            "description": feld(block, "description") or feld(block, "summary"),
            "datum_roh": feld(block, "pubDate") or feld(block, "published") or
                        feld(block, "updated") or feld(block, "dc:date"),
            "kategorien": re.findall(r"<category\b[^>]*>(.*?)</category>", block, re.S),
            "block": block,
        })
    return ergebnis


NS = {
    "media": "http://search.yahoo.com/mrss/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
}


def parse_feed(daten):
    text = repariere_xml(dekodiere(daten))
    try:
        wurzel = ET.fromstring(text)
    except ET.ParseError as fehler:
        warnung("XML kaputt (%s) — nutze Notfallparser" % fehler)
        return notfall_parser(text)

    eintraege = []
    for knoten in list(wurzel.iter("item")) + list(wurzel.iter("entry")):
        def text_von(name, ns=None):
            el = knoten.find(name, ns or {})
            if el is None:
                return ""
            return "".join(el.itertext())

        link = text_von("link")
        if not link:
            el = knoten.find("link")
            link = el.get("href", "") if el is not None else ""

        eintraege.append({
            "titel": nur_text(text_von("title")),
            "link": (link or "").strip(),
            # Beide Felder getrennt mitnehmen. Welches das bessere ist, zeigt
            # sich erst nach dem Saeubern — bei Positive News steckt im
            # content:encoded nur die Verlagszeile, im description der Text.
            "content_encoded": text_von("content:encoded", NS),
            "description": text_von("description", NS) or text_von("summary", NS),
            "datum_roh": text_von("pubDate") or text_von("published", NS) or
                        text_von("updated", NS) or text_von("dc:date", NS),
            "kategorien": [nur_text("".join(el.itertext()))
                           for el in knoten.findall("category")],
            "knoten": knoten,
        })
    return eintraege


UNBRAUCHBAR = re.compile(r"/icon|/logo|/sprite|1x1|pixel|spacer|blank\.|/avatar|"
                         r"placeholder|/ad[sx]?/|doubleclick|/badge", re.I)


def ist_brauchbares_bild(url):
    if not url or not url.startswith(("http://", "https://")):
        return False
    return not UNBRAUCHBAR.search(url)


def abs_url(url, basis):
    if not url:
        return ""
    url = html.unescape(url.strip().strip("'\""))
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if basis:
        return urllib.parse.urljoin(basis, url)
    return ""


def bild_aus_block(block_html, basis):
    """Erstes brauchbares <img> im HTML, groesste srcset-Variante bevorzugt."""
    for tag in re.findall(r"<img\b[^>]*>", block_html or "", re.I):
        quelle = ""
        srcset = re.search(r'srcset=["\']([^"\']+)["\']', tag, re.I)
        if srcset:
            kandidaten = []
            for teil in srcset.group(1).split(","):
                teil = teil.strip()
                if not teil:
                    continue
                teile = teil.split()
                mass = 0
                if len(teile) > 1:
                    m = re.search(r"(\d+)w", teile[1])
                    if m:
                        mass = int(m.group(1))
                kandidaten.append((mass, teile[0]))
            if kandidaten:
                kandidaten.sort()
                quelle = kandidaten[-1][1]
        if not quelle:
            m = re.search(r'\bsrc=["\']([^"\']+)["\']', tag, re.I)
            if m:
                quelle = m.group(1)
        if not quelle:
            m = re.search(r'\bdata-src=["\']([^"\']+)["\']', tag, re.I)
            if m:
                quelle = m.group(1)
        fertig = abs_url(quelle, basis)
        if ist_brauchbares_bild(fertig):
            return fertig
    return ""


def feed_bild(eintrag, basis):
    """Artikelbild aus enclosure, media:content, media:thumbnail oder <img>."""
    knoten = eintrag.get("knoten")

    if knoten is not None:
        for enc in knoten.findall("enclosure"):
            if (enc.get("type") or "").startswith("image"):
                url = abs_url(enc.get("url"), basis)
                if ist_brauchbares_bild(url):
                    return url

        for pfad in ("media:content", "media:thumbnail"):
            for el in knoten.findall(pfad, NS):
                art = (el.get("medium") or "") + " " + (el.get("type") or "")
                if "image" not in art.lower() and el.get("medium") != "image":
                    # media:content fuehrt auch Videos — ohne Bildhinweis nicht nehmen
                    if not re.search(r"\.(jpe?g|png|webp|gif)(\?|$)", el.get("url", ""), re.I):
                        continue
                url = abs_url(el.get("url"), basis)
                if ist_brauchbares_bild(url):
                    return url

    # Manche Feeds liefern Bilder nur im Item-Tag selbst
    if knoten is not None and eintrag.get("block") is None:
        roh = ET.tostring(knoten, encoding="unicode")
    else:
        roh = eintrag.get("block") or ""

    treffer = bild_aus_block(roh, basis)
    if treffer:
        return treffer

    return (bild_aus_block(eintrag.get("content_encoded") or "", basis)
            or bild_aus_block(eintrag.get("description") or "", basis))


def parse_datum(roh):
    """RFC822 und ISO8601 gemischt — beide Formen kommen in denselben Feeds vor."""
    if not roh:
        return None
    roh = roh.strip()
    try:
        d = parsedate_to_datetime(roh)
        if d:
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    sauber = roh.replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(sauber)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for muster in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(roh, muster).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def hole_quelle(quelle, args):
    """Eine Quelle komplett: holen, parsen, Bilder und Daten anreichern."""
    name = quelle["name"]
    url = quelle["url"]
    pfad = cache_pfad("feeds", url, ".xml")
    daten = hole(url, pfad, TTL_FEED, args)
    if not daten:
        return name, []

    try:
        rohe = parse_feed(daten)
    except Exception as fehler:
        warnung("%s: Feed nicht lesbar (%s)" % (name, fehler))
        return name, []

    artikel = []
    for roh in rohe:
        if not roh["titel"] or not roh["link"]:
            continue
        artikel.append({
            "titel": roh["titel"],
            "link": roh["link"],
            # Beide Textfelder saeubern und das ergiebigere nehmen. Bei
            # Positive News enthaelt content:encoded nur "The post ... appeared
            # first on ...", waehrend in description der echte Anriss steht —
            # wer hier blind das erste Feld nimmt, bekommt leere Karten.
            "beschreibung": max(
                saubre_text(nur_text(roh.get("content_encoded") or "")),
                saubre_text(nur_text(roh.get("description") or "")),
                key=len,
            ),
            "bild_feed": feed_bild(roh, roh["link"] or url),
            "datum": parse_datum(roh["datum_roh"]),
            "kategorien": roh["kategorien"],
            "quelle": name,
            "trust": quelle["trust"],
            "sprache": quelle.get("sprache", "de"),
        })
    return name, artikel


# --------------------------------------------------------------------------
# Der Filter
# --------------------------------------------------------------------------

def passt(eintrag, text):
    art, wert = eintrag
    if art == "stamm":
        return wert in text
    return re.search(wert, text) is not None


def treffer_positionen(eintrag, text):
    """Alle Fundstellen als (start, ende) — fuer den Negations-Waechter."""
    art, wert = eintrag
    if art == "stamm":
        stellen = []
        start = text.find(wert)
        while start != -1:
            stellen.append((start, start + len(wert)))
            start = text.find(wert, start + 1)
        return stellen
    return [(m.start(), m.end()) for m in re.finditer(wert, text)]


def negiert(text, position):
    """Steht im Fenster davor ein Negationswort?"""
    fenster = text[max(0, position - NEG_FENSTER):position]
    return NEGATION.search(fenster) is not None


def entschaerft(text, cfg):
    for eintrag in cfg["entschaerfer"]:
        if passt(eintrag, text):
            return True
    return False


def override_status(link, cfg):
    for u in cfg["force_drop"]:
        if match_override(u, link):
            return "drop"
    for u in cfg["force_keep"]:
        if match_override(u, link):
            return "keep"
    return None


def match_override(muster, link):
    muster = muster.strip()
    if muster.startswith("praefix:"):
        return link.startswith(muster[len("praefix:"):])
    return link.rstrip("/") == muster.rstrip("/")


def bewerte(artikel, cfg):
    """Rueckgabe: (punkte, verworfen, grund, spuren).

    punkte == -999 heisst: Endgueltig raus, ohne Schwellenpruefung.
    """
    spuren = []

    status = override_status(artikel["link"], cfg)
    if status == "drop":
        return -999, True, "override:force_drop", spuren
    if status == "keep":
        return 999, False, "override:force_keep", spuren

    titel = falte(artikel["titel"])
    beschr = falte(artikel["beschreibung"])
    gesamt = titel + "  ||  " + beschr

    # --- Serien und Fueller: gar keine Nachricht, sondern Rubrik ---
    for eintrag in cfg["serien"]:
        if passt(eintrag, titel):
            return -999, True, "serie:%s" % eintrag[1], spuren

    # --- Stufe A: Fehlschlag-Phrasen, vor allem anderen ---
    for eintrag in cfg["fehlschlag"]:
        if passt(eintrag, gesamt):
            return -999, True, "fehlschlag:%s" % eintrag[1], spuren

    # --- Stufe B: harte Sperre, mit Negations-Waechter und Entschaerfern ---
    weich = entschaerft(gesamt, cfg)
    for eintrag in cfg["hart"]:
        for start, _ in treffer_positionen(eintrag, gesamt):
            if negiert(gesamt, start):
                continue
            if weich:
                spuren.append("hart-entschaerft:%s" % eintrag[1])
                continue
            return -999, True, "hart:%s" % eintrag[1], spuren

    # --- Stufe C: Punktwertung ---
    punkte = 0

    # Objektregeln zuerst — die erste passende gewinnt.
    for regel in cfg["objekt"]:
        if regel["muster"].search(gesamt):
            if regel["punkte"] <= -200:
                return -999, True, "objekt:%s" % regel["name"], spuren
            punkte += regel["punkte"]
            spuren.append("objekt:%s(%+d)" % (regel["name"], regel["punkte"]))
            break

    # Positivsignale zaehlen im Titel doppelt. Das ist die Bremse gegen
    # Clickbait: ein positiv klingender Titel mit negativem Inhalt wird von
    # der Beschreibung nach unten gezogen.
    for eintrag in cfg["positiv"]:
        if passt(eintrag, titel):
            punkte += 2
            spuren.append("positiv-titel:%s" % eintrag[1])
        elif passt(eintrag, beschr):
            punkte += 1
            spuren.append("positiv-text:%s" % eintrag[1])

    for eintrag in cfg["kontext"]:
        if passt(eintrag, titel):
            punkte -= 2
            spuren.append("kontext-titel:%s" % eintrag[1])
        elif passt(eintrag, beschr):
            punkte -= 1
            spuren.append("kontext-text:%s" % eintrag[1])

    thema = thema_von(artikel, cfg)
    if thema_bonus(artikel):
        punkte += 1
        spuren.append("themen-bonus:+1")

    return punkte, False, "", spuren


KATEGORIE_BONUS = re.compile(
    r"^(sport|wissen|wissenschaft|kultur|ratgeber|unterhaltung|netzwelt|"
    r"gesundheit|umwelt|klima|digital|auto|reise|familie|bildung)$", re.I)


def thema_bonus(artikel):
    for kat in artikel.get("kategorien", []):
        if KATEGORIE_BONUS.match(kat.strip()):
            return True
    return False


def thema_von(artikel, cfg=None):
    text = falte(artikel["titel"] + " " + artikel["beschreibung"][:400])
    for name, muster in THEMEN:
        if re.search(muster, text):
            return name
    return "Gesellschaft"


def schwelle_fuer(artikel, cfg):
    if artikel["trust"] == "kuratiert":
        return cfg["schwelle_kuratiert"]
    return cfg["schwelle_allgemein"]


# --------------------------------------------------------------------------
# Deduplizierung
# --------------------------------------------------------------------------

def fingerabdruck(titel, stopp):
    woerter = re.findall(r"[a-zäöüß0-9]+", falte(titel))
    return {w for w in woerter if w not in stopp and len(w) > 3}


def aehnlich(a, b, stopp):
    A = fingerabdruck(a, stopp)
    B = fingerabdruck(b, stopp)
    if not A or not B:
        return False
    jaccard = len(A & B) / len(A | B)
    if jaccard > 0.5:
        return True
    # Zweites Netz fuer umformulierte Titel. Bewusst locker — im Zweifel lieber
    # zwei aehnliche Karten zeigen als eine echte Geschichte schlucken.
    return difflib.SequenceMatcher(None, falte(a), falte(b)).ratio() > 0.78


def kanonisch(link):
    zerteilt = urllib.parse.urlsplit(link)
    host = zerteilt.netloc.lower().replace("www.", "")
    pfad = zerteilt.path.rstrip("/")
    return "%s%s" % (host, pfad)


def entdopple(artikel, stopp):
    nach_zeit = sorted(
        artikel,
        key=lambda a: a["datum"] or datetime(1970, 1, 1, tzinfo=timezone.utc),
        reverse=True,
    )
    behalten = []
    gesehen = {}
    for kand in nach_zeit:
        kan = kanonisch(kand["link"])
        if kan in gesehen:
            gesehen[kan]["auch_bei"].append(kand["quelle"])
            continue
        # Nur gegen die letzten 72 h vergleichen — sonst wird es quadratisch.
        zwilling = None
        grenze = (kand["datum"] or datetime.now(timezone.utc)) - timedelta(hours=72)
        for bisher in behalten:
            if bisher["datum"] and bisher["datum"] < grenze:
                continue
            if aehnlich(kand["titel"], bisher["titel"], stopp):
                zwilling = bisher
                break
        if zwilling is not None:
            zwilling["auch_bei"].append(kand["quelle"])
            continue
        kand["auch_bei"] = []
        behalten.append(kand)
        gesehen[kan] = kand
    return behalten


# --------------------------------------------------------------------------
# Bilder
# --------------------------------------------------------------------------

def bild_masse(pfad):
    """Breite/Hoehe aus dem Dateikopf lesen. Kein Pillow verfuegbar."""
    try:
        with open(pfad, "rb") as datei:
            kopf = datei.read(32)
            if kopf[:8] == b"\x89PNG\r\n\x1a\n":
                b, h = struct.unpack(">II", kopf[16:24])
                return b, h
            if kopf[:3] == b"GIF":
                b, h = struct.unpack("<HH", kopf[6:10])
                return b, h
            if kopf[:2] == b"\xff\xd8":
                datei.seek(2)
                while True:
                    byte = datei.read(1)
                    if not byte:
                        return 0, 0
                    if byte != b"\xff":
                        continue
                    marker = datei.read(1)
                    while marker == b"\xff":
                        marker = datei.read(1)
                    if not marker:
                        return 0, 0
                    if marker[0] in (0xD8, 0xD9) or 0xD0 <= marker[0] <= 0xD7:
                        continue
                    laenge_roh = datei.read(2)
                    if len(laenge_roh) < 2:
                        return 0, 0
                    laenge = struct.unpack(">H", laenge_roh)[0]
                    if marker[0] in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6,
                                     0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                        daten = datei.read(5)
                        if len(daten) < 5:
                            return 0, 0
                        h, b = struct.unpack(">HH", daten[1:5])
                        return b, h
                    datei.seek(laenge - 2, 1)
            if kopf[:4] == b"RIFF" and kopf[8:12] == b"WEBP":
                datei.seek(12)
                vier = datei.read(4)
                if vier == b"VP8X":
                    datei.read(4)
                    b = int.from_bytes(datei.read(3), "little") + 1
                    h = int.from_bytes(datei.read(3), "little") + 1
                    return b, h
                if vier == b"VP8 ":
                    datei.read(6)
                    rohdaten = datei.read(4)
                    if len(rohdaten) == 4:
                        b, h = struct.unpack("<HH", rohdaten)
                        return b & 0x3FFF, h & 0x3FFF
    except Exception:
        pass
    return 0, 0


def suchbegriffe(titel, stopp):
    """Suchwoerter fuer die Bildsuche aus dem Titel ziehen.

    Die deutsche Substantiv-Grossschreibung ist der kostenlose POS-Tagger:
    grossgeschriebene Woerter sind mit hoher Wahrscheinlichkeit das Motiv.
    """
    woerter = re.findall(r"[A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß\-]{2,}", titel or "")
    kand = [w for w in woerter if w.casefold() not in stopp]
    substantiv = [w for w in kand if w[:1].isupper() and len(w) > 3]
    auswahl = substantiv if substantiv else sorted(kand, key=len, reverse=True)
    return " ".join(auswahl[:2])


def openverse_suche(begriff, args):
    """Kandidaten in Wunschreihenfolge — nicht nur der beste.

    Frueher kam hier nur treffer[0] zurueck. Das raechte sich: dieselbe Suche
    liefert bei jedem Aufruf dieselbe Antwort, also bekam jedes Artikel desselben
    Themas dasselbe Foto. Auf der Seite stand viermal dasselbe Sonnenblumenfeld.
    Der Aufrufer geht die Liste jetzt durch und ueberspringt, was schon
    vergeben ist.
    """
    if not begriff:
        return []
    params = urllib.parse.urlencode({
        "q": begriff,
        "license_type": "commercial,modification",
        "page_size": "8",
    })
    url = OPENVERSE + "?" + params
    pfad = cache_pfad("openverse", url, ".json")
    daten = hole(url, pfad, TTL_OPENVERSE, args)
    if not daten:
        return []
    try:
        antwort = json.loads(daten.decode("utf-8", "replace"))
    except ValueError:
        return []

    rang = {"cc0": 0, "pdm": 1, "by": 2, "by-sa": 3}

    def passt_form(r):
        """Querformat bevorzugen.

        Die Karte zeigt einen 3:2-Rahmen mit object-fit:cover. Ein Hochformat
        wird dabei auf einen schmalen Streifen beschnitten — bei einem Portraet
        faellt dann regelmaessig der Kopf weg.
        """
        b, h = r.get("width") or 0, r.get("height") or 0
        if b < 700 or h < 400:
            return False
        return 1.2 <= (b / h) <= 2.2

    treffer = [r for r in antwort.get("results", [])
               if r.get("license") in rang and passt_form(r)]
    if not treffer:
        # Zweiter Versuch ohne Formvorgabe, aber weiterhin mit Mindestgroesse.
        treffer = [r for r in antwort.get("results", [])
                   if r.get("license") in rang and (r.get("width") or 0) >= 700]
    # Beste Lizenz zuerst, dann die groesste Flaeche.
    treffer.sort(key=lambda r: (rang[r["license"]],
                                -((r.get("width") or 0) * (r.get("height") or 0))))
    return treffer


def bild_herunterladen(url, args, ordner=BILDER_CC):
    ordner.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
    for endung in (".jpg", ".png", ".webp", ".gif"):
        vorhanden = ordner / (h + endung)
        if vorhanden.exists() and vorhanden.stat().st_size > 4096:
            verkleinere(vorhanden)
            return vorhanden

    if args.offline:
        return None

    try:
        anfrage = urllib.request.Request(kodiere_url(url), headers={"User-Agent": UA})
        with urllib.request.urlopen(anfrage, timeout=25) as antwort:
            art = antwort.headers.get("Content-Type", "")
            if not art.startswith("image/"):
                return None
            daten = antwort.read(8 * 1024 * 1024 + 1)
    except Exception as fehler:
        warnung("Bild nicht ladbar (%s): %s" % (fehler, url[:70]))
        return None

    if len(daten) < 5120 or len(daten) > 8 * 1024 * 1024:
        return None

    if b"\xff\xd8" in daten[:4] or daten[6:10] == b"JFIF":
        endung = ".jpg"
    elif daten[:8] == b"\x89PNG\r\n\x1a\n":
        endung = ".png"
    elif daten[:4] == b"RIFF" and daten[8:12] == b"WEBP":
        endung = ".webp"
    elif daten[:3] == b"GIF":
        endung = ".gif"
    else:
        return None

    ziel = ordner / (h + endung)
    schreib_atomar(ziel, daten)
    verkleinere(ziel)
    return ziel


MAX_BILD_BREITE = 1200
MAX_BILD_BYTES = 300 * 1024
MARKE_OPTIMIERT = CACHE / "bilder-optimiert.txt"


def schon_optimiert():
    """Namen der Bilder, die bereits verkleinert wurden.

    Ohne diese Liste waere die Groessenschwelle unten kein verlaesslicher
    Indikator: ein sehr detailreiches Foto bleibt auch nach q78 ueber 300 KB
    und wuerde dann bei *jedem* Build erneut komprimiert — jedes Mal ein
    Stueck schlechter. Das ist kein theoretischer Fall, das ist beim Test
    sofort passiert.
    """
    try:
        return set(MARKE_OPTIMIERT.read_text(encoding="utf-8").split())
    except OSError:
        return set()


def merke_optimiert(name):
    CACHE.mkdir(parents=True, exist_ok=True)
    try:
        with MARKE_OPTIMIERT.open("a", encoding="utf-8") as datei:
            datei.write(name + "\n")
    except OSError:
        pass


def verkleinere(pfad):
    """Bild auf eine vernuenftige Groesse bringen — Breite UND Dateigroesse.

    Die Karten zeigen rund 340 px. Ein 1920-px-Original mit 1,2 MB ist reine
    Verschwendung; bei drei Dutzend Bildern summiert sich das auf Megabyte,
    und die Seite soll schlank bleiben.

    Zwei Faelle, die beide vorkommen:
      * zu breit  -> auf MAX_BILD_BREITE skalieren
      * schon schmal, aber schlecht komprimiert -> nur neu komprimieren

    Der zweite Fall ist der gemeine. Ein 1000-px-JPEG mit 1,2 MB ist nicht zu
    gross, sondern falsch gespeichert. Ein reines sips -Z haette es sogar auf
    1200 px *hochskaliert* und dabei kein Byte gespart — genau das ist beim
    ersten Versuch passiert. Erst das Neukomprimieren bringt es auf ein
    Fuenftel.

    Damit der Aufruf wiederholbar ist, merkt sich der Build jedes bearbeitete
    Bild in .cache/bilder-optimiert.txt. Ein zweiter Lauf fasst es dann nicht
    mehr an. Die Dateigroesse allein taugt als Kriterium nicht — siehe
    schon_optimiert().

    Gebraucht wird ein Bildwerkzeug. sips gehoert zu macOS und ist dort immer
    da; auf Linux (etwa dem Runner des stuendlichen Builds) uebernimmt
    ImageMagick. Fehlt beides, bleibt das Bild wie es ist — kein Fehler, nur
    unnoetig gross.
    """
    werkzeug, programm = bildwerkzeug()
    if werkzeug is None:
        return
    if pfad.name in schon_optimiert():
        return

    breite, hoehe = bild_masse(pfad)
    zu_breit = breite > MAX_BILD_BREITE
    zu_schwer = pfad.stat().st_size > MAX_BILD_BYTES
    if not (zu_breit or zu_schwer):
        return

    ist_jpeg = pfad.suffix.lower() in (".jpg", ".jpeg")
    if not zu_breit and not ist_jpeg:
        # Zu schwer, aber weder zu breit noch ein JPEG — etwa ein grosses PNG.
        # Umkodieren wuerde hier Transparenz kosten, das ist es nicht wert.
        return

    try:
        if werkzeug == "sips":
            befehl = [programm]
            if zu_breit:
                befehl += ["-Z", str(MAX_BILD_BREITE)]
            if ist_jpeg:
                befehl += ["-s", "format", "jpeg", "-s", "formatOptions", "78"]
            befehl.append(str(pfad))
            subprocess.run(befehl, check=True, capture_output=True, timeout=60)
        else:
            # ImageMagick kann nicht in dieselbe Datei schreiben, aus der es
            # liest. Der Umweg liegt im Cache, nicht in img/ — sonst waere er
            # einen Wimpernschlag lang Teil der Veroeffentlichung.
            CACHE.mkdir(parents=True, exist_ok=True)
            umweg = CACHE / ("umweg-" + pfad.name)
            befehl = [programm, str(pfad)]
            if zu_breit:
                # Das ">" heisst: nur verkleinern, nie vergroessern.
                befehl += ["-resize", "%dx>" % MAX_BILD_BREITE]
            if ist_jpeg:
                befehl += ["-quality", "78"]
            befehl.append(str(umweg))
            subprocess.run(befehl, check=True, capture_output=True, timeout=60)
            os.replace(umweg, pfad)
    except (subprocess.SubprocessError, OSError):
        return
    merke_optimiert(pfad.name)


def bildwerkzeug():
    """Erstes brauchbares Bildwerkzeug: ('sips', Pfad) oder ('magick', Pfad).

    Zwei Werkzeuge statt einem, weil der stuendliche Build auf Linux laeuft und
    dort kein sips existiert. Ohne diesen Zweig wuerde der Runner neue Bilder
    unkomprimiert ins Repository schreiben — ein paar hundert Kilobyte pro
    Stunde, die sich ansammeln.
    """
    sips = shutil.which("sips")
    if sips:
        return "sips", sips
    for name in ("magick", "convert"):
        pfad = shutil.which(name)
        if pfad:
            return "magick", pfad
    return None, None


def raeume_bilder_auf(artikel):
    """Loescht Bilder, die keine Karte mehr benutzt.

    Ohne das waechst img/cc/ unbegrenzt: die Seite zeigt 32 Artikel, aber jeder
    Lauf laedt fuer neue Artikel neue Bilder herunter, ohne die alten zu
    entfernen. Stuendlich gerechnet sind das schnell einige Megabyte am Tag,
    und alles davon landet im Repository, weil die Bilder mit veroeffentlicht
    werden muessen.

    Nur img/cc/ wird angefasst. Kacheln und Favicon liegen in img/, Pressefotos
    in img/presse/ — beide bleiben unberuehrt.
    """
    benutzt = set()
    for art in artikel:
        pfad = (art.get("bild") or {}).get("pfad") or ""
        if pfad.startswith("cc/"):
            benutzt.add(pfad.split("/")[-1])

    if not BILDER_CC.exists():
        return 0

    entfernt = 0
    for datei in BILDER_CC.iterdir():
        if not datei.is_file() or datei.name in benutzt:
            continue
        try:
            datei.unlink()
            entfernt += 1
        except OSError:
            pass

    # Die Markierung muss mit aufgeraeumt werden. Sonst gilt ein geloeschtes
    # Bild beim naechsten Mal noch als "schon verkleinert" — und die dann frisch
    # heruntergeladene Rohdatei bliebe unkomprimiert liegen.
    if entfernt:
        rest = sorted(n for n in schon_optimiert() if n in benutzt)
        try:
            MARKE_OPTIMIERT.write_text("\n".join(rest) + ("\n" if rest else ""),
                                       encoding="utf-8")
        except OSError:
            pass
    return entfernt


def freies_bild(artikel, args):
    """Stufe 2: CC-Bild ueber Openverse.

    Nur genommen, wenn der Treffer-Titel mindestens ein Suchwort enthaelt —
    ein unpassendes Stockfoto ist schlechter als eine Kachel. Innerhalb einer
    Suche wird kein Bild zweimal vergeben (siehe args.belegte_bilder): dieselbe
    Suche liefert ja dieselbe Antwort, und ohne diese Sperre stand viermal
    dasselbe Sonnenblumenfeld auf der Seite.
    """
    stopp = args.cfg["stopp"]
    aus_titel = False
    if artikel.get("sprache") == "de":
        # Bei deutschen Artikeln braechte die Suche mit deutschen Substantiven
        # nichts: gefunden werden fast nur englisch betitelte CC-Bilder, und
        # die Relevanzpruefung unten wuerde jeden Treffer verwerfen. Also
        # gleich ein englisches Stimmungsmotiv zum Thema suchen.
        begriffe = BILD_MOOD.get(artikel["thema"], MOOD_ERSATZ)
    else:
        eigen = suchbegriffe(artikel["titel"], stopp)
        aus_titel = bool(eigen)
        begriffe = (eigen,) if eigen else BILD_MOOD.get(artikel["thema"], MOOD_ERSATZ)

    belegt = args.belegte_bilder
    for begriff in begriffe:
        for treffer in openverse_suche(begriff, args):
            quelle = treffer.get("thumbnail") or treffer.get("url")
            if not quelle or quelle in belegt:
                continue

            # Relevanzpruefung — aber nur bei Suchbegriffen aus dem Titel. Dort
            # ist ein thematisch danebenliegendes Foto wahrscheinlich, und eine
            # saubere Kachel ist dann die bessere Wahl. Ein Stimmungsmotiv
            # dagegen ist absichtlich unspezifisch und wird als Symbolbild
            # ausgewiesen; hier zaehlt die Relevanzsortierung von Openverse.
            if aus_titel:
                suchwoerter = [w.casefold() for w in begriff.split() if len(w) > 3]
                treffer_titel = falte(treffer.get("title") or "")
                if suchwoerter and not any(w in treffer_titel for w in suchwoerter):
                    continue

            pfad = bild_herunterladen(quelle, args)
            if pfad is None:
                continue

            breite, hoehe = bild_masse(pfad)
            if breite < 300 or hoehe < 200:
                continue

            belegt.add(quelle)
            return {
                "pfad": "%s/%s" % (pfad.parent.name, pfad.name),
                "breite": breite,
                "hoehe": hoehe,
                "urheber": treffer.get("creator") or "",
                "urheber_url": treffer.get("creator_url") or "",
                "lizenz": (treffer.get("license") or "").upper(),
                "lizenz_version": treffer.get("license_version") or "",
                "lizenz_url": treffer.get("license_url") or "",
                "original": treffer.get("foreign_landing_url") or "",
            }

    # Alle Kandidaten vergeben oder unbrauchbar — dann lieber eine Kachel als
    # dasselbe Foto ein fuenftes Mal.
    return None


def bild_fuer(artikel, args):
    """Drei Stufen: Feed-Bild, CC-Bild, lokale Kachel."""
    if not args.nur_cc_bilder and artikel.get("bild_feed"):
        pfad = bild_herunterladen(artikel["bild_feed"], args, BILDER_PRESSE)
        if pfad is not None:
            breite, hoehe = bild_masse(pfad)
            # 400 px Breite, weil die Karte etwa 340 px breit zeigt. Kleineres
            # Material wird hochskaliert und wirkt unscharf — dann ist eine
            # Kachel oder ein freies Bild die bessere Wahl.
            if breite >= 400 and hoehe >= 250:
                return {
                    "pfad": "%s/%s" % (pfad.parent.name, pfad.name),
                    "breite": breite,
                    "hoehe": hoehe,
                    "presse": True,
                    "urheber": "",
                    "lizenz": "",
                }

    frei = freies_bild(artikel, args)
    if frei is not None:
        return frei

    # Stufe 3: lokale Kachel. Die Seite hat so nie ein Loch — und offline
    # funktioniert sie ohnehin nur mit lokalen Bildern.
    return {
        "pfad": "kachel-%s.svg" % artikel["thema_slug"],
        "breite": 800,
        "hoehe": 500,
        "kachel": True,
        "urheber": "",
        "lizenz": "",
    }


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

def themen_slug(theme):
    ersetzungen = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "&": "", " ": "-"}
    slug = falte(theme)
    for von, nach in ersetzungen.items():
        slug = slug.replace(von, nach)
    slug = re.sub(r"[^a-z0-9-]+", "", slug).strip("-")
    # "Tiere & Natur" wird nach dem Entfernen des & zu "tiere--natur". Ohne
    # diese Zeile hiesse die Kachel anders als die Datei in img/ — und jedes
    # Bild waere ein 404.
    return re.sub(r"-{2,}", "-", slug)


def datum_lokal(datum):
    if datum is None:
        return None
    return datum.astimezone()


def gruppe_von(datum):
    if datum is None:
        return "Älter"
    lokal = datum_lokal(datum)
    heute = datetime.now().astimezone().date()
    tage = (heute - lokal.date()).days
    if tage <= 0:
        return "Heute"
    if tage == 1:
        return "Gestern"
    if tage <= 7:
        return "Diese Woche"
    return "Älter"


def datum_text(datum):
    if datum is None:
        return ""
    lokal = datum_lokal(datum)
    monate = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
              "August", "September", "Oktober", "November", "Dezember"]
    return "%d. %s %d" % (lokal.day, monate[lokal.month - 1], lokal.year)


def esc(text):
    return html.escape(text or "", quote=True)


def karte_html(artikel, nummer):
    bild = artikel["bild"]
    if bild.get("kachel"):
        bild_html = (
            '<img class="karte__bild" src="img/%s" alt="" width="%d" height="%d" '
            'loading="%s" decoding="async">'
            % (esc(bild["pfad"]), bild["breite"], bild["hoehe"],
               "eager" if nummer < 3 else "lazy")
        )
    else:
        bild_html = (
            '<img class="karte__bild" src="img/%s" alt="%s" width="%d" height="%d" '
            'loading="%s" decoding="async">'
            % (esc(bild["pfad"]), esc(artikel["bild_alt"]), bild["breite"], bild["hoehe"],
               "eager" if nummer < 3 else "lazy")
        )

    # Bildnachweis: bei CC ist die Namensnennung Pflicht, nicht Kuer.
    nachweis = ""
    if bild.get("kachel"):
        nachweis = '<span class="karte__nachweis">Illustration</span>'
    elif bild.get("presse"):
        nachweis = ('<span class="karte__nachweis">Bild: %s</span>'
                    % esc(artikel["quelle"]))
    elif bild.get("urheber"):
        lizenz = esc(bild.get("lizenz", ""))
        if bild.get("lizenz_version"):
            lizenz += " " + esc(bild["lizenz_version"])
        urheber = esc(bild["urheber"])
        if bild.get("urheber_url"):
            urheber = '<a href="%s" rel="noopener">%s</a>' % (esc(bild["urheber_url"]), urheber)
        lizenz_txt = lizenz
        if bild.get("lizenz_url"):
            lizenz_txt = '<a href="%s" rel="noopener">%s</a>' % (esc(bild["lizenz_url"]), lizenz)
        nachweis = '<span class="karte__nachweis">Bild: %s · %s</span>' % (urheber, lizenz_txt)

    weitere = ""
    if artikel.get("auch_bei"):
        quellen = sorted(set(artikel["auch_bei"]))
        weitere = ('<span class="karte__weitere">auch bei: %s</span>'
                   % esc(", ".join(quellen)))

    teaser = artikel.get("teaser") or ""
    teaser_html = ('<p class="karte__teaser">%s</p>' % esc(teaser)) if teaser else ""

    sprache = ('<span class="karte__sprache" title="Sprache des Originals">%s</span>'
               % esc(artikel["sprache"].upper()))

    return """
        <article class="karte" data-thema="%(slug)s" data-sprache="%(sprache)s">
          <div class="karte__bildrahmen">%(bild)s</div>
          <div class="karte__inhalt">
            <p class="karte__kopf">
              <span class="karte__thema">%(thema)s</span>
              %(sprache_badge)s
            </p>
            <h3 class="karte__titel">
              <a href="%(link)s" target="_blank" rel="noopener">%(titel)s</a>
            </h3>
            %(teaser)s
            <p class="karte__fuss">
              <span class="karte__quelle">Quelle: <strong>%(quelle)s</strong>%(datum)s</span>
              %(nachweis)s
              %(weitere)s
            </p>
          </div>
        </article>""" % {
        "slug": esc(artikel["thema_slug"]),
        "sprache": esc(artikel["sprache"]),
        "bild": bild_html,
        "thema": esc(artikel["thema"]),
        "sprache_badge": sprache,
        "link": esc(artikel["link"]),
        "titel": esc(artikel["titel"]),
        "teaser": teaser_html,
        "quelle": esc(artikel["quelle"]),
        "datum": (" · " + esc(datum_text(artikel["datum"]))) if artikel["datum"] else "",
        "nachweis": nachweis,
        "weitere": weitere,
    }


def baue_html(artikel, cfg, statistik, nur_cc_bilder=False):
    gruppen_reihenfolge = ["Heute", "Gestern", "Diese Woche", "Älter"]
    nach_gruppe = {}
    for art in artikel:
        nach_gruppe.setdefault(gruppe_von(art["datum"]), []).append(art)

    bloecke = []
    nummer = 0
    for gruppe in gruppen_reihenfolge:
        eintraege = nach_gruppe.get(gruppe)
        if not eintraege:
            continue
        karten = []
        for art in eintraege:
            karten.append(karte_html(art, nummer))
            nummer += 1
        bloecke.append(
            '      <section class="gruppe" aria-labelledby="gruppe-%s">\n'
            '        <h2 class="gruppe__titel" id="gruppe-%s">%s'
            '<span class="gruppe__zahl">%d</span></h2>\n'
            '        <div class="raster">%s\n        </div>\n'
            '      </section>' % (
                themen_slug(gruppe), themen_slug(gruppe), esc(gruppe),
                len(eintraege), "".join(karten))
        )

    themen_gezaehlt = {}
    for art in artikel:
        themen_gezaehlt[art["thema"]] = themen_gezaehlt.get(art["thema"], 0) + 1

    chips = ['<button class="chip is-aktiv" type="button" data-filter="alle" '
             'aria-pressed="true">Alle<span class="chip__zahl">%d</span></button>'
             % len(artikel)]
    for name, _ in THEMEN:
        anzahl = themen_gezaehlt.get(name)
        if not anzahl:
            continue
        chips.append(
            '<button class="chip" type="button" data-filter="%s" aria-pressed="false">'
            '%s<span class="chip__zahl">%d</span></button>'
            % (esc(themen_slug(name)), esc(name), anzahl))
    anzahl_gesellschaft = themen_gezaehlt.get("Gesellschaft")
    if anzahl_gesellschaft:
        chips.append(
            '<button class="chip" type="button" data-filter="gesellschaft" '
            'aria-pressed="false">Gesellschaft<span class="chip__zahl">%d</span></button>'
            % anzahl_gesellschaft)

    quellen = sorted({art["quelle"] for art in artikel})

    vorlage = VORLAGE.read_text(encoding="utf-8")
    ersetzungen = {
        "<!--ARTIKEL-->": "\n".join(bloecke),
        "<!--CHIPS-->": "\n          ".join(chips),
        "<!--ANZAHL-->": str(len(artikel)),
        "<!--QUELLENZAHL-->": str(len(quellen)),
        "<!--QUELLEN-->": esc(", ".join(quellen)),
        "<!--STAND-->": datetime.now().astimezone().strftime("%d.%m.%Y um %H:%M Uhr"),
        "<!--GEPRUEFT-->": "{:,}".format(statistik["geprueft"]).replace(",", "."),
        "<!--VERWORFEN-->": "{:,}".format(statistik["verworfen"]).replace(",", "."),
        "<!--BILDMODUS-->": (
            " Diese Ausgabe nutzt ausschließlich frei lizenzierte Bilder und"
            " eigene Illustrationen — keine Pressefotos."
            if nur_cc_bilder else
            " Artikelbilder stammen aus den Feeds der genannten Quellen; für"
            " eine Veröffentlichung ist der Modus ohne Pressefotos gedacht."
        ),
        "<!--QUELLEN_GESAMT-->": str(statistik["quellen_ok"]),
        "<!--QUELLEN_ERWARTET-->": str(statistik["quellen_gesamt"]),
    }
    for marker, wert in ersetzungen.items():
        vorlage = vorlage.replace(marker, wert)
    return vorlage


# --------------------------------------------------------------------------
# Hauptlauf
# --------------------------------------------------------------------------

def main():
    zerleger = argparse.ArgumentParser(
        description="PositiveDinge — baut index.html aus guten Nachrichten.")
    zerleger.add_argument("--frisch", action="store_true",
                          help="Cache ignorieren, alles neu holen")
    zerleger.add_argument("--offline", action="store_true",
                          help="nie ins Netz, nur aus dem Cache")
    zerleger.add_argument("--nur-cc-bilder", action="store_true",
                          help="keine Pressefotos, nur frei lizenzierte Bilder")
    zerleger.add_argument("--aufraeumen", action="store_true",
                          help="Bilder loeschen, die keine Karte mehr benutzt")
    zerleger.add_argument("--strict", action="store_true",
                          help="Exit 1, wenn eine Quelle nicht erreichbar war")
    args = zerleger.parse_args()

    try:
        cfg = lade_config()
    except (OSError, ValueError) as fehler:
        print("Konfiguration nicht lesbar: %s" % fehler, file=sys.stderr)
        return 2
    args.cfg = cfg
    # URLs der schon vergebenen Bilder. Ohne diese Sperre bekaeme jeder Artikel
    # desselben Themas dasselbe Foto — dieselbe Suche liefert ja dieselbe
    # Antwort. Bilder werden unten nacheinander zugeteilt, deshalb genuegt ein
    # einfaches Set ohne Sperre.
    args.belegte_bilder = set()

    info("PositiveDinge — Build")
    info("  Quellen: %d aktiv%s" % (len(cfg["quellen"]),
                                    ", nur freie Bilder" if args.nur_cc_bilder else ""))

    # --- Feeds holen, parallel: die Summe der Abrufe waere sonst spuerbar ---
    alle = []
    erfolgreich = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for name, artikel in pool.map(lambda q: hole_quelle(q, args), cfg["quellen"]):
            if artikel:
                erfolgreich.append(name)
                alle.extend(artikel)
            else:
                warnung("%s: keine Artikel" % name)

    info("  Quellen erreichbar: %d/%d" % (len(erfolgreich), len(cfg["quellen"])))

    if not alle:
        print("\nKeine Artikel geholt — index.html bleibt unveraendert.", file=sys.stderr)
        print("Laeuft der Build zum ersten Mal und ohne Netz?", file=sys.stderr)
        return 1

    # --- Filtern ---
    gut = []
    audit = []
    for art in alle:
        punkte, verworfen, grund, spuren = bewerte(art, cfg)
        schwelle = schwelle_fuer(art, cfg)
        if not verworfen and punkte < schwelle:
            verworfen, grund = True, "schwelle:%d<%d" % (punkte, schwelle)
        audit.append({
            "titel": art["titel"],
            "quelle": art["quelle"],
            "trust": art["trust"],
            "link": art["link"],
            "punkte": punkte,
            "schwelle": schwelle,
            "behalten": not verworfen,
            "grund": grund,
            "spuren": spuren[:40],
        })
        if not verworfen:
            gut.append(art)

    info("  Geprueft: %d   behalten: %d   verworfen: %d"
         % (len(alle), len(gut), len(alle) - len(gut)))

    # --- Entdoppeln ---
    gut = entdopple(gut, cfg["stopp"])
    info("  nach Entdopplung: %d" % len(gut))

    # --- Quellen-Diversitaet: sonst fluten ntv und Spiegel alles ---
    begrenzt = []
    gezaehlt = {}
    sortiert = sorted(
        gut,
        key=lambda a: (a["datum"] or datetime(1970, 1, 1, tzinfo=timezone.utc)),
        reverse=True,
    )
    for art in sortiert:
        if len(begrenzt) >= MAX_ARTIKEL:
            break
        n = gezaehlt.get(art["quelle"], 0)
        if n >= cfg["max_pro_quelle"]:
            continue
        gezaehlt[art["quelle"]] = n + 1
        begrenzt.append(art)

    if not begrenzt:
        print("\nFilter hat alles verworfen — index.html bleibt unveraendert.",
              file=sys.stderr)
        print("Ein Blick in .cache/filter-report.json zeigt, warum.", file=sys.stderr)
        schreibe_audit(audit)
        return 1

    # --- Bilder und Themen ---
    for i, art in enumerate(begrenzt):
        art["thema"] = thema_von(art)
        art["thema_slug"] = themen_slug(art["thema"])
        art["bild"] = bild_fuer(art, args)
        art["teaser"] = kuerze(art["beschreibung"], 210)
        art["bild_alt"] = ("Symbolbild zum Thema %s" % art["thema"])
        if (i + 1) % 8 == 0:
            info("  Bilder: %d/%d" % (i + 1, len(begrenzt)))

    kacheln = sum(1 for a in begrenzt if a["bild"].get("kachel"))
    presse = sum(1 for a in begrenzt if a["bild"].get("presse"))
    frei = len(begrenzt) - kacheln - presse
    info("  Bilder: %d Presse, %d frei lizenziert, %d Kacheln" % (presse, frei, kacheln))

    statistik = {
        "geprueft": len(alle),
        "verworfen": len(alle) - len(gut),
        "quellen_ok": len(erfolgreich),
        "quellen_gesamt": len(cfg["quellen"]),
    }
    seite = baue_html(begrenzt, cfg, statistik, args.nur_cc_bilder)

    # Atomar schreiben: kein halbfertiges index.html, wenn etwas abbricht
    tmp = ZIEL.with_suffix(".html.tmp")
    tmp.write_text(seite, encoding="utf-8")
    os.replace(tmp, ZIEL)

    schreibe_audit(audit)

    info("  index.html geschrieben (%d Artikel, %.0f KB)"
         % (len(begrenzt), len(seite.encode("utf-8")) / 1024))
    info("  Audit-Log: .cache/filter-report.json")

    # Erst nach dem Schreiben: was keine Karte mehr benutzt, kann weg. Vorher
    # aufzuraeumen wuerde bei einem Abbruch Bilder loeschen, die die noch
    # stehende alte Seite braucht.
    if args.aufraeumen:
        weg = raeume_bilder_auf(begrenzt)
        info("  Aufgeraeumt: %d nicht mehr benutzte Bilder entfernt" % weg)

    if args.strict and len(erfolgreich) < len(cfg["quellen"]):
        fehlend = [q["name"] for q in cfg["quellen"] if q["name"] not in erfolgreich]
        print("Fehlende Quellen: %s" % ", ".join(fehlend), file=sys.stderr)
        return 1
    return 0


def schreibe_audit(audit):
    CACHE.mkdir(parents=True, exist_ok=True)
    audit.sort(key=lambda a: (not a["behalten"], -a["punkte"]))
    pfad = CACHE / "filter-report.json"
    pfad.write_text(json.dumps({
        "erstellt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "geprueft": len(audit),
        "behalten": sum(1 for a in audit if a["behalten"]),
        "verworfen": sum(1 for a in audit if not a["behalten"]),
        "artikel": audit,
    }, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
