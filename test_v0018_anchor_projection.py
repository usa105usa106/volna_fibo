# Historical v0018 synthetic geometry tests. These do NOT verify the named assets against parquet.
# Real archive regressions are in test_v0019_regressions.py.
from __future__ import annotations

import pandas as pd

from core_senior import SeniorWaveDetector, _targets


def _h4(rows, start='2026-09-01T00:00:00Z'):
    base = pd.Timestamp(start)
    return pd.DataFrame([
        {
            'timestamp': base + pd.Timedelta(hours=4*i),
            'open': o,
            'high': h,
            'low': l,
            'close': c,
            'volume': 1.0,
            'n': 4,
        }
        for i, (o,h,l,c) in enumerate(rows)
    ])


def test_bch_isolated_mexc_wick_cannot_replace_structural_w3_1_anchor():
    # Parent W2 212.90. 317.70 is the structural W3-(1).  The later 366.88 print
    # is deliberately a one-candle upper wick with no close/peer confirmation.
    rows = [
        (214, 220, 212.9, 218),
        (218, 240, 217, 236),
        (236, 268, 232, 264),
        (264, 292, 260, 288),
        (288, 307, 282, 303),
        (303, 317.70, 301, 314),
        (314, 315, 300, 306),
        (306, 310, 296.10, 301),
        (301, 312, 298, 308),
        # isolated bad futures wick; body remains near the structural range
        (308, 366.88, 304, 310),
        (310, 314, 305, 311),
        (311, 313, 306, 310),
    ]
    frame = _h4(rows)
    parent = {
        'origin': 180.0,
        'high': 300.0,
        'w2_low': 212.9,
        'w2_ts': frame['timestamp'].iloc[0] - pd.Timedelta(hours=4),
    }
    nested = SeniorWaveDetector()._detect_nested(frame, parent)
    assert nested is not None
    assert abs(nested['high'] - 317.70) < 1e-9
    assert abs(nested['low'] - 296.10) < 1e-9
    got = _targets(nested['low'], nested['high'] - parent['w2_low'])
    want = [400.90, 465.6664, 570.4664, 740.0328]
    for a,b in zip(got,want,strict=True):
        assert abs(a-b) < 1e-9


def test_usoil_isolated_wick_cannot_inflate_targets():
    rows = [
        (76.5, 78.0, 76.0, 77.5),
        (77.5, 84.0, 77.0, 82.5),
        (82.5, 90.0, 81.0, 88.5),
        (88.5, 96.0, 87.0, 94.5),
        (94.5, 98.71, 93.0, 97.8),
        (97.8, 96.5, 91.0, 93.2),
        (93.2, 94.0, 88.31, 90.4),
        (90.4, 92.4, 89.2, 91.3),
        (91.3, 108.88, 90.5, 91.5),  # isolated futures wick
        (91.5, 93.0, 90.7, 92.0),
    ]
    frame = _h4(rows)
    parent = {
        'origin': 50.0,
        'high': 100.0,
        'w2_low': 76.0,
        'w2_ts': frame['timestamp'].iloc[0] - pd.Timedelta(hours=4),
    }
    nested = SeniorWaveDetector()._detect_nested(frame, parent)
    assert nested is not None
    assert abs(nested['high'] - 98.71) < 1e-9
    assert abs(nested['low'] - 88.31) < 1e-9
    got = _targets(nested['low'], nested['high'] - parent['w2_low'])
    want = [111.02, 125.05478, 147.76478, 184.50956]
    for a,b in zip(got,want,strict=True):
        assert abs(a-b) < 1e-9
