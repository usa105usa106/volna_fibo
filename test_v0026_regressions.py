"""v0026: quiet delivery, V1 execution zones and independent V2 evidence.

Binance subdivision tests use the archived parquet. MEXC zone arithmetic uses
reported anchors only. Future transitions and alternative shapes are synthetic.
"""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from conftest import state as generic_state
from core_major_rules import PROTOCOL, clean_five, impulse_errors, point
from core_major_state import SCORE_MODEL, btc_hard_rules, choose_btc
from core_majors import MajorWaveEngine, degree_scores
from core_senior import complete4h
from services_formatter import _row_values_flags
from services_major_formatter import major_count_text
from services_scanner import RunResult, ScannerService
from test_controller_lifecycle import setup_controller
from test_v0025_majors import live, snapshot


@pytest.fixture(scope="module")
def majors():
    return {
        base: MajorWaveEngine().evaluate(snapshot(base), None, 1, 300)
        for base in ("BTC", "ETH")
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["manual", "search", "track"])
@pytest.mark.parametrize("outside_table", [False, True])
async def test_report_only_photo_then_txt_with_all_major_details(
    cfg, repo, majors, monkeypatch, mode, outside_table
):
    controller, _ = setup_controller(cfg, repo)
    calls = []

    async def photo(*args, **kwargs):
        calls.append("photo")

    async def document(*args, **kwargs):
        calls.append("document")

    controller.bot.send_photo.side_effect = photo
    controller.bot.send_document.side_effect = document
    # Delivery contract is tested without a dependency on font/raster rendering.
    monkeypatch.setattr("bot_handlers.render_table_png", lambda *a, **kw: b"test PNG")
    result = RunResult(
        mode,
        [generic_state("DOGEUSDT")] if outside_table else list(majors.values()),
        100,
        2,
        [],
        "binance_spot",
        100,
    )
    result.major_context = list(majors.values())
    assert await controller._report(result, 1)
    assert calls == ["photo", "document"]
    controller.bot.send_message.assert_not_awaited()
    document_file = controller.bot.send_document.call_args.args[1]
    assert document_file.filename.startswith(mode + "_")
    assert document_file.filename.endswith(".txt")
    content = document_file.data.decode()
    assert content.count("BTC COUNT:") == 1
    assert content.count("ETH BASE:") == 1
    assert "V2 SUBDIVISION:" in content and "V3 SUBDIVISION:" in content
    assert "W3-(3) TARGETS:" in content


def test_btc_v1_zones_shown_without_inventing_extension_targets(majors):
    btc = majors["BTC"]
    assert btc.base_zone == pytest.approx((82563, 83703.51012))
    assert btc.deep_zone == pytest.approx((82341.15, 82563))
    assert btc.strict_origin <= btc.deep_zone[0] <= btc.deep_zone[1]
    assert btc.targets == []  # W3-(5) ceiling is not a W3 extension projection.
    row, _ = _row_values_flags(btc, 1)
    assert row[8] != "—" and row[9] != "—"
    assert "94990.7" in row[-1]


def render_with_low(majors, low):
    b = deepcopy(majors["BTC"].structure_evidence["major_count"])
    b["working_low"] = b["v1_low"] = low
    snap = snapshot("BTC")
    return MajorWaveEngine()._render(snap, b, complete4h(snap.hourly_closed), 1)


@pytest.mark.parametrize("distance", [0.0, 0.1, 20.0, 45.0])
def test_v1_zones_near_overlap_boundary_never_invert(majors, distance):
    state = render_with_low(majors, 82300 + distance)
    assert state.base_zone[0] == state.working_low
    assert state.base_zone[1] > state.base_zone[0]
    if distance == 0:
        assert state.deep_zone is None  # A sweep below low would retire V1.
    else:
        assert 82300 <= state.deep_zone[0] < state.deep_zone[1] == state.working_low


def test_reported_mexc_zones_use_mexc_prices_only():
    fixture = Path(__file__).parent / "fixtures/v0026/mexc-btc-manual-book.json"
    b = json.loads(fixture.read_text())["book"]
    snap = snapshot("BTC")
    snap.exchange, snap.symbol, snap.live_price = "mexc_futures", "BTC_USDT", 85594.6
    # Renderer-only arithmetic: this is NOT fabricated MEXC historical OHLC.
    h4 = pd.DataFrame(
        {
            "timestamp": pd.date_range(
                b["working_low_ts"], b["last_bucket"], freq="4h"
            ),
            "close": 85512.4,
        }
    )
    state = MajorWaveEngine()._render(snap, b, h4, 1)
    assert state.base_zone == pytest.approx((82502.4, 83653.2304))
    assert state.deep_zone == pytest.approx((82298.02845, 82502.4))
    assert b["box"]["lower"] == 82256.9 and b["box"]["upper"] == 94966.8


def test_real_v2_five_found_without_promoting_every_minor_pivot(majors):
    b = majors["BTC"].structure_evidence["major_count"]
    score = b["scores"]["V2"]
    assert score["hard_rules"] == "PASS"
    assert score["subdivision"] == "PROVEN"
    assert [p["price"] for p in score["points"]] == [
        62275,
        65474.46,
        62535.24,
        82300,
        74967.97,
        87395.67,
    ]
    assert score["search"]["pivot_count"] > 6
    assert score["search"]["valid_candidates"] == 1
    assert (
        impulse_errors(score["points"], complete4h(snapshot("BTC").hourly_closed)) == []
    )
    assert score["total"] == 8.5 and b["scores"]["V3"]["total"] == 9.0


@pytest.mark.parametrize("scale,shift", [(0.8, 17), (1.003, -40)])
def test_subdivision_not_hardcoded_to_btc_prices_or_dates(majors, scale, shift):
    h4 = complete4h(snapshot("BTC").hourly_closed)
    b = deepcopy(majors["BTC"].structure_evidence["major_count"])
    h4[["open", "high", "low", "close"]] *= scale
    delta = pd.Timedelta(days=shift)
    h4.timestamp += delta
    for p in b["anchors"].values():
        p["price"] *= scale
        p["timestamp"] = (pd.Timestamp(p["timestamp"]) + delta).isoformat()
        p["market"] = "synthetic_scaled_venue"
    result = clean_five(h4, b["anchors"]["w2"], b["anchors"]["peak"])
    assert result["status"] == "PROVEN"
    expected = [62275, 65474.46, 62535.24, 82300, 74967.97, 87395.67]
    assert [p["price"] for p in result["points"]] == pytest.approx(
        [v * scale for v in expected]
    )
    assert all(p["market"] == "synthetic_scaled_venue" for p in result["points"])


def test_h1_subdivision_resolves_actual_extreme_hours(majors):
    a = deepcopy(majors["BTC"].structure_evidence["major_count"]["anchors"])
    original = deepcopy(a)
    h1 = snapshot("BTC").hourly_closed
    result = clean_five(h1, a["w2"], a["peak"])
    assert result["status"] == "PROVEN"
    for p in result["points"]:
        row = h1[h1.timestamp.eq(pd.Timestamp(p["timestamp"]))].iloc[0]
        assert row[p["kind"]] == p["price"] and p["timeframe"] == "1H"
    assert a == original  # H1 support does not re-anchor senior C4H points.


def synthetic_frame(prices, positions=None, freq="4h"):
    positions = positions or list(range(0, 12 * len(prices), 12))
    y = np.interp(np.arange(positions[-1] + 1), positions, prices)
    return pd.DataFrame(
        {
            "timestamp": pd.date_range(
                "2026-01-01", periods=len(y), freq=freq, tz="UTC"
            ),
            "open": y,
            "high": y + 0.01,
            "low": y - 0.01,
            "close": y,
            "volume": 100.0,
        }
    )


def five_from_frame(frame):
    return clean_five(
        frame,
        point(frame.iloc[0], "low", "synthetic"),
        point(frame.iloc[-1], "high", "synthetic"),
    )


def test_hierarchical_five_can_contain_internal_swings():
    f = synthetic_frame([100, 125, 110, 150, 135, 160, 140, 180])
    result = five_from_frame(f)
    assert result["status"] == "PROVEN"
    assert [p["price"] for p in result["points"]] == pytest.approx(
        [99.99, 125.01, 109.99, 160.01, 139.99, 180.01]
    )
    assert impulse_errors(result["points"], f) == []


@pytest.mark.parametrize(
    "prices",
    [
        [100, 180],  # No internal five at all.
        [100, 125, 124, 160, 159, 180],  # Tiny pullbacks below the significance floor.
        [100, 150, 120, 170, 140, 190],  # Wave 4 / wave 1 overlap.
        [100, 130, 120, 140, 135, 180],  # Wave 3 shortest.
        [100, 125, 95, 160, 140, 180],  # Wave 2 broke origin.
    ],
)
def test_finder_does_not_force_invalid_or_micro_fives(prices):
    result = five_from_frame(synthetic_frame(prices))
    assert result["status"] == "UNPROVEN" and result["points"] == []


def test_hidden_overlap_wick_is_not_discarded_as_interior_noise():
    f = synthetic_frame([100, 125, 110, 160, 140, 180])
    f.loc[42, "low"] = 120  # Inside wave 4; below wave 1 territory.
    assert five_from_frame(f)["status"] == "UNPROVEN"


def test_endpoint_price_and_chronology_must_exist_in_candles():
    f = synthetic_frame([100, 125, 110, 160, 140, 180])
    s, e = point(f.iloc[0], "low", "synthetic"), point(f.iloc[-1], "high", "synthetic")
    assert clean_five(f, e, s)["status"] == "UNPROVEN"
    e["price"] += 10
    assert clean_five(f, s, e)["status"] == "UNPROVEN"


@pytest.mark.parametrize("low,high", [(82299, 85700), (85000, 95000)])
def test_real_postbox_dual_count_remains_unresolved_with_v2_alive(majors, low, high):
    e = MajorWaveEngine()
    state = e.evaluate(live(snapshot("BTC"), low, high, 85700), majors["BTC"], 1, 300)
    b = state.structure_evidence["major_count"]
    assert set(b["retired"]) == {"V1"}
    assert b["state"] == "BTC_POSTBOX_DUAL" and b["unresolved"]
    assert b["primary"] == "V3" and b["alt"] == ["V2"]
    assert b["scores"]["V2"]["subdivision"] == "PROVEN"
    assert b["variants"]["V2"]["targets"]
    assert state.strict_origin == 57800.19
    assert state.base_zone and state.deep_zone
    bounced = e.evaluate(live(snapshot("BTC"), 85000, 86000, 85700), state, 1, 300)
    assert "V1" in bounced.structure_evidence["major_count"]["retired"]
    assert bounced.structure_evidence["major_count"]["alt"] == ["V2"]


def stronger_v2_scores():
    # Complete synthetic daily/H4 history with valid alternatives. V2 has better
    # daily turning-point support and time proportionality, without fake scores.
    f = synthetic_frame(
        [50, 70, 55, 90, 72, 125, 100, 170], [0, 6, 9, 30, 60, 90, 120, 150]
    )
    daily = (
        f.set_index("timestamp")
        .resample("D")
        .agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
        )
        .reset_index()
    )
    a = {
        k: point(f.iloc[i], kind, "synthetic")
        for k, i, kind in [
            ("origin", 0, "low"),
            ("w1", 6, "high"),
            ("w2", 9, "low"),
            ("i1", 90, "high"),
            ("i2", 120, "low"),
            ("peak", 150, "high"),
        ]
    }
    return degree_scores(a, f, daily, f, {})


def test_v2_can_actually_win_from_candle_evidence_and_keep_v3_alt():
    scores = stronger_v2_scores()
    assert scores["V2"]["hard_rules"] == scores["V3"]["hard_rules"] == "PASS"
    assert scores["V2"]["total"] == 8 and scores["V3"]["total"] == 6
    b = {"retired": {"V1": {"reason": "synthetic box exit", "at": "2026-02-01T00:00Z"}}}
    choose_btc(b, scores, "2026-02-01T00:00Z")
    assert b["primary"] == "V2" and b["state"] == "BTC_V2_PRIMARY"
    assert b["alt"] == ["V3"] and set(b["retired"]) == {"V1"}


def test_soft_v3_to_v2_switch_requires_two_closed_distinct_buckets(majors):
    b = deepcopy(majors["BTC"].structure_evidence["major_count"])
    btc_hard_rules(b, 82299, 85700, "2026-10-05T00:00Z")
    choose_btc(b, b["scores"], "2026-10-05T00:00Z")
    assert b["primary"] == "V3"
    scores = stronger_v2_scores()
    choose_btc(b, scores, "2026-10-05T04:00Z")
    choose_btc(b, scores, "2026-10-05T04:00Z")
    assert b["primary"] == "V3" and b["streak"] == 1
    choose_btc(b, scores, "2026-10-05T08:00Z")
    assert b["primary"] == "V2" and b["alt"] == ["V3"]


def test_unproven_subdivision_is_unknown_not_zero_or_hard_invalid(majors, monkeypatch):
    monkeypatch.setattr(
        "core_majors.clean_five",
        lambda *a: {
            "status": "UNPROVEN",
            "points": [],
            "reason": "insufficient evidence",
        },
    )
    state = MajorWaveEngine().evaluate(
        live(snapshot("BTC"), 82299, 85700, 85500), majors["BTC"], 1, 300
    )
    b = state.structure_evidence["major_count"]
    assert b["scores"]["V2"]["total"] is None
    assert b["scores"]["V2"]["hard_rules"] == "UNKNOWN"
    assert b["variants"]["V2"]["hard_status"] == "UNKNOWN"
    assert "V2" not in b["retired"] and "V2" in b["alt"]
    assert b["unresolved"] and b["state"] == "BTC_POSTBOX_DUAL"
    assert "V2 SCORE: UNAVAILABLE" in major_count_text(state)


@pytest.mark.asyncio
async def test_upgrade_v25_recomputes_soft_scores_but_preserves_retirement(
    cfg, repo, majors
):
    old = deepcopy(majors["BTC"])
    b = old.structure_evidence["major_count"]
    btc_hard_rules(b, 82299, 85700, b["observed_at"])
    b.update(
        primary="V3",
        alt=["V2"],
        state="BTC_V3_PRIMARY",
        unresolved=False,
        score_bucket=b["last_bucket"],
        challenger="V3",
        streak=1,
    )
    b.pop("score_model", None)
    b["scores"]["V2"]["total"] = 0
    retired = deepcopy(b["retired"])
    await repo.save_major_count(old, 0)
    scanner = ScannerService(cfg, repo, SimpleNamespace())
    new = await scanner._analyze(snapshot("BTC"), 1, 300)
    saved, rev = await repo.major_count("binance_spot", "BTCUSDT", PROTOCOL)
    assert rev == 2
    b = saved.structure_evidence["major_count"]
    assert b["protocol"] == "majors-0025-20261005"
    assert b["retired"] == retired and b["primary"] != "V1"
    assert b["score_model"] == SCORE_MODEL and b["scores"]["V2"]["total"] == 8.5
    assert b["unresolved"] and b["alt"] == ["V2"] and b["streak"] == 0
    assert new.to_dict() == saved.to_dict()
