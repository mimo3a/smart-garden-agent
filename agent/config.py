"""Zentrale Konfiguration: LLM-Anbindung und harte Grenzwerte (Guardrails).

Die Grenzwerte stehen bewusst an EINER Stelle: Das Tool `giessregeln_abrufen`
zeigt sie dem LLM an, und die deterministischen Guardrails setzen sie durch.
"""
import os
from dataclasses import dataclass
from pathlib import Path

try:  # python-dotenv ist optional
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

ROOT = Path(__file__).resolve().parent.parent
SCENARIO_DIR = ROOT / "scenarios"
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
KILL_SWITCH_FILE = ROOT / "STOP"  # existiert diese Datei, bricht der Agent ab


@dataclass(frozen=True)
class Limits:
    max_liter_pro_zone: int = 30        # pro Bewässerungsvorgang
    max_liter_pro_tag: int = 80         # alle Zonen zusammen, pro Tag
    tank_reserve_liter: int = 20        # muss immer im Tank bleiben
    freigabe_ab_liter: int = 15         # darüber: Freigabe durch Menschen
    frost_grenze_c: float = 3.0         # darunter: keine Bewässerung
    akku_minimum_prozent: int = 20      # darunter: Pumpe bleibt aus
    regen_grenze_mm: float = 5.0        # weiche Regel: ab so viel Regen nicht gießen
    max_benachrichtigungen: int = 3     # pro Lauf
    max_schritte: int = 12              # Agent-Loop
    max_tool_aufrufe: int = 30
    timeout_sekunden: int = 300


LIMITS = Limits()


@dataclass(frozen=True)
class LLMConfig:
    provider: str      # "ollama", "openai" oder "demo"
    model: str
    base_url: str | None
    api_key: str


def llm_config(provider: str | None = None, model: str | None = None) -> LLMConfig:
    provider = (provider or os.getenv("LLM_PROVIDER", "demo")).lower()
    if provider == "ollama":
        return LLMConfig(
            provider="ollama",
            model=model or os.getenv("LLM_MODEL", "qwen2.5:7b"),
            base_url=os.getenv("LLM_BASE_URL", "http://localhost:11434/v1"),
            api_key="ollama",  # Ollama ignoriert den Key, das SDK braucht aber einen Wert
        )
    if provider == "openai":
        return LLMConfig(
            provider="openai",
            model=model or os.getenv("LLM_MODEL", "gpt-4o-mini"),
            base_url=os.getenv("LLM_BASE_URL") or None,
            api_key=os.getenv("OPENAI_API_KEY", ""),
        )
    return LLMConfig(provider="demo", model="demo-planner", base_url=None, api_key="")
