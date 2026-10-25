"""Smart-Garden-Bewässerungsagent - Kommandozeile.

Beispiele:
    python main.py                               # Szenario "normal", Modus aus .env (Standard: demo)
    python main.py --mode ollama                 # mit lokalem Modell über Ollama
    python main.py --scenario injection          # Prompt-Injection-Szenario
    python main.py --scenario injection --no-input-guard   # zeigt die nächste Schutzebene
    python main.py --list                        # alle Szenarien anzeigen
"""
import argparse
import os
import sys

from agent.agent import DEFAULT_GOAL, Printer, run_agent
from agent.audit import AuditLog
from agent.config import LIMITS, llm_config
from agent.garden import Garden, list_scenarios
from agent.guardrails import InputGuard, make_approver
from agent.llm import make_backend


def main() -> int:
    if os.name == "nt":
        os.system("")  # ANSI-Farben in der Windows-Konsole aktivieren
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

    p = argparse.ArgumentParser(description="KI-Agent für die Bewässerung eines Smart Gardens")
    p.add_argument("--scenario", default="normal", help="Szenario aus dem Ordner scenarios/")
    p.add_argument("--mode", choices=["demo", "ollama", "openai"], help="LLM-Anbindung (Standard: LLM_PROVIDER aus .env)")
    p.add_argument("--model", help="Modellname, z. B. qwen2.5:7b oder gpt-4o-mini")
    p.add_argument("--goal", default=DEFAULT_GOAL, help="Auftrag an den Agenten")
    approval = p.add_mutually_exclusive_group()
    approval.add_argument("--auto-approve", action="store_true", help="alle Freigaben automatisch erteilen")
    approval.add_argument("--auto-deny", action="store_true", help="alle Freigaben automatisch verweigern")
    p.add_argument("--no-input-guard", action="store_true", help="Input-Guard abschalten (nur zur Demonstration)")
    p.add_argument("--list", action="store_true", help="Szenarien auflisten")
    args = p.parse_args()

    out = Printer()
    if args.list:
        for name in list_scenarios():
            garden = Garden.from_scenario(name, persist=False)
            out(f"{name:<10} {garden.state['beschreibung']}")
        return 0

    cfg = llm_config(args.mode, args.model)
    try:
        backend = make_backend(cfg)
    except RuntimeError as err:
        out(f"Fehler: {err}", "red")
        return 1

    garden = Garden.from_scenario(args.scenario)
    audit = AuditLog()
    mode = "yes" if args.auto_approve else "no" if args.auto_deny else "interactive"

    out("🌱 Smart-Garden-Bewässerungsagent", "bold")
    out(f"Szenario: {args.scenario} – {garden.state['beschreibung']}", "grey")
    out(f"Modell:   {cfg.provider} / {cfg.model}   Freigabe: {mode}   "
        f"Input-Guard: {'aus' if args.no_input_guard else 'an'}", "grey")
    out(f"Auftrag:  {args.goal}", "grey")

    try:
        result = run_agent(garden, backend, make_approver(mode), goal=args.goal,
                           input_guard=InputGuard(enabled=not args.no_input_guard), audit=audit, printer=out)
    except Exception as err:  # z. B. Ollama nicht gestartet
        out(f"\nFehler bei der Ausführung: {err}", "red")
        if cfg.provider == "ollama":
            out("Läuft Ollama? Test: curl http://localhost:11434/v1/models", "yellow")
        return 1

    out("\n══════════ Ergebnis ══════════", "bold")
    if result.final_text:
        out(result.final_text)
    if result.stopped_by:
        out(f"Abgebrochen: {result.stopped_by}", "red")

    out("\nVerifiziert laut Audit-Log:", "bold")
    watered = [a for a in result.executed if a["tool"] == "bewaesserung_starten"]
    for a in watered:
        r = a["ergebnis"]
        out(f"  ✔ Zone {r['zone']}: {r['liter']:g} l  (Feuchte {r['feuchte_vorher']} % → {r['feuchte_nachher']} %)", "green")
    if not watered:
        out("  – keine Bewässerung ausgeführt")
    for r in result.rejected:
        out(f"  ✖ {r['tool']} {r['args']}: {r['grund']}", "red")
    for n in result.notifications:
        out(f"  ✉ Nachricht an Besitzer: {n}", "yellow")
    out(f"  Tank: {garden.tank:g} l   heute verbraucht: {garden.liters_today():g} / {LIMITS.max_liter_pro_tag} l", "grey")
    for w in result.output_warnings:
        out(f"  ⚠ Output-Guard: {w}", "yellow")
    if audit.path:
        out(f"\nAudit-Log: {audit.path}", "grey")
    return 0


if __name__ == "__main__":
    sys.exit(main())
