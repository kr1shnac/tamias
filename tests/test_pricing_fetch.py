from __future__ import annotations

import io
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tamias.pricing import load_price_sheet
from tamias.pricing_fetch import fetch_models, render_sheet


class Response(io.BytesIO):
    def __enter__(self) -> Response:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def test_fetch_models_and_render_sheet_with_injected_opener(tmp_path: Path) -> None:
    payload = {
        "data": [
            {"id": "paid", "pricing": {"prompt": "0.000003", "completion": "0.000015"}},
            {"id": "free", "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "unknown", "pricing": {"prompt": "0.1"}},
        ]
    }
    called: list[str] = []

    def opener(url: str) -> Response:
        called.append(url)
        return Response(json.dumps(payload).encode())

    result = fetch_models("https://fixture.invalid/models", opener=opener)
    assert called == ["https://fixture.invalid/models"]
    assert result.free == 1
    assert result.skipped == {"missing or invalid input/output rate": 1}
    sheet_path = tmp_path / "prices.toml"
    sheet_path.write_text(render_sheet(result, today=date(2026, 10, 6)), encoding="utf-8")
    sheet = load_price_sheet(sheet_path)
    assert sheet.date == "2026-10-06"
    assert sheet.models["paid"].input == 3.0
    assert sheet.models["paid"].output == 15.0
