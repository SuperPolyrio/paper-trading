"""Durable local buffering for paper-service health samples."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class HealthSpoolBatch:
    records: tuple[dict[str, Any], ...]
    consumed_lines: int


class HealthSpool:
    """Append-only JSONL spool acknowledged after a successful DB write."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def append(self, payload: dict[str, Any]) -> None:
        if not payload.get("sampled_at"):
            raise ValueError("health spool payload requires sampled_at")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = (
            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
            + "\n"
        ).encode("utf-8")
        descriptor = os.open(
            self.path,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            0o600,
        )
        try:
            os.write(descriptor, line)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def peek(self, *, limit: int = 1000) -> HealthSpoolBatch:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return HealthSpoolBatch((), 0)
        records: list[dict[str, Any]] = []
        consumed = 0
        for line in lines:
            if len(records) >= max(1, int(limit)):
                break
            consumed += 1
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("sampled_at"):
                records.append(payload)
        return HealthSpoolBatch(tuple(records), consumed)

    def acknowledge(self, consumed_lines: int) -> None:
        consumed = max(0, int(consumed_lines))
        if consumed == 0:
            return
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines(keepends=True)
        except FileNotFoundError:
            return
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text("".join(lines[consumed:]), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)
