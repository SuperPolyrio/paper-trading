"""Guarded official adapter for one real standard negative-risk conversion."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from eth_abi import decode
from eth_utils import keccak, to_checksum_address

from quant.simulator.admission import (
    AdmissionOperation,
    AdmissionRequest,
    ExposureEffect,
    MemoryAdmissionStore,
    UnifiedAdmissionService,
)

from .settlement_redeemer import (
    CHAIN_ID,
    CONDITIONAL_TOKENS,
    NEG_RISK_ADAPTER,
    RelayerCredentialsMissing,
    SafeSettlementRedeemer,
    SettlementPreflightError,
    SettlementRedeemError,
    _calldata,
)

NEG_RISK_COLLATERAL_ADAPTER = to_checksum_address(
    "0xadA2005600Dec949baf300f4C6120000bDB6eAab"
)


@dataclass(frozen=True)
class NegRiskTokenPair:
    index: int
    condition_id: str
    yes_asset_id: str
    no_asset_id: str


@dataclass(frozen=True)
class NegRiskConvertPreflight:
    market_id: str
    event_slug: str
    source_index: int
    index_set: int
    amount: Decimal
    amount_base_units: int
    amount_out: Decimal
    fee_bips: int
    pairs: tuple[NegRiskTokenPair, ...]
    balances_before: Mapping[str, Decimal]
    signer_address: str
    safe_address: str
    safe_nonce: int
    relayer_nonce: int
    target_contract: str
    calldata: str
    outcome_tokens_approved: bool
    write_route: Mapping[str, Any]
    admission: Mapping[str, Any]
    auth_mode: str
    official_event_payload_hash: str
    checked_at: str

    def as_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row.pop("calldata", None)
        row["amount"] = format(self.amount, "f")
        row["amount_out"] = format(self.amount_out, "f")
        row["balances_before"] = {
            key: format(value, "f") for key, value in self.balances_before.items()
        }
        row["calldata_hash"] = "0x" + keccak(hexstr=self.calldata).hex()
        return row


class SafeNegRiskConvertAdapter(SafeSettlementRedeemer):
    """Convert exactly one NO position through the official pUSD V2 adapter."""

    def __init__(self, *args: Any, max_amount: Decimal = Decimal(1), **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.max_amount = Decimal(max_amount)
        self._admission = UnifiedAdmissionService(store=MemoryAdmissionStore())

    def preflight_convert(
        self,
        *,
        official_event: Mapping[str, Any],
        source_no_asset_id: str,
        amount: Decimal,
        require_auth: bool,
    ) -> NegRiskConvertPreflight:
        amount = Decimal(amount)
        if amount <= 0 or amount > self.max_amount:
            raise SettlementPreflightError(
                f"negative-risk conversion amount must be within (0, {self.max_amount}]"
            )
        raw_units = amount * Decimal(1_000_000)
        if raw_units != raw_units.to_integral_value():
            raise SettlementPreflightError("conversion amount must have at most six decimals")
        amount_base_units = int(raw_units)
        market_id, event_slug, gamma_pairs, event_hash = _normalize_standard_event(
            official_event
        )
        if self._rpc("eth_chainId", []) != hex(CHAIN_ID):
            raise SettlementPreflightError("RPC is not Polygon mainnet")
        self._validate_safe()
        safe_nonce = self._safe_nonce()
        relayer_nonce = self._relayer_nonce()
        if relayer_nonce != safe_nonce:
            raise SettlementPreflightError("relayer nonce does not match Safe nonce")
        question_count = self._uint_call(
            NEG_RISK_ADAPTER,
            "getQuestionCount(bytes32)",
            (market_id,),
            ("bytes32",),
        )
        fee_bips = self._uint_call(
            NEG_RISK_ADAPTER,
            "getFeeBips(bytes32)",
            (market_id,),
            ("bytes32",),
        )
        if question_count != len(gamma_pairs):
            raise SettlementPreflightError("Gamma and onchain question counts differ")
        pairs = self._onchain_pairs(market_id=market_id, count=question_count)
        gamma_tokens = {
            (pair.yes_asset_id, pair.no_asset_id) for pair in gamma_pairs
        }
        if {(pair.yes_asset_id, pair.no_asset_id) for pair in pairs} != gamma_tokens:
            raise SettlementPreflightError("Gamma and onchain negative-risk token sets differ")
        source = [pair for pair in pairs if pair.no_asset_id == str(source_no_asset_id)]
        if len(source) != 1:
            raise SettlementPreflightError("source NO token does not map to exactly one question")
        source_pair = source[0]
        index_set = 1 << source_pair.index
        balances = {"pUSD": self._pusd_balance()}
        for pair in pairs:
            balances[pair.yes_asset_id] = self._token_balance(pair.yes_asset_id)
            balances[pair.no_asset_id] = self._token_balance(pair.no_asset_id)
        if balances[source_pair.no_asset_id] < amount:
            raise SettlementPreflightError("insufficient source NO balance for conversion")
        approved = bool(
            decode(
                ["bool"],
                self._call(
                    CONDITIONAL_TOKENS,
                    "isApprovedForAll(address,address)",
                    (self.safe_address, NEG_RISK_COLLATERAL_ADAPTER),
                    ("address", "address"),
                ),
            )[0]
        )
        if not approved:
            raise SettlementPreflightError(
                "negative-risk collateral adapter is not approved for outcome tokens"
            )
        calldata = _calldata(
            "convertPositions(bytes32,uint256,uint256)",
            (market_id, index_set, amount_base_units),
            ("bytes32", "uint256", "uint256"),
        )
        self._rpc(
            "eth_call",
            [
                {
                    "from": self.safe_address,
                    "to": NEG_RISK_COLLATERAL_ADAPTER,
                    "data": calldata,
                },
                "latest",
            ],
        )
        route = self._write_route()
        if route.get("blocked") is not False:
            raise SettlementPreflightError("write route is blocked or unverified")
        now = datetime.now(timezone.utc)
        decision = self._admission.decide(
            AdmissionRequest(
                request_id=(
                    f"live-neg-risk-convert:{market_id.hex()}:{index_set}:"
                    f"{amount_base_units}:{safe_nonce}"
                ),
                operation=AdmissionOperation.NEG_RISK_CONVERT,
                account_id=self.safe_address.lower(),
                strategy_id="operation-fidelity-live-v1",
                market_id="0x" + market_id.hex(),
                asset_id=source_pair.no_asset_id,
                exposure_effect=ExposureEffect.NEUTRAL,
                exposure_before=amount,
                exposure_after=amount,
                observed_at=now,
                metadata={"source": "neg_risk_convert_adapter", "event_slug": event_slug},
            )
        )
        if not decision.allowed:
            raise SettlementPreflightError("unified admission denied conversion")
        auth_mode = self._auth_mode()
        if auth_mode == "RELAYER_API_KEY":
            self._require_relayer_key_owner()
        if require_auth and auth_mode == "MISSING":
            raise RelayerCredentialsMissing("relayer credentials are missing")
        amount_out_units = amount_base_units - amount_base_units * fee_bips // 10_000
        return NegRiskConvertPreflight(
            market_id="0x" + market_id.hex(),
            event_slug=event_slug,
            source_index=source_pair.index,
            index_set=index_set,
            amount=amount,
            amount_base_units=amount_base_units,
            amount_out=Decimal(amount_out_units) / Decimal(1_000_000),
            fee_bips=fee_bips,
            pairs=pairs,
            balances_before=balances,
            signer_address=self.signer_address,
            safe_address=self.safe_address,
            safe_nonce=safe_nonce,
            relayer_nonce=relayer_nonce,
            target_contract=NEG_RISK_COLLATERAL_ADAPTER,
            calldata=calldata,
            outcome_tokens_approved=approved,
            write_route=route,
            admission=decision.as_dict(),
            auth_mode=auth_mode,
            official_event_payload_hash=event_hash,
            checked_at=now.isoformat(),
        )

    def submit_convert_and_wait(
        self,
        preflight: NegRiskConvertPreflight,
        *,
        metadata: str,
        on_submitted: Callable[[str], None] | None = None,
        poll_seconds: float = 2,
        max_polls: int = 60,
    ) -> dict[str, Any]:
        if preflight.safe_nonce != self._safe_nonce():
            raise SettlementPreflightError("Safe nonce changed after preflight")
        if self._write_route().get("blocked") is not False:
            raise SettlementPreflightError("write route became blocked after preflight")
        request = self._build_safe_request(preflight, metadata=metadata)
        headers = self._auth_headers("POST", "/submit", request, preflight.auth_mode)
        response = self.client.post(
            f"{self.relayer_url}/submit", headers=headers, json=request
        )
        if not response.is_success:
            raise SettlementRedeemError(
                f"relayer submit rejected with HTTP {response.status_code}",
                evidence={"http_status": response.status_code},
            )
        transaction_id = str(response.json().get("transactionID") or "")
        if not transaction_id:
            raise SettlementRedeemError("relayer submit returned no transactionID")
        if on_submitted:
            on_submitted(transaction_id)
        terminal: Mapping[str, Any] | None = None
        for _ in range(max(1, int(max_polls))):
            for row in self._relayer_transaction(transaction_id):
                state = str(row.get("state") or "")
                if state in {"STATE_FAILED", "STATE_INVALID"}:
                    raise SettlementRedeemError(
                        f"relayer transaction ended in {state}",
                        evidence={"transaction_id": transaction_id, "state": state},
                    )
                if state in {"STATE_MINED", "STATE_CONFIRMED"}:
                    terminal = row
                    break
            if terminal:
                break
            time.sleep(max(0.1, float(poll_seconds)))
        if terminal is None:
            raise SettlementRedeemError(
                "conversion terminal state is unknown; do not resubmit",
                evidence={"transaction_id": transaction_id, "state": "UNKNOWN"},
            )
        tx_hash = str(terminal.get("transactionHash") or "")
        receipt = self._rpc("eth_getTransactionReceipt", [tx_hash]) if tx_hash else None
        if not isinstance(receipt, Mapping) or int(str(receipt.get("status") or "0x0"), 16) != 1:
            raise SettlementRedeemError(
                "conversion receipt is absent or failed",
                evidence={"transaction_id": transaction_id, "transaction_hash": tx_hash},
            )
        after = {"pUSD": self._pusd_balance()}
        for pair in preflight.pairs:
            after[pair.yes_asset_id] = self._token_balance(pair.yes_asset_id)
            after[pair.no_asset_id] = self._token_balance(pair.no_asset_id)
        expected = dict(preflight.balances_before)
        for asset_id, delta in neg_risk_conversion_deltas(
            pairs=preflight.pairs,
            source_index=preflight.source_index,
            amount=preflight.amount,
            amount_out=preflight.amount_out,
        ).items():
            expected[asset_id] += delta
        mismatches = {
            asset: {"expected": str(expected[asset]), "actual": str(after[asset])}
            for asset in expected
            if expected[asset] != after[asset]
        }
        if mismatches:
            raise SettlementRedeemError(
                "conversion mined but token/collateral conservation failed",
                evidence={
                    "transaction_id": transaction_id,
                    "transaction_hash": tx_hash,
                    "mismatches": mismatches,
                },
            )
        return {
            "schema_version": "live-neg-risk-convert-v1",
            "operation_type": "NEG_RISK_CONVERT",
            "transaction_id": transaction_id,
            "transaction_hash": tx_hash,
            "block_number": int(str(receipt["blockNumber"]), 16),
            "state": str(terminal.get("state") or ""),
            "preflight": preflight.as_dict(),
            "before": {key: format(value, "f") for key, value in preflight.balances_before.items()},
            "after": {key: format(value, "f") for key, value in after.items()},
            "balance_conservation": "PASS",
            "polygon_receipt": dict(receipt),
            "confirmed_at": datetime.now(timezone.utc).isoformat(),
        }

    def _validate_safe(self) -> None:
        if self._derive_safe().lower() != self.safe_address.lower():
            raise SettlementPreflightError("configured funder is not signer-derived Safe")
        code = self._rpc("eth_getCode", [self.safe_address, "latest"])
        if not code or int(code, 16) == 0:
            raise SettlementPreflightError("configured Safe has no deployed bytecode")
        owners = {
            to_checksum_address(item).lower()
            for item in decode(["address[]"], self._call(self.safe_address, "getOwners()"))[0]
        }
        threshold = self._uint_call(self.safe_address, "getThreshold()")
        if self.signer_address.lower() not in owners or threshold != 1:
            raise SettlementPreflightError("unsupported Safe owner or threshold")

    def _onchain_pairs(self, *, market_id: bytes, count: int) -> tuple[NegRiskTokenPair, ...]:
        base = int.from_bytes(market_id, "big")
        return tuple(
            NegRiskTokenPair(
                index=index,
                condition_id="",
                yes_asset_id=str(
                    self._uint_call(
                        NEG_RISK_ADAPTER,
                        "getPositionId(bytes32,bool)",
                        ((base | index).to_bytes(32, "big"), True),
                        ("bytes32", "bool"),
                    )
                ),
                no_asset_id=str(
                    self._uint_call(
                        NEG_RISK_ADAPTER,
                        "getPositionId(bytes32,bool)",
                        ((base | index).to_bytes(32, "big"), False),
                        ("bytes32", "bool"),
                    )
                ),
            )
            for index in range(count)
        )

    def _uint_call(
        self,
        to: str,
        signature: str,
        values: tuple[Any, ...] = (),
        types: tuple[str, ...] = (),
    ) -> int:
        return int(decode(["uint256"], self._call(to, signature, values, types))[0])


def _normalize_standard_event(
    event: Mapping[str, Any],
) -> tuple[bytes, str, tuple[NegRiskTokenPair, ...], str]:
    if not _bool(event.get("enableNegRisk")) or _bool(event.get("negRiskAugmented")):
        raise SettlementPreflightError("event is not standard negative risk")
    if not _bool_default(event.get("active"), True) or _bool(event.get("closed")):
        raise SettlementPreflightError("negative-risk event is not active")
    market_id = bytes.fromhex(str(event.get("negRiskMarketID") or "").removeprefix("0x"))
    if len(market_id) != 32 or market_id[-1] != 0:
        raise SettlementPreflightError("negative-risk market ID is invalid")
    raw_markets = event.get("markets")
    if not isinstance(raw_markets, list) or len(raw_markets) < 2:
        raise SettlementPreflightError("negative-risk event markets are incomplete")
    pairs = []
    for index, row in enumerate(raw_markets):
        if not isinstance(row, Mapping) or not _bool(row.get("negRisk")):
            raise SettlementPreflightError("event contains a non-negative-risk market")
        tokens = _json_list(row.get("clobTokenIds"))
        outcomes = [str(item).upper() for item in _json_list(row.get("outcomes"))]
        if len(tokens) != 2 or outcomes != ["YES", "NO"]:
            raise SettlementPreflightError("negative-risk token pair is invalid")
        pairs.append(
            NegRiskTokenPair(
                index=index,
                condition_id=str(row.get("conditionId") or ""),
                yes_asset_id=str(tokens[0]),
                no_asset_id=str(tokens[1]),
            )
        )
    payload = json.dumps(event, sort_keys=True, default=str, separators=(",", ":"))
    event_hash = str(event.get("_raw_payload_hash") or hashlib.sha256(payload.encode()).hexdigest())
    return market_id, str(event.get("slug") or ""), tuple(pairs), event_hash


def neg_risk_conversion_deltas(
    *,
    pairs: tuple[NegRiskTokenPair, ...],
    source_index: int,
    amount: Decimal,
    amount_out: Decimal,
) -> dict[str, Decimal]:
    source = [pair for pair in pairs if pair.index == int(source_index)]
    if len(source) != 1:
        raise ValueError("source index does not map to exactly one question")
    deltas = {source[0].no_asset_id: -Decimal(amount)}
    for pair in pairs:
        if pair.index != int(source_index):
            deltas[pair.yes_asset_id] = Decimal(amount_out)
    return deltas


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    return []


def _bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def _bool_default(value: Any, default: bool) -> bool:
    return default if value is None else _bool(value)
