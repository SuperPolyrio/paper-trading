"""Guarded Polymarket Safe redemption through the official relayer protocol."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
import time
from typing import Any, Mapping, Sequence

from eth_abi import decode, encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address
import httpx


CHAIN_ID = 137
RELAYER_URL = "https://relayer-v2.polymarket.com"
DATA_API_URL = "https://data-api.polymarket.com"
GEOBLOCK_URL = "https://polymarket.com/api/geoblock"
PUSD = to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")
CONDITIONAL_TOKENS = to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
NEG_RISK_ADAPTER = to_checksum_address("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296")
ZERO_BYTES32 = b"\x00" * 32


class SettlementRedeemError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.evidence = dict(evidence or {})


class SettlementPreflightError(SettlementRedeemError):
    pass


class RelayerCredentialsMissing(SettlementRedeemError):
    pass


@dataclass(frozen=True)
class RedeemCandidate:
    title: str
    outcome: str
    condition_id: str
    asset_id: str
    size: Decimal
    negative_risk: bool
    redeemable: bool
    current_value: Decimal
    current_price: Decimal

    @classmethod
    def from_position(cls, row: Mapping[str, Any]) -> "RedeemCandidate":
        return cls(
            title=str(row.get("title") or ""),
            outcome=str(row.get("outcome") or ""),
            condition_id=str(row.get("conditionId") or ""),
            asset_id=str(row.get("asset") or ""),
            size=Decimal(str(row.get("size") or "0")),
            negative_risk=bool(row.get("negativeRisk")),
            redeemable=bool(row.get("redeemable")),
            current_value=Decimal(str(row.get("currentValue") or "0")),
            current_price=Decimal(str(row.get("curPrice") or "0")),
        )

    def as_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["size"] = format(self.size, "f")
        row["current_value"] = format(self.current_value, "f")
        row["current_price"] = format(self.current_price, "f")
        return row

    @property
    def has_positive_payout(self) -> bool:
        return self.current_value > 0 and self.current_price > 0


@dataclass(frozen=True)
class RedeemPreflight:
    candidate: RedeemCandidate
    signer_address: str
    safe_address: str
    safe_owners: tuple[str, ...]
    safe_threshold: int
    safe_nonce: int
    relayer_nonce: int
    token_balance: Decimal
    pusd_balance: Decimal
    target_contract: str
    calldata: str
    write_route: Mapping[str, Any]
    auth_mode: str
    checked_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate.as_dict(),
            "signer_address": self.signer_address,
            "safe_address": self.safe_address,
            "safe_owners": list(self.safe_owners),
            "safe_threshold": self.safe_threshold,
            "safe_nonce": self.safe_nonce,
            "relayer_nonce": self.relayer_nonce,
            "token_balance": format(self.token_balance, "f"),
            "pusd_balance": format(self.pusd_balance, "f"),
            "target_contract": self.target_contract,
            "calldata_hash": "0x" + keccak(hexstr=self.calldata).hex(),
            "write_route": dict(self.write_route),
            "auth_mode": self.auth_mode,
            "checked_at": self.checked_at,
        }


class SafeSettlementRedeemer:
    """Build, submit once, and reconcile a Safe CTF redemption."""

    def __init__(
        self,
        *,
        private_key: str,
        safe_address: str,
        rpc_url: str,
        proxy_url: str,
        relayer_url: str = RELAYER_URL,
        environ: Mapping[str, str] | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.private_key = str(private_key)
        self.safe_address = to_checksum_address(safe_address)
        self.rpc_url = str(rpc_url)
        self.proxy_url = str(proxy_url)
        self.relayer_url = str(relayer_url).rstrip("/")
        self.environ = os.environ if environ is None else environ
        self.client = httpx.Client(
            proxy=self.proxy_url,
            timeout=timeout_seconds,
            trust_env=False,
        )
        self.signer_address = Account.from_key(self.private_key).address
        self._rpc_id = 0

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "SafeSettlementRedeemer":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def list_redeemable_positions(self) -> list[RedeemCandidate]:
        response = self.client.get(
            f"{DATA_API_URL}/positions",
            params={
                "user": self.safe_address,
                "sizeThreshold": "0",
                "limit": "500",
                "offset": "0",
            },
        )
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list):
            raise SettlementRedeemError("Data API positions response is not a list")
        return [
            candidate
            for row in rows
            if isinstance(row, Mapping)
            and (candidate := RedeemCandidate.from_position(row)).redeemable
            and candidate.size > 0
            and candidate.has_positive_payout
        ]

    def select_candidate(
        self,
        *,
        asset_id: str | None = None,
        condition_id: str | None = None,
        allow_negative_risk: bool = False,
        max_size: Decimal = Decimal("20"),
    ) -> RedeemCandidate:
        candidates = self.list_redeemable_positions()
        if asset_id:
            candidates = [row for row in candidates if row.asset_id == str(asset_id)]
        if condition_id:
            normalized = str(condition_id).lower()
            candidates = [row for row in candidates if row.condition_id.lower() == normalized]
        candidates = [
            row
            for row in candidates
            if (allow_negative_risk or not row.negative_risk)
            and row.size <= max_size
            and row.has_positive_payout
        ]
        if not candidates:
            raise SettlementPreflightError("no redeemable position satisfies the selection policy")
        return min(candidates, key=lambda row: (row.size, row.condition_id, row.asset_id))

    def preflight(
        self,
        candidate: RedeemCandidate,
        *,
        negative_risk_amounts: Sequence[int] | None = None,
        require_auth: bool,
    ) -> RedeemPreflight:
        if not candidate.redeemable or candidate.size <= 0:
            raise SettlementPreflightError("candidate is not redeemable")
        if self._rpc("eth_chainId", []) != hex(CHAIN_ID):
            raise SettlementPreflightError("RPC is not Polygon mainnet")

        expected_safe = self._derive_safe()
        if expected_safe.lower() != self.safe_address.lower():
            raise SettlementPreflightError(
                f"derived Safe {expected_safe} does not match configured funder"
            )
        code = self._rpc("eth_getCode", [self.safe_address, "latest"])
        if not code or int(code, 16) == 0:
            raise SettlementPreflightError("configured Safe has no deployed bytecode")

        owners = tuple(
            to_checksum_address(item)
            for item in decode(["address[]"], self._call(self.safe_address, "getOwners()"))[0]
        )
        threshold = int(decode(["uint256"], self._call(self.safe_address, "getThreshold()"))[0])
        safe_nonce = int(decode(["uint256"], self._call(self.safe_address, "nonce()"))[0])
        if self.signer_address.lower() not in {owner.lower() for owner in owners}:
            raise SettlementPreflightError("configured signer is not a Safe owner")
        if threshold != 1:
            raise SettlementPreflightError(f"unsupported Safe threshold: {threshold}")

        relayer_nonce = self._relayer_nonce()
        if relayer_nonce != safe_nonce:
            raise SettlementPreflightError(
                f"relayer nonce {relayer_nonce} does not match Safe nonce {safe_nonce}"
            )
        token_balance_raw = int(
            decode(
                ["uint256"],
                self._call(
                    CONDITIONAL_TOKENS,
                    "balanceOf(address,uint256)",
                    (self.safe_address, int(candidate.asset_id)),
                    ("address", "uint256"),
                ),
            )[0]
        )
        token_balance = Decimal(token_balance_raw) / Decimal("1000000")
        if token_balance <= 0:
            raise SettlementPreflightError("candidate has no onchain token balance")
        if abs(token_balance - candidate.size) > Decimal("0.0001"):
            raise SettlementPreflightError(
                f"Data API size {candidate.size} does not match chain balance {token_balance}"
            )
        pusd_balance = Decimal(
            int(
                decode(
                    ["uint256"],
                    self._call(PUSD, "balanceOf(address)", (self.safe_address,), ("address",)),
                )[0]
            )
        ) / Decimal("1000000")

        target, calldata = self._redeem_call(
            candidate,
            negative_risk_amounts=negative_risk_amounts,
        )
        self._rpc(
            "eth_call",
            [{"from": self.safe_address, "to": target, "data": calldata}, "latest"],
        )
        route = self._write_route()
        if route.get("blocked") is not False:
            raise SettlementPreflightError("write route is blocked or unverified")
        auth_mode = self._auth_mode()
        if auth_mode == "RELAYER_API_KEY":
            self._require_relayer_key_owner()
        if require_auth and auth_mode == "MISSING":
            raise RelayerCredentialsMissing(
                "configure Relayer API credentials or Builder API credentials before submission"
            )
        return RedeemPreflight(
            candidate=candidate,
            signer_address=self.signer_address,
            safe_address=self.safe_address,
            safe_owners=owners,
            safe_threshold=threshold,
            safe_nonce=safe_nonce,
            relayer_nonce=relayer_nonce,
            token_balance=token_balance,
            pusd_balance=pusd_balance,
            target_contract=target,
            calldata=calldata,
            write_route=route,
            auth_mode=auth_mode,
            checked_at=datetime.now(timezone.utc).isoformat(),
        )

    def submit_and_wait(
        self,
        preflight: RedeemPreflight,
        *,
        metadata: str,
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
            f"{self.relayer_url}/submit",
            headers=headers,
            json=request,
        )
        if not response.is_success:
            evidence = self._submission_rejection_evidence(
                response=response,
                preflight=preflight,
                metadata=metadata,
            )
            transaction = evidence.get("relayer_transaction")
            detail = (
                str(transaction.get("error_message") or "")
                if isinstance(transaction, Mapping)
                else ""
            )
            if not detail:
                detail = str(evidence.get("response_message") or "")
            suffix = f": {detail}" if detail else ""
            raise SettlementRedeemError(
                f"relayer submit rejected with HTTP {response.status_code}{suffix}",
                evidence=evidence,
            )
        submitted = response.json()
        transaction_id = str(submitted.get("transactionID") or "")
        if not transaction_id:
            raise SettlementRedeemError("relayer submit returned no transactionID")

        terminal: Mapping[str, Any] | None = None
        for _ in range(max(1, int(max_polls))):
            rows = self._relayer_transaction(transaction_id)
            for row in rows:
                state = str(row.get("state") or "")
                if state in {"STATE_FAILED", "STATE_INVALID"}:
                    raise SettlementRedeemError(
                        f"relayer transaction ended in {state}: {transaction_id}",
                        evidence={
                            "relayer_transaction": _safe_transaction_evidence(row),
                        },
                    )
                if state in {"STATE_MINED", "STATE_CONFIRMED"}:
                    terminal = row
                    break
            if terminal is not None:
                break
            time.sleep(max(0.1, float(poll_seconds)))
        if terminal is None:
            raise SettlementRedeemError(f"relayer transaction did not reach a terminal state")

        tx_hash = str(terminal.get("transactionHash") or "")
        receipt = self._rpc("eth_getTransactionReceipt", [tx_hash]) if tx_hash else None
        if not isinstance(receipt, Mapping) or int(str(receipt.get("status") or "0x0"), 16) != 1:
            raise SettlementRedeemError("redemption transaction receipt is absent or failed")
        token_after = self._token_balance(preflight.candidate.asset_id)
        pusd_after = self._pusd_balance()
        payout = pusd_after - preflight.pusd_balance
        return {
            "schema_version": "calibration_real_settlement_redeem_v1",
            "transaction_id": transaction_id,
            "transaction_hash": tx_hash,
            "block_number": int(str(receipt["blockNumber"]), 16),
            "state": str(terminal.get("state") or ""),
            "candidate": preflight.candidate.as_dict(),
            "preflight": preflight.as_dict(),
            "before": {
                "token_balance": format(preflight.token_balance, "f"),
                "pusd_balance": format(preflight.pusd_balance, "f"),
            },
            "after": {
                "token_balance": format(token_after, "f"),
                "pusd_balance": format(pusd_after, "f"),
            },
            "observed_payout": format(payout, "f"),
            "token_burn_confirmed": token_after == 0,
            "confirmed_at": datetime.now(timezone.utc).isoformat(),
        }

    def _submission_rejection_evidence(
        self,
        *,
        response: httpx.Response,
        preflight: RedeemPreflight,
        metadata: str,
    ) -> dict[str, Any]:
        try:
            payload: Any = response.json()
        except ValueError:
            payload = response.text
        response_message = _safe_response_message(payload)
        transaction = _safe_transaction_evidence(payload) if isinstance(payload, Mapping) else {}
        if not transaction.get("transaction_id"):
            matching = self._find_recent_relayer_transaction(
                preflight=preflight,
                metadata=metadata,
            )
            if matching is not None:
                transaction = _safe_transaction_evidence(matching)

        try:
            token_balance = self._token_balance(preflight.candidate.asset_id)
            pusd_balance = self._pusd_balance()
            safe_nonce = self._safe_nonce()
            chain_after: dict[str, Any] = {
                "token_balance": format(token_balance, "f"),
                "pusd_balance": format(pusd_balance, "f"),
                "safe_nonce": safe_nonce,
                "token_balance_unchanged": token_balance == preflight.token_balance,
                "pusd_balance_unchanged": pusd_balance == preflight.pusd_balance,
                "safe_nonce_unchanged": safe_nonce == preflight.safe_nonce,
            }
        except Exception as exc:
            chain_after = {
                "audit_error": f"{exc.__class__.__name__}:{str(exc)[:300]}",
            }
        return {
            "http_status": response.status_code,
            "response_message": response_message,
            "relayer_transaction": transaction or None,
            "chain_after_rejection": chain_after,
            "audited_at": datetime.now(timezone.utc).isoformat(),
        }

    def _find_recent_relayer_transaction(
        self,
        *,
        preflight: RedeemPreflight,
        metadata: str,
    ) -> Mapping[str, Any] | None:
        try:
            headers = self._auth_headers(
                "GET",
                "/transactions",
                {},
                preflight.auth_mode,
            )
            response = self.client.get(
                f"{self.relayer_url}/transactions",
                headers=headers,
            )
            if not response.is_success:
                return None
            payload = response.json()
        except Exception:
            return None
        if isinstance(payload, list):
            rows: Sequence[Any] = payload
        elif isinstance(payload, Mapping):
            candidate_rows = payload.get("transactions") or payload.get("data") or []
            rows = candidate_rows if isinstance(candidate_rows, list) else []
        else:
            rows = []
        matches = [
            row
            for row in rows
            if isinstance(row, Mapping)
            and str(row.get("metadata") or "") == metadata
            and str(row.get("nonce") or "") == str(preflight.safe_nonce)
        ]
        return matches[-1] if matches else None

    def _redeem_call(
        self,
        candidate: RedeemCandidate,
        *,
        negative_risk_amounts: Sequence[int] | None,
    ) -> tuple[str, str]:
        condition = bytes.fromhex(candidate.condition_id.removeprefix("0x"))
        if len(condition) != 32:
            raise SettlementPreflightError("condition_id must be bytes32")
        if candidate.negative_risk:
            if negative_risk_amounts is None or len(negative_risk_amounts) != 2:
                raise SettlementPreflightError(
                    "negative-risk redemption requires exact [YES, NO] base-unit amounts"
                )
            values = tuple(int(item) for item in negative_risk_amounts)
            if min(values) < 0:
                raise SettlementPreflightError("negative-risk redeem amounts must be nonnegative")
            payload = _calldata(
                "redeemPositions(bytes32,uint256[])",
                (condition, list(values)),
                ("bytes32", "uint256[]"),
            )
            return NEG_RISK_ADAPTER, payload
        payload = _calldata(
            "redeemPositions(address,bytes32,bytes32,uint256[])",
            (PUSD, ZERO_BYTES32, condition, [1, 2]),
            ("address", "bytes32", "bytes32", "uint256[]"),
        )
        return CONDITIONAL_TOKENS, payload

    def _build_safe_request(
        self,
        preflight: RedeemPreflight,
        *,
        metadata: str,
    ) -> dict[str, Any]:
        from py_builder_relayer_client.builder.safe import build_safe_transaction_request
        from py_builder_relayer_client.config import get_contract_config
        from py_builder_relayer_client.models import (
            OperationType,
            SafeTransaction,
            SafeTransactionArgs,
        )
        from py_builder_relayer_client.signer import Signer

        transaction = SafeTransaction(
            to=preflight.target_contract,
            operation=OperationType.Call,
            data=preflight.calldata,
            value="0",
        )
        args = SafeTransactionArgs(
            from_address=self.signer_address,
            nonce=str(preflight.safe_nonce),
            chain_id=CHAIN_ID,
            transactions=[transaction],
        )
        request = build_safe_transaction_request(
            Signer(self.private_key, CHAIN_ID),
            args,
            get_contract_config(CHAIN_ID),
            metadata=metadata,
        ).to_dict()
        if str(request.get("proxyWallet") or "").lower() != self.safe_address.lower():
            raise SettlementPreflightError("official SDK derived a different Safe")
        return request

    def _auth_mode(self) -> str:
        relayer_key = str(self.environ.get("POLYMARKET_RELAYER_API_KEY") or "").strip()
        relayer_address = str(
            self.environ.get("POLYMARKET_RELAYER_API_KEY_ADDRESS") or ""
        ).strip()
        if relayer_key and relayer_address:
            return "RELAYER_API_KEY"
        builder_names = (
            "POLYMARKET_BUILDER_API_KEY",
            "POLYMARKET_BUILDER_SECRET",
            "POLYMARKET_BUILDER_PASSPHRASE",
        )
        if all(str(self.environ.get(name) or "").strip() for name in builder_names):
            return "BUILDER_API_KEY"
        return "MISSING"

    def _require_relayer_key_owner(self) -> None:
        key_owner = to_checksum_address(
            str(self.environ["POLYMARKET_RELAYER_API_KEY_ADDRESS"])
        )
        if key_owner.lower() != self.signer_address.lower():
            raise SettlementPreflightError(
                "Relayer API key owner does not match the configured signer"
            )

    def _auth_headers(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any],
        mode: str,
    ) -> dict[str, str]:
        if mode == "RELAYER_API_KEY":
            return {
                "RELAYER_API_KEY": str(self.environ["POLYMARKET_RELAYER_API_KEY"]),
                "RELAYER_API_KEY_ADDRESS": str(
                    self.environ["POLYMARKET_RELAYER_API_KEY_ADDRESS"]
                ),
            }
        if mode == "BUILDER_API_KEY":
            from py_builder_signing_sdk.config import BuilderApiKeyCreds, BuilderConfig

            config = BuilderConfig(
                local_builder_creds=BuilderApiKeyCreds(
                    key=str(self.environ["POLYMARKET_BUILDER_API_KEY"]),
                    secret=str(self.environ["POLYMARKET_BUILDER_SECRET"]),
                    passphrase=str(self.environ["POLYMARKET_BUILDER_PASSPHRASE"]),
                )
            )
            signed = config.generate_builder_headers(method, path, str(dict(body)))
            return signed.to_dict()
        raise RelayerCredentialsMissing("relayer credentials are missing")

    def _relayer_nonce(self) -> int:
        response = self.client.get(
            f"{self.relayer_url}/nonce",
            params={"address": self.signer_address, "type": "SAFE"},
        )
        response.raise_for_status()
        return int(response.json()["nonce"])

    def _relayer_transaction(self, transaction_id: str) -> list[Mapping[str, Any]]:
        response = self.client.get(
            f"{self.relayer_url}/transaction",
            params={"id": str(transaction_id)},
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, Mapping):
            return [payload]
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, Mapping)]
        raise SettlementRedeemError("relayer transaction response is invalid")

    def _derive_safe(self) -> str:
        from py_builder_relayer_client.builder.derive import derive
        from py_builder_relayer_client.config import get_contract_config

        return derive(
            self.signer_address,
            get_contract_config(CHAIN_ID).safe_factory,
        )

    def _write_route(self) -> dict[str, Any]:
        response = self.client.get(GEOBLOCK_URL)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping) or not isinstance(payload.get("blocked"), bool):
            raise SettlementPreflightError("geoblock response is invalid")
        return {
            "blocked": bool(payload["blocked"]),
            "country": str(payload.get("country") or ""),
            "region": str(payload.get("region") or ""),
            "ip": str(payload.get("ip") or ""),
            "proxy_url": self.proxy_url,
        }

    def _safe_nonce(self) -> int:
        return int(decode(["uint256"], self._call(self.safe_address, "nonce()"))[0])

    def _token_balance(self, asset_id: str) -> Decimal:
        raw = int(
            decode(
                ["uint256"],
                self._call(
                    CONDITIONAL_TOKENS,
                    "balanceOf(address,uint256)",
                    (self.safe_address, int(asset_id)),
                    ("address", "uint256"),
                ),
            )[0]
        )
        return Decimal(raw) / Decimal("1000000")

    def _pusd_balance(self) -> Decimal:
        raw = int(
            decode(
                ["uint256"],
                self._call(PUSD, "balanceOf(address)", (self.safe_address,), ("address",)),
            )[0]
        )
        return Decimal(raw) / Decimal("1000000")

    def _call(
        self,
        to: str,
        signature: str,
        values: Sequence[Any] = (),
        types: Sequence[str] = (),
    ) -> bytes:
        data = _calldata(signature, tuple(values), tuple(types))
        result = self._rpc("eth_call", [{"to": to, "data": data}, "latest"])
        return bytes.fromhex(str(result).removeprefix("0x"))

    def _rpc(self, method: str, params: Sequence[Any]) -> Any:
        self._rpc_id += 1
        response = self.client.post(
            self.rpc_url,
            json={
                "jsonrpc": "2.0",
                "id": self._rpc_id,
                "method": method,
                "params": list(params),
            },
        )
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise SettlementPreflightError(
                f"RPC {method} failed: {json.dumps(payload['error'], sort_keys=True)}"
            )
        return payload.get("result")


def _calldata(
    signature: str,
    values: Sequence[Any] = (),
    types: Sequence[str] = (),
) -> str:
    return "0x" + (keccak(text=signature)[:4] + encode(list(types), list(values))).hex()


def _safe_response_message(payload: Any) -> str:
    if isinstance(payload, Mapping):
        for name in ("errorMsg", "error", "message", "detail"):
            value = payload.get(name)
            if value:
                return str(value)[:1000]
        return f"keys={','.join(sorted(str(key) for key in payload)[:20])}"
    return str(payload or "")[:1000]


def _safe_transaction_evidence(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "transaction_id": str(row.get("transactionID") or row.get("transactionId") or ""),
        "state": str(row.get("state") or ""),
        "transaction_hash": str(row.get("transactionHash") or ""),
        "nonce": str(row.get("nonce") or ""),
        "target": str(row.get("target") or row.get("to") or ""),
        "metadata": str(row.get("metadata") or ""),
        "error_message": str(row.get("errorMsg") or row.get("error") or ""),
    }
