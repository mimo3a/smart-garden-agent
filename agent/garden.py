"""Simulierter Smart Garden.

Ersetzt die echte Hardware (STM32 + ADS1115-Sensoren) und das Backend:
4 Zonen mit je 4 Bodenfeuchtesensoren (= 16 Sensoren), Luft- und Bodentemperatur,
Akkustand, Wassertank und Bewässerungshistorie (Langzeitgedächtnis).

Jeder Lauf startet aus einer Szenario-Datei, damit die Ergebnisse reproduzierbar sind.
Der Zustand wird nach jeder Aktion in data/state.json gespeichert.
"""
import json
import shutil
from datetime import date
from pathlib import Path

from .config import DATA_DIR, SCENARIO_DIR

# Stammdaten der Zonen (würden im echten System aus der Datenbank kommen)
ZONES = {
    "A": {"name": "Tomatenbeet", "ziel_feuchte": 45, "liter_pro_prozentpunkt": 0.8},
    "B": {"name": "Salatbeet", "ziel_feuchte": 55, "liter_pro_prozentpunkt": 0.6},
    "C": {"name": "Kräuterspirale", "ziel_feuchte": 35, "liter_pro_prozentpunkt": 0.4},
    "D": {"name": "Rasen", "ziel_feuchte": 40, "liter_pro_prozentpunkt": 1.2},
}

SENSOR_MIN, SENSOR_MAX = 5, 95  # Werte außerhalb gelten als Sensorfehler


def is_plausible(value: float) -> bool:
    return SENSOR_MIN <= value <= SENSOR_MAX


def list_scenarios() -> list[str]:
    return sorted(p.stem for p in SCENARIO_DIR.glob("*.json"))


class Garden:
    def __init__(self, state: dict, state_file: Path | None = None):
        self.state = state
        self.state_file = state_file

    # ---------- Laden / Speichern ----------
    @classmethod
    def from_scenario(cls, name: str, persist: bool = True) -> "Garden":
        src = SCENARIO_DIR / f"{name}.json"
        if not src.exists():
            raise FileNotFoundError(f"Szenario '{name}' nicht gefunden. Verfügbar: {list_scenarios()}")
        state = json.loads(src.read_text(encoding="utf-8"))
        # Platzhalter "heute" in der Historie durch das aktuelle Datum ersetzen
        today = date.today().isoformat()
        for entry in state.get("historie", []):
            if entry.get("datum") == "heute":
                entry["datum"] = today
        state_file = None
        if persist:
            DATA_DIR.mkdir(exist_ok=True)
            state_file = DATA_DIR / "state.json"
            shutil.copy(src, DATA_DIR / "scenario_start.json")
        garden = cls(state, state_file)
        garden.save()
        return garden

    def save(self) -> None:
        if self.state_file:
            self.state_file.write_text(json.dumps(self.state, indent=2, ensure_ascii=False), encoding="utf-8")

    # ---------- Lesezugriffe ----------
    @property
    def air_temp(self) -> float:
        return self.state["luft_temperatur_c"]

    @property
    def battery(self) -> int:
        return self.state["akku_prozent"]

    @property
    def tank(self) -> float:
        return self.state["tank_liter"]

    @property
    def weather(self) -> dict:
        return self.state["wetter"]

    def zone_readings(self, zone: str) -> list[float]:
        return self.state["sensoren"][zone]

    def plausible_average(self, zone: str) -> float | None:
        values = [v for v in self.zone_readings(zone) if is_plausible(v)]
        return round(sum(values) / len(values), 1) if values else None

    def liters_today(self) -> float:
        today = date.today().isoformat()
        return sum(e["liter"] for e in self.state["historie"] if e["datum"] == today)

    def zone_watered_today(self, zone: str) -> bool:
        today = date.today().isoformat()
        return any(e["datum"] == today and e["zone"] == zone for e in self.state["historie"])

    # ---------- Aktionen ----------
    def irrigate(self, zone: str, liters: float) -> dict:
        """Führt die Bewässerung aus. Prüfungen passieren VORHER in den Guardrails."""
        before = self.plausible_average(zone)
        gain = liters / ZONES[zone]["liter_pro_prozentpunkt"]
        self.state["sensoren"][zone] = [
            round(min(v + gain, SENSOR_MAX), 1) if is_plausible(v) else v
            for v in self.state["sensoren"][zone]
        ]
        self.state["tank_liter"] = round(self.tank - liters, 1)
        self.state["historie"].append({"datum": date.today().isoformat(), "zone": zone, "liter": liters})
        self.save()
        return {
            "zone": zone,
            "liter": liters,
            "feuchte_vorher": before,
            "feuchte_nachher": self.plausible_average(zone),
            "tank_rest_liter": self.tank,
        }
