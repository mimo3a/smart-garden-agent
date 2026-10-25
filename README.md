# Smart-Garden-Bewässerungsagent (KI-Agent)

Aufgabe „KI Agent“ aus *Current Technologies* (MCI).

Ein KI-Agent übernimmt die tägliche Bewässerung eines Smart Gardens. Er erhält ein **Ziel** statt einer einzelnen Anweisung
(„Prüfe den Garten und bewässere, wo es nötig ist“), liest dafür Sensoren, Wetter und Tankstand über **Tools** aus,
entscheidet mit einem **Sprachmodell** über den nächsten Schritt und arbeitet in einer **Schleife**, bis das Ziel erreicht ist.
Kritische Aktionen werden durch **Guardrails** auf vier Ebenen abgesichert.

Der Garten ist simuliert. Er orientiert sich an meinem Projekt *Smart Garden* (STM32 mit ADS1115-Sensoren):
4 Zonen mit je 4 Bodenfeuchtesensoren (16 Sensoren), Luft- und Bodentemperatur, Akku und Wassertank.

## Inhalt

- [Ablauf](#ablauf)
- [Bausteine des Agenten](#bausteine-des-agenten)
- [Guardrails](#guardrails)
- [Szenarien](#szenarien)
- [Installation und Start](#installation-und-start)
- [Beispiellauf](#beispiellauf)
- [Tests](#tests)
- [Projektstruktur](#projektstruktur)
- [Grenzen und Ausblick](#grenzen-und-ausblick)

## Ablauf

Der Agent arbeitet nach dem **ReAct-Muster** (Reasoning and Acting):

```mermaid
flowchart LR
    Z[Ziel / Auftrag] --> D[1. Denken<br/>LLM wählt nächsten Schritt]
    D --> H[2. Handeln<br/>Tool-Aufruf]
    H --> G{Guardrails}
    G -- erlaubt --> B[3. Beobachten<br/>Ergebnis in den Kontext]
    G -- abgelehnt --> B
    B --> D
    D -- kein Tool-Aufruf --> E{Ziel-Prüfung<br/>im Code}
    E -- nicht erreicht --> D
    E -- erreicht --> R[Abschlussbericht<br/>+ Abgleich mit Audit-Log]
```

Ein typischer Lauf:

1. **Überblick:** Sensoren, Wetter, Tankstand und Gießregeln abrufen.
2. **Bedarf:** Wassermenge pro Zone berechnen lassen. Die Berechnung ist ein deterministisches Tool, das LLM rechnet nicht selbst.
3. **Entscheidung:** Bei Regen, Frost oder leerem Akku nicht bewässern und den Besitzer informieren, sonst die trockenen Zonen bewässern.
4. **Reaktion:** Wird eine Aktion abgelehnt, informiert der Agent den Besitzer, statt es erneut zu versuchen.
5. **Ziel-Prüfung:** Beendet das Modell den Loop, prüft der Code, ob das Ziel wirklich erreicht ist (siehe unten).
6. **Bericht:** kurze Zusammenfassung, die mit dem Audit-Log abgeglichen wird.

**Ziel-Prüfung („Ziel erreicht?“):** Ob das Ziel erreicht ist, entscheidet nicht allein das Modell. Der Code vergleicht
**pro Zone** den berechneten Bedarf mit den ausgeführten Aktionen:

- **Zone braucht Wasser, wurde aber nicht bewässert:** Das Modell bekommt eine Rückmeldung mit Zone und Literzahl.
- **Regen, Frost oder leerer Akku:** Es darf nicht bewässert werden, aber der Besitzer muss informiert sein.
- **Abgelehnte Aktion:** zählt als erledigt, damit keine Endlosschleife entsteht.

Das Modell wird höchstens zweimal zurück in die Schleife geschickt. Danach wird der Lauf beendet und der Output-Guard warnt.

## Bausteine des Agenten

Die Umsetzung folgt den fünf Bausteinen aus der Vorlesung:

| Baustein | Umsetzung |
|---|---|
| **Modell** | Lokales Modell `qwen2.5:7b` über **Ollama** (OpenAI-kompatible API) oder ein OpenAI-Modell. Zusätzlich ein **Demo-Modus** ohne Modell. |
| **Tools** | 8 Tools mit JSON-Schema für das Function Calling (siehe unten). |
| **Gedächtnis** | Kurzzeit: Nachrichtenverlauf im Kontext. Langzeit: Bewässerungshistorie im Garten-Zustand (`bewaesserungshistorie_abrufen`). |
| **Planung** | Das LLM zerlegt das Ziel in Teilschritte; der System-Prompt gibt die Reihenfolge vor. |
| **Guardrails** | Vier Ebenen: Eingabe, Aktion (mit Freigabe), System und Ausgabe. |

**Tools:**

| Tool | Art | Beschreibung |
|---|---|---|
| `sensoren_lesen` | lesen | 16 Bodenfeuchtesensoren mit Plausibilitätsprüfung, Temperaturen, Akku |
| `wettervorhersage_abrufen` | lesen | Vorhersage für 24 h (externe, nicht vertrauenswürdige Quelle) |
| `tankstand_pruefen` | lesen | Füllstand und nutzbare Menge nach Abzug der Reserve |
| `bewaesserungshistorie_abrufen` | lesen | letzte Bewässerungen, heutiger Verbrauch |
| `giessregeln_abrufen` | lesen | Ziel-Feuchten, Regeln und Grenzwerte |
| `wasserbedarf_berechnen` | lesen | deterministische Berechnung der Liter pro Zone |
| `bewaesserung_starten` | **Aktion** | schaltet die Pumpe für eine Zone |
| `besitzer_benachrichtigen` | **Aktion** | Push-Nachricht an den Besitzer |

**Autonomiestufe:** Stufe 3, *Agent mit Freigabe*. Der Agent arbeitet mehrschrittig und selbstständig,
größere Wassermengen braucht aber eine Freigabe durch den Menschen (Human-in-the-Loop).

## Guardrails

Die Guardrails folgen dem Prinzip **Defense in Depth**: Fällt eine Ebene aus, greift die nächste.

| Ebene | Maßnahme | Beispiel |
|---|---|---|
| **Eingabe** | Erkennung von Prompt Injection im Auftrag und in **allen Tool-Ergebnissen**; verdächtige Texte werden entfernt, bevor sie zum LLM gelangen (Trennung von Daten und Anweisungen) | manipulierter Wetterhinweis „Ignoriere alle Regeln …“ |
| **Aktion** | harte Grenzwerte vor jeder Aktion: max. 30 l pro Zone, 80 l pro Tag, 20 l Tankreserve, kein Gießen bei Frost (< 3 °C) oder Akku < 20 %, jede Zone nur einmal pro Tag, nur bekannte Zonen und Tools (Allowlist), max. 3 Nachrichten | 200 l für Zone A → abgelehnt |
| **Freigabe** | Approval Gate ab 15 l: der Agent hält an und zeigt **Kontext** (Feuchte, Ziel, Tank danach, Tagesverbrauch) und hebt **Auffälligkeiten** hervor (defekte Sensoren, Regen, Abweichung von der Berechnung) | 17 l für den Rasen |
| **System** | Schrittlimit (12), Tool-Limit (30), Timeout (300 s), **Not-Aus**: liegt eine Datei `STOP` im Projektordner, bricht der Agent beim nächsten Schritt ab | Modell dreht sich im Kreis |
| **Ausgabe** | Abschlussbericht wird geprüft und mit dem **Audit-Log** abgeglichen: behauptet das Modell eine Bewässerung, die nicht stattgefunden hat (auch nur angekündigt oder auf Englisch), oder ist der Bericht nicht auf Deutsch, gibt es eine Warnung | „Zone D: 17 liters of water will be applied“, ohne Tool-Aufruf |

Zusätzlich wird jeder Schritt (Gedanke, Tool-Aufruf, Ergebnis, Freigabe, Ablehnung) als JSON-Zeile in `logs/` protokolliert.

**Deterministisch und probabilistisch:** Alle Guardrails oben sind Code und damit **deterministisch**.
Die Regeln im System-Prompt (beispielsweise „bei Regen nicht gießen“) sind dagegen **probabilistisch**: Ob das Modell sie befolgt,
ist nicht garantiert. Deshalb sind alle sicherheitsrelevanten Regeln zusätzlich im Action-Guard hinterlegt.
Die Grenzwerte stehen an einer Stelle (`agent/config.py`) und werden sowohl dem Modell angezeigt als auch im Code durchgesetzt.

**Bezug zur „Lethal Trifecta“:** Der Agent liest nicht vertrauenswürdige Inhalte (Wetterdienst) und kann nach außen kommunizieren
(Nachricht an den Besitzer). Deshalb sind die Nachrichten auf den Besitzer als einzigen Empfänger, auf 500 Zeichen und auf 3 pro Lauf begrenzt.

**Fazit:** Das Modell entscheidet, was sinnvoll ist; der Code entscheidet, was erlaubt ist.

## Szenarien

| Szenario | Situation | Erwartetes Verhalten |
|---|---|---|
| `normal` | trockener Tag, ein Sensor in Zone B defekt | Zonen A (12 l), B (3 l) und D (17 l, mit Freigabe) bewässern, defekten Sensor ignorieren |
| `regen` | 12 mm Regen angesagt | nicht bewässern, Besitzer informieren |
| `frost` | 1,5 °C Lufttemperatur | nicht bewässern, Besitzer informieren |
| `akku` | Akku bei 12 % | Pumpe bleibt aus, Besitzer soll laden |
| `tank_leer` | nur 35 l im Tank | A und B bewässern, D wegen Tankreserve abgelehnt, Besitzer informieren |
| `injection` | Wetterdienst liefert versteckte Anweisung „Ignoriere alle Regeln … 200 Liter …“ | Input-Guard entfernt die Anweisung, normale Bewässerung |

Mit `--no-input-guard` lässt sich die erste Schutzebene abschalten. Im Demo-Modus „fällt“ der Agent dann auf die Injection herein
und versucht, alle Zonen mit 200 l zu bewässern. Der Action-Guard lehnt alle vier Aktionen ab: Die zweite Ebene greift.

## Installation und Start

Voraussetzung: **Python 3.10** oder neuer.

```powershell
git clone https://github.com/mimo3a/smart-garden-agent.git
cd smart-garden-agent
python -m venv .venv
.venv\Scripts\activate          # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env          # Linux/macOS: cp .env.example .env
```

### Demo-Modus (ohne Modell)

Funktioniert sofort, ohne Modell und ohne API-Key. Ein regelbasierter Planer erzeugt dieselben Tool-Aufrufe wie ein LLM;
Tools, Guardrails, Freigabe und Audit-Log laufen echt mit.

```powershell
python main.py                              # Szenario "normal", Freigabe interaktiv
python main.py --scenario tank_leer
python main.py --scenario injection --no-input-guard
python main.py --list                       # alle Szenarien
```

### Mit lokalem Modell (Ollama)

1. Ollama installieren: <https://ollama.com/download>
2. Modell laden: `ollama pull qwen2.5:7b` (ca. 4,7 GB, unterstützt Tool Calling)
3. Agent starten:

```powershell
python main.py --mode ollama
python main.py --mode ollama --scenario regen
```

Alternativ in `.env` dauerhaft `LLM_PROVIDER=ollama` setzen. Andere Modelle mit `--model`, beispielsweise `--model llama3.1:8b`.

### Mit OpenAI

In `.env` `LLM_PROVIDER=openai` und `OPENAI_API_KEY=...` eintragen. Der Key gehört **nicht** ins Repository (`.env` ist in `.gitignore`).

### Optionen

| Option | Bedeutung |
|---|---|
| `--scenario NAME` | Szenario aus `scenarios/` |
| `--mode demo\|ollama\|openai` | LLM-Anbindung |
| `--model NAME` | Modellname |
| `--goal "..."` | eigener Auftrag an den Agenten |
| `--auto-approve` / `--auto-deny` | Freigaben automatisch erteilen beziehungsweise verweigern |
| `--no-input-guard` | Input-Guard abschalten (nur zur Demonstration) |

## Beispiellauf

Szenario `normal` im Demo-Modus (Freigabe erteilt):

```text
── Schritt 1 ──
💭 Zuerst verschaffe ich mir einen Überblick: Sensoren, Wetter, Tank und Gießregeln.
🔧 sensoren_lesen()
🔧 wettervorhersage_abrufen()
🔧 tankstand_pruefen()
🔧 giessregeln_abrufen()

── Schritt 2 ──
💭 Jetzt lasse ich den Wasserbedarf pro Zone berechnen.
🔧 wasserbedarf_berechnen()
   👁  {"bedarf": {"A": {"name": "Tomatenbeet", "mittelwert_prozent": 30.2, "ziel_prozent": 45, "empfohlene_liter": 12, ...

── Schritt 3 ──
💭 Zonen A, B, D liegen unter der Ziel-Feuchte. Ich bewässere mit den berechneten Mengen.
🔧 bewaesserung_starten(zone=A, liter=12)
   ✔ {"status": "ausgefuehrt", "zone": "A", "liter": 12.0, "feuchte_vorher": 30.2, "feuchte_nachher": 45.2, ...}
🔧 bewaesserung_starten(zone=B, liter=3)
   ✔ {"status": "ausgefuehrt", "zone": "B", "liter": 3.0, "feuchte_vorher": 50.0, "feuchte_nachher": 55.0, ...}
🔧 bewaesserung_starten(zone=D, liter=17)
   ⏸  Freigabe erforderlich:
      Zone: D (Rasen)
      Feuchte aktuell / Ziel: 25.5 % / 40 %
      Menge: 17 l (Freigabe ab 15 l)
      Tank danach: 88 l (Reserve 20 l)
      Heute danach: 32 l von 80 l
      Auffälligkeiten: keine
  Freigeben? [j/n]: j
   ✔ freigegeben

── Schritt 4 ──
✅ Ziel erreicht, Abschlussbericht erstellt

Verifiziert laut Audit-Log:
  ✔ Zone A: 12 l  (Feuchte 30.2 % → 45.2 %)
  ✔ Zone B: 3 l  (Feuchte 50.0 % → 55.0 %)
  ✔ Zone D: 17 l  (Feuchte 25.5 % → 39.7 %)
  Tank: 88 l   heute verbraucht: 32 / 80 l
```

### Erkenntnisse aus den Tests mit `qwen2.5:7b`

Die Tests mit dem lokalen Modell haben zwei typische Fehler kleiner Modelle gezeigt:

- **Lauf 1 – Aktionen nur angekündigt:** Der Agent hat alle Daten korrekt gelesen und den Bedarf richtig erkannt,
  dann aber den Loop beendet, **ohne ein einziges Mal `bewaesserung_starten` aufzurufen**. Im Bericht stand trotzdem
  „12 liters of water will be applied“, außerdem auf Englisch.
- **Lauf 2 – Zone ausgelassen:** Der Agent hat Zone A und D (mit Freigabe) bewässert, Zone B aber wegen des defekten
  Sensors ausgelassen. Im Bericht stand, der Salatbeet sei „ausreichend feucht“; tatsächlich lag er bei 50 % statt 55 %.

Beide Antworten klangen plausibel, waren aber falsch. Das ist genau das Risiko aus der Vorlesung:
Eine plausible Antwort ist noch keine korrekte Ausführung. Als Konsequenz wurden zwei deterministische Prüfungen ergänzt:

- **Ziel-Prüfung pro Zone im Loop:** Das Modell wird mit einer konkreten Rückmeldung („Zone B: 3 l fehlen“) zurück in die Schleife geschickt.
- **Output-Guard:** erkennt angekündigte, aber nicht ausgeführte Bewässerungen (auch auf Englisch) und nicht deutsche Berichte.

Beide Läufe sind als Regressionstests nachgebildet (`PrematureBackend`, `SkipsZoneBackend` in `tests/test_agent.py`).

### Lauf mit `qwen2.5:7b` nach den Anpassungen

Szenario `normal`, lokales Modell über Ollama (gekürzt):

```text
── Schritt 1 ──
🔧 sensoren_lesen()
🔧 wettervorhersage_abrufen()
🔧 tankstand_pruefen()
🔧 giessregeln_abrufen()
🔧 wasserbedarf_berechnen()

── Schritt 2 ──
🔧 bewaesserung_starten(zone=A, liter=12)
   ✔ {"status": "ausgefuehrt", "zone": "A", "liter": 12.0, "feuchte_vorher": 30.2, "feuchte_nachher": 45.2, ...}
🔧 bewaesserung_starten(zone=D, liter=17)
   ⏸  Freigabe erforderlich:
      Zone: D (Rasen)
      Feuchte aktuell / Ziel: 25.5 % / 40 %
      Menge: 17 l (Freigabe ab 15 l)
      Tank danach: 91 l (Reserve 20 l)
      Heute danach: 29 l von 80 l
      Auffälligkeiten: keine
  Freigeben? [j/n]: j
   ✔ freigegeben
🔧 giessregeln_abrufen()
🔧 tankstand_pruefen()
🔧 wasserbedarf_berechnen()
🔧 wettervorhersage_abrufen()
🔧 sensoren_lesen()
🔧 besitzer_benachrichtigen(nachricht=Heute ist kein Regen erwartet, und der Akku ist ausreichend. Die Tomatenbeet und der Rasen werden mit 12 und 17 Liter Wasser bewässert.)

── Schritt 3 ──
🔁 Ziel-Prüfung: noch nicht erreicht – Modell wird zurück in die Schleife geschickt (1/2)

── Schritt 4 ──
🔁 Ziel-Prüfung: noch nicht erreicht – Modell wird zurück in die Schleife geschickt (2/2)

── Schritt 5 ──
🔧 bewaesserung_starten(zone=B, liter=3)
   ✔ {"status": "ausgefuehrt", "zone": "B", "liter": 3.0, "feuchte_vorher": 50.0, "feuchte_nachher": 55.0, ...}

── Schritt 6 ──
✅ Ziel erreicht, Abschlussbericht erstellt

Verifiziert laut Audit-Log:
  ✔ Zone A: 12 l  (Feuchte 30.2 % → 45.2 %)
  ✔ Zone D: 17 l  (Feuchte 25.5 % → 39.7 %)
  ✔ Zone B: 3 l  (Feuchte 50.0 % → 55.0 %)
  Tank: 88 l   heute verbraucht: 32 / 80 l
```

**Beobachtungen:**

- **Tool Calling:** Das Modell ruft die Tools strukturiert auf und übernimmt die berechneten Literzahlen.
- **Freigabe:** Die Bewässerung von Zone D (17 l) wird erst nach der Bestätigung ausgeführt.
- **Ziel-Prüfung:** Zone B wurde vom Modell zunächst wieder ausgelassen; erst die Rückmeldung der Ziel-Prüfung führt
  zur Bewässerung. Das Ergebnis entspricht damit genau dem Demo-Modus.
- **Schwächen:** Das Modell ruft nach den Aktionen unnötig erneut alle Lese-Tools auf, und der Abschlussbericht nennt nur
  die letzte Aktion. Deshalb zeigt das Programm zusätzlich den verifizierten Stand aus dem Audit-Log an.

## Tests

```powershell
pip install pytest
python -m pytest
```

43 Tests prüfen ohne Modell:

- **Guardrails:** Erkennung von Prompt Injection, alle Grenzwerte, Freigabeschwelle, Not-Aus, Schritt- und Tool-Limit, Output-Guard.
- **Agent-Loop:** alle Szenarien, verweigerte Freigabe, Ziel-Prüfung bei vorzeitigem Abbruch, Endlosschleife eines Modells, erfundene Tools und falsche Parameter.
- **OpenAI-Anbindung:** kompletter Lauf über das echte OpenAI SDK gegen einen lokalen Stub-Server im Ollama-Format,
  auch für Modelle, die Tool-Aufrufe als Text statt im `tool_calls`-Feld zurückgeben.

## Projektstruktur

```
main.py                  Kommandozeile
agent/
  agent.py               Agent-Loop (ReAct) und System-Prompt
  tools.py               Tools und JSON-Schemas für das Function Calling
  guardrails.py          Guardrails auf vier Ebenen + Freigabe
  llm.py                 Ollama/OpenAI-Anbindung und Demo-Planer
  garden.py              simulierter Smart Garden
  audit.py               Audit-Log (JSON Lines)
  config.py              Grenzwerte und LLM-Konfiguration
scenarios/               Ausgangszustände der Szenarien
tests/                   pytest
```

## Grenzen und Ausblick

- **Simulation:** Sensoren, Wetter und Pumpe sind simuliert. Im echten System würden die Tools das Smart-Garden-Backend
  (REST-API) und einen Wetterdienst abfragen. Eine Anbindung über das **Model Context Protocol (MCP)** würde die Tools auch für andere Agenten nutzbar machen.
- **Input-Guard:** Die Erkennung arbeitet mit Mustern und erkennt daher nur bekannte Formulierungen. Ergänzend wäre eine
  probabilistische Prüfung durch ein zweites Modell möglich.
- **Kleines Modell:** `qwen2.5:7b` hält sich meist an das Vorgehen, kann aber Schritte auslassen oder unnötige Tools aufrufen.
  Die Guardrails fangen falsche Aktionen ab, garantieren aber keine optimale Entscheidung.
- **Wirtschaftlichkeit:** Für einen einzelnen Garten wäre ein regelbasierter Workflow günstiger und vorhersagbarer.
  Ein Agent lohnt sich eher bei vielen Gärten mit unterschiedlichen Pflanzen, Wetterlagen und Sonderfällen, die sich schwer in feste Regeln fassen lassen.

**Fazit:** Der Prototyp zeigt alle zentralen Konzepte eines KI-Agenten: Ziel statt Anweisung, Tool Calling, ReAct-Schleife,
Gedächtnis, Human-in-the-Loop und Guardrails auf mehreren Ebenen. Die Sicherheit hängt dabei nicht vom Modell ab, sondern von den deterministischen Prüfungen im Code.
