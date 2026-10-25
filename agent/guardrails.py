"""Guardrails nach dem Prinzip "Defense in Depth" (vier Ebenen, siehe Vorlesung):

1. Eingabe   - InputGuard:  Prompt-Injection in Auftrag und Tool-Ergebnissen erkennen
2. Aktion    - ActionGuard: harte Grenzwerte vor jeder Aktion prüfen (+ Freigabe)
3. System    - SystemGuard: Schrittlimit, Tool-Limit, Timeout, Not-Aus-Schalter
4. Ausgabe   - OutputGuard: Abschlussbericht prüfen und mit dem Audit-Log abgleichen

Alle Guardrails hier sind deterministisch (Code). Die weichen Regeln im
System-Prompt (z. B. "bei Regen nicht gießen") sind dagegen probabilistisch:
Ob das LLM sie befolgt, ist nicht garantiert.
"""
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from .config import KILL_SWITCH_FILE, LIMITS, Limits
from .garden import ZONES, Garden

# ---------------------------------------------------------------- Ebene 1: Eingabe

INJECTION_PATTERNS = [
    r"ignorier\w*\s+(\w+\s+){0,3}(regeln|anweisungen|instruktionen)",
    r"vergiss\w*\s+(\w+\s+){0,3}(regeln|anweisungen)",
    r"ignore\s+(all\s+)?(previous|prior|above)?\s*(instructions|rules)",
    r"disregard\s+(all\s+)?(previous|prior)?\s*(instructions|rules)",
    r"neue\s+anweisung",
    r"du\s+bist\s+(jetzt|ab\s+sofort)",
    r"system\s*prompt",
    r"benachrichtige\s+(den\s+)?besitzer\s+nicht",
]
_INJECTION_RE = re.compile("|".join(INJECTION_PATTERNS), re.IGNORECASE)
REDACTED = "[ENTFERNT: verdächtige Anweisung in externen Daten erkannt]"


class InputGuard:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled

    @staticmethod
    def scan(text: str) -> list[str]:
        return [m.group(0) for m in _INJECTION_RE.finditer(text)]

    def check_goal(self, goal: str) -> list[str]:
        """Prüft den Auftrag des Benutzers. Treffer führen zum Abbruch."""
        return self.scan(goal) if self.enabled else []

    def sanitize(self, data: Any) -> tuple[Any, list[str]]:
        """Ersetzt verdächtige Strings in Tool-Ergebnissen, bevor sie zum LLM gehen.

        Tool-Ergebnisse sind Daten, keine Anweisungen ("Trennung von Daten und Anweisungen").
        """
        if not self.enabled:
            return data, []
        findings: list[str] = []

        def walk(obj):
            if isinstance(obj, str):
                hits = self.scan(obj)
                if hits:
                    findings.extend(hits)
                    return REDACTED
                return obj
            if isinstance(obj, dict):
                return {k: walk(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [walk(v) for v in obj]
            return obj

        return walk(data), findings


# ---------------------------------------------------------------- Ebene 2: Aktion

@dataclass
class Verdict:
    allowed: bool
    reason: str = ""
    needs_approval: bool = False


class ActionGuard:
    def __init__(self, garden: Garden, limits: Limits = LIMITS):
        self.garden = garden
        self.limits = limits
        self.notifications_sent = 0

    def check(self, tool: str, args: dict) -> Verdict:
        if tool == "bewaesserung_starten":
            return self._check_irrigation(args)
        if tool == "besitzer_benachrichtigen":
            return self._check_notification(args)
        return Verdict(False, f"Unbekannte Aktion '{tool}'")

    def _check_irrigation(self, args: dict) -> Verdict:
        L, g = self.limits, self.garden
        zone = str(args.get("zone", "")).strip().upper()
        try:
            liters = float(args.get("liter"))
        except (TypeError, ValueError):
            return Verdict(False, "Literzahl fehlt oder ist keine Zahl")
        args["zone"], args["liter"] = zone, liters  # normalisieren für die Ausführung

        if zone not in ZONES:
            return Verdict(False, f"Unbekannte Zone '{zone}' (erlaubt: {', '.join(ZONES)})")
        if liters <= 0:
            return Verdict(False, "Literzahl muss größer als 0 sein")
        if liters > L.max_liter_pro_zone:
            return Verdict(False, f"{liters:g} l überschreiten das Maximum von {L.max_liter_pro_zone} l pro Zone")
        if g.air_temp < L.frost_grenze_c:
            return Verdict(False, f"Frostgefahr: Lufttemperatur {g.air_temp} °C liegt unter {L.frost_grenze_c} °C")
        if g.battery < L.akku_minimum_prozent:
            return Verdict(False, f"Akku bei {g.battery} % (Minimum {L.akku_minimum_prozent} %): Pumpe bleibt aus")
        if g.zone_watered_today(zone):
            return Verdict(False, f"Zone {zone} wurde heute bereits bewässert")
        if g.liters_today() + liters > L.max_liter_pro_tag:
            return Verdict(False, f"Tageslimit: heute {g.liters_today():g} l + {liters:g} l > {L.max_liter_pro_tag} l")
        if g.tank - liters < L.tank_reserve_liter:
            return Verdict(False, f"Tankreserve: {g.tank:g} l - {liters:g} l unterschreitet die Reserve von {L.tank_reserve_liter} l")
        return Verdict(True, needs_approval=liters > L.freigabe_ab_liter)

    def _check_notification(self, args: dict) -> Verdict:
        text = str(args.get("nachricht", "")).strip()
        if not text:
            return Verdict(False, "Leere Nachricht")
        if len(text) > 500:
            return Verdict(False, "Nachricht zu lang (max. 500 Zeichen)")
        if self.notifications_sent >= self.limits.max_benachrichtigungen:
            return Verdict(False, f"Maximal {self.limits.max_benachrichtigungen} Nachrichten pro Lauf")
        self.notifications_sent += 1
        return Verdict(True)


def approval_context(garden: Garden, args: dict) -> dict:
    """Kontext für die Freigabe: nur das Nötige, Auffälligkeiten hervorgehoben."""
    zone, liters = args["zone"], args["liter"]
    meta = ZONES[zone]
    avg = garden.plausible_average(zone)
    hints = []
    defect = [v for v in garden.zone_readings(zone) if not (5 <= v <= 95)]
    if defect:
        hints.append(f"{len(defect)} Sensor(en) in Zone {zone} liefern unplausible Werte")
    if garden.weather.get("regen_mm_24h", 0) >= LIMITS.regen_grenze_mm:
        hints.append(f"Regen angesagt: {garden.weather['regen_mm_24h']} mm")
    expected = round(max(meta["ziel_feuchte"] - (avg or 0), 0) * meta["liter_pro_prozentpunkt"])
    if abs(expected - liters) > 2:
        hints.append(f"Menge weicht von der Berechnung ab (berechnet: {expected} l)")
    return {
        "Zone": f"{zone} ({meta['name']})",
        "Feuchte aktuell / Ziel": f"{avg} % / {meta['ziel_feuchte']} %",
        "Menge": f"{liters:g} l (Freigabe ab {LIMITS.freigabe_ab_liter} l)",
        "Tank danach": f"{garden.tank - liters:g} l (Reserve {LIMITS.tank_reserve_liter} l)",
        "Heute danach": f"{garden.liters_today() + liters:g} l von {LIMITS.max_liter_pro_tag} l",
        "Auffälligkeiten": hints or ["keine"],
    }


def make_approver(mode: str, ask: Callable[[str], str] = input) -> Callable[[dict], bool]:
    """mode: 'interactive' (Konsole), 'yes' (alles freigeben) oder 'no' (alles ablehnen)."""

    def approver(context: dict) -> bool:
        if mode == "yes":
            return True
        if mode == "no":
            return False
        answer = ask("  Freigeben? [j/n]: ").strip().lower()
        return answer in ("j", "ja", "y", "yes")

    return approver


# ---------------------------------------------------------------- Ebene 3: System

class GuardrailStop(Exception):
    """Wird geworfen, wenn eine System-Guardrail den Lauf beendet."""


class SystemGuard:
    def __init__(self, limits: Limits = LIMITS, kill_switch=KILL_SWITCH_FILE):
        self.limits = limits
        self.kill_switch = kill_switch
        self.started = time.monotonic()
        self.tool_calls = 0

    def before_step(self, step: int) -> None:
        if self.kill_switch.exists():
            raise GuardrailStop(f"Not-Aus: Datei '{self.kill_switch.name}' gefunden")
        if step > self.limits.max_schritte:
            raise GuardrailStop(f"Schrittlimit von {self.limits.max_schritte} erreicht")
        if time.monotonic() - self.started > self.limits.timeout_sekunden:
            raise GuardrailStop(f"Timeout nach {self.limits.timeout_sekunden} s")

    def before_tool(self) -> None:
        self.tool_calls += 1
        if self.tool_calls > self.limits.max_tool_aufrufe:
            raise GuardrailStop(f"Tool-Limit von {self.limits.max_tool_aufrufe} Aufrufen erreicht")


# ---------------------------------------------------------------- Ebene 4: Ausgabe

class OutputGuard:
    def check(self, final_text: str | None, executed: list[dict]) -> list[str]:
        """Prüft den Abschlussbericht des LLM gegen die tatsächlich ausgeführten Aktionen."""
        warnings = []
        text = (final_text or "").strip()
        if not text:
            warnings.append("Kein Abschlussbericht vom Modell")
        if InputGuard.scan(text):
            warnings.append("Abschlussbericht enthält verdächtige Anweisungen")
        watered = {a["args"]["zone"] for a in executed if a["tool"] == "bewaesserung_starten"}
        action_words = re.compile(r"bewässert|gegossen|watered|irrigated|applied|mit\s+\d+([.,]\d+)?\s*(l|liter\w*)\b",
                                  re.IGNORECASE)
        negations = re.compile(r"\b(nicht|kein\w*|no|not|abgelehnt|rejected|empfiehlt|empfohlen|recommends?)\b",
                               re.IGNORECASE)
        for line in text.splitlines():  # zeilenweise: Behauptung = Zone + Aktionswort ohne Verneinung
            if not action_words.search(line) or negations.search(line):
                continue
            for zone, meta in ZONES.items():
                if re.search(rf"\b(zone\s+{zone}|{re.escape(meta['name'])})\b", line, re.IGNORECASE) and zone not in watered:
                    warnings.append(f"Bericht behauptet eine Bewässerung von Zone {zone}, laut Audit-Log ist sie nicht passiert")
        german = len(re.findall(r"\b(der|die|das|und|ist|mit|wurde|nicht|keine?|heute|zone)\b", text, re.IGNORECASE))
        english = len(re.findall(r"\b(the|is|and|of|with|will|be|are|was|today|water)\b", text, re.IGNORECASE))
        if text and english > german:
            warnings.append("Abschlussbericht ist nicht auf Deutsch")
        return warnings
