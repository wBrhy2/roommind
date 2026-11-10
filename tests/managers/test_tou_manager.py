"""Tests for the Time-of-Use tariff manager."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.roommind.const import (
    TOU_SOURCE_PERIOD,
    TOU_TIER_MID,
    TOU_TIER_OFF,
    TOU_TIER_PEAK,
    TOU_TIER_ULTRA_LOW,
    TOU_TIER_UNKNOWN,
    TargetTemps,
)
from custom_components.roommind.managers.tou_manager import (
    TouManager,
    TouSegment,
    TouTimeline,
    normalize_period_state,
)


def _make_hass(states: dict[str, str]) -> MagicMock:
    hass = MagicMock()
    entity_states = {}
    for eid, state in states.items():
        s = MagicMock()
        s.state = state
        s.attributes = {"unit_of_measurement": "CAD/kWh"}
        entity_states[eid] = s
    hass.states.get = MagicMock(side_effect=entity_states.get)
    hass.services.async_call = AsyncMock(return_value=None)
    return hass


def _timeline(*tiers_and_minutes: tuple[str, float]) -> TouTimeline:
    """Build a gap-free timeline from (tier, duration_minutes) pairs."""
    now = time.time()
    segments: list[TouSegment] = []
    cursor = now
    for tier, minutes in tiers_and_minutes:
        end = cursor + minutes * 60.0
        segments.append(TouSegment(start=cursor, end=end, tier=tier))
        cursor = end
    segments[-1].end = now + 16 * 3600.0
    return TouTimeline(segments, current_tier=tiers_and_minutes[0][0], current_rate=None)


def _room(**overrides) -> dict:
    room = {
        "area_id": "living",
        "comfort_heat": 21.0,
        "comfort_cool": 24.0,
        "eco_heat": 17.0,
        "eco_cool": 27.0,
        "tou_mode": "setback",
    }
    room.update(overrides)
    return room


# ---------------------------------------------------------------------------
# Period normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "tier"),
    [
        ("ulo_on_peak", TOU_TIER_PEAK),
        ("ulo_mid_peak", TOU_TIER_MID),
        ("ulo_off_peak", TOU_TIER_OFF),
        ("ulo_overnight", TOU_TIER_ULTRA_LOW),
        ("on-peak", TOU_TIER_PEAK),
        ("mid-peak", TOU_TIER_MID),
        ("off-peak", TOU_TIER_OFF),
        ("super_off_peak", TOU_TIER_ULTRA_LOW),
        ("peak", TOU_TIER_PEAK),
        ("", TOU_TIER_UNKNOWN),
        ("unavailable", TOU_TIER_UNKNOWN),
        ("nonsense", TOU_TIER_UNKNOWN),
    ],
)
def test_normalize_period_state(raw: str, tier: str) -> None:
    assert normalize_period_state(raw) == tier


# ---------------------------------------------------------------------------
# Timeline building from sources
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_returns_none() -> None:
    mgr = TouManager(_make_hass({}))
    assert await mgr.async_build_timeline({}) is None
    assert mgr.timeline is None


@pytest.mark.asyncio
async def test_period_sensor_source() -> None:
    hass = _make_hass({"sensor.rate_period": "ulo_on_peak", "sensor.rate": "0.25"})
    mgr = TouManager(hass)
    timeline = await mgr.async_build_timeline(
        {"tou_enabled": True, "tou_period_entity": "sensor.rate_period", "tou_rate_entity": "sensor.rate"}
    )
    assert timeline is not None
    assert timeline.current_tier == TOU_TIER_PEAK
    assert timeline.current_rate == 0.25
    snap = mgr.snapshot()
    assert snap["enabled"] and snap["source"] == TOU_SOURCE_PERIOD


@pytest.mark.asyncio
async def test_flags_source() -> None:
    hass = _make_hass(
        {
            "input_boolean.tou_h": "off",
            "input_boolean.tou_m": "on",
            "input_boolean.tou_l": "off",
            "input_boolean.tou_ul": "off",
        }
    )
    mgr = TouManager(hass)
    timeline = await mgr.async_build_timeline(
        {
            "tou_enabled": True,
            "tou_flag_entities": {
                "peak": "input_boolean.tou_h",
                "mid": "input_boolean.tou_m",
                "off": "input_boolean.tou_l",
                "ultra_low": "input_boolean.tou_ul",
            },
        }
    )
    assert timeline is not None
    assert timeline.current_tier == TOU_TIER_MID


@pytest.mark.asyncio
async def test_rate_thresholds_source() -> None:
    hass = _make_hass({"sensor.rate": "0.20"})
    mgr = TouManager(hass)
    timeline = await mgr.async_build_timeline(
        {
            "tou_enabled": True,
            "tou_rate_entity": "sensor.rate",
            "tou_peak_threshold": 0.18,
            "tou_mid_threshold": 0.10,
        }
    )
    assert timeline is not None
    assert timeline.current_tier == TOU_TIER_PEAK


@pytest.mark.asyncio
async def test_unknown_source_shapes_nothing() -> None:
    mgr = TouManager(_make_hass({}))
    timeline = await mgr.async_build_timeline({"tou_enabled": True})
    assert timeline is not None
    assert timeline.current_tier == TOU_TIER_UNKNOWN
    assert mgr.make_adjuster(_room(), {"tou_enabled": True}) is None


@pytest.mark.asyncio
async def test_calendar_events_build_segments() -> None:
    now = time.time()
    # Relative to now: absolute dates go stale the moment they fall behind the
    # clock, and past events are outside the horizon by design.
    from homeassistant.util import dt as dt_util

    start_iso = dt_util.utc_from_timestamp(now + 2 * 3600.0).isoformat()
    end_iso = dt_util.utc_from_timestamp(now + 7 * 3600.0).isoformat()
    hass = _make_hass({"sensor.rate_period": "ulo_off_peak", "calendar.tou_peak": "on"})
    hass.services.async_call = AsyncMock(
        return_value={
            "calendar.tou_peak": {"events": [{"start": {"date_time": start_iso}, "end": {"date_time": end_iso}}]}
        }
    )
    mgr = TouManager(hass)
    timeline = await mgr.async_build_timeline(
        {
            "tou_enabled": True,
            "tou_period_entity": "sensor.rate_period",
            "tou_calendar_entities": [{"entity_id": "calendar.tou_peak", "tier": "peak"}],
        }
    )
    assert timeline is not None
    parsed = dt_util.parse_datetime(start_iso).timestamp()
    # The event window paints its tier wherever it lands, and where it does not
    # cover a timestamp, the current tier still rules there.
    assert timeline.tier_at(parsed + 60) == TOU_TIER_PEAK
    assert timeline.tier_at(now + 1) == TOU_TIER_OFF


@pytest.mark.asyncio
async def test_next_change_sensor_step() -> None:
    future = time.time() + 2 * 3600.0
    from homeassistant.util import dt as dt_util

    iso = dt_util.utc_from_timestamp(future).isoformat()
    hass = _make_hass({"sensor.rate_period": "ulo_off_peak", "sensor.next_peak": iso})
    mgr = TouManager(hass)
    timeline = await mgr.async_build_timeline(
        {
            "tou_enabled": True,
            "tou_period_entity": "sensor.rate_period",
            "tou_next_change_entity": "sensor.next_peak",
            "tou_next_change_tier": "peak",
        }
    )
    assert timeline is not None
    assert timeline.tier_at(time.time() + 60) == TOU_TIER_OFF
    assert timeline.tier_at(future + 60) == TOU_TIER_PEAK


@pytest.mark.asyncio
async def test_calendar_service_failure_is_graceful() -> None:
    hass = _make_hass({"sensor.rate_period": "ulo_on_peak"})
    hass.services.async_call = AsyncMock(side_effect=RuntimeError("boom"))
    mgr = TouManager(hass)
    timeline = await mgr.async_build_timeline(
        {
            "tou_enabled": True,
            "tou_period_entity": "sensor.rate_period",
            "tou_calendar_entities": [{"entity_id": "calendar.x", "tier": "peak"}],
        }
    )
    assert timeline is not None
    assert timeline.current_tier == TOU_TIER_PEAK


# ---------------------------------------------------------------------------
# Adjuster: tier shaping
# ---------------------------------------------------------------------------


def test_peak_setback_lowers_heat_raises_cool() -> None:
    mgr = TouManager(_make_hass({}))
    mgr._timeline = _timeline((TOU_TIER_OFF, 0), (TOU_TIER_PEAK, 120))
    adjust = mgr.make_adjuster(_room(), {"tou_enabled": True})
    assert adjust is not None
    ts = time.time() + 60.0
    result = adjust(ts, TargetTemps(heat=21.0, cool=24.0))
    assert result.heat == 18.0  # DEFAULT_TOU_PEAK_HEAT_FLOOR
    assert result.cool == 28.0  # DEFAULT_TOU_PEAK_COOL_CEILING


def test_peak_never_raises_a_colder_schedule() -> None:
    mgr = TouManager(_make_hass({}))
    mgr._timeline = _timeline((TOU_TIER_PEAK, 120))
    adjust = mgr.make_adjuster(_room(), {})
    assert adjust is not None
    result = adjust(time.time() + 60.0, TargetTemps(heat=16.0, cool=20.0))
    assert result.heat == 16.0  # min(16, 18) — setback never *raises* heat
    assert result.cool == 28.0  # but raising the cool target is demand reduction


def test_mid_delta_shift() -> None:
    mgr = TouManager(_make_hass({}))
    mgr._timeline = _timeline((TOU_TIER_MID, 120))
    adjust = mgr.make_adjuster(_room(), {})
    assert adjust is not None
    result = adjust(time.time() + 60.0, TargetTemps(heat=21.0, cool=24.0))
    assert result.heat == 19.0
    assert result.cool == 26.0


def test_boost_only_for_boost_rooms_and_off_never_touched() -> None:
    mgr = TouManager(_make_hass({}))
    mgr._timeline = _timeline((TOU_TIER_ULTRA_LOW, 120))
    setback = mgr.make_adjuster(_room(tou_mode="setback"), {})
    assert setback is not None
    base = setback(time.time() + 60.0, TargetTemps(heat=21.0, cool=24.0))
    assert base.heat == 21.0  # setback rooms never boost

    booster = mgr.make_adjuster(_room(tou_mode="setback_boost"), {})
    assert booster is not None
    boosted = booster(time.time() + 60.0, TargetTemps(heat=21.0, cool=24.0))
    assert boosted.heat == 23.0
    assert boosted.cool == 22.0

    off = mgr.make_adjuster(_room(tou_mode="off"), {})
    assert off is None
    forced_off = booster(time.time() + 60.0, TargetTemps(heat=None, cool=None))
    assert forced_off == TargetTemps(heat=None, cool=None)  # off stays off


def test_boost_gates_on_presence_vacation_and_cap() -> None:
    mgr = TouManager(_make_hass({}))
    mgr._timeline = _timeline((TOU_TIER_ULTRA_LOW, 120))
    ts = time.time() + 60.0
    room = _room(tou_mode="setback_boost", tou_max_boost=22.5)

    away = mgr.make_adjuster(room, {}, room_vacant=True, presence_away=False)
    assert away is not None
    assert away(ts, TargetTemps(heat=21.0, cool=24.0)).heat == 21.0

    unoccupied = mgr.make_adjuster({**room, "tou_boost_unoccupied": True}, {}, room_vacant=True)
    assert unoccupied is not None
    assert unoccupied(ts, TargetTemps(heat=21.0, cool=24.0)).heat == 22.5  # capped

    vacation_settings = {"vacation_until": time.time() + 3600, "vacation_temp": 15.0}
    vac = mgr.make_adjuster(room, vacation_settings)
    assert vac is not None
    assert vac(ts, TargetTemps(heat=21.0, cool=24.0)).heat == 21.0  # no boost in vacation


def test_override_never_shaved() -> None:
    mgr = TouManager(_make_hass({}))
    mgr._timeline = _timeline((TOU_TIER_PEAK, 120))
    room = _room(override_heat=24.0, override_cool=None, override_until=None)
    adjust = mgr.make_adjuster(room, {})
    assert adjust is not None
    result = adjust(time.time() + 60.0, TargetTemps(heat=24.0, cool=27.0))
    assert result.heat == 24.0  # the override value passes through untouched


def test_mold_active_blocks_heat_setback() -> None:
    mgr = TouManager(_make_hass({}))
    mgr._timeline = _timeline((TOU_TIER_PEAK, 120))
    adjust = mgr.make_adjuster(_room(), {}, mold_active=True)
    assert adjust is not None
    result = adjust(time.time() + 60.0, TargetTemps(heat=21.0, cool=24.0))
    assert result.heat == 21.0  # prevention outranks tariff setback
    assert result.cool == 28.0  # cooling side is unaffected


# ---------------------------------------------------------------------------
# Coast rules
# ---------------------------------------------------------------------------


def test_coast_window_duration_mode() -> None:
    mgr = TouManager(_make_hass({}))
    boundary = time.time() + 30.0
    mgr._timeline = TouTimeline(
        [
            TouSegment(start=time.time(), end=boundary, tier=TOU_TIER_OFF),
            TouSegment(start=boundary, end=boundary + 3600, tier=TOU_TIER_PEAK),
            TouSegment(start=boundary + 3600, end=time.time() + 16 * 3600, tier=TOU_TIER_OFF),
        ],
        current_tier=TOU_TIER_OFF,
        current_rate=None,
    )
    settings = {
        "tou_coast_rules": [
            {
                "id": "r1",
                "name": "peak coast",
                "enabled": True,
                "rooms": ["living"],
                "trigger": {"from_tier": "off", "to_tier": "peak"},
                "coast_temp": 19.0,
                "end": {"mode": "duration", "value": 30},
                "post_hold": "schedule",
            }
        ]
    }
    adjust = mgr.make_adjuster(_room(), settings)
    assert adjust is not None
    # Inside the coast window: the rule's temperature replaces peak setback.
    inside = adjust(boundary + 120, TargetTemps(heat=21.0, cool=24.0))
    assert inside.heat == 19.0
    assert inside.cool == 24.0
    # After the 30-minute hold (still inside the peak segment): setback 18 again.
    assert adjust(boundary + 31 * 60, TargetTemps(heat=21.0, cool=24.0)).heat == 18.0


def test_coast_window_tier_window_and_cap() -> None:
    mgr = TouManager(_make_hass({}))
    boundary = time.time() + 30.0
    # Peak that never ends within the horizon: tier_window end falls back to the cap.
    mgr._timeline = TouTimeline(
        [
            TouSegment(start=time.time(), end=boundary, tier=TOU_TIER_ULTRA_LOW),
            TouSegment(start=boundary, end=time.time() + 16 * 3600, tier=TOU_TIER_PEAK),
        ],
        current_tier=TOU_TIER_ULTRA_LOW,
        current_rate=None,
    )
    settings = {
        "tou_coast_max_minutes": 90,
        "tou_coast_rules": [
            {
                "id": "r1",
                "name": "hold to cheap",
                "enabled": True,
                "rooms": ["living"],
                "trigger": {"from_tier": "ultra_low", "to_tier": "peak"},
                "coast_temp": 19.0,
                "end": {"mode": "tier_window", "value": "ultra_low"},
                "post_hold": "schedule",
            }
        ],
    }
    adjust = mgr.make_adjuster(_room(), settings)
    assert adjust is not None
    assert adjust(boundary + 120, TargetTemps(heat=21.0, cool=24.0)).heat == 19.0
    assert adjust(boundary + 91 * 60, TargetTemps(heat=21.0, cool=24.0)).heat == 18.0


def test_coast_rule_requires_matching_room_and_enabled() -> None:
    mgr = TouManager(_make_hass({}))
    boundary = time.time() + 30.0
    mgr._timeline = TouTimeline(
        [
            TouSegment(start=time.time(), end=boundary, tier=TOU_TIER_OFF),
            TouSegment(start=boundary, end=time.time() + 16 * 3600, tier=TOU_TIER_PEAK),
        ],
        current_tier=TOU_TIER_OFF,
        current_rate=None,
    )
    rule = {
        "id": "r1",
        "name": "x",
        "enabled": True,
        "rooms": ["other"],
        "trigger": {"from_tier": "", "to_tier": ""},
        "coast_temp": 19.0,
        "end": {"mode": "duration", "value": 60},
        "post_hold": "schedule",
    }
    adjust = mgr.make_adjuster(_room(), {"tou_coast_rules": [rule]})
    assert adjust is not None
    assert adjust(boundary + 120, TargetTemps(heat=21.0, cool=24.0)).heat == 18.0

    disabled = {**rule, "rooms": ["living"], "enabled": False}
    adjust2 = mgr.make_adjuster(_room(), {"tou_coast_rules": [disabled]})
    assert adjust2 is not None
    assert adjust2(boundary + 120, TargetTemps(heat=21.0, cool=24.0)).heat == 18.0


def test_coast_clock_end_rolls_to_next_day() -> None:
    mgr = TouManager(_make_hass({}))
    now = time.time()
    boundary = now + 30.0
    mgr._timeline = TouTimeline(
        [
            TouSegment(start=now, end=boundary, tier=TOU_TIER_OFF),
            TouSegment(start=boundary, end=now + 16 * 3600, tier=TOU_TIER_PEAK),
        ],
        current_tier=TOU_TIER_OFF,
        current_rate=None,
    )
    settings = {
        "tou_coast_rules": [
            {
                "id": "r1",
                "name": "x",
                "enabled": True,
                "rooms": ["living"],
                "trigger": {"from_tier": "", "to_tier": "peak"},
                "coast_temp": 19.0,
                # 00:30 — boundary is "now"; this can only be reached after midnight.
                "end": {"mode": "clock", "value": "00:30"},
                "post_hold": "stay",
            }
        ]
    }
    adjust = mgr.make_adjuster(_room(), settings)
    assert adjust is not None
    assert adjust(boundary + 120, TargetTemps(heat=21.0, cool=24.0)).heat == 19.0


def test_coast_stay_holds_until_next_boundary() -> None:
    mgr = TouManager(_make_hass({}))
    now = time.time()
    b1 = now + 30.0
    b2 = now + 4 * 3600.0
    mgr._timeline = TouTimeline(
        [
            TouSegment(start=now, end=b1, tier=TOU_TIER_OFF),
            TouSegment(start=b1, end=b2, tier=TOU_TIER_PEAK),
            TouSegment(start=b2, end=now + 16 * 3600, tier=TOU_TIER_ULTRA_LOW),
        ],
        current_tier=TOU_TIER_OFF,
        current_rate=None,
    )
    settings = {
        "tou_coast_rules": [
            {
                "id": "r1",
                "name": "x",
                "enabled": True,
                "rooms": ["living"],
                "trigger": {"from_tier": "", "to_tier": "peak"},
                "coast_temp": 19.0,
                "end": {"mode": "duration", "value": 30},
                "post_hold": "stay",
            }
        ]
    }
    adjust = mgr.make_adjuster(_room(), settings)
    assert adjust is not None
    # Hold ended at +60min but "stay" keeps the coast until the next boundary.
    assert adjust(b1 + 1800, TargetTemps(heat=21.0, cool=24.0)).heat == 19.0
    # After the next boundary (ULTRA_LOW, setback room: no shaping) the schedule stands.
    assert adjust(b2 + 60, TargetTemps(heat=21.0, cool=24.0)).heat == 21.0


# ---------------------------------------------------------------------------
# Price weighting
# ---------------------------------------------------------------------------


def test_price_builder_gating() -> None:
    mgr = TouManager(_make_hass({}))
    mgr._timeline = _timeline((TOU_TIER_OFF, 60), (TOU_TIER_PEAK, 60))
    builder = mgr.make_price_series_builder(_room(), {"tou_enabled": True})
    assert builder is not None
    # One hour per block: block 0 sits in OFF, the rest land past the +60min boundary.
    series = builder(time.time(), 4, 60.0)
    assert series[0] == 1.0  # off
    assert series[-1] == 3.0  # peak
    assert mgr.make_price_series_builder(_room(tou_mode="off"), {}) is None
    assert mgr.make_price_series_builder(_room(), {"tou_price_weighting": False}) is None
    no_timeline = TouManager(_make_hass({}))
    assert no_timeline.make_price_series_builder(_room(), {}) is None


def test_snapshot_disabled() -> None:
    mgr = TouManager(_make_hass({}))
    snap = mgr.snapshot()
    assert snap["enabled"] is False
    assert snap["tier"] == TOU_TIER_UNKNOWN


# ---------------------------------------------------------------------------
# Source parser guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unavailable_period_falls_back_through_chain() -> None:
    """Missing/unavailable sources degrade to tier unknown without raising."""
    hass = _make_hass({"sensor.period": "unavailable", "sensor.rate": "not_a_number"})
    mgr = TouManager(hass)
    timeline = await mgr.async_build_timeline(
        {
            "tou_enabled": True,
            "tou_period_entity": "sensor.period",
            "tou_rate_entity": "sensor.rate",
            "tou_flag_entities": "not-a-dict",
        }
    )
    assert timeline is not None
    assert timeline.current_tier == TOU_TIER_UNKNOWN
    assert timeline.current_rate is None


@pytest.mark.asyncio
async def test_flag_entity_missing_or_not_string() -> None:
    """Flags entries with empty ids or wrong types are skipped."""
    hass = _make_hass({"input_boolean.on_flag": "on"})
    mgr = TouManager(hass)
    timeline = await mgr.async_build_timeline(
        {
            "tou_enabled": True,
            "tou_flag_entities": {"mid": "", "peak": 42, "off": "input_boolean.on_flag"},
        }
    )
    assert timeline is not None
    assert timeline.current_tier == TOU_TIER_OFF


@pytest.mark.asyncio
async def test_calendar_entry_validation_skips_bad_rows() -> None:
    """Calendar rows without an entity or with an unknown tier are ignored."""
    hass = _make_hass({"sensor.period": "ulo_on_peak"})
    mgr = TouManager(hass)
    timeline = await mgr.async_build_timeline(
        {
            "tou_enabled": True,
            "tou_period_entity": "sensor.period",
            "tou_calendar_entities": [
                {"entity_id": "", "tier": "peak"},
                {"entity_id": "calendar.x", "tier": "bogus"},
            ],
        }
    )
    assert timeline is not None
    assert timeline.current_tier == TOU_TIER_PEAK
    assert len(timeline.segments) == 1


@pytest.mark.asyncio
async def test_calendar_payload_shapes_skipped() -> None:
    """Non-dict events and unparseable payloads never become segments."""
    hass = _make_hass({"sensor.period": "ulo_off_peak"})
    hass.services.async_call = AsyncMock(
        return_value={
            "calendar.x": {
                "events": [
                    "not-a-dict",
                    {"start": "garbage", "end": {"date_time": "2026-11-06T07:00:00-05:00"}},
                    {"start": {"date_time": "nope"}, "end": {"date_time": "nope"}},
                ]
            }
        }
    )
    mgr = TouManager(hass)
    timeline = await mgr.async_build_timeline(
        {
            "tou_enabled": True,
            "tou_period_entity": "sensor.period",
            "tou_calendar_entities": [{"entity_id": "calendar.x", "tier": "peak"}],
        }
    )
    assert timeline is not None
    assert len(timeline.segments) == 1


@pytest.mark.asyncio
async def test_next_change_guards() -> None:
    """Unavailable, unparseable or bogus next-change readings fall back safely."""
    hass = _make_hass(
        {
            "sensor.period": "ulo_off_peak",
            "sensor.nx1": "unavailable",
            "sensor.nx2": "not-a-date",
        }
    )
    mgr = TouManager(hass)
    timeline = await mgr.async_build_timeline(
        {
            "tou_enabled": True,
            "tou_period_entity": "sensor.period",
            "tou_next_change_entity": "sensor.nx1",
        }
    )
    assert timeline is not None and len(timeline.segments) == 1

    timeline = await mgr.async_build_timeline(
        {
            "tou_enabled": True,
            "tou_period_entity": "sensor.period",
            "tou_next_change_entity": "sensor.nx2",
        }
    )
    assert timeline is not None and len(timeline.segments) == 1

    # A valid timestamp but an unknown tier name: the tier defaults to peak.
    future = time.time() + 3600.0
    from homeassistant.util import dt as dt_util

    hass.states.get = MagicMock(
        side_effect={
            "sensor.period": _state("ulo_off_peak"),
            "sensor.nx3": _state(dt_util.utc_from_timestamp(future).isoformat()),
        }.get
    )
    timeline = await mgr.async_build_timeline(
        {
            "tou_enabled": True,
            "tou_period_entity": "sensor.period",
            "tou_next_change_entity": "sensor.nx3",
            "tou_next_change_tier": "bogus",
        }
    )
    assert timeline is not None
    assert timeline.tier_at(future + 30) == TOU_TIER_PEAK


def _state(value: str) -> MagicMock:
    s = MagicMock()
    s.state = value
    s.attributes = {}
    return s


def test_tier_at_empty_and_past_horizon() -> None:
    timeline = TouTimeline([], current_tier=TOU_TIER_UNKNOWN, current_rate=None)
    assert timeline.tier_at(time.time()) == TOU_TIER_UNKNOWN

    full = _timeline((TOU_TIER_OFF, 60))
    assert full.tier_at(full.segments[0].start - 5) == TOU_TIER_OFF
    assert full.tier_at(full.segments[-1].end + 999999) == TOU_TIER_OFF


def test_coast_windows_ignores_malformed_rules() -> None:
    mgr = TouManager(_make_hass({}))
    boundary = time.time() + 30.0
    mgr._timeline = TouTimeline(
        [
            TouSegment(start=time.time(), end=boundary, tier=TOU_TIER_OFF),
            TouSegment(start=boundary, end=time.time() + 16 * 3600, tier=TOU_TIER_PEAK),
        ],
        current_tier=TOU_TIER_OFF,
        current_rate=None,
    )
    settings = {
        "tou_coast_rules": [
            "not-a-dict",
            {"id": "no-coast", "rooms": ["living"], "end": {"mode": "duration", "value": 30}},
            {
                "id": "bad-end",
                "rooms": ["living"],
                "coast_temp": 19.0,
                "end": {"mode": "sundial", "value": 30},
            },
            {
                "id": "zero-duration",
                "rooms": ["living"],
                "coast_temp": 19.0,
                "end": {"mode": "duration", "value": "not-a-number"},
            },
        ]
    }
    adjust = mgr.make_adjuster(_room(), settings)
    assert adjust is not None
    # None of the malformed rules produced a window: plain peak setback applies.
    assert adjust(boundary + 120, TargetTemps(heat=21.0, cool=24.0)).heat == 18.0
