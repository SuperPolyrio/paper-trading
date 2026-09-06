from types import SimpleNamespace

from quant.calibration.real_live_adapter import (
    AccountSnapshot,
    PolymarketV2LiveAdapter,
)


def _account() -> AccountSnapshot:
    return AccountSnapshot(
        funder_address="0xfunder",
        signer_address="0xsigner",
        signature_type=2,
        collateral={
            "allowances": {
                "0xE111180000d2663C0091e4f400237545B87B996B": "100",
                "0xe2222d279d744050d28e00520010520000310F59": "200",
                "0xe3333700cA9d93003F00f0F71f8515005F6c00Aa": "0",
            }
        },
        conditional={
            "allowances": {
                "0xE111180000d2663C0091e4f400237545B87B996B": "300",
                "0xe2222d279d744050d28e00520010520000310F59": "400",
            }
        },
        open_orders=(),
        matching_engine_mode="ACTIVE",
        matching_engine_status={},
        observed_at=None,
    )


def _adapter() -> PolymarketV2LiveAdapter:
    adapter = object.__new__(PolymarketV2LiveAdapter)
    adapter.plan = SimpleNamespace(network=SimpleNamespace(chain_id=137))
    adapter._contract_config = SimpleNamespace(
        exchange_v2="0xE111180000d2663C0091e4f400237545B87B996B",
        neg_risk_exchange_v2="0xe2222d279d744050d28e00520010520000310F59",
    )
    return adapter


def test_standard_v2_order_ignores_unrelated_zero_allowance() -> None:
    result = _adapter().required_order_allowance(
        _account(),
        asset_type="COLLATERAL",
        neg_risk=False,
    )

    assert result["spender"] == "0xE111180000d2663C0091e4f400237545B87B996B"
    assert result["allowance"] == "100"


def test_neg_risk_v2_order_selects_neg_risk_exchange() -> None:
    result = _adapter().required_order_allowance(
        _account(),
        asset_type="CONDITIONAL",
        neg_risk=True,
    )

    assert result["spender"] == "0xe2222d279d744050d28e00520010520000310F59"
    assert result["allowance"] == "400"
