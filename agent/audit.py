"""Audit-Log: jeder Schritt des Agenten wird als JSON-Zeile protokolliert (Nachvollziehbarkeit)."""
import json
from datetime import datetime
from pathlib import Path

from .config import LOG_DIR


class AuditLog:
    def __init__(self, log_dir: Path = LOG_DIR, enabled: bool = True):
        self.entries: list[dict] = []
        self.path = None
        if enabled:
            log_dir.mkdir(exist_ok=True)
            stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
            self.path = log_dir / f"audit-{stamp}.jsonl"
            n = 1
            while self.path.exists():  # mehrere Läufe in derselben Sekunde
                n += 1
                self.path = log_dir / f"audit-{stamp}-{n}.jsonl"

    def log(self, event: str, **data) -> None:
        entry = {"zeit": datetime.now().isoformat(timespec="seconds"), "ereignis": event, **data}
        self.entries.append(entry)
        if self.path:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    def executed_actions(self) -> list[dict]:
        return [e for e in self.entries if e["ereignis"] == "aktion_ausgefuehrt"]
