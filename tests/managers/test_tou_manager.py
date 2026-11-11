"""Tests for the Time-of-Use tariff manager."""

from __future__ import annotations

import math
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.util import dt as dt_util

from custom_components.roommind.const import (
    TOU_SOURCE_PERIOD,
    TOU_TIER_MID,
    TOU_TIER_OFF,
    TOU_TIER_PEAK,
    TOU_TIER_ULTRA_LOW,
    TOU_TIER_UNKNOWN,
    TOU_WEEKDAYS,
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
async def test_tier_source_mode_pins_the_source() -> None:
    """An explicit tier source ignores the others, even while they stay bound."""
    hass = _make_hass(
        {
            "sensor.period": "on_peak",
            "input_boolean.flag_off": "on",
            "sensor.rate": "0.22",
        }
    )
    base: dict = {
        "tou_enabled": True,
        "tou_period_entity": "sensor.period",
        "tou_flag_entities": {"off": "input_boolean.flag_off"},
        "tou_rate_entity": "sensor.rate",
        "tou_peak_threshold": 0.20,
    }
    mgr = TouManager(hass)

    timeline = await mgr.async_build_timeline({**base, "tou_tier_source": "period"})
    assert timeline is not None and timeline.current_tier == TOU_TIER_PEAK
    assert timeline.current_rate == 0.22  # the rate still feeds the sensors

    timeline = await mgr.async_build_timeline({**base, "tou_tier_source": "flags"})
    assert timeline is not None and timeline.current_tier == TOU_TIER_OFF

    timeline = await mgr.async_build_timeline({**base, "tou_tier_source": "rate"})
    assert timeline is not None and timeline.current_tier == TOU_TIER_PEAK

    # auto (never touched): the legacy cascade — period wins.
    timeline = await mgr.async_build_timeline(base)
    assert timeline is not None and timeline.current_tier == TOU_TIER_PEAK


@pytest.mark.asyncio
async def test_tier_source_mode_has_no_fallback() -> None:
    """Explicit modes do not cascade: an unavailable period sensor stays unknown."""
    hass = _make_hass({"sensor.period": "unavailable", "sensor.rate": "0.22"})
    mgr = TouManager(hass)
    timeline = await mgr.async_build_timeline(
        {
            "tou_enabled": True,
            "tou_tier_source": "period",
            "tou_period_entity": "sensor.period",
            "tou_rate_entity": "sensor.rate",
            "tou_peak_threshold": 0.20,
        }
    )
    assert timeline is not None and timeline.current_tier == TOU_TIER_UNKNOWN
    assert timeline.current_rate == 0.22


@pytest.mark.asyncio
async def test_lookahead_mode_selects_the_future_source() -> None:
    """Calendar / next-change / none decide what RoomMind sees ahead."""
    from homeassistant.util import dt as dt_util

    now = time.time()
    cal_iso_start = dt_util.utc_from_timestamp(now + 1 * 3600.0).isoformat()
    cal_iso_end = dt_util.utc_from_timestamp(now + 4 * 3600.0).isoformat()
    nx_iso = dt_util.utc_from_timestamp(now + 2 * 3600.0).isoformat()
    hass = _make_hass({"sensor.period": "ulo_off_peak", "sensor.nx": nx_iso, "calendar.c": "on"})
    hass.services.async_call = AsyncMock(
        return_value={
            "calendar.c": {"events": [{"start": {"date_time": cal_iso_start}, "end": {"date_time": cal_iso_end}}]}
        }
    )
    mgr = TouManager(hass)
    base: dict = {
        "tou_enabled": True,
        "tou_period_entity": "sensor.period",
        "tou_calendar_entities": [{"entity_id": "calendar.c", "tier": "peak"}],
        "tou_next_change_entity": "sensor.nx",
        "tou_next_change_tier": "mid",
    }

    # auto: calendars cover the horizon window and win over next-change.
    timeline = await mgr.async_build_timeline(base)
    assert timeline is not None and timeline.tier_at(now + 2 * 3600.0) == TOU_TIER_PEAK

    # next_change: calendars ignored, the mid step lands at +2h.
    timeline = await mgr.async_build_timeline({**base, "tou_lookahead": "next_change"})
    assert timeline is not None
    assert timeline.tier_at(now + 1) == TOU_TIER_OFF
    assert timeline.tier_at(now + 2 * 3600.0 + 30) == TOU_TIER_MID

    # calendar: the next-change sensor is ignored.
    timeline = await mgr.async_build_timeline({**base, "tou_lookahead": "calendar"})
    assert timeline is not None and timeline.tier_at(now + 2 * 3600.0 + 30) == TOU_TIER_PEAK

    # none: flat current tier regardless of bindings.
    timeline = await mgr.async_build_timeline({**base, "tou_lookahead": "none"})
    assert timeline is not None
    assert len(timeline.segments) == 1
    assert timeline.tier_at(now + 2 * 3600.0) == TOU_TIER_OFF


@pytest.mark.asyncio
async def test_lookahead_next_change_rate() -> None:
    """The rate published for the period after the next change decides the tier."""
    from homeassistant.util import dt as dt_util

    now = time.time()
    future = now + 2 * 3600.0
    iso = dt_util.utc_from_timestamp(future).isoformat()
    hass = _make_hass(
        {
            "sensor.period": "ulo_off_peak",
            "sensor.nx": iso,
            "sensor.next_rate": "0.24",
        }
    )
    mgr = TouManager(hass)
    base: dict = {
        "tou_enabled": True,
        "tou_period_entity": "sensor.period",
        "tou_next_change_entity": "sensor.nx",
        "tou_next_rate_entity": "sensor.next_rate",
        "tou_next_change_tier": "off",  # fixed-tier choice must not win over the rate
        "tou_peak_threshold": 0.20,
    }

    timeline = await mgr.async_build_timeline({**base, "tou_lookahead": "next_change_rate"})
    assert timeline is not None
    assert timeline.tier_at(now + 60) == TOU_TIER_OFF
    assert timeline.tier_at(future + 30) == TOU_TIER_PEAK

    # explicit fixed-tier mode ignores the rate sensor entirely.
    timeline = await mgr.async_build_timeline({**base, "tou_lookahead": "next_change"})
    assert timeline is not None
    assert timeline.tier_at(future + 30) == TOU_TIER_OFF

    # legacy auto with no tier selected: a bound rate sensor is still used.
    timeline = await mgr.async_build_timeline({**base, "tou_next_change_tier": "", "tou_lookahead": "auto"})
    assert timeline is not None and timeline.tier_at(future + 30) == TOU_TIER_PEAK

    # unavailable next rate -> no guessing; the lookahead stays off.
    hass.states.get = MagicMock(
        side_effect={
            "sensor.period": _state("ulo_off_peak"),
            "sensor.nx": _state(iso),
            "sensor.next_rate": _state("unavailable"),
        }.get
    )
    timeline = await mgr.async_build_timeline({**base, "tou_lookahead": "next_change_rate"})
    assert timeline is not None and len(timeline.segments) == 1


def test_rate_window_events_and_guards() -> None:
    """Manual tariff rows paint tier events; inert input is ignored, never guessed."""
    from homeassistant.util import dt as dt_util

    now = time.time()
    horizon = now + 16 * 3600.0
    mgr = TouManager(_make_hass({}))

    # end == start wraps a full 24 h, so the window always covers "now".
    all_day = [{"id": "w1", "rate": 0.24, "start": "00:00", "end": "00:00", "days": []}]
    events = mgr._rate_window_events({"tou_peak_threshold": 0.20, "tou_rate_windows": all_day}, now, horizon)
    assert events and all(tier == TOU_TIER_PEAK for _, _, tier in events)
    assert any(start <= now < end for start, end, _ in events)

    # no thresholds configured -> rates cannot classify; rows stay inert.
    assert mgr._rate_window_events({"tou_rate_windows": all_day}, now, horizon) == []

    # a weekday that cannot occur inside the horizon matches nothing.
    today = dt_util.as_local(dt_util.utc_from_timestamp(now)).weekday()
    far = TOU_WEEKDAYS[(today + 3) % 7]
    inert = mgr._rate_window_events(
        {
            "tou_peak_threshold": 0.20,
            "tou_rate_windows": [{"id": "w2", "rate": 0.30, "start": "10:00", "end": "11:00", "days": [far]}],
        },
        now,
        horizon,
    )
    assert inert == []

    # end before start runs past midnight: two-hour events, never longer.
    wrapped = mgr._rate_window_events(
        {
            "tou_peak_threshold": 0.20,
            "tou_rate_windows": [{"id": "w3", "rate": 0.24, "start": "23:00", "end": "01:00", "days": []}],
        },
        now,
        horizon,
    )
    assert wrapped and all(0 < end - start <= 2 * 3600.0 for start, end, _ in wrapped)


@pytest.mark.asyncio
async def test_rate_window_paints_timeline_and_beats_next_change() -> None:
    """Windows are calendar-grade future: they paint over the tier and skip the fallback."""
    from homeassistant.util import dt as dt_util

    now = time.time()
    nx = dt_util.utc_from_timestamp(now + 2 * 3600.0).isoformat()
    hass = _make_hass({"sensor.period": "ulo_off_peak", "sensor.nx": nx})
    mgr = TouManager(hass)
    timeline = await mgr.async_build_timeline(
        {
            "tou_enabled": True,
            "tou_period_entity": "sensor.period",
            "tou_peak_threshold": 0.20,
            "tou_next_change_entity": "sensor.nx",
            "tou_next_change_tier": "mid",
            "tou_rate_windows": [{"id": "all", "rate": 0.24, "start": "00:00", "end": "00:00", "days": []}],
        }
    )
    assert timeline is not None
    assert timeline.tier_at(now + 60) == TOU_TIER_PEAK
    # windows count as events, so the next-change "mid" step never applies.
    assert timeline.tier_at(now + 2 * 3600.0 + 30) == TOU_TIER_PEAK


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
# Tariff schedule rows and records
# ---------------------------------------------------------------------------


def test_row_window_duration_end() -> None:
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
    room = _room(
        tou_overrides=[
            {
                "id": "r1",
                "name": "peak push-out",
                "enabled": True,
                "tier": "peak",
                "edge": "start",
                "action": "set",
                "heat": 19.0,
                "cool": None,
                "until": "minutes",
                "minutes": 30,
            }
        ]
    )
    adjust = mgr.make_adjuster(room, {})
    assert adjust is not None
    # Inside the row window: the row's temperature replaces peak setback.
    inside = adjust(boundary + 120, TargetTemps(heat=21.0, cool=24.0))
    assert inside.heat == 19.0
    # Blank cool side: the row skips cooling; plain peak ceiling shaping applies.
    assert inside.cool == 28.0
    # After the 30-minute hold (still inside the peak segment): setback 18 again.
    assert adjust(boundary + 31 * 60, TargetTemps(heat=21.0, cool=24.0)).heat == 18.0


def test_record_tier_change_end_with_cap() -> None:
    mgr = TouManager(_make_hass({}))
    boundary = time.time() + 30.0
    # Peak that never ends within the horizon: the tier-change end falls back to the cap.
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
        "tou_schedules": [
            {
                "id": "r1-heat",
                "name": "hold to cheap",
                "enabled": True,
                "rooms": ["living"],
                "side": "heat",
                "tier": "peak",
                "edge": "start",
                "phase": "after",
                "until": "next_tier_change",
                "until_tier": "ultra_low",
                "value": {"mode": "set", "temp": 19.0},
            }
        ],
    }
    adjust = mgr.make_adjuster(_room(), settings)
    assert adjust is not None
    assert adjust(boundary + 120, TargetTemps(heat=21.0, cool=24.0)).heat == 19.0
    assert adjust(boundary + 91 * 60, TargetTemps(heat=21.0, cool=24.0)).heat == 18.0


def test_row_side_blanks_skip_per_mode() -> None:
    """A blank row side skips this row in that mode; plain shaping applies there."""
    mgr = TouManager(_make_hass({}))
    boundary = time.time() + 30.0
    mgr._timeline = TouTimeline(
        [
            TouSegment(start=time.time(), end=boundary, tier=TOU_TIER_ULTRA_LOW),
            TouSegment(start=boundary, end=time.time() + 16 * 3600.0, tier=TOU_TIER_PEAK),
        ],
        current_tier=TOU_TIER_ULTRA_LOW,
        current_rate=None,
    )

    def row(**extra: object) -> dict:
        base: dict = {
            "id": "r",
            "name": "n",
            "enabled": True,
            "tier": "peak",
            "edge": "start",
            "action": "set",
            "until": "minutes",
            "minutes": 120,
        }
        base.update(extra)
        return base

    # Both sides set: each gets its own value within the same window.
    adjust = mgr.make_adjuster(_room(tou_overrides=[row(heat=18.5, cool=26.0)]), {})
    assert adjust is not None
    out = adjust(boundary + 60.0, TargetTemps(heat=21.0, cool=24.0))
    assert out.heat == 18.5
    assert out.cool == 26.0

    # Heating blank: the row is skipped there (plain peak cap applies), cooling holds.
    adjust = mgr.make_adjuster(_room(tou_overrides=[row(heat=None, cool=26.0)]), {})
    assert adjust is not None
    out = adjust(boundary + 60.0, TargetTemps(heat=21.0, cool=24.0))
    assert out.heat == 18.0
    assert out.cool == 26.0

    # Both blank: no window at all — plain peak shaping on both sides.
    adjust = mgr.make_adjuster(_room(tou_overrides=[row(heat=None, cool=None)]), {})
    assert adjust is not None
    out = adjust(boundary + 60.0, TargetTemps(heat=21.0, cool=24.0))
    assert out.heat == 18.0
    assert out.cool == 28.0


def test_row_window_ends_at_next_boundary() -> None:
    """A tier-window end closes the row window at the next tier change."""
    mgr = TouManager(_make_hass({}))
    now = time.time()
    b1 = now + 60.0
    b2 = now + 120.0
    mgr._timeline = TouTimeline(
        [
            TouSegment(start=now, end=b1, tier=TOU_TIER_OFF),
            TouSegment(start=b1, end=b2, tier=TOU_TIER_PEAK),
            TouSegment(start=b2, end=now + 16 * 3600.0, tier=TOU_TIER_OFF),
        ],
        current_tier=TOU_TIER_OFF,
        current_rate=None,
    )
    room = _room(
        tou_overrides=[
            {
                "id": "r-any",
                "name": "any",
                "enabled": True,
                "tier": "peak",
                "edge": "start",
                "action": "set",
                "heat": 19.0,
                "until": "tier_window",
            }
        ]
    )
    adjust = mgr.make_adjuster(room, {})
    assert adjust is not None
    assert adjust(b1 + 5.0, TargetTemps(heat=21.0, cool=24.0)).heat == 19.0
    # after the peak->off boundary nothing is held and the tier is plain off:
    # the schedule target returns untouched.
    assert adjust(b2 + 5.0, TargetTemps(heat=21.0, cool=24.0)).heat == 21.0


def test_row_records_expansion() -> None:
    """Card rows fan out into one engine record per set side."""
    recs = TouManager.row_records(
        _room(
            tou_overrides=[
                {
                    "id": "d",
                    "name": "mid-peak relax",
                    "enabled": True,
                    "tier": "mid",
                    "edge": "start",
                    "action": "raise",
                    "heat": 2.0,
                    "cool": 3.0,
                    "until": "minutes",
                    "minutes": 90,
                    "days": ["monday"],
                    "between": {"from": "17:00", "to": "22:00"},
                },
                {
                    "id": "a2",
                    "name": "warm before the door",
                    "enabled": True,
                    "tier": "ultra_low",
                    "edge": "end",
                    "action": "arrive",
                    "heat": 20.0,
                    "cool": None,
                    "clock": "09:00",
                },
                {"id": "l", "name": "lower", "enabled": True, "tier": "any", "edge": "start",
                 "action": "lower", "heat": -2.0},
            ]
        )
    )
    by_id = {r["id"]: r for r in recs}
    assert len(recs) == 4
    heat = by_id["d-heat"]
    assert heat["side"] == "heat" and heat["rooms"] == ["living"]
    assert heat["value"] == {"mode": "shift", "direction": "raise", "delta": 2.0}
    assert heat["until"] == "minutes" and heat["minutes"] == 90
    assert heat["days"] == ["monday"] and heat["between"] == {"from": "17:00", "to": "22:00"}
    assert by_id["d-cool"]["value"]["delta"] == 3.0
    arrive = by_id["a2-heat"]
    assert arrive["value"] == {"mode": "arrive", "temp": 20.0}
    assert arrive["until"] == "clock" and arrive["clock"] == "09:00"
    assert arrive["tier"] == "ultra_low" and arrive["edge"] == "end" and arrive["phase"] == "after"
    lower = by_id["l-heat"]
    assert lower["value"] == {"mode": "shift", "direction": "lower", "delta": 2.0}


def test_row_arrive_curve_derives_boundary_temperature() -> None:
    """The contributor scenario: 20 °C at the arrival time, heating off since
    the ultra-low boundary — the floor slides down the model's decay curve."""
    mgr = TouManager(_make_hass({}))
    now = time.time()
    b = now + 3600.0
    mgr._timeline = TouTimeline(
        [
            TouSegment(start=now, end=b, tier=TOU_TIER_ULTRA_LOW),
            TouSegment(start=b, end=now + 16 * 3600, tier=TOU_TIER_OFF),
        ],
        current_tier=TOU_TIER_ULTRA_LOW,
        current_rate=None,
    )
    settings = {
        "tou_schedules": [
            {
                "id": "a",
                "name": "20 by nine",
                "enabled": True,
                "rooms": ["living"],
                "side": "heat",
                "tier": "ultra_low",
                "edge": "end",
                "phase": "after",
                "until": "minutes",
                "minutes": 120,
                "value": {"mode": "arrive", "temp": 20.0},
            }
        ]
    }
    room = _room(tou_mode="setback")
    adjust = mgr.make_adjuster(room, settings, decay=(0.1, 10.0))
    assert adjust is not None

    def expected(ts: float) -> float:
        dt_hours = (b + 120 * 60.0 - ts) / 3600.0
        return 10.0 + (20.0 - 10.0) * math.exp(0.1 * dt_hours)

    # Before the boundary: plain schedule target, the MPC plans the pre-charge.
    assert adjust(b - 60.0, TargetTemps(heat=21.0, cool=None)).heat == 21.0
    # At the boundary the derived (indirect) target is above the arrival temp.
    at_boundary = adjust(b, TargetTemps(heat=21.0, cool=None)).heat
    assert at_boundary == pytest.approx(expected(b))
    assert at_boundary > 20.0
    # Mid-window the floor has slid partway down...
    mid = adjust(b + 3600.0, TargetTemps(heat=21.0, cool=None)).heat
    assert mid == pytest.approx(expected(b + 3600.0))
    assert mid < at_boundary
    # ...and it lands exactly on the arrival temperature at the window end.
    assert adjust(b + 120 * 60.0 - 1.0, TargetTemps(heat=21.0, cool=None)).heat == pytest.approx(20.0, abs=0.01)
    # After the window the schedule stands again.
    assert adjust(b + 121 * 60.0, TargetTemps(heat=21.0, cool=None)).heat == 21.0


def test_row_arrive_fallbacks_flat() -> None:
    """Immature model or wrong drift direction: hold the flat target (coast)."""
    mgr = TouManager(_make_hass({}))
    now = time.time()
    b = now + 3600.0
    mgr._timeline = TouTimeline(
        [
            TouSegment(start=now, end=b, tier=TOU_TIER_ULTRA_LOW),
            TouSegment(start=b, end=now + 16 * 3600, tier=TOU_TIER_OFF),
        ],
        current_tier=TOU_TIER_ULTRA_LOW,
        current_rate=None,
    )
    rec = {
        "id": "a",
        "name": "20 by nine",
        "enabled": True,
        "rooms": ["living"],
        "side": "heat",
        "tier": "ultra_low",
        "edge": "end",
        "phase": "after",
        "until": "minutes",
        "minutes": 120,
        "value": {"mode": "arrive", "temp": 20.0},
    }
    settings = {"tou_schedules": [rec]}
    room = _room(tou_mode="setback_boost")

    # No learned model yet: flat 20 across the window.
    adjust = mgr.make_adjuster(room, settings)
    assert adjust is not None
    assert adjust(b + 60.0, TargetTemps(heat=21.0, cool=None)).heat == 20.0
    assert adjust(b + 60 * 60.0, TargetTemps(heat=21.0, cool=None)).heat == 20.0

    # Outdoor warmer than the heating target: drift goes the wrong way, flat.
    adjust = mgr.make_adjuster(room, settings, decay=(0.1, 22.0))
    assert adjust is not None
    assert adjust(b + 60.0, TargetTemps(heat=21.0, cool=None)).heat == 20.0


def test_row_arrive_clamped_by_band() -> None:
    """An aggressive derived curve never demands more than the clamp band."""
    mgr = TouManager(_make_hass({}))
    now = time.time()
    b = now + 3600.0
    mgr._timeline = TouTimeline(
        [
            TouSegment(start=now, end=b, tier=TOU_TIER_ULTRA_LOW),
            TouSegment(start=b, end=now + 16 * 3600, tier=TOU_TIER_OFF),
        ],
        current_tier=TOU_TIER_ULTRA_LOW,
        current_rate=None,
    )
    settings = {
        "tou_schedules": [
            {
                "id": "a",
                "name": "x",
                "enabled": True,
                "rooms": ["living"],
                "side": "heat",
                "tier": "ultra_low",
                "edge": "end",
                "phase": "after",
                "until": "minutes",
                "minutes": 120,
                "value": {"mode": "arrive", "temp": 20.0},
            }
        ]
    }
    adjust = mgr.make_adjuster(_room(tou_mode="setback_boost"), settings, decay=(1.0, -20.0))
    assert adjust is not None
    assert adjust(b, TargetTemps(heat=21.0, cool=None)).heat == 28.0  # clamp_max default


def test_rows_days_filter() -> None:
    """Days restrict which tier boundaries a row fires on."""
    mgr = TouManager(_make_hass({}))
    now = time.time()
    b = now + 60.0
    mgr._timeline = TouTimeline(
        [
            TouSegment(start=now, end=b, tier=TOU_TIER_ULTRA_LOW),
            TouSegment(start=b, end=now + 16 * 3600, tier=TOU_TIER_OFF),
        ],
        current_tier=TOU_TIER_ULTRA_LOW,
        current_rate=None,
    )
    weekday = TOU_WEEKDAYS[dt_util.as_local(dt_util.utc_from_timestamp(b)).weekday()]
    other = next(d for d in TOU_WEEKDAYS if d != weekday)

    def adjust_for(days: list[str]):
        room = _room(
            tou_overrides=[
                {
                    "id": "d",
                    "name": "x",
                    "enabled": True,
                    "tier": "off",
                    "edge": "start",
                    "action": "set",
                    "heat": 19.0,
                    "until": "tier_window",
                    "days": days,
                }
            ]
        )
        return mgr.make_adjuster(room, {})

    assert adjust_for([weekday])(b + 5.0, TargetTemps(heat=21.0, cool=None)).heat == 19.0
    assert adjust_for([other])(b + 5.0, TargetTemps(heat=21.0, cool=None)).heat == 21.0
    assert adjust_for([])(b + 5.0, TargetTemps(heat=21.0, cool=None)).heat == 19.0


def test_preview_includes_room_rows() -> None:
    mgr = TouManager(_make_hass({}))
    now = time.time()
    b = now + 3600.0
    mgr._timeline = TouTimeline(
        [
            TouSegment(start=now, end=b, tier=TOU_TIER_OFF),
            TouSegment(start=b, end=now + 16 * 3600, tier=TOU_TIER_PEAK),
        ],
        current_tier=TOU_TIER_OFF,
        current_rate=None,
    )
    rooms = {
        "living": {
            "area_id": "living",
            "tou_mode": "setback",
            "tou_overrides": [
                {
                    "id": "r1",
                    "name": "x",
                    "enabled": True,
                    "tier": "peak",
                    "edge": "start",
                    "action": "set",
                    "heat": 19.0,
                    "until": "tier_window",
                }
            ],
        }
    }
    out = mgr.preview({"tou_enabled": True}, None, rooms)
    assert out is not None
    assert out["areas"]["living"], "the room row window must show up in the preview"
    assert "r1-heat" in out["records"]


def test_schedule_arrive_then_coast_chain() -> None:
    """The contributor scenario: reach 23 by the peak, then coast to an 18 floor."""
    mgr = TouManager(_make_hass({}))
    b = time.time() + 3600.0
    mgr._timeline = TouTimeline(
        [
            TouSegment(start=time.time(), end=b, tier=TOU_TIER_OFF),
            TouSegment(start=b, end=time.time() + 16 * 3600, tier=TOU_TIER_PEAK),
        ],
        current_tier=TOU_TIER_OFF,
        current_rate=None,
    )
    settings = {
        "tou_schedules": [
            {
                "id": "arrival",
                "name": "warm by peak",
                "enabled": True,
                "rooms": ["living"],
                "side": "heat",
                "tier": "peak",
                "edge": "start",
                "phase": "before",
                "minutes": 40,
                "until": "window",
                "value": {"mode": "set", "temp": 23.0},
            },
            {
                "id": "peakfloor",
                "name": "coast peak",
                "enabled": True,
                "rooms": ["living"],
                "side": "heat",
                "tier": "peak",
                "edge": "start",
                "phase": "after",
                "until": "window",
                "value": {"mode": "set", "temp": 18.0},
            },
        ]
    }
    adjust = mgr.make_adjuster(_room(), settings)
    assert adjust is not None
    # lead window [b-40m, b): floor raised to 23 so the MPC pre-heats,
    # despite the plain peak shaping wanting to cap heat at 18.
    assert adjust(b - 1800.0, TargetTemps(heat=21.0, cool=None)).heat == 23.0
    # at the boundary the arrival record ends and the coast floor applies.
    assert adjust(b + 60.0, TargetTemps(heat=21.0, cool=None)).heat == 18.0
    # before the lead, plain tier shaping (off -> no peak cap yet: schedule).
    assert adjust(b - 3600.0, TargetTemps(heat=21.0, cool=None)).heat == 21.0


def test_schedule_fires_every_occurrence_and_shift_is_plain_relative() -> None:
    """Two peaks in the horizon both get the window; shift bases on the schedule."""
    mgr = TouManager(_make_hass({}))
    b1 = time.time() + 1800.0
    b2 = b1 + 3600.0
    mgr._timeline = TouTimeline(
        [
            TouSegment(start=time.time(), end=b1, tier=TOU_TIER_OFF),
            TouSegment(start=b1, end=b2, tier=TOU_TIER_PEAK),
            TouSegment(start=b2, end=b2 + 1800.0, tier=TOU_TIER_OFF),
            TouSegment(start=b2 + 1800.0, end=time.time() + 16 * 3600, tier=TOU_TIER_PEAK),
        ],
        current_tier=TOU_TIER_OFF,
        current_rate=None,
    )
    settings = {
        "tou_schedules": [
            {
                "id": "relax",
                "name": "relax",
                "enabled": True,
                "rooms": ["living"],
                "side": "cool",
                "tier": "peak",
                "edge": "start",
                "phase": "after",
                "until": "window",
                "value": {"mode": "shift", "direction": "raise", "delta": 1.5},
            }
        ]
    }
    adjust = mgr.make_adjuster(_room(), settings)
    assert adjust is not None
    # every peak gets it: +1.5 on the plain schedule cool, not on the peak-shaped value
    assert adjust(b1 + 60.0, TargetTemps(heat=None, cool=24.0)).cool == 25.5
    assert adjust(b2 + 1860.0, TargetTemps(heat=None, cool=24.0)).cool == 25.5
    # off tier between peaks: schedule untouched
    assert adjust(b2 + 60.0, TargetTemps(heat=None, cool=24.0)).cool == 24.0


def test_user_blocks_outrank_tariff_and_gate_on_presence() -> None:
    """Room schedule blocks beat tariff records; occupied/away gates flip that."""
    mgr = TouManager(_make_hass({}))
    b = time.time() + 1800.0
    mgr._timeline = TouTimeline(
        [
            TouSegment(start=time.time(), end=b, tier=TOU_TIER_OFF),
            TouSegment(start=b, end=time.time() + 16 * 3600, tier=TOU_TIER_PEAK),
        ],
        current_tier=TOU_TIER_OFF,
        current_rate=None,
    )
    local = dt_util.as_local(dt_util.utc_from_timestamp(b + 60.0))
    at = local.hour * 60 + local.minute
    block = {
        "id": "comfort",
        "name": "comfort when in",
        "enabled": True,
        "side": "heat",
        "gate": "occupied",
        "when": {
            "start": f"{(at - 60) % 1440 // 60:02d}:{(at - 60) % 60:02d}",
            "end": f"{(at + 60) % 1440 // 60:02d}:{(at + 60) % 60:02d}",
            "days": [],
        },
        "value": {"mode": "set", "temp": 21.0},
    }
    settings = {
        "tou_schedules": [
            {
                "id": "peak18",
                "name": "peak floor 18",
                "enabled": True,
                "rooms": ["living"],
                "side": "heat",
                "tier": "peak",
                "edge": "start",
                "phase": "after",
                "until": "window",
                "value": {"mode": "set", "temp": 18.0},
            }
        ]
    }
    room = {**_room(), "schedule_blocks": [block]}
    # occupied: the user block wins over the tariff coast
    adjust = mgr.make_adjuster(room, settings)
    assert adjust is not None
    assert adjust(b + 60.0, TargetTemps(heat=21.0, cool=None)).heat == 21.0
    # vacant: the gate hides the block, the tariff floor applies
    adjust = mgr.make_adjuster(room, settings, room_vacant=True)
    assert adjust is not None
    assert adjust(b + 60.0, TargetTemps(heat=21.0, cool=None)).heat == 18.0


def test_user_blocks_never_activate_off_sides_and_preview_reports_windows() -> None:
    mgr = TouManager(_make_hass({}))
    now = time.time()
    mgr._timeline = TouTimeline(
        [
            TouSegment(start=now, end=now + 1800.0, tier=TOU_TIER_OFF),
            TouSegment(start=now + 1800.0, end=now + 16 * 3600, tier=TOU_TIER_PEAK),
        ],
        current_tier=TOU_TIER_OFF,
        current_rate=None,
    )
    local = dt_util.as_local(dt_util.utc_from_timestamp(now + 3600.0))
    at = local.hour * 60 + local.minute
    room = {
        **_room(),
        "schedule_blocks": [
            {
                "id": "all",
                "name": "always both",
                "enabled": True,
                "side": "both",
                "gate": "always",
                "when": {
                    "start": f"{(at - 60) % 1440 // 60:02d}:{(at - 60) % 60:02d}",
                    "end": f"{(at + 60) % 1440 // 60:02d}:{(at + 60) % 60:02d}",
                    "days": [],
                },
                "value": {"mode": "set", "temp": 20.0},
            }
        ],
    }
    adjust = mgr.make_adjuster(room, {})
    assert adjust is not None
    out = adjust(now + 3600.0, TargetTemps(heat=None, cool=24.0))
    assert out.heat is None  # off stays off: no block activates a side
    assert out.cool == 20.0

    preview = mgr.preview(
        {
            "tou_enabled": True,
            "tou_schedules": [
                {
                    "id": "p1",
                    "name": "p",
                    "enabled": True,
                    "rooms": ["living"],
                    "side": "heat",
                    "tier": "peak",
                    "edge": "start",
                    "phase": "after",
                    "until": "window",
                    "value": {"mode": "set", "temp": 18.0},
                }
            ],
        }
    )
    assert preview is not None
    assert len(preview["segments"]) == 2
    assert preview["records"]["p1"]["count"] == 1
    assert len(preview["areas"]["living"]) == 1


def test_record_requires_matching_room_and_enabled() -> None:
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
    rec = {
        "id": "r1-heat",
        "name": "x",
        "enabled": True,
        "rooms": ["other"],
        "side": "heat",
        "tier": "peak",
        "edge": "start",
        "phase": "after",
        "until": "minutes",
        "minutes": 60,
        "value": {"mode": "set", "temp": 19.0},
    }
    adjust = mgr.make_adjuster(_room(), {"tou_schedules": [rec]})
    assert adjust is not None
    assert adjust(boundary + 120, TargetTemps(heat=21.0, cool=24.0)).heat == 18.0

    disabled = {**rec, "rooms": ["living"], "enabled": False}
    adjust2 = mgr.make_adjuster(_room(), {"tou_schedules": [disabled]})
    assert adjust2 is not None
    assert adjust2(boundary + 120, TargetTemps(heat=21.0, cool=24.0)).heat == 18.0

    # A row is scoped to its own room by construction: it compiles for "living".
    row_adjust = mgr.make_adjuster(
        _room(
            tou_overrides=[
                {
                    "id": "x",
                    "name": "x",
                    "enabled": True,
                    "tier": "peak",
                    "edge": "start",
                    "action": "set",
                    "heat": 19.0,
                    "until": "minutes",
                    "minutes": 60,
                }
            ]
        ),
        {},
    )
    assert row_adjust is not None
    assert row_adjust(boundary + 120, TargetTemps(heat=21.0, cool=24.0)).heat == 19.0


def test_record_clock_end_rolls_to_next_day() -> None:
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
        "tou_schedules": [
            {
                "id": "r1-heat",
                "name": "x",
                "enabled": True,
                "rooms": ["living"],
                "side": "heat",
                "tier": "peak",
                "edge": "start",
                "phase": "after",
                "until": "clock",
                # 00:30 — boundary is "now"; this can only be reached after midnight.
                "clock": "00:30",
                "value": {"mode": "set", "temp": 19.0},
            }
        ]
    }
    adjust = mgr.make_adjuster(_room(), settings)
    assert adjust is not None
    assert adjust(boundary + 120, TargetTemps(heat=21.0, cool=24.0)).heat == 19.0


def test_record_next_change_holds_until_boundary() -> None:
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
        "tou_schedules": [
            {
                "id": "r1-heat",
                "name": "x",
                "enabled": True,
                "rooms": ["living"],
                "side": "heat",
                "tier": "peak",
                "edge": "start",
                "phase": "after",
                "until": "next_change",
                "value": {"mode": "set", "temp": 19.0},
            }
        ]
    }
    adjust = mgr.make_adjuster(_room(), settings)
    assert adjust is not None
    # The hold runs until the next tier boundary...
    assert adjust(b1 + 1800, TargetTemps(heat=21.0, cool=24.0)).heat == 19.0
    # ...and the schedule stands after it (ULTRA_LOW, setback room: no shaping).
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

    # A valid timestamp with an unknown tier name: no guessing — the
    # lookahead stays off entirely (same as "Not set").
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
    assert timeline is not None and len(timeline.segments) == 1
    assert timeline.tier_at(future + 30) == TOU_TIER_OFF

    timeline = await mgr.async_build_timeline(
        {
            "tou_enabled": True,
            "tou_period_entity": "sensor.period",
            "tou_next_change_entity": "sensor.nx3",
            "tou_next_change_tier": "",
        }
    )
    assert timeline is not None and len(timeline.segments) == 1
    assert timeline.tier_at(future + 30) == TOU_TIER_OFF


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


def test_rows_ignores_malformed_entries() -> None:
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
    base = {"id": "ok", "name": "x", "enabled": True, "tier": "peak", "edge": "start"}
    room = _room(
        tou_overrides=[
            "not-a-dict",
            {"id": "no-action", "heat": 19.0},
            {**base, "action": "sundial", "heat": 19.0},
            {**base, "action": "arrive", "heat": 20.0, "clock": ""},  # arrive needs a clock
            {**base, "action": "set", "heat": None, "cool": None},  # no side to apply
            {**base, "action": "set", "heat": 19.0, "enabled": False},
        ]
    )
    records = TouManager.row_records(room)
    assert records == []
    adjust = mgr.make_adjuster(room, {})
    assert adjust is not None
    # None of the malformed rows produced a window: plain peak setback applies.
    assert adjust(boundary + 120, TargetTemps(heat=21.0, cool=24.0)).heat == 18.0


def test_unified_chain_arrive_coast_and_occupied_override() -> None:
    """The contributor scenario: reach 23 before the peak, coast to 18 during it,
    and an occupied user block (21) outranks the tariff machinery live."""
    mgr = TouManager(_make_hass({}))
    now = time.time()
    b = now + 3600.0  # peak starts in one hour
    mgr._timeline = TouTimeline(
        [
            TouSegment(start=now, end=b, tier=TOU_TIER_OFF),
            TouSegment(start=b, end=now + 16 * 3600.0, tier=TOU_TIER_PEAK),
        ],
        current_tier=TOU_TIER_OFF,
        current_rate=None,
    )
    settings = {
        "tou_schedules": [
            {
                "id": "arrive",
                "name": "top up before the peak",
                "rooms": ["living"],
                "side": "heat",
                "tier": "peak",
                "edge": "start",
                "phase": "before",
                "minutes": 40,
                "value": {"mode": "set", "temp": 23.0},
            },
            {
                "id": "coast",
                "name": "coast through the peak",
                "rooms": ["living"],
                "side": "heat",
                "tier": "peak",
                "edge": "start",
                "phase": "after",
                "until": "next_change",
                "value": {"mode": "set", "temp": 18.0},
            },
        ]
    }
    room = _room()
    adjust = mgr.make_adjuster(room, settings)
    assert adjust is not None
    # lead window [b-40m, b): the floor rises to 23 so the MPC tops up cheaply
    assert adjust(b - 1800.0, TargetTemps(heat=21.0, cool=None)).heat == 23.0
    # during the peak: the coast record replaces shaping, floor 18, drift allowed
    assert adjust(b + 60.0, TargetTemps(heat=21.0, cool=None)).heat == 18.0

    # An occupied user block outranks both tariff records...
    room["schedule_blocks"] = [
        {
            "id": "comfort",
            "name": "someone is here",
            "when": {"start": "00:00", "end": "00:00"},
            "gate": "occupied",
            "side": "heat",
            "value": {"mode": "set", "temp": 21.0},
        }
    ]
    assert mgr.make_adjuster(room, settings)(b + 60.0, TargetTemps(heat=21.0, cool=None)).heat == 21.0
    # ...and vanishes when the room goes vacant, leaving the coast intact
    vacant = mgr.make_adjuster(room, settings, room_vacant=True)
    assert vacant is not None
    assert vacant(b + 60.0, TargetTemps(heat=21.0, cool=None)).heat == 18.0


def test_schedule_fires_every_occurrence_and_previews() -> None:
    """Two peaks in the horizon produce two windows (legacy took the first only)."""
    mgr = TouManager(_make_hass({}))
    now = time.time()
    marks = [now, now + 60.0, now + 3660.0, now + 7260.0, now + 10860.0, now + 16 * 3600.0]
    tiers = [TOU_TIER_OFF, TOU_TIER_PEAK, TOU_TIER_OFF, TOU_TIER_PEAK]
    mgr._timeline = TouTimeline(
        [TouSegment(start=marks[i], end=marks[i + 1], tier=tiers[i]) for i in range(4)],
        current_tier=TOU_TIER_OFF,
        current_rate=None,
    )
    settings = {
        "tou_enabled": True,
        "tou_schedules": [
            {
                "id": "p",
                "name": "peak coast",
                "rooms": ["living", "bed"],
                "side": "heat",
                "tier": "peak",
                "edge": "start",
                "phase": "after",
                "until": "window",
                "value": {"mode": "set", "temp": 18.5},
            }
        ],
    }
    windows = mgr._compile_schedules("living", settings, mgr._timeline, TouManager.schedules(settings))
    assert len(windows["heat"]) == 2
    assert len(windows["cool"]) == 0

    preview = mgr.preview(settings)
    assert preview is not None
    assert len(preview["segments"]) == 4
    assert preview["records"]["p"]["count"] == 4  # two peaks x two rooms
