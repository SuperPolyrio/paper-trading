"""Safe parser for the official accounting snapshot ZIP."""

from __future__ import annotations

import csv
import hashlib
import io
import stat
import zipfile
from typing import Any

from .models import AccountingEquity, AccountingPosition, ParsedAccountingSnapshot
from .normalizer import parse_decimal, parse_timestamp


class AccountingSnapshotParser:
    REQUIRED_FILES = frozenset({"positions.csv", "equity.csv"})
    POSITION_COLUMNS = frozenset(
        {"conditionId", "asset", "size", "curPrice", "valuationTime"}
    )
    EQUITY_COLUMNS = frozenset(
        {"cashBalance", "positionsValue", "equity", "valuationTime"}
    )

    def __init__(
        self,
        *,
        max_files: int = 8,
        max_uncompressed_bytes: int = 16 * 1024 * 1024,
        max_rows: int = 100_000,
    ) -> None:
        self.max_files = int(max_files)
        self.max_uncompressed_bytes = int(max_uncompressed_bytes)
        self.max_rows = int(max_rows)

    def parse(self, content: bytes) -> ParsedAccountingSnapshot:
        zip_hash = hashlib.sha256(content).hexdigest()
        try:
            archive = zipfile.ZipFile(io.BytesIO(content))
        except zipfile.BadZipFile as exc:
            raise ValueError("official accounting snapshot is not a valid ZIP") from exc
        with archive:
            infos = archive.infolist()
            if len(infos) > self.max_files:
                raise ValueError("official accounting ZIP contains too many files")
            total_size = sum(int(info.file_size) for info in infos)
            if total_size > self.max_uncompressed_bytes:
                raise ValueError(
                    "official accounting ZIP exceeds uncompressed size limit"
                )
            names = set()
            for info in infos:
                self._validate_member(info)
                names.add(info.filename)
            missing = self.REQUIRED_FILES - names
            if missing:
                raise ValueError(
                    "official accounting ZIP is missing: " + ",".join(sorted(missing))
                )
            positions_bytes = archive.read("positions.csv")
            equity_bytes = archive.read("equity.csv")
        positions = self._parse_positions(positions_bytes)
        equity = self._parse_equity(equity_bytes)
        valuation_times = {row.valuation_time for row in positions}
        valuation_times.add(equity.valuation_time)
        if len(valuation_times) != 1:
            raise ValueError("official accounting CSV valuationTime values disagree")
        return ParsedAccountingSnapshot(
            positions=positions,
            equity=equity,
            positions_csv_sha256=hashlib.sha256(positions_bytes).hexdigest(),
            equity_csv_sha256=hashlib.sha256(equity_bytes).hexdigest(),
            zip_sha256=zip_hash,
        )

    def _validate_member(self, info: zipfile.ZipInfo) -> None:
        name = str(info.filename)
        if (
            not name
            or name.startswith(("/", "\\"))
            or "\\" in name
            or any(part in {"", ".", ".."} for part in name.split("/"))
        ):
            raise ValueError("unsafe path in official accounting ZIP")
        if info.flag_bits & 0x1:
            raise ValueError("encrypted official accounting ZIP is unsupported")
        unix_mode = (info.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(unix_mode)
        if file_type and file_type != stat.S_IFREG:
            raise ValueError("non-regular member in official accounting ZIP")

    def _parse_positions(self, content: bytes) -> tuple[AccountingPosition, ...]:
        rows = self._csv_rows(
            content, required=self.POSITION_COLUMNS, name="positions.csv"
        )
        positions: list[AccountingPosition] = []
        seen: set[str] = set()
        for row in rows:
            asset_id = _required_text(row.get("asset"), "asset")
            if asset_id in seen:
                raise ValueError(f"duplicate asset in positions.csv: {asset_id}")
            seen.add(asset_id)
            positions.append(
                AccountingPosition(
                    condition_id=_required_text(row.get("conditionId"), "conditionId"),
                    asset_id=asset_id,
                    size=_decimal(row.get("size"), "size"),
                    current_price=_decimal(row.get("curPrice"), "curPrice"),
                    valuation_time=parse_timestamp(
                        row.get("valuationTime"), field_name="valuationTime"
                    ),
                )
            )
        return tuple(sorted(positions, key=lambda item: item.asset_id))

    def _parse_equity(self, content: bytes) -> AccountingEquity:
        rows = self._csv_rows(content, required=self.EQUITY_COLUMNS, name="equity.csv")
        if len(rows) != 1:
            raise ValueError("equity.csv must contain exactly one data row")
        row = rows[0]
        return AccountingEquity(
            cash_balance=_decimal(row.get("cashBalance"), "cashBalance"),
            positions_value=_decimal(row.get("positionsValue"), "positionsValue"),
            equity=_decimal(row.get("equity"), "equity"),
            valuation_time=parse_timestamp(
                row.get("valuationTime"), field_name="valuationTime"
            ),
        )

    def _csv_rows(
        self, content: bytes, *, required: frozenset[str], name: str
    ) -> list[dict[str, Any]]:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{name} is not UTF-8") from exc
        reader = csv.DictReader(io.StringIO(text))
        columns = set(reader.fieldnames or ())
        missing = required - columns
        if missing:
            raise ValueError(f"{name} missing columns: {','.join(sorted(missing))}")
        rows = [dict(row) for row in reader]
        if len(rows) > self.max_rows:
            raise ValueError(f"{name} exceeds row limit")
        return rows


def _required_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"official accounting field is missing: {field_name}")
    return text


def _decimal(value: Any, field_name: str):
    parsed = parse_decimal(value, field_name=field_name)
    assert parsed is not None
    return parsed
