"""
tests/test_load.py

These cover the parsing layer, which is where the real-world mess lives.
Every case here is something the Polymarket API actually did:

  * clobTokenIds arriving as a JSON-encoded string instead of a list
  * outcomePrices arriving the same way, with string numbers inside
  * markets that never settled cleanly to 0 or 1
  * short markets with no price anywhere near a 90-day horizon

Run with:  pytest -v
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from load import (  # noqa: E402
    parse_json_field,
    parse_outcome,
    parse_volume,
    parse_dt,
    parse_event,
    duration_days,
    price_at_horizon,
)


# --------------------------------------------------------------------------- #
# parse_json_field — the field that broke the first fetch attempt
# --------------------------------------------------------------------------- #

def test_json_field_handles_real_list():
    market = {"clobTokenIds": ["111", "222"]}
    assert parse_json_field(market, "clobTokenIds") == ["111", "222"]


def test_json_field_handles_json_encoded_string():
    """Gamma returns this as a string on some markets and a list on others."""
    market = {"clobTokenIds": '["111", "222"]'}
    assert parse_json_field(market, "clobTokenIds") == ["111", "222"]


def test_json_field_handles_missing_key():
    assert parse_json_field({}, "clobTokenIds") == []


def test_json_field_handles_null():
    assert parse_json_field({"clobTokenIds": None}, "clobTokenIds") == []


def test_json_field_handles_malformed_string():
    """A truncated payload must not raise — it just yields nothing."""
    market = {"clobTokenIds": '["111", "22'}
    assert parse_json_field(market, "clobTokenIds") == []


def test_json_field_handles_wrong_type():
    assert parse_json_field({"clobTokenIds": 42}, "clobTokenIds") == []


# --------------------------------------------------------------------------- #
# parse_outcome — decides whether a market is usable at all
# --------------------------------------------------------------------------- #

def test_outcome_yes_won():
    assert parse_outcome({"outcomePrices": '["1", "0"]'}) == 1


def test_outcome_no_won():
    assert parse_outcome({"outcomePrices": '["0", "1"]'}) == 0


def test_outcome_accepts_near_boundary():
    """Settlement is not always exactly 1.0."""
    assert parse_outcome({"outcomePrices": ["0.995", "0.005"]}) == 1


def test_outcome_rejects_unsettled_market():
    """A market sitting at 0.5 did not resolve and cannot be scored."""
    assert parse_outcome({"outcomePrices": ["0.5", "0.5"]}) is None


def test_outcome_rejects_missing_field():
    assert parse_outcome({}) is None


def test_outcome_rejects_non_numeric():
    assert parse_outcome({"outcomePrices": ["yes", "no"]}) is None


# --------------------------------------------------------------------------- #
# parse_volume — the field name is not stable across records
# --------------------------------------------------------------------------- #

def test_volume_prefers_volume_num():
    assert parse_volume({"volumeNum": 1000, "volume": 2000}) == 1000.0


def test_volume_falls_back():
    assert parse_volume({"volume": "2000"}) == 2000.0


def test_volume_skips_empty_string():
    assert parse_volume({"volumeNum": "", "volume": 500}) == 500.0


def test_volume_returns_none_when_absent():
    assert parse_volume({}) is None


# --------------------------------------------------------------------------- #
# dates
# --------------------------------------------------------------------------- #

def test_parse_dt_handles_z_suffix():
    dt = parse_dt("2025-01-20T12:00:00Z")
    assert dt is not None and dt.year == 2025 and dt.month == 1


def test_parse_dt_handles_garbage():
    assert parse_dt("not a date") is None
    assert parse_dt(None) is None


def test_duration_days():
    market = {"startDate": "2025-01-01T00:00:00Z", "endDate": "2025-01-31T00:00:00Z"}
    assert duration_days(market) == pytest.approx(30.0)


def test_duration_falls_back_to_created_at():
    market = {"createdAt": "2025-01-01T00:00:00Z", "endDate": "2025-01-11T00:00:00Z"}
    assert duration_days(market) == pytest.approx(10.0)


def test_duration_none_without_end():
    assert duration_days({"startDate": "2025-01-01T00:00:00Z"}) is None


# --------------------------------------------------------------------------- #
# parse_event — needed because markets cluster and are not independent
# --------------------------------------------------------------------------- #

def test_event_extracted():
    market = {"events": [{"id": 77, "slug": "uk-election"}]}
    assert parse_event(market) == ("77", "uk-election")


def test_event_absent():
    assert parse_event({}) == ("", None)


def test_event_empty_list():
    assert parse_event({"events": []}) == ("", None)


# --------------------------------------------------------------------------- #
# price_at_horizon — the core transform
# --------------------------------------------------------------------------- #

DAY = 86400
END_TS = 1_700_000_000


def _arc(days_back):
    """A daily price point for each offset in days_back."""
    return [{"t": END_TS - d * DAY, "p": 0.1 * (10 - d) if d < 10 else 0.5}
            for d in days_back]


def test_horizon_finds_exact_point():
    points = _arc([30, 7, 1])
    result = price_at_horizon(points, END_TS, 7)
    assert result is not None
    price, ts, gap_hours = result
    assert ts == END_TS - 7 * DAY
    assert gap_hours == pytest.approx(0.0)


def test_horizon_finds_nearest_point():
    """Daily fidelity means the nearest point can be hours off the target."""
    points = [{"t": END_TS - 7 * DAY + 6 * 3600, "p": 0.4}]
    result = price_at_horizon(points, END_TS, 7)
    assert result is not None
    _, _, gap_hours = result
    assert gap_hours == pytest.approx(6.0)


def test_horizon_rejects_point_too_far_away():
    """A market that only lived a week has no price 90 days before the end."""
    points = _arc([7, 3, 1])
    assert price_at_horizon(points, END_TS, 90) is None


def test_horizon_handles_empty_history():
    assert price_at_horizon([], END_TS, 30) is None


def test_horizon_skips_malformed_points():
    points = [
        {"t": "bad", "p": 0.5},
        {"p": 0.5},
        {"t": END_TS - 30 * DAY, "p": 0.33},
    ]
    result = price_at_horizon(points, END_TS, 30)
    assert result is not None
    assert result[0] == pytest.approx(0.33)


def test_horizon_returns_none_when_all_points_malformed():
    points = [{"t": None, "p": None}, {"nonsense": 1}]
    assert price_at_horizon(points, END_TS, 30) is None