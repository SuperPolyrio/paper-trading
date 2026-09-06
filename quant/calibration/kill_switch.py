"""Filesystem kill switch used by every live-probe boundary."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class KillSwitchActive(RuntimeError):
    pass


@dataclass(frozen=True)
class KillSwitch:
    path: Path

    @property
    def active(self) -> bool:
        return self.path.exists()

    def require_clear(self) -> None:
        if self.active:
            raise KillSwitchActive(f"live probe kill switch is active: {self.path}")

    def activate(self, reason: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(str(reason).strip() + "\n", encoding="utf-8")
        temporary.replace(self.path)

    def reason(self) -> str | None:
        if not self.active:
            return None
        try:
            return self.path.read_text(encoding="utf-8").strip() or "active"
        except OSError:
            return "active"
