"""Guarded Safe adapter for real standard-CTF split and merge validation."""

from __future__ import annotations

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
    PUSD,
    ZERO_BYTES32,
    RelayerCredentialsMissing,
    SafeSettlementRedeemer,
    SettlementPreflightError,
    SettlementRedeemError,
    _calldata,
)

CTF_COLLATERAL_ADAPTER = to_checksum_address(
    "0xAdA100Db00Ca00073811820692005400218FcE1f"
)
USDC_E = to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")


@dataclass(frozen=True)
class CtfOperationPreflight:
    operation_type: str
    condition_id: str
    yes_asset_id: str
    no_asset_id: str
    amount: Decimal
    amount_base_units: int
    signer_address: str
    safe_address: str
    safe_nonce: int
    relayer_nonce: int
    pusd_balance: Decimal
    yes_balance: Decimal
    no_balance: Decimal
    pusd_allowance_base_units: int
    outcome_tokens_approved: bool
    target_contract: str
    calldata: str
    write_route: Mapping[str, Any]
    admission: Mapping[str, Any]
    auth_mode: str
    checked_at: str

    def as_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row.pop("calldata", None)
        row["amount"] = format(self.amount, "f")
        row["pusd_balance"] = format(self.pusd_balance, "f")
        row["yes_balance"] = format(self.yes_balance, "f")
        row["no_balance"] = format(self.no_balance, "f")
        row["calldata_hash"] = "0x" + keccak(hexstr=self.calldata).hex()
        return row


class SafeCtfPositionOperationAdapter(SafeSettlementRedeemer):
    """Submit one exact standard-CTF split or merge through the official relayer."""

    def __init__(self, *args: Any, max_amount: Decimal = Decimal(1), **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.max_amount = Decimal(max_amount)
        self._admission = UnifiedAdmissionService(store=MemoryAdmissionStore())

    def preflight_operation(
        self,
        *,
        operation_type: str,
        condition_id: str,
        yes_asset_id: str,
        no_asset_id: str,
        amount: Decimal,
        require_auth: bool,
    ) -> CtfOperationPreflight:
        operation = str(operation_type).upper()
        if operation not in {"SPLIT", "MERGE"}:
            raise ValueError("operation_type must be SPLIT or MERGE")
        amount = Decimal(amount)
        if amount <= 0 or amount > self.max_amount:
            raise SettlementPreflightError(
                f"CTF amount must be within (0, {self.max_amount}]"
            )
        base_units_decimal = amount * Decimal(1000000)
        if base_units_decimal != base_units_decimal.to_integral_value():
            raise SettlementPreflightError("CTF amount must have at most six decimals")
        amount_base_units = int(base_units_decimal)
        condition = bytes.fromhex(str(condition_id).removeprefix("0x"))
        if len(condition) != 32:
            raise SettlementPreflightError("condition_id must be bytes32")
        if self._rpc("eth_chainId", []) != hex(CHAIN_ID):
            raise SettlementPreflightError("RPC is not Polygon mainnet")
        if self._derive_safe().lower() != self.safe_address.lower():
            raise SettlementPreflightError("configured funder is not the signer-derived Safe")
        code = self._rpc("eth_getCode", [self.safe_address, "latest"])
        if not code or int(code, 16) == 0:
            raise SettlementPreflightError("configured Safe has no deployed bytecode")
        owners = {
            to_checksum_address(item).lower()
            for item in decode(["address[]"], self._call(self.safe_address, "getOwners()"))[0]
        }
        threshold = int(decode(["uint256"], self._call(self.safe_address, "getThreshold()"))[0])
        if self.signer_address.lower() not in owners or threshold != 1:
            raise SettlementPreflightError("unsupported Safe owner or threshold")
        safe_nonce = self._safe_nonce()
        relayer_nonce = self._relayer_nonce()
        if relayer_nonce != safe_nonce:
            raise SettlementPreflightError("relayer nonce does not match Safe nonce")

        expected_yes = self._position_id(condition=condition, index_set=1)
        expected_no = self._position_id(condition=condition, index_set=2)
        if expected_yes != int(yes_asset_id) or expected_no != int(no_asset_id):
            raise SettlementPreflightError("CLOB token IDs do not match onchain CTF position IDs")
        pusd_balance = self._pusd_balance()
        yes_balance = self._token_balance(yes_asset_id)
        no_balance = self._token_balance(no_asset_id)
        allowance = int(
            decode(
                ["uint256"],
                self._call(
                    PUSD,
                    "allowance(address,address)",
                    (self.safe_address, CTF_COLLATERAL_ADAPTER),
                    ("address", "address"),
                ),
            )[0]
        )
        outcome_tokens_approved = bool(
            decode(
                ["bool"],
                self._call(
                    CONDITIONAL_TOKENS,
                    "isApprovedForAll(address,address)",
                    (self.safe_address, CTF_COLLATERAL_ADAPTER),
                    ("address", "address"),
                ),
            )[0]
        )
        if operation == "SPLIT":
            if pusd_balance < amount:
                raise SettlementPreflightError("insufficient pUSD for split")
            if allowance < amount_base_units:
                raise SettlementPreflightError("insufficient pUSD allowance for CTF split")
            signature = "splitPosition(address,bytes32,bytes32,uint256[],uint256)"
            exposure_effect = ExposureEffect.NEUTRAL
            admission_operation = AdmissionOperation.SPLIT
        else:
            if yes_balance < amount or no_balance < amount:
                raise SettlementPreflightError("insufficient complete-set tokens for merge")
            if not outcome_tokens_approved:
                raise SettlementPreflightError(
                    "CTF collateral adapter is not approved for outcome tokens"
                )
            signature = "mergePositions(address,bytes32,bytes32,uint256[],uint256)"
            exposure_effect = ExposureEffect.NEUTRAL
            admission_operation = AdmissionOperation.MERGE
        calldata = _calldata(
            signature,
            (PUSD, ZERO_BYTES32, condition, [1, 2], amount_base_units),
            ("address", "bytes32", "bytes32", "uint256[]", "uint256"),
        )
        self._rpc(
            "eth_call",
            [
                {
                    "from": self.safe_address,
                    "to": CTF_COLLATERAL_ADAPTER,
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
                    f"live-ctf:{operation}:{condition_id}:{amount_base_units}:"
                    f"{safe_nonce}"
                ),
                operation=admission_operation,
                account_id=self.safe_address.lower(),
                strategy_id="operation-fidelity-live-v1",
                condition_id=str(condition_id),
                exposure_effect=exposure_effect,
                exposure_before=amount,
                exposure_after=amount,
                observed_at=now,
                metadata={"source": "ctf_operation_adapter"},
            )
        )
        if not decision.allowed:
            raise SettlementPreflightError("unified admission denied CTF operation")
        auth_mode = self._auth_mode()
        if auth_mode == "RELAYER_API_KEY":
            self._require_relayer_key_owner()
        if require_auth and auth_mode == "MISSING":
            raise RelayerCredentialsMissing("relayer credentials are missing")
        return CtfOperationPreflight(
            operation_type=operation,
            condition_id=str(condition_id),
            yes_asset_id=str(yes_asset_id),
            no_asset_id=str(no_asset_id),
            amount=amount,
            amount_base_units=amount_base_units,
            signer_address=self.signer_address,
            safe_address=self.safe_address,
            safe_nonce=safe_nonce,
            relayer_nonce=relayer_nonce,
            pusd_balance=pusd_balance,
            yes_balance=yes_balance,
            no_balance=no_balance,
            pusd_allowance_base_units=allowance,
            outcome_tokens_approved=outcome_tokens_approved,
            target_contract=CTF_COLLATERAL_ADAPTER,
            calldata=calldata,
            write_route=route,
            admission=decision.as_dict(),
            auth_mode=auth_mode,
            checked_at=now.isoformat(),
        )

    def submit_operation_and_wait(
        self,
        preflight: CtfOperationPreflight,
        *,
        metadata: str,
        on_submitted: Callable[[str], None] | None = None,
        poll_seconds: float = 2.0,
        max_polls: int = 60,
    ) -> dict[str, Any]:
        if preflight.auth_mode == "MISSING":
            raise RelayerCredentialsMissing("relayer credentials are missing")
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
                f"relayer submit rejected with HTTP {response.status_code}: "
                f"{_safe_response(response)}",
                evidence={"http_status": response.status_code},
            )
        payload = response.json()
        transaction_id = str(payload.get("transactionID") or "")
        if not transaction_id:
            raise SettlementRedeemError("relayer submit returned no transactionID")
        if on_submitted is not None:
            on_submitted(transaction_id)
        terminal: Mapping[str, Any] | None = None
        for _ in range(max(1, int(max_polls))):
            for row in self._relayer_transaction(transaction_id):
                state = str(row.get("state") or "")
                if state in {"STATE_FAILED", "STATE_INVALID"}:
                    raise SettlementRedeemError(
                        f"relayer transaction ended in {state}: {transaction_id}",
                        evidence={"transaction_id": transaction_id, "state": state},
                    )
                if state in {"STATE_MINED", "STATE_CONFIRMED"}:
                    terminal = row
                    break
            if terminal is not None:
                break
            time.sleep(max(0.1, float(poll_seconds)))
        if terminal is None:
            raise SettlementRedeemError(
                "relayer transaction terminal state is unknown; do not resubmit",
                evidence={"transaction_id": transaction_id, "state": "UNKNOWN"},
            )
        tx_hash = str(terminal.get("transactionHash") or "")
        receipt = self._rpc("eth_getTransactionReceipt", [tx_hash]) if tx_hash else None
        if not isinstance(receipt, Mapping) or int(str(receipt.get("status") or "0x0"), 16) != 1:
            raise SettlementRedeemError(
                "CTF transaction receipt is absent or failed",
                evidence={"transaction_id": transaction_id, "transaction_hash": tx_hash},
            )
        after = {
            "pusd_balance": self._pusd_balance(),
            "yes_balance": self._token_balance(preflight.yes_asset_id),
            "no_balance": self._token_balance(preflight.no_asset_id),
            "safe_nonce": self._safe_nonce(),
        }
        expected_sign = Decimal(-1) if preflight.operation_type == "SPLIT" else Decimal(1)
        expected = {
            "pusd_balance": preflight.pusd_balance + expected_sign * preflight.amount,
            "yes_balance": preflight.yes_balance - expected_sign * preflight.amount,
            "no_balance": preflight.no_balance - expected_sign * preflight.amount,
            "safe_nonce": preflight.safe_nonce + 1,
        }
        mismatches = {
            key: {"expected": str(expected[key]), "actual": str(after[key])}
            for key in expected
            if after[key] != expected[key]
        }
        if mismatches:
            raise SettlementRedeemError(
                "CTF transaction mined but balance conservation failed",
                evidence={
                    "transaction_id": transaction_id,
                    "transaction_hash": tx_hash,
                    "mismatches": mismatches,
                },
            )
        return {
            "schema_version": "live-ctf-position-operation-v1",
            "operation_type": preflight.operation_type,
            "transaction_id": transaction_id,
            "transaction_hash": tx_hash,
            "block_number": int(str(receipt["blockNumber"]), 16),
            "state": str(terminal.get("state") or ""),
            "preflight": preflight.as_dict(),
            "before": {
                "pusd_balance": format(preflight.pusd_balance, "f"),
                "yes_balance": format(preflight.yes_balance, "f"),
                "no_balance": format(preflight.no_balance, "f"),
                "safe_nonce": preflight.safe_nonce,
            },
            "after": {
                "pusd_balance": format(after["pusd_balance"], "f"),
                "yes_balance": format(after["yes_balance"], "f"),
                "no_balance": format(after["no_balance"], "f"),
                "safe_nonce": after["safe_nonce"],
            },
            "balance_conservation": "PASS",
            "polygon_receipt": dict(receipt),
            "confirmed_at": datetime.now(timezone.utc).isoformat(),
        }

    def _position_id(self, *, condition: bytes, index_set: int) -> int:
        collection_id = decode(
            ["bytes32"],
            self._call(
                CONDITIONAL_TOKENS,
                "getCollectionId(bytes32,bytes32,uint256)",
                (ZERO_BYTES32, condition, int(index_set)),
                ("bytes32", "bytes32", "uint256"),
            ),
        )[0]
        return int(
            decode(
                ["uint256"],
                self._call(
                    CONDITIONAL_TOKENS,
                    "getPositionId(address,bytes32)",
                    (USDC_E, collection_id),
                    ("address", "bytes32"),
                ),
            )[0]
        )


def _safe_response(response: Any) -> str:
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        payload = response.text
    if isinstance(payload, Mapping):
        for key in ("error", "errorMsg", "message", "state"):
            if payload.get(key):
                return str(payload[key])[:300]
        return "response object contained no public error message"
    return str(payload)[:300]
