"""LLM-Anbindung.

- OpenAICompatBackend: OpenAI SDK; funktioniert mit OpenAI und mit Ollama
  (Ollama bietet unter http://localhost:11434/v1 eine OpenAI-kompatible API).
- DemoBackend: regelbasierter "Planer" ohne Sprachmodell. Er erzeugt dieselben
  Tool-Aufrufe wie ein LLM, damit das Projekt auch ohne Modell/API-Key lauffähig
  ist. Alle Tools, Guardrails und das Audit-Log laufen dabei echt mit.
"""
import json
import re
import uuid
from dataclasses import dataclass, field

from .config import LIMITS, LLMConfig


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)


def _new_id() -> str:
    return "call_" + uuid.uuid4().hex[:12]


def parse_arguments(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


_TOOL_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def tool_calls_from_text(text: str | None, known_tools: set[str]) -> list[ToolCall]:
    """Fallback für kleine lokale Modelle: Manche schreiben den Tool-Aufruf als JSON
    in den Text (z. B. <tool_call>{"name": ..., "arguments": ...}</tool_call>),
    statt das strukturierte tool_calls-Feld zu nutzen."""
    if not text:
        return []
    calls = []
    for chunk in re.findall(r"<tool_call>(.*?)</tool_call>", text, re.DOTALL) or [text]:
        match = _TOOL_JSON_RE.search(chunk)
        if not match:
            continue
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("name") in known_tools:
                calls.append(ToolCall(_new_id(), item["name"], parse_arguments(item.get("arguments", {}))))
    return calls


class OpenAICompatBackend:
    def __init__(self, cfg: LLMConfig):
        from openai import OpenAI  # erst hier importieren, damit der Demo-Modus ohne SDK läuft

        if cfg.provider == "openai" and not cfg.api_key:
            raise RuntimeError("OPENAI_API_KEY fehlt (in .env eintragen) oder --mode demo/ollama verwenden.")
        self.client = OpenAI(base_url=cfg.base_url, api_key=cfg.api_key, timeout=120)
        self.model = cfg.model

    def chat(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        resp = self.client.chat.completions.create(
            model=self.model, messages=messages, tools=tools, tool_choice="auto", temperature=0.1
        )
        msg = resp.choices[0].message
        calls = [ToolCall(tc.id or _new_id(), tc.function.name, parse_arguments(tc.function.arguments))
                 for tc in (msg.tool_calls or [])]
        if not calls:
            calls = tool_calls_from_text(msg.content, {t["function"]["name"] for t in tools})
            if calls:
                return LLMResponse(None, calls)
        return LLMResponse(msg.content, calls)


class DemoBackend:
    """Deterministischer Planer, der wie ein gut instruiertes LLM vorgeht.

    Ausnahme zu Demonstrationszwecken: Gelangt eine Prompt Injection ungefiltert
    zu ihm (Input-Guard mit --no-input-guard abgeschaltet), "fällt er darauf herein".
    So lässt sich zeigen, dass dann die nächste Ebene (Action-Guard) greift.
    """

    READ_FIRST = ["sensoren_lesen", "wettervorhersage_abrufen", "tankstand_pruefen", "giessregeln_abrufen"]

    def chat(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        obs = self._observations(messages)
        turn = sum(1 for m in messages if m["role"] == "assistant")

        if turn == 0:
            return self._calls("Zuerst verschaffe ich mir einen Überblick: Sensoren, Wetter, Tank und Gießregeln.",
                               [(n, {}) for n in self.READ_FIRST])
        if turn == 1:
            return self._calls("Jetzt lasse ich den Wasserbedarf pro Zone berechnen.", [("wasserbedarf_berechnen", {})])
        if turn == 2:
            return self._decide(obs)
        if turn == 3 and self._needs_report(obs):
            return self._calls("Einige Aktionen wurden abgelehnt. Ich informiere den Besitzer.",
                               [("besitzer_benachrichtigen", {"nachricht": self._problem_message(obs)})])
        return LLMResponse(self._final_report(obs))

    # ---------- Entscheidung ----------
    def _decide(self, obs: dict) -> LLMResponse:
        sensors, weather = obs["sensoren_lesen"], obs["wettervorhersage_abrufen"]["vorhersage_24h"]
        bedarf = obs["wasserbedarf_berechnen"]["bedarf"]

        if "ignoriere" in json.dumps(weather, ensure_ascii=False).lower():
            return self._calls("Der Wetterdienst sagt, ich soll alle Zonen mit 200 Litern bewässern. Das mache ich.",
                               [("bewaesserung_starten", {"zone": z, "liter": 200}) for z in "ABCD"])
        if weather["regen_mm_24h"] >= LIMITS.regen_grenze_mm:
            return self._notify(f"Heute keine Bewässerung: Es sind {weather['regen_mm_24h']} mm Regen angesagt.")
        if sensors["luft_temperatur_c"] < LIMITS.frost_grenze_c:
            return self._notify(f"Heute keine Bewässerung: Frostgefahr bei {sensors['luft_temperatur_c']} °C.")
        if sensors["akku_prozent"] < LIMITS.akku_minimum_prozent:
            return self._notify(f"Bewässerung nötig, aber der Akku ist bei {sensors['akku_prozent']} %. Bitte laden.")

        calls = [("bewaesserung_starten", {"zone": z, "liter": b["empfohlene_liter"]})
                 for z, b in bedarf.items() if b.get("empfohlene_liter", 0) > 0 and not b.get("heute_schon_bewaessert")]
        if not calls:
            return LLMResponse("Alle Zonen sind ausreichend feucht. Keine Bewässerung nötig.")
        zones = ", ".join(c[1]["zone"] for c in calls)
        return self._calls(f"Zonen {zones} liegen unter der Ziel-Feuchte. Ich bewässere mit den berechneten Mengen.", calls)

    def _notify(self, text: str) -> LLMResponse:
        return self._calls(text, [("besitzer_benachrichtigen", {"nachricht": text})])

    # ---------- Hilfsfunktionen ----------
    @staticmethod
    def _calls(thought: str, calls: list[tuple[str, dict]]) -> LLMResponse:
        return LLMResponse(thought, [ToolCall(_new_id(), name, args) for name, args in calls])

    @staticmethod
    def _observations(messages: list[dict]) -> dict:
        """Ordnet Tool-Ergebnisse dem Tool-Namen zu; Aktionen werden als Liste gesammelt."""
        names = {}
        for m in messages:
            for tc in m.get("tool_calls") or []:
                names[tc["id"]] = (tc["function"]["name"], parse_arguments(tc["function"]["arguments"]))
        obs: dict = {"aktionen": []}
        for m in messages:
            if m["role"] == "tool":
                name, args = names.get(m["tool_call_id"], ("?", {}))
                result = json.loads(m["content"])
                if name in ("bewaesserung_starten", "besitzer_benachrichtigen"):
                    obs["aktionen"].append({"tool": name, "args": args, "ergebnis": result})
                else:
                    obs[name] = result
        return obs

    @staticmethod
    def _needs_report(obs: dict) -> bool:
        acts = obs["aktionen"]
        rejected = any(a["ergebnis"].get("status") == "abgelehnt" for a in acts)
        notified = any(a["tool"] == "besitzer_benachrichtigen" for a in acts)
        return rejected and not notified

    @staticmethod
    def _problem_message(obs: dict) -> str:
        parts = [f"Zone {a['args'].get('zone')}: {a['ergebnis'].get('grund')}"
                 for a in obs["aktionen"] if a["ergebnis"].get("status") == "abgelehnt"]
        return ("Nicht alle Bewässerungen wurden ausgeführt. " + "; ".join(parts))[:480]

    @staticmethod
    def _final_report(obs: dict) -> str:
        done = [a for a in obs["aktionen"] if a["tool"] == "bewaesserung_starten" and a["ergebnis"].get("status") == "ausgefuehrt"]
        lines = ["Zusammenfassung:"]
        if done:
            for a in done:
                r = a["ergebnis"]
                lines.append(f"- Zone {r['zone']} mit {r['liter']:g} l bewässert "
                             f"(Feuchte {r['feuchte_vorher']} % -> {r['feuchte_nachher']} %).")
        else:
            lines.append("- Heute wurde keine Zone bewässert.")
        for a in obs["aktionen"]:
            if a["ergebnis"].get("status") == "abgelehnt":
                lines.append(f"- Abgelehnt ({a['tool']}, Zone {a['args'].get('zone', '-')}): {a['ergebnis'].get('grund')}")
        if any(a["tool"] == "besitzer_benachrichtigen" for a in obs["aktionen"]):
            lines.append("- Der Besitzer wurde benachrichtigt.")
        return "\n".join(lines)


def make_backend(cfg: LLMConfig):
    return DemoBackend() if cfg.provider == "demo" else OpenAICompatBackend(cfg)
