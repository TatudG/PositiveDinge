# PositiveDinge

Aktuelle Nachrichten aus deutschen und englischsprachigen Quellen — aber nur die
guten. Jede Meldung mit Bild, Themenzuordnung und Quellenangabe.

Eine statische Seite: eine `index.html`, kein Server, kein API-Key, kein npm.
Läuft per Doppelklick, auch ohne Netz.

## Benutzen

```bash
python3 build.py
```

Holt die Feeds, filtert, schreibt `index.html`. Danach im Browser öffnen —
oder auf einen Webserver kopieren. Aktualisieren heißt: nochmal ausführen.

| Schalter | Wirkung |
|---|---|
| *(keiner)* | normaler Build, nutzt den Cache |
| `--frisch` | Cache ignorieren, alles neu holen |
| `--offline` | nie ins Netz — harter Test, ob die Seite aus dem Cache entsteht |
| `--nur-cc-bilder` | keine Pressefotos, nur frei lizenzierte Bilder (siehe unten) |
| `--strict` | Exit-Code 1, wenn eine Quelle nicht erreichbar war |

Nur die Standardbibliothek, getestet mit Python 3.9. Kein `pip install`.

## Stündlich automatisch

Eine statische Seite kann sich nicht selbst aktualisieren — irgendetwas muss
`build.py` ausführen, und zwar auch dann, wenn der Rechner aus ist. Das
übernimmt `.github/workflows/stuendlich.yml`: GitHub baut die Seite jede
Stunde neu, legt sie als Commit ab, und Pages stellt den Branch daraufhin von
selbst neu bereit. Es ist nichts weiter zu tun.

Drei Dinge sind dabei zu wissen:

- **„Stündlich" ist eine Bitte, keine Zusage.** GitHub verteilt geplante Läufe
  über alle Repositories. Unter Last kommen zehn bis dreißig Minuten
  Verspätung zusammen. Der Lauf steht deshalb auf Minute 17 und nicht auf
  Minute 0 — zur vollen Stunde stauen sich die Aufträge.
- **Nach 60 Tagen ohne Aktivität schaltet GitHub geplante Workflows ab.** Hier
  ist das unkritisch, weil der Workflow selbst Commits erzeugt und damit als
  Aktivität zählt. Wer die Seite lange nicht anfasst und nichts mehr committet
  wird: im Reiter *Actions* lässt sich der Lauf jederzeit von Hand starten
  (*Run workflow*), danach läuft er wieder stündlich.
- **Der Openverse-Zwischenspeicher liegt mit im Repository**
  (`.cache/openverse/`). Ohne Schlüssel erlaubt Openverse nur 200 Anfragen am
  Tag, und jeder Lauf startet in einem frischen Checkout. Ohne die
  gespeicherten Antworten wären das rund 20 Suchen pro Stunde, also 480 am Tag
  — das Limit wäre nach fünf Stunden erreicht und neue Artikel hätten kein
  Bild mehr. Deshalb steht in `.gitignore` `.cache/*` statt `.cache/`.

Der Workflow läuft mit `--nur-cc-bilder --aufraeumen`. Beides ist nötig:
ohne den ersten Schalter stünden Pressefotos auf einer öffentlichen Seite, ohne
den zweiten wüchse `img/cc/` mit jedem Lauf, weil neue Artikel neue Bilder
bringen, ohne dass die alten verschwinden — und alle davon landen im Repository.

Wer den Lauf lieber lokal haben möchte (und den Rechner dafür laufen lässt),
kann `build.py` auch per `launchd` oder `cron` starten. Der Unterschied: die
Seite im Netz aktualisiert sich dann erst, wenn zusätzlich ein `git push`
erfolgt.

## Wie der Filter arbeitet

Das ist der Teil, der die Seite ausmacht, und er ist bewusst anders gebaut, als
man zuerst denkt.

**Eine reine Sperrliste reicht nicht.** Gegen die echten Schlagzeilen getestet
ließen 78–94 % der allgemeinen Nachrichten eine Sperrliste passieren — darunter
„Messerangriff an Schule – sieben Verletzte". Die meisten Nachrichten sind
nämlich *neutral*, nicht negativ-verschlüsselt. Es gibt kein Wort, das man
sperren könnte.

Deshalb ist der Filter umgekehrt: **Positiv muss man sich verdienen.**

```
Stufe A   Fehlschlag-Phrasen   "Rettung gescheitert"        → sofort raus
Stufe B   harte Sperre         Gewalt, Unglück, Katastrophe → raus
                               (mit Negations-Wächter)
Stufe C   Punktwertung         Objektregeln, Positiv ×2 im Titel,
                               Kontext-Abzug, Themen-Bonus
Schwelle  kuratierte Quellen ≥ 0, allgemeine Quellen ≥ 4
```

Zwei Feinheiten, ohne die es nicht funktioniert:

- **Der Negations-Wächter.** Steht vor einem Sperrtreffer ein „ohne / kein /
  nicht / without / no", greift die Sperre nicht. „Ohne Verletzte" ist eine gute
  Nachricht, kein Unglück.
- **Die Objektregeln.** „Tötet Viren" ist Medizin, „tötet Menschen" ist es
  nicht — derselbe Wortstamm, das Objekt entscheidet. Reine Wortlisten scheitern
  daran zwangsläufig.

### Ehrlich gesagt

Wortlisten können **nicht garantieren**, dass nie eine unpassende Meldung
erscheint. Sie senken die Trefferquote deutlich, mehr nicht. Der Filter ist eine
Heuristik, kein Beweis. Genau deshalb:

- Jeder Build schreibt ein **Audit-Log** nach `.cache/filter-report.json`:
  jeder geprüfte Artikel mit Punktzahl, Schwelle, Entscheidung und den
  auslösenden Treffern. Ohne das könnte man den Filter nie nachjustieren.
- **`config/overrides.json`** korrigiert jeden Fehlgriff in Sekunden, ohne
  Wortlisten anzufassen. Die URL steht im Audit-Log.

```json
{
  "force_drop": ["https://www.spiegel.de/…"],
  "force_keep": ["praefix:https://www.beispiel.de/"]
}
```

## Anpassen

Alles Inhaltliche steht in `config/`. `build.py` muss dafür nicht angefasst
werden.

| Datei | Zweck |
|---|---|
| `sources.json` | Feeds, `trust`, Schwellen, `max_pro_quelle` |
| `positiv-de.txt` / `-en.txt` | Positivsignale — das Herzstück |
| `hart-de.txt` / `-en.txt` | Sperrliste gegen Gewalt und Unglück |
| `kontext-de.txt` / `-en.txt` | Punktabzug, aber keine Sperre |
| `fehlschlag.txt` | „Rettung gescheitert" — gut klingende Fehlschläge |
| `entschaerfer.txt` | hebt einen Sperrtreffer auf (Waffenstillstand, Peace Deal) |
| `serien.txt` | Rubriken und Füller, die keine Nachricht sind |
| `objekt-regeln.json` | „tötet Viren" vs. „tötet Menschen" |
| `overrides.json` | von Hand gepflegte Korrekturen |

**Schreibweise der Listen:** eine nackte Zeile ist ein **Stamm** und wird als
Teilzeichenkette gesucht — nur so werden Komposita erfasst
(`Kriegsverbrechen`, `Flugzeugabsturz`). Eine Zeile mit `re:` ist ein regulärer
Ausdruck, gedacht für kurze Wörter wie `war`, `win`, `schuss`, die als
Teilzeichenkette zu viel träfen. Verglichen wird auf gefaltetem Text:
Kleinschreibung, `ß`→`ss`, typografische Apostrophe vereinheitlicht.

Soll der Filter weniger streng sein: `schwelle_allgemein` in `sources.json` auf
3 setzen. Strenger: auf 5.

## Bilder

Drei Stufen, in dieser Reihenfolge:

1. **Artikelbild aus dem Feed** — `enclosure`, `media:content`,
   `media:thumbnail` oder `<img>` im Beschreibungstext, mindestens 400 × 250.
2. **Freies Bild über [Openverse](https://openverse.org)** — nur CC0, PDM, BY
   und BY-SA. `by-nc` und `by-nd` sind ausgeschlossen. Querformat wird bevorzugt,
   und der Treffer muss ein Suchwort im Titel tragen — ein unpassendes Stockfoto
   ist schlechter als eine Kachel.
3. **Lokale Themenkachel** (`img/kachel-*.svg`).

Alle Bilder werden **lokal gespeichert** (`img/cc/`), nicht verlinkt. Sonst
würde die Seite offline nicht funktionieren.

Beim Speichern wird jedes Bild einmal aufbereitet: höchstens 1200 px breit,
JPEGs zusätzlich auf Qualität 78 neu komprimiert. Das ist der Unterschied
zwischen 4,6 MB und 2,1 MB — und zwar ohne sichtbaren Verlust, weil die Karten
das Bild ohnehin nur 340 px breit zeigen. Ein reines Verkleinern hätte nicht
gereicht: ein schmales, aber schlecht gespeichertes Foto wird erst durch das
Neukomprimieren klein. `sips` gehört zu macOS; fehlt es, bleiben die Bilder
unverändert groß. Welche Bilder schon bearbeitet sind, merkt sich der Build in
`.cache/bilder-optimiert.txt`. Diese Datei zu löschen ist gefahrlos — dann
läuft die Aufbereitung einmal erneut.

Bei CC-Bildern stehen Urheber und Lizenz unter dem Bild — das ist bei CC-BY
Pflicht, nicht Höflichkeit.

### Urheberrecht — bitte vor dem Veröffentlichen lesen

Artikelbilder von Nachrichtenverlagen sind **Pressefotos mit vollem
Urheberrecht**. Für den privaten, lokalen Gebrauch ist das unproblematisch.
Sobald die Seite aber **öffentlich erreichbar** ist, ist das Einbetten fremder
Pressefotos eine Urheberrechtsverletzung.

Dafür gibt es den Schalter:

```bash
python3 build.py --nur-cc-bilder
```

Er ignoriert Feed-Bilder vollständig und nutzt nur CC-Bilder und Kacheln. Für
eine Veröffentlichung ist das der richtige Modus.

Zwei Vorkehrungen greifen dabei zusammen:

- **Getrennte Ordner.** Pressefotos landen in `img/presse/`, frei lizenzierte in
  `img/cc/`. `img/presse/` steht in `.gitignore` und wird damit nie
  veröffentlicht. Das ist Absicht und nicht bloß Ordnungsliebe: auf GitHub Pages
  ist jede Datei im Branch abrufbar, auch eine, die keine Karte einbindet. Lägen
  die Pressefotos in `img/cc/`, wären sie öffentlich, sobald einmal jemand den
  Dateinamen errät.
- **Der Hinweis in der Fußzeile** nennt je nach Modus, woher die Bilder stammen.

Die veröffentlichte `index.html` ist also immer die aus dem `--nur-cc-bilder`-Lauf.
Wer danach lokal ohne den Schalter baut, überschreibt sie wieder mit der
Pressefoto-Version — vor dem nächsten `git push` also erneut mit dem Schalter
bauen.

Ein Bild kommt auf der Seite **nur einmal** vor. Das klingt selbstverständlich,
ist es aber nicht: dieselbe Openverse-Suche liefert dieselbe Antwort, und ohne
Gegenmaßnahme stand viermal dasselbe Sonnenblumenfeld auf der Seite. Der Build
merkt sich vergebene Bilder und geht die Kandidatenliste weiter, bis ein
unbenutztes passt — notfalls bis zur Kachel.

## Aufbau

```
build.py              Generator (nur stdlib)
vorlage.html          HTML-Gerüst mit Markern, die build.py füllt
css/style.css         handgeschrieben — build.py fasst sie nie an
js/main.js            nur Themenfilter; ohne JS sind alle Karten sichtbar
config/               alle Wortlisten und Einstellungen
img/cc/               heruntergeladene freie Bilder (wird veröffentlicht)
img/presse/           Pressefotos aus den Feeds (nicht versioniert)
img/                  Kacheln, Favicon
index.html            GENERIERT — nicht von Hand editieren
.cache/               Feeds, Openverse-Antworten, Audit-Log, Bildmarken
```

Design ändern: `css/style.css`, Farben und Maße stehen als Variablen in `:root`.
Struktur ändern: `vorlage.html`. Inhalt ändern: `config/`. `index.html` wird bei
jedem Build überschrieben.

## Bekannte Grenzen

- **Kein garantiert positiver Ausgang.** Siehe „Ehrlich gesagt" oben.
- **ZDF ist standardmäßig aus.** Der Feed mischt TV-Programmhinweise unter die
  Nachrichten. In `sources.json` auf `aktiv: true` setzen, wenn das nicht stört.
- **Themen sind grob.** Die Zuordnung läuft über Schlüsselwörter, nicht über
  Semantik. Ein Artikel über einen Waldbrand-Schutz kann unter „Umwelt & Klima"
  landen, obwohl er von Feuer handelt.
- **Openverse ohne Key ist begrenzt** (20 Anfragen/Minute, 200/Tag). Mit Cache
  kein Problem, ohne Cache schon. Der Cache ist deshalb nicht optional.
