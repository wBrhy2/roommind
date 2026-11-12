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
    }
    room.update(overrides)
    return room


def _room_schedule(*defs: dict, enabled: bool = True) -> tuple[dict, dict]:
    """(room, settings) pair wiring card-style references to a shared library.

    Each definition becomes a ``tou_rules`` entry (id ``rl{n}``) and one
    ``tou_overrides`` row referencing it; the room's schedule section is on
    unless ``enabled`` says otherwise.
    """
    rules = [{"id": f"rl{i}", "name": f"rule {i}", **d} for i, d in enumerate(defs)]
    rows = [{"id": f"row{i}", "rule_id": f"rl{i}", "enabled": True} for i in range(len(defs))]
    room = _room(tou_schedule_enabled=enabled, tou_overrides=rows)
    return room, {"tou_rules": rules}


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


def test_period_aliases_override_word_recognition() -> None:
    """A user alias outranks the built-in vocabulary; unaliased strings (and
    aliases to a tier RoomMind does not know) keep the built-in answer."""
    settings = {"tou_period_aliases": {"partially_peak": "ultra_low", "bogus_target": "nope"}}
    assert TouManager._normalize_period("Partially_Peak", settings) == TOU_TIER_ULTRA_LOW
    assert TouManager._normalize_period("ulo_on_peak", settings) == TOU_TIER_PEAK
    # "bogus_target" is not a real string from any source; an alias whose
    # value is not a tier must be ignored: "peak" alone still maps normally.
    assert TouManager._normalize_period("peak", settings) == TOU_TIER_PEAK
    assert TouManager._normalize_period("partially_peak", {}) == TOU_TIER_PEAK


@pytest.mark.asyncio
async def test_tier_info_observes_names_rates_and_unmapped() -> None:
    """The dictionary block reads live: mapped names with the rate seen in
    that tier, plus strings the normalizer could not place."""
    hass = _make_hass({"sensor.period": "ulo_on_peak", "sensor.rate": "0.337"})
    mgr = TouManager(hass)
    settings = {"tou_enabled": True, "tou_period_entity": "sensor.period", "tou_rate_entity": "sensor.rate"}
    assert await mgr.async_build_timeline(settings) is not None
    info = mgr.tier_info(settings)
    peak = info["tiers"][TOU_TIER_PEAK]
    assert peak["current"] is True
    assert peak["rate"] == 0.337 and peak["kind"] == "observed"
    assert "ulo_on_peak" in peak["names"]
    assert info["tiers"][TOU_TIER_OFF]["rate"] is None
    assert info["unmapped"] == []

    # A provider wording the normalizer cannot read lands in unmapped.
    weird = _make_hass({"sensor.period": "tax block a"})
    mgr2 = TouManager(weird)
    settings2 = {"tou_enabled": True, "tou_period_entity": "sensor.period"}
    assert await mgr2.async_build_timeline(settings2) is not None
    info2 = mgr2.tier_info(settings2)
    assert {"state": "tax block a", "count": 1} in info2["unmapped"]


def test_tier_info_falls_back_to_windows_and_cutoffs() -> None:
    """No observations (fresh start): manual-window rates first, then cost
    thresholds marked as cutoffs so the UI can say "from"."""
    mgr = TouManager(_make_hass({}))
    settings = {
        "tou_peak_threshold": 0.3,
        "tou_mid_threshold": 0.15,
        "tou_ultra_threshold": 0.02,
        "tou_rate_windows": [{"id": "w", "rate": 0.42, "start": "17:00", "end": "21:00"}],
    }
    info = mgr.tier_info(settings)
    assert info["tiers"][TOU_TIER_PEAK]["rate"] == 0.42
    assert info["tiers"][TOU_TIER_PEAK]["kind"] == "window"
    assert info["tiers"][TOU_TIER_MID]["rate"] == 0.15
    assert info["tiers"][TOU_TIER_MID]["kind"] == "cutoff"
    assert info["tiers"][TOU_TIER_OFF]["rate"] is None  # between: no number to show


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


def test_store_relaxes_expensive_run() -> None:
    mgr = TouManager(_make_hass({}))
    mgr._timeline = _timeline((TOU_TIER_OFF, 0), (TOU_TIER_PEAK, 120))
    adjust = mgr.make_adjuster(_room(tou_store_enabled=True), {"tou_enabled": True})
    assert adjust is not None
    ts = time.time() + 60.0
    result = adjust(ts, TargetTemps(heat=21.0, cool=24.0))
    assert result.heat == 18.0  # swung down from 21, floor above band & eco
    assert result.cool == 27.0  # drifts up toward the eco cooling target


def test_store_never_raises_a_colder_schedule() -> None:
    mgr = TouManager(_make_hass({}))
    mgr._timeline = _timeline((TOU_TIER_OFF, 0), (TOU_TIER_PEAK, 120))
    adjust = mgr.make_adjuster(_room(tou_store_enabled=True), {})
    assert adjust is not None
    result = adjust(time.time() + 60.0, TargetTemps(heat=16.0, cool=20.0))
    assert result.heat == 16.0  # relaxation only lowers, never raises heat
    assert result.cool == 23.0  # cooling relaxes upward by the swing


def test_store_run_merges_contiguous_expensive_tiers() -> None:
    """Mid + peak in a row are one coast: relaxation starts at the mid tier."""
    mgr = TouManager(_make_hass({}))
    mgr._timeline = _timeline((TOU_TIER_OFF, 0), (TOU_TIER_MID, 60), (TOU_TIER_PEAK, 60))
    adjust = mgr.make_adjuster(_room(tou_store_enabled=True), {})
    assert adjust is not None
    result = adjust(time.time() + 30 * 60, TargetTemps(heat=21.0, cool=24.0))
    assert result.heat == 18.0
    assert result.cool == 27.0


def test_store_is_quiet_without_a_run() -> None:
    """Cheap tier, nothing expensive ahead: no relaxation, no pre-charge —
    even with a learned decay model that could coast somewhere."""
    mgr = TouManager(_make_hass({}))
    mgr._timeline = _timeline((TOU_TIER_ULTRA_LOW, 120))
    adjust = mgr.make_adjuster(_room(tou_store_enabled=True), {}, decay=(0.2, 8.0))
    assert adjust is not None
    out = adjust(time.time() + 60.0, TargetTemps(heat=21.0, cool=24.0))
    assert out.heat == 21.0 and out.cool == 24.0

    off = mgr.make_adjuster(_room(), {}, decay=(0.2, 8.0))
    assert off is None  # no store, no rows, no blocks: no participation
    forced_off = adjust(time.time() + 60.0, TargetTemps(heat=None, cool=None))
    assert forced_off == TargetTemps(heat=None, cool=None)  # off stays off


def test_store_precharge_bounds_and_gates() -> None:
    """Raise toward the coast floor before a run, bounded by the swing and the
    consent band; skipped for vacation and immature models; presence-blind."""
    mgr = TouManager(_make_hass({}))
    b = time.time()
    mgr._timeline = _timeline((TOU_TIER_ULTRA_LOW, 60), (TOU_TIER_PEAK, 300))
    ts = b + 30 * 60  # mid cheap window, the (long) peak still ahead
    decay = (0.2, 10.0)
    room = _room(tou_store_enabled=True)

    raised = mgr.make_adjuster(room, {}, decay=decay)
    assert raised is not None
    # floor_end 18, huge lead → needed() explodes → the swing cap rules: 21 + 3.
    assert raised(ts, TargetTemps(heat=21.0, cool=None)).heat == 24.0

    capped = mgr.make_adjuster({**room, "tou_heat_max": 22.5}, {}, decay=decay)
    assert capped is not None
    assert capped(ts, TargetTemps(heat=21.0, cool=None)).heat == 22.5  # the band wins

    vacant = mgr.make_adjuster(room, {}, room_vacant=True, presence_away=True, decay=decay)
    assert vacant is not None
    assert vacant(ts, TargetTemps(heat=21.0, cool=None)).heat == 24.0  # economics, not occupancy

    vacation = {"vacation_until": b + 3600.0, "vacation_temp": 15.0}
    vac = mgr.make_adjuster(room, vacation, decay=decay)
    assert vac is not None
    assert vac(ts, TargetTemps(heat=21.0, cool=None)).heat == 21.0  # no pre-charge in vacation

    inside = mgr.make_adjuster(room, {}, decay=decay)
    assert inside is not None
    assert inside(b + 90 * 60, TargetTemps(heat=21.0, cool=None)).heat == 18.0  # the run relaxes

    no_model = mgr.make_adjuster(room, {})
    assert no_model is not None
    assert no_model(ts, TargetTemps(heat=21.0, cool=None)).heat == 21.0  # nothing to derive from


def test_store_prechills_and_relaxes_cooling_run() -> None:
    """Cooling mirrors heating: drift up toward the eco ceiling during an
    expensive run, pre-chill within the swing before it, and degrade the
    same way (no chill without a model, none during vacation)."""
    mgr = TouManager(_make_hass({}))
    b = time.time()
    mgr._timeline = _timeline((TOU_TIER_OFF, 60), (TOU_TIER_PEAK, 600))
    ts = b + 30 * 60  # inside the off stretch, the (long) peak ahead
    room = _room(tou_store_enabled=True)

    # Pre-chill: outdoor 35 °C will pull the room up; the demand lands on the
    # swing floor (comfort_cool 24 − 3), never deeper.
    chilled = mgr.make_adjuster(room, {}, decay=(0.2, 35.0))
    assert chilled is not None
    assert chilled(ts, TargetTemps(heat=None, cool=24.0)).cool == 21.0

    # During the run: relaxed toward min(swing, band, eco_cool) = 27.
    inside = mgr.make_adjuster(room, {}, decay=(0.2, 35.0))
    assert inside is not None
    assert inside(b + 90 * 60, TargetTemps(heat=None, cool=24.0)).cool == 27.0

    # No model: nothing to derive a chill from — the schedule stands.
    no_model = mgr.make_adjuster(room, {})
    assert no_model is not None
    assert no_model(ts, TargetTemps(heat=None, cool=24.0)).cool == 24.0

    # Vacation: no pre-chill spending.
    vacation = {"vacation_until": b + 3600.0, "vacation_temp": 28.0}
    vac = mgr.make_adjuster(room, vacation, decay=(0.2, 35.0))
    assert vac is not None
    assert vac(ts, TargetTemps(heat=None, cool=24.0)).cool == 24.0

    # A cool target that would need warming *down* (outdoor below the
    # ceiling) has no coast to exploit: untouched.
    mild = mgr.make_adjuster(room, {}, decay=(0.2, 20.0))
    assert mild is not None
    assert mild(ts, TargetTemps(heat=None, cool=24.0)).cool == 24.0


def test_override_never_shaved() -> None:
    mgr = TouManager(_make_hass({}))
    mgr._timeline = _timeline((TOU_TIER_PEAK, 120))
    room = _room(tou_store_enabled=True, override_heat=24.0, override_cool=None, override_until=None)
    adjust = mgr.make_adjuster(room, {})
    assert adjust is not None
    result = adjust(time.time() + 60.0, TargetTemps(heat=24.0, cool=27.0))
    assert result.heat == 24.0  # the override value passes through untouched


def test_mold_active_blocks_heat_relaxation() -> None:
    mgr = TouManager(_make_hass({}))
    mgr._timeline = _timeline((TOU_TIER_PEAK, 120))
    adjust = mgr.make_adjuster(_room(tou_store_enabled=True), {}, mold_active=True)
    assert adjust is not None
    result = adjust(time.time() + 60.0, TargetTemps(heat=21.0, cool=24.0))
    assert result.heat == 21.0  # prevention outranks tariff relaxation
    assert result.cool == 27.0  # cooling side is unaffected


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
    room, settings = _room_schedule(
        {
            "tier": "peak",
            "edge": "start",
            "action": "set",
            "heat": 19.0,
            "cool": None,
            "until": "minutes",
            "minutes": 30,
        }
    )
    adjust = mgr.make_adjuster(room, settings)
    assert adjust is not None
    # Inside the row window: the rule's temperature replaces the schedule.
    inside = adjust(boundary + 120, TargetTemps(heat=21.0, cool=24.0))
    assert inside.heat == 19.0
    # Blank cool side: the rule skips cooling; the plain target stands.
    assert inside.cool == 24.0
    # After the 30-minute hold (still inside the peak segment): schedule resumes.
    assert adjust(boundary + 31 * 60, TargetTemps(heat=21.0, cool=24.0)).heat == 21.0


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
    assert adjust(boundary + 91 * 60, TargetTemps(heat=21.0, cool=24.0)).heat == 21.0


def test_row_side_blanks_skip_per_mode() -> None:
    """A blank rule side skips that mode; plain shaping applies there."""
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
            "tier": "peak",
            "edge": "start",
            "action": "set",
            "until": "minutes",
            "minutes": 120,
        }
        base.update(extra)
        return base

    # Both sides set: each gets its own value within the same window.
    room, settings = _room_schedule(row(heat=18.5, cool=26.0))
    adjust = mgr.make_adjuster(room, settings)
    assert adjust is not None
    out = adjust(boundary + 60.0, TargetTemps(heat=21.0, cool=24.0))
    assert out.heat == 18.5
    assert out.cool == 26.0

    # Heating blank: the rule is skipped there (plain target stands), cooling holds.
    room, settings = _room_schedule(row(heat=None, cool=26.0))
    adjust = mgr.make_adjuster(room, settings)
    assert adjust is not None
    out = adjust(boundary + 60.0, TargetTemps(heat=21.0, cool=24.0))
    assert out.heat == 21.0
    assert out.cool == 26.0

    # Both blank: no window at all — and with no store layer and no blocks
    # there is nothing for the adjuster to do.
    room, settings = _room_schedule(row(heat=None, cool=None))
    assert mgr.make_adjuster(room, settings) is None


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
    room, settings = _room_schedule(
        {
            "tier": "peak",
            "edge": "start",
            "action": "set",
            "heat": 19.0,
            "until": "tier_window",
        }
    )
    adjust = mgr.make_adjuster(room, settings)
    assert adjust is not None
    assert adjust(b1 + 5.0, TargetTemps(heat=21.0, cool=24.0)).heat == 19.0
    # after the peak->off boundary nothing is held and the tier is plain off:
    # the schedule target returns untouched.
    assert adjust(b2 + 5.0, TargetTemps(heat=21.0, cool=24.0)).heat == 21.0


def test_row_records_expansion() -> None:
    """Referenced shared rules fan out into one engine record per set side."""
    rules = TouManager.rule_library(
        {
            "tou_rules": [
                {
                    "id": "d",
                    "name": "mid-peak relax",
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
                    "tier": "ultra_low",
                    "edge": "end",
                    "action": "arrive",
                    "heat": 20.0,
                    "cool": None,
                    "clock": "09:00",
                },
                {"id": "l", "name": "lower", "tier": "any", "edge": "start", "action": "lower", "heat": -2.0},
            ]
        }
    )
    room = _room(
        tou_schedule_enabled=True,
        tou_overrides=[
            {"id": "r1", "rule_id": "d", "enabled": True},
            {"id": "r2", "rule_id": "a2", "enabled": True},
            {"id": "r3", "rule_id": "l", "enabled": True},
        ],
    )
    recs = TouManager.row_records(room, rules)
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


def test_row_records_ignores_broken_references() -> None:
    """Dangling, disabled, non-dict and legacy inline rows compile to nothing."""
    rules = TouManager.rule_library(
        {
            "tou_rules": [
                {"id": "ok", "name": "n", "tier": "peak", "edge": "start", "action": "sundial", "heat": 19.0},
            ]
        }
    )
    room = _room(
        tou_schedule_enabled=True,
        tou_overrides=[
            "not-a-dict",
            {"id": "legacy", "tier": "peak", "action": "set", "heat": 19.0},  # inline format: no rule_id
            {"id": "dangling", "rule_id": "gone", "enabled": True},
            {"id": "off", "rule_id": "ok", "enabled": False},
        ],
    )
    assert TouManager.row_records(room, rules) == []
    # The rule with the unknown action compiles to nothing even when referenced.
    live = _room(tou_schedule_enabled=True, tou_overrides=[{"id": "r", "rule_id": "ok", "enabled": True}])
    assert TouManager.row_records(live, rules) == []


def test_schedule_disabled_section_cannot_steer() -> None:
    """The section toggle gates the whole schedule: rows stay, they just don't act.

    Reference counting for the shared library still sees the rows (temporary
    measures never lose rules), but the room's targets keep the plain shaping.
    """
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
    room, settings = _room_schedule(
        {"tier": "peak", "edge": "start", "action": "set", "heat": 19.0, "until": "tier_window"},
        enabled=False,
    )
    assert TouManager.row_records(room, TouManager.rule_library(settings)) == []
    # The room participates via the store toggle; the gated rows never fire.
    adjust = mgr.make_adjuster({**room, "tou_store_enabled": True}, settings)
    assert adjust is not None
    assert adjust(boundary + 120, TargetTemps(heat=21.0, cool=None)).heat == 18.0
    # Rows still exist for the library's reference count — enabling brings them back.
    room_on = {**room, "tou_schedule_enabled": True}
    adjust_on = mgr.make_adjuster(room_on, settings)
    assert adjust_on is not None
    assert adjust_on(boundary + 120, TargetTemps(heat=21.0, cool=None)).heat == 19.0


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
    room = _room()
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


def test_row_arrive_clock_infers_time_mode() -> None:
    """An arrive rule carrying a clock is time-mode regardless of ``edge``/
    ``until`` echo (the card hides those fields): the window runs from the
    period's end to the clock."""
    room, settings = _room_schedule(
        {"tier": "ultra_low", "edge": "start", "action": "arrive", "heat": 20.0, "clock": "09:00"}
    )
    (rec,) = TouManager.row_records(room, TouManager.rule_library(settings))
    assert rec["until"] == "clock" and rec["clock"] == "09:00"
    assert rec["edge"] == "end" and rec["tier"] == "ultra_low"
    assert rec["value"] == {"mode": "arrive", "temp": 20.0}


def test_row_arrive_coast_through_lands_at_period_end() -> None:
    """A clock-less arrive rule coasts through the anchored period: the window
    is the period itself and the decay curve lands the target at its end."""
    mgr = TouManager(_make_hass({}))
    now = time.time()
    b = now + 3600.0  # peak starts in one hour, runs four
    mgr._timeline = TouTimeline(
        [
            TouSegment(start=now, end=b, tier=TOU_TIER_MID),
            TouSegment(start=b, end=b + 4 * 3600.0, tier=TOU_TIER_PEAK),
            TouSegment(start=b + 4 * 3600.0, end=now + 16 * 3600, tier=TOU_TIER_OFF),
        ],
        current_tier=TOU_TIER_MID,
        current_rate=None,
    )
    room, settings = _room_schedule(
        {"tier": "peak", "edge": "end", "action": "arrive", "heat": 19.0, "until": "tier_window", "clock": ""}
    )
    (rec,) = TouManager.row_records(room, TouManager.rule_library(settings))
    assert rec["until"] == "window"
    assert rec["edge"] == "start"  # forced: the coast begins when the period starts
    assert rec["value"] == {"mode": "arrive", "temp": 19.0}

    adjust = mgr.make_adjuster({**room, "tou_schedule_enabled": False}, settings)
    assert adjust is None  # store off and the section gated: nothing participates
    adjust = mgr.make_adjuster({**room, "tou_schedule_enabled": True}, settings, decay=(0.1, 10.0))
    assert adjust is not None

    def expected(ts: float) -> float:
        dt_hours = (b + 4 * 3600.0 - ts) / 3600.0
        return 10.0 + (19.0 - 10.0) * math.exp(0.1 * dt_hours)

    # Inside the coast: the derived floor slides down the decay curve...
    at_start = adjust(b, TargetTemps(heat=21.0, cool=None)).heat
    assert at_start == pytest.approx(expected(b))
    assert at_start > 19.0
    mid = adjust(b + 2 * 3600.0, TargetTemps(heat=21.0, cool=None)).heat
    assert mid == pytest.approx(expected(b + 2 * 3600.0)) and mid < at_start
    # ...landing exactly on the arrival temperature as the period ends.
    assert adjust(b + 4 * 3600.0 - 1.0, TargetTemps(heat=21.0, cool=None)).heat == pytest.approx(19.0, abs=0.02)
    # Before and after the period the row keeps its hands off; with the store
    # layer off, the plain schedule stands on both sides.
    assert adjust(b - 60.0, TargetTemps(heat=21.0, cool=None)).heat == 21.0
    assert adjust(b + 4 * 3600.0 + 60.0, TargetTemps(heat=21.0, cool=None)).heat == 21.0


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
    room = _room()

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
    adjust = mgr.make_adjuster(_room(), settings, decay=(1.0, -20.0))
    assert adjust is not None
    assert adjust(b, TargetTemps(heat=21.0, cool=None)).heat == 28.0  # clamp_max default


def test_rows_days_filter() -> None:
    """Days on a shared rule restrict which tier boundaries its rows fire on."""
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
        room, settings = _room_schedule(
            {
                "tier": "off",
                "edge": "start",
                "action": "set",
                "heat": 19.0,
                "until": "tier_window",
                "days": days,
            }
        )
        return mgr.make_adjuster(room, settings)

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
            "tou_schedule_enabled": True,
            "tou_overrides": [{"id": "r1", "rule_id": "shared1", "enabled": True}],
        }
    }
    settings = {
        "tou_enabled": True,
        "tou_rules": [
            {
                "id": "shared1",
                "name": "x",
                "tier": "peak",
                "edge": "start",
                "action": "set",
                "heat": 19.0,
                "until": "tier_window",
            }
        ],
    }
    out = mgr.preview(settings, None, rooms)
    assert out is not None
    assert out["areas"]["living"], "the room row window must show up in the preview"
    assert "shared1-heat" in out["records"]


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
    assert adjust(boundary + 120, TargetTemps(heat=21.0, cool=24.0)).heat == 21.0

    disabled = {**rec, "rooms": ["living"], "enabled": False}
    adjust2 = mgr.make_adjuster(_room(), {"tou_schedules": [disabled]})
    assert adjust2 is not None
    assert adjust2(boundary + 120, TargetTemps(heat=21.0, cool=24.0)).heat == 21.0

    # A row is scoped to its own room by construction: it compiles for "living".
    row_room, row_settings = _room_schedule(
        {"tier": "peak", "edge": "start", "action": "set", "heat": 19.0, "until": "minutes", "minutes": 60}
    )
    row_adjust = mgr.make_adjuster(row_room, row_settings)
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
    builder = mgr.make_price_series_builder(_room(tou_store_enabled=True), {"tou_enabled": True})
    assert builder is not None
    # One hour per block: block 0 sits in OFF, the rest land past the +60min boundary.
    series = builder(time.time(), 4, 60.0)
    assert series[0] == 1.0  # off
    assert series[-1] == 3.0  # peak
    assert mgr.make_price_series_builder(_room(), {}) is None
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


def test_rules_ignore_malformed_entries() -> None:
    """Malformed library rules referenced by rows compile to nothing."""
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
    base = {"id": "ok", "name": "x", "tier": "peak", "edge": "start"}
    room, settings = _room_schedule(
        {"action": "sundial", "heat": 19.0},  # unknown action
        {"action": "arrive", "heat": 20.0, "clock": "", "until": "clock"},  # by-time without a time
        {"action": "set", "heat": None, "cool": None},  # no side to apply
    )
    settings["tou_rules"].append({**base, "action": "set", "heat": 19.0, "until": "tier_window"})
    room["tou_overrides"].append({"id": "r-good", "rule_id": "ok", "enabled": False})
    records = TouManager.row_records(room, TouManager.rule_library(settings))
    assert records == []
    # The store toggle carries participation; none of the malformed rules made a window.
    adjust = mgr.make_adjuster({**room, "tou_store_enabled": True}, settings)
    assert adjust is not None
    assert adjust(boundary + 120, TargetTemps(heat=21.0, cool=24.0)).heat == 18.0  # the run relaxes


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
