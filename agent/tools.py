"""Werkzeuge (Tools) des Agenten.

Jedes Tool besteht aus einer Python-Funktion und einem JSON-Schema für das
Function Calling. `kind` unterscheidet Lese-Tools von Aktionen: Nur Aktionen
laufen durch den Action-Guard und gegebenenfalls durch die Freigabe.

Im echten System würden die Lese-Tools das Smart-Garden-Backend (REST-API) bzw.
einen Wetterdienst abfragen, und `bewaesserung_starten` würde die Pumpe über den
STM32-Controller schalten. Hier arbeiten sie gegen die Simulation in garden.py.
"""
from dataclasses import dataclass
from typing import Callable

from .config import LIMITS
from .garden import ZONES, Garden, is_plausible


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    func: Callable[..., dict]
    kind: str  # "read" oder "action"

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


NO_PARAMS = {"type": "object", "properties": {}, "required": []}


def build_tools(garden: Garden, notifications: list[str]) -> dict[str, Tool]:
    """Erzeugt die Tools für einen Lauf. `notifications` sammelt gesendete Nachrichten."""

    def sensoren_lesen() -> dict:
        zonen = {}
        for zone, meta in ZONES.items():
            werte = garden.zone_readings(zone)
            zonen[zone] = {
                "name": meta["name"],
                "sensoren": [{"wert_prozent": v, "plausibel": is_plausible(v)} for v in werte],
                "mittelwert_plausibel": garden.plausible_average(zone),
            }
        return {
            "bodenfeuchte": zonen,
            "luft_temperatur_c": garden.air_temp,
            "boden_temperatur_c": garden.state["boden_temperatur_c"],
            "akku_prozent": garden.battery,
        }

    def wettervorhersage_abrufen() -> dict:
        return {"quelle": "Wetterdienst (simuliert)", "vorhersage_24h": garden.weather}

    def tankstand_pruefen() -> dict:
        nutzbar = max(garden.tank - LIMITS.tank_reserve_liter, 0)
        return {"tank_liter": garden.tank, "reserve_liter": LIMITS.tank_reserve_liter, "nutzbar_liter": nutzbar}

    def bewaesserungshistorie_abrufen() -> dict:
        return {"eintraege": garden.state["historie"][-10:], "heute_verbraucht_liter": garden.liters_today()}

    def giessregeln_abrufen() -> dict:
        return {
            "zonen": {z: {"name": m["name"], "ziel_feuchte_prozent": m["ziel_feuchte"]} for z, m in ZONES.items()},
            "regeln": [
                "Nur Zonen bewässern, deren Feuchte-Mittelwert unter der Ziel-Feuchte liegt.",
                "Sensorwerte mit plausibel=false ignorieren (Sensorfehler).",
                "Literzahl immer mit wasserbedarf_berechnen ermitteln, nicht selbst schätzen.",
                f"Bei Regen ab {LIMITS.regen_grenze_mm} mm in 24 h nicht bewässern.",
                f"Unter {LIMITS.frost_grenze_c} °C Lufttemperatur nicht bewässern (Frost).",
                f"Unter {LIMITS.akku_minimum_prozent} % Akku nicht bewässern, Besitzer informieren.",
                f"Maximal {LIMITS.max_liter_pro_zone} l pro Zone und {LIMITS.max_liter_pro_tag} l pro Tag.",
                f"Im Tank müssen immer {LIMITS.tank_reserve_liter} l Reserve bleiben.",
                f"Bewässerungen über {LIMITS.freigabe_ab_liter} l braucht eine Freigabe durch den Besitzer.",
                "Jede Zone höchstens einmal pro Tag bewässern.",
            ],
        }

    def wasserbedarf_berechnen() -> dict:
        """Deterministische Berechnung: das LLM soll hier nicht selbst rechnen."""
        bedarf = {}
        for zone, meta in ZONES.items():
            avg = garden.plausible_average(zone)
            defekt = sum(1 for v in garden.zone_readings(zone) if not is_plausible(v))
            if avg is None:
                bedarf[zone] = {"name": meta["name"], "status": "keine plausiblen Sensorwerte", "empfohlene_liter": 0}
                continue
            defizit = max(meta["ziel_feuchte"] - avg, 0)
            liter = min(round(defizit * meta["liter_pro_prozentpunkt"]), LIMITS.max_liter_pro_zone)
            bedarf[zone] = {
                "name": meta["name"],
                "mittelwert_prozent": avg,
                "ziel_prozent": meta["ziel_feuchte"],
                "defizit_prozentpunkte": round(defizit, 1),
                "empfohlene_liter": liter,
                "defekte_sensoren": defekt,
                "heute_schon_bewaessert": garden.zone_watered_today(zone),
            }
        return {"bedarf": bedarf}

    def bewaesserung_starten(zone: str, liter: float) -> dict:
        result = garden.irrigate(zone, float(liter))
        return {"status": "ausgefuehrt", **result}

    def besitzer_benachrichtigen(nachricht: str) -> dict:
        notifications.append(nachricht)
        return {"status": "gesendet", "kanal": "Push-Nachricht (simuliert)"}

    tools = [
        Tool("sensoren_lesen", "Liest alle 16 Bodenfeuchtesensoren (4 Zonen A-D), Luft- und Bodentemperatur und den Akkustand.",
             NO_PARAMS, sensoren_lesen, "read"),
        Tool("wettervorhersage_abrufen", "Liefert die Wettervorhersage für die nächsten 24 Stunden.",
             NO_PARAMS, wettervorhersage_abrufen, "read"),
        Tool("tankstand_pruefen", "Liefert den Füllstand des Wassertanks und die nutzbare Wassermenge.",
             NO_PARAMS, tankstand_pruefen, "read"),
        Tool("bewaesserungshistorie_abrufen", "Liefert die letzten Bewässerungen und den heutigen Verbrauch.",
             NO_PARAMS, bewaesserungshistorie_abrufen, "read"),
        Tool("giessregeln_abrufen", "Liefert die Gießregeln, Ziel-Feuchten und Grenzwerte. Vor jeder Entscheidung abrufen.",
             NO_PARAMS, giessregeln_abrufen, "read"),
        Tool("wasserbedarf_berechnen", "Berechnet pro Zone den Wasserbedarf in Litern aus den plausiblen Sensorwerten.",
             NO_PARAMS, wasserbedarf_berechnen, "read"),
        Tool("bewaesserung_starten", "Startet die Bewässerung einer Zone mit einer bestimmten Wassermenge.",
             {
                 "type": "object",
                 "properties": {
                     "zone": {"type": "string", "enum": list(ZONES), "description": "Zone A, B, C oder D"},
                     "liter": {"type": "number", "description": "Wassermenge in Litern, laut wasserbedarf_berechnen"},
                 },
                 "required": ["zone", "liter"],
             },
             bewaesserung_starten, "action"),
        Tool("besitzer_benachrichtigen", "Sendet dem Gartenbesitzer eine kurze Nachricht (z. B. bei Problemen oder ausgelassener Bewässerung).",
             {
                 "type": "object",
                 "properties": {"nachricht": {"type": "string", "description": "Kurze Nachricht auf Deutsch"}},
                 "required": ["nachricht"],
             },
             besitzer_benachrichtigen, "action"),
    ]
    return {t.name: t for t in tools}
