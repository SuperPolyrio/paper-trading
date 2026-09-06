"""Compare strict, risk-averse, and probabilistic maker predictions."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from quant.execution.models.maker_queue import (
    MakerQueueEngine,
    MakerQueueState,
    QueueModel,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episode", type=Path, required=True)
    parser.add_argument("--models", default="strict,risk_averse,probabilistic")
    args = parser.parse_args(argv)
    metadata_path = args.episode / "metadata.json"
    if not metadata_path.is_file():
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "reason": f"metadata_not_found:{metadata_path}",
                    "live_submission_performed": False,
                },
                indent=2,
                sort_keys=True,
            ),
            flush=True,
        )
        return 2
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    state = MakerQueueState(
        paper_order_id="comparison",
        asset_id=str(metadata["asset_id"]),
        side=str(metadata.get("side") or "BUY"),
        price_tick=Decimal(str(metadata.get("price_tick") or "0.5")),
        queue_model_version="maker_queue_research_v1",
        displayed_size_at_accept=Decimal(str(metadata.get("displayed_size") or "10")),
        own_orders_ahead=Decimal("0"),
        estimated_external_queue_ahead=Decimal(
            str(metadata.get("displayed_size") or "10")
        ),
        order_size=Decimal(str(metadata.get("order_size") or "1")),
    )
    aliases = {
        "strict": QueueModel.STRICT_TRADE_EVIDENCE,
        "risk_averse": QueueModel.RISK_AVERSE_QUEUE,
        "probabilistic": QueueModel.PROBABILISTIC_QUEUE,
    }
    rows = {}
    for name in args.models.split(","):
        model = aliases[name.strip()]
        prediction = MakerQueueEngine(model).predict(
            state,
            forecast_trade_volume=Decimal(
                str(metadata.get("forecast_trade_volume") or "0")
            ),
            horizon_seconds=Decimal(str(metadata.get("horizon_seconds") or "60")),
        )
        rows[model.value] = {
            key: str(value) for key, value in asdict(prediction).items()
        }
    print(
        json.dumps({"status": "PASS", "models": rows}, indent=2, sort_keys=True),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
