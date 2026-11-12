"""Time-of-Use (TOU) tariff manager for RoomMind.

Reads the configured tariff source (rate/period/next-change sensors, tariff
calendars, or plain tier flags), builds a per-cycle tier timeline, and exposes:

* ``make_adjuster``  — a target-temperature modifier spliced into the schedule
  target resolver, applying tier setback/boost shaping and tariff-schedule
  windows. A schedule window behaves exactly like a synthesized schedule
  block: the MPC treats it as the next scheduled target, so conditioning
  stops at the tier boundary and the learned thermal model coasts the room
  through the expensive window on stored heat (mirrored for cooling).
  Windows come from two sources: the advanced global ``tou_schedules``
  records and the room's ``tou_overrides`` rows — thin references
  (``{id, rule_id, enabled}``) into the shared rule library
  (``settings["tou_rules"]``) that the user authors on the device card as
  (tier) (action) (heat) (cool) (end); same name means same rule in every
  room. The per-room ``tou_schedule_enabled`` toggle gates the whole
  schedule.
* ``make_price_builder`` — per-block energy-price multipliers that scale the
  MPC optimizer's ``w_energy`` term, biasing runs into cheap tiers.
* ``snapshot`` — current tier/rate/next-change for the global energy sensors
  and diagnostics.

Safety rules encoded by the adjuster:
* a manual override at the timestamp is never shaved by tariff logic;
* "off" targets (None sides) stay off;
* setback never *raises* demand; boost never *lowers* it;
* boost only for ``setback_boost`` rooms, not during vacation, and only when
  occupied unless ``tou_boost_unoccupied`` is set;
* heat-lowering steps are skipped while mold prevention is active;
* every shift is guarded by the clamp band without overriding user targets
  that already sit outside it.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ..const import (
    DEFAULT_TOU_BOOST_DELTA,
    DEFAULT_TOU_CLAMP_MAX,
    DEFAULT_TOU_CLAMP_MIN,
    DEFAULT_TOU_COAST_MAX_MINUTES,
    DEFAULT_TOU_MID_SETBACK_DELTA,
    DEFAULT_TOU_PEAK_COOL_CEILING,
    DEFAULT_TOU_PEAK_HEAT_FLOOR,
    TOU_ARRIVAL_EXPONENT_CAP,
    TOU_SOURCE_FLAGS,
    TOU_SOURCE_NONE,
    TOU_SOURCE_PERIOD,
    TOU_SOURCE_RATE,
    TOU_TIER_MID,
    TOU_TIER_OFF,
    TOU_TIER_PEAK,
    TOU_TIER_ULTRA_LOW,
    TOU_TIER_UNKNOWN,
    TOU_TIMELINE_HORIZON_MINUTES,
    TOU_WEEKDAYS,
    TargetTemps,
    tou_price_multiplier,
)

_LOGGER = logging.getLogger(__name__)

AdjustFn = Callable[[float, TargetTemps], TargetTemps]

#: (alpha_per_hour, outdoor_temp_C) from the learned room model: enough to
#: back-cast the temperature a coasting room needs *now* to reach a target at
#: a later time. None while the model is too young (arrive rows then hold the
#: flat temperature, the classic coast behavior).
DecayFn = tuple[float, float]



def normalize_period_state(state: str) -> str:
    """Map a period sensor state (any tariff wording) onto a RoomMind tier.

    Order matters: ``ulo_off_peak`` contains both "off" and "peak" and must
    resolve to off, while ``ulo_on_peak`` resolves to peak and ``ulo_overnight``
    (the Ontario ultra-low overnight window) resolves to ultra_low.
    """
    s = (state or "").strip().lower().replace("-", "_")
    if s in ("", "unknown", "unavailable"):
        return TOU_TIER_UNKNOWN
    if "overnight" in s or "ultra" in s or "super_off" in s or "deep_off" in s:
        return TOU_TIER_ULTRA_LOW
    if "mid" in s:
        return TOU_TIER_MID
    if "off" in s:
        return TOU_TIER_OFF
    if "peak" in s or "high" in s or s in ("on", "expensive"):
        return TOU_TIER_PEAK
    return TOU_TIER_UNKNOWN


def _hhmm_to_minutes(raw: Any) -> int | None:
    parts = str(raw or "").split(":")
    if len(parts) < 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def _value_candidate(value: Any, plain: float) -> float | None:
    """Resolve a record/block value against the plain schedule target."""
    if not isinstance(value, dict):
        return None
    try:
        if str(value.get("mode") or "set") == "shift":
            delta = abs(float(value.get("delta")))  # type: ignore[arg-type]
            if str(value.get("direction") or "raise") == "lower":
                delta = -delta
            return plain + delta
        return float(value.get("temp"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _arrive_candidate(
    value: Any, window: dict[str, Any], ts: float, decay: DecayFn | None, is_heat: bool
) -> float | None:
    """Decay-derived floor for an ``arrive`` record at *ts*.

    The room must coast to ``temp`` exactly at the window end with
    conditioning stopped. With the learned loss rate alpha and outdoor
    temperature t_out, an idle room decays as
    ``T(t) = t_out + (T(t0) - t_out) * e^(-alpha*(t - t0))``, so the
    temperature needed right now is
    ``t_out + (temp - t_out) * e^(alpha*(t_end - ts))`` — the "indirect
    target" the user schedules, replanned every cycle and continuous at the
    arrival instant. Mirrors automatically for the cooling side. Without a
    confident learned model (``decay`` None) — or when the outdoors would
    drift the room the wrong way (above the heating target / below the
    cooling target) — this degrades to the flat temperature, i.e. classic
    coast behavior.
    """
    if not isinstance(value, dict) or str(value.get("mode")) != "arrive":
        return None
    try:
        temp = float(value.get("temp"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if decay is None:
        return temp
    alpha, t_out = decay
    if is_heat and t_out >= temp:
        return temp
    if not is_heat and t_out <= temp:
        return temp
    dt_hours = max(window["end"] - ts, 0.0) / 3600.0
    exponent = min(max(alpha * dt_hours, 0.0), TOU_ARRIVAL_EXPONENT_CAP)
    return t_out + (temp - t_out) * math.exp(exponent)



def _active_window(windows: list[dict[str, Any]], ts: float) -> dict[str, Any] | None:
    found: dict[str, Any] | None = None
    for w in windows:
        if w["start"] <= ts < w["end"]:
            found = w  # list order = priority: the later record wins
    return found


def _heat_guarded(cand: float, shaped: float, plain: float, mold_active: bool, vacation_now: bool) -> bool:
    """True when a heating-side candidate must be rejected (safety wins)."""
    return (mold_active and cand < shaped) or (vacation_now and cand > plain)


def _apply_tariff_windows(
    windows: list[dict[str, Any]],
    ts: float,
    plain: float | None,
    shaped: float,
    clamp_min: float,
    clamp_max: float,
    mold_active: bool,
    vacation_now: bool,
    is_heat: bool,
    decay: DecayFn | None = None,
) -> float | None:
    """Tariff schedule records replace tier shaping on their side (chain step 4)."""
    if plain is None:
        return None  # off stays off: no tariff record activates a side
    active = _active_window(windows, ts)
    if active is None:
        return shaped
    rec = active["rec"]
    value = rec.get("value")
    cand = _arrive_candidate(value, active, ts, decay, is_heat)
    if cand is None:
        cand = _value_candidate(value, plain)
    if cand is None:
        return shaped

    if rec.get("guard"):
        # Migrated legacy coast: one-way drift versus the schedule, shaping
        # bypassed — exactly the old coast-window semantics.
        if is_heat:
            return cand if cand < plain else plain
        return cand if cand > plain else plain
    cand = min(max(cand, clamp_min), clamp_max)
    if is_heat and _heat_guarded(cand, shaped, plain, mold_active, vacation_now):
        return shaped
    return cand


def _block_active(block: Any, side: str, ts: float, occupied: bool) -> bool:
    """User schedule block: side match, presence gate, weekday and clock window."""
    if not isinstance(block, dict) or not block.get("enabled", True):
        return False
    if str(block.get("side") or "both") not in (side, "both"):
        return False
    gate = str(block.get("gate") or "always")
    if gate == "occupied" and not occupied:
        return False
    if gate == "away" and occupied:
        return False
    when = block.get("when") or {}
    start = _hhmm_to_minutes(when.get("start"))
    end = _hhmm_to_minutes(when.get("end"))
    if start is None or end is None:
        return False
    local = dt_util.as_local(dt_util.utc_from_timestamp(ts))
    days = [d for d in (when.get("days") or []) if isinstance(d, str)]
    if days and TOU_WEEKDAYS[local.weekday()] not in days:
        return False
    at = local.hour * 60 + local.minute
    if start == end:
        return True  # full day
    if start < end:
        return bool(start <= at < end)
    return bool(at >= start or at < end)  # wraps past midnight


def _apply_user_blocks(
    blocks: Any,
    side: str,
    ts: float,
    occupied: bool,
    plain: float | None,
    shaped: float,
    mold_active: bool,
    vacation_now: bool,
) -> float | None:
    """Room-level schedule blocks outrank the tariff machinery (chain step 3)."""
    if plain is None:
        return None  # off stays off
    if not isinstance(blocks, list):
        return shaped
    active: Any = None
    for block in blocks:
        if _block_active(block, side, ts, occupied):
            active = block  # later in the list wins
    if active is None:
        return shaped
    cand = _value_candidate(active.get("value"), plain)
    if cand is None:
        return shaped
    if side == "heat" and _heat_guarded(cand, shaped, plain, mold_active, vacation_now):
        return shaped
    return cand


@dataclass
class TouSegment:
    """A contiguous stretch of one tariff tier inside the horizon."""

    start: float
    end: float
    tier: str


class TouTimeline:
    """Tier timeline covering [now, now + horizon] with no gaps."""

    def __init__(self, segments: list[TouSegment], *, current_tier: str, current_rate: float | None) -> None:
        self.segments = segments
        self.current_tier = current_tier
        self.current_rate = current_rate

    def tier_at(self, ts: float) -> str:
        """Return the tier covering *ts* (clamped to the timeline edges)."""
        if not self.segments:
            return TOU_TIER_UNKNOWN
        if ts <= self.segments[0].start:
            return self.segments[0].tier
        for seg in self.segments:
            if seg.start <= ts < seg.end:
                return seg.tier
        return self.segments[-1].tier

    def boundaries(self) -> list[tuple[float, str, str]]:
        """Return (ts, tier_before, tier_after) for every change in the horizon."""
        out: list[tuple[float, str, str]] = []
        for prev, nxt in zip(self.segments, self.segments[1:], strict=False):
            if nxt.tier != prev.tier:
                out.append((nxt.start, prev.tier, nxt.tier))
        return out

    def next_boundary_after(self, ts: float, to_tier: str | None = None) -> float | None:
        """First change after *ts* (optionally into *to_tier*), or None."""
        for b_ts, _, tier_after in self.boundaries():
            if b_ts > ts and (to_tier is None or tier_after == to_tier):
                return b_ts
        return None


class TouManager:
    """Owns the tariff source reading and the TOU target shaping."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._timeline: TouTimeline | None = None
        self._source_kind: str = TOU_SOURCE_NONE
        self._rate_unit: str = ""

    @property
    def timeline(self) -> TouTimeline | None:
        """Return the timeline computed by the last cycle."""
        return self._timeline

    # ------------------------------------------------------------------
    # Timeline construction
    # ------------------------------------------------------------------

    async def async_build_timeline(self, settings: dict[str, Any]) -> TouTimeline | None:
        """Read the configured sources and build the horizon timeline.

        Returns None when the feature is disabled. A missing or unavailable
        source yields a timeline with ``current_tier`` unknown, which shapes
        nothing — behavior is identical to TOU being off. ``tou_lookahead``
        picks which future source counts (calendars, next-change, or none);
        "auto" keeps the legacy order: calendars, with next-change as the
        fallback when no calendar window covers the horizon.
        """
        if not settings.get("tou_enabled", False):
            self._timeline = None
            self._source_kind = TOU_SOURCE_NONE
            return None

        now = time.time()
        horizon = now + TOU_TIMELINE_HORIZON_MINUTES * 60.0

        current, rate, unit, source = self._resolve_current_tier(settings)
        self._source_kind = source
        self._rate_unit = unit

        lookahead = str(settings.get("tou_lookahead") or "auto")
        points: set[float] = {now, horizon}
        events: list[tuple[float, float, str]] = []
        if lookahead in ("auto", "calendar"):
            for entry in settings.get("tou_calendar_entities") or []:
                entity_id = str(entry.get("entity_id", ""))
                tier = str(entry.get("tier", ""))
                if not entity_id or tier not in (TOU_TIER_PEAK, TOU_TIER_MID, TOU_TIER_OFF, TOU_TIER_ULTRA_LOW):
                    continue
                for ev_start, ev_end in await self._async_calendar_windows(entity_id, now, horizon):
                    points.update((ev_start, ev_end))
                    events.append((ev_start, ev_end, tier))
            for win_start, win_end, win_tier in self._rate_window_events(settings, now, horizon):
                points.update((win_start, win_end))
                events.append((win_start, win_end, win_tier))

        if not events and lookahead in ("auto", "next_change", "next_change_rate"):
            next_ts, next_tier = self._resolve_next_change(settings, lookahead)
            if next_ts is not None and now < next_ts < horizon:
                points.add(next_ts)
                events.append((next_ts, horizon, next_tier))

        segments = self._paint_segments(now, horizon, current, events)
        timeline = TouTimeline(segments, current_tier=current, current_rate=rate)
        self._timeline = timeline
        return timeline

    def _resolve_current_tier(self, settings: dict[str, Any]) -> tuple[str, float | None, str, str]:
        """Return (tier, rate, rate_unit, source_kind) for *now*.

        ``tou_tier_source`` selects which source may answer; "auto" keeps the
        legacy cascade (period -> flags -> rate). The numeric rate is always
        read when a cost sensor is bound — it feeds the rate sensors and the
        price display even when another source classifies the tier.
        """
        mode = str(settings.get("tou_tier_source") or "auto")
        period_entity = str(settings.get("tou_period_entity") or "")
        if period_entity and mode in ("auto", "period"):
            state = self.hass.states.get(period_entity)
            if state is not None and state.state not in ("unavailable", "unknown"):
                tier = normalize_period_state(state.state)
                if tier != TOU_TIER_UNKNOWN:
                    rate, unit = self._read_rate(settings)
                    return tier, rate, unit, TOU_SOURCE_PERIOD

        flags = settings.get("tou_flag_entities") or {}
        if isinstance(flags, dict) and mode in ("auto", "flags"):
            for tier, entity_id in flags.items():
                if not entity_id or not isinstance(entity_id, str):
                    continue
                state = self.hass.states.get(entity_id)
                if state is not None and state.state == "on":
                    rate, unit = self._read_rate(settings)
                    return str(tier), rate, unit, TOU_SOURCE_FLAGS

        rate, unit = self._read_rate(settings)
        if mode not in ("auto", "rate"):
            return TOU_TIER_UNKNOWN, rate, unit, TOU_SOURCE_NONE
        tier = self._tier_from_rate(rate, settings)
        if tier != TOU_TIER_UNKNOWN:
            return tier, rate, unit, TOU_SOURCE_RATE
        return TOU_TIER_UNKNOWN, rate, unit, TOU_SOURCE_NONE

    def _read_rate(self, settings: dict[str, Any]) -> tuple[float | None, str]:
        entity_id = str(settings.get("tou_rate_entity") or "")
        if not entity_id:
            return None, ""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            return None, ""
        try:
            rate = float(state.state)
        except (TypeError, ValueError):
            return None, ""
        unit = str(state.attributes.get("unit_of_measurement", ""))
        return rate, unit

    @staticmethod
    def _tier_from_rate(rate: float | None, settings: dict[str, Any]) -> str:
        """Classify *rate* against the configured tier thresholds."""
        if rate is None:
            return TOU_TIER_UNKNOWN
        peak = settings.get("tou_peak_threshold")
        mid = settings.get("tou_mid_threshold")
        ultra = settings.get("tou_ultra_threshold")
        if isinstance(peak, (int, float)) and rate >= float(peak):
            return TOU_TIER_PEAK
        if isinstance(mid, (int, float)) and rate >= float(mid):
            return TOU_TIER_MID
        if isinstance(ultra, (int, float)) and rate <= float(ultra):
            return TOU_TIER_ULTRA_LOW
        return TOU_TIER_OFF

    def _resolve_next_change(self, settings: dict[str, Any], lookahead: str) -> tuple[float | None, str]:
        """Read the next-change timestamp and the tier that follows it.

        ``next_change_rate`` mode classifies the rate the provider publishes
        for the period after the change (same cost thresholds as the rate
        tier source); the fixed-tier modes read the tier dropdown. An
        unusable input at either step yields no lookahead at all rather
        than a guessed tier.
        """
        entity_id = str(settings.get("tou_next_change_entity") or "")
        if not entity_id:
            return None, TOU_TIER_UNKNOWN
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            return None, TOU_TIER_UNKNOWN
        raw: datetime | None
        if isinstance(state.state, datetime):
            raw = state.state
        else:
            raw = dt_util.parse_datetime(str(state.state))
        if raw is None:
            return None, TOU_TIER_UNKNOWN
        if lookahead == "next_change_rate":
            if not self._thresholds_present(settings):
                return None, TOU_TIER_UNKNOWN
            next_rate = self._read_float_state(str(settings.get("tou_next_rate_entity") or ""))
            tier = self._tier_from_rate(next_rate, settings)
            if tier == TOU_TIER_UNKNOWN:
                return None, TOU_TIER_UNKNOWN
            return raw.timestamp(), tier
        tier = str(settings.get("tou_next_change_tier") or "")
        if tier not in (TOU_TIER_PEAK, TOU_TIER_MID, TOU_TIER_OFF, TOU_TIER_ULTRA_LOW):
            if lookahead == "auto" and settings.get("tou_next_rate_entity") and self._thresholds_present(settings):
                tier = self._tier_from_rate(self._read_float_state(str(settings["tou_next_rate_entity"])), settings)
                if tier != TOU_TIER_UNKNOWN:
                    return raw.timestamp(), tier
            return None, TOU_TIER_UNKNOWN
        return raw.timestamp(), tier

    def _read_float_state(self, entity_id: str) -> float | None:
        """Numeric state of any bound entity, or None (unavailable/garbage)."""
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _thresholds_present(settings: dict[str, Any]) -> bool:
        return any(
            isinstance(settings.get(key), (int, float))
            for key in ("tou_peak_threshold", "tou_mid_threshold", "tou_ultra_threshold")
        )

    @staticmethod
    def _hhmm_minutes(hhmm: str) -> int | None:
        parts = hhmm.split(":")
        if len(parts) < 2:
            return None
        try:
            hour, minute = int(parts[0]), int(parts[1])
        except ValueError:
            return None
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return hour * 60 + minute

    def _rate_window_events(
        self, settings: dict[str, Any], now: float, horizon: float
    ) -> list[tuple[float, float, str]]:
        """Materialize manual tariff-window rows into [start, end, tier] events.

        Each row applies its rate between two wall-clock times on the chosen
        weekdays (no day ticked = every day; an end not after the start wraps
        past midnight). Rates map to tiers through the cost thresholds —
        without any threshold configured the rows cannot classify and are
        ignored rather than guessed.
        """
        rows = settings.get("tou_rate_windows")
        if not isinstance(rows, list) or not rows or not self._thresholds_present(settings):
            return []
        out: list[tuple[float, float, str]] = []
        base = dt_util.as_local(dt_util.utc_from_timestamp(now)).replace(hour=0, minute=0, second=0, microsecond=0)
        for row in rows:
            if not isinstance(row, dict):
                continue
            rate = row.get("rate")
            if not isinstance(rate, (int, float)):
                continue
            tier = self._tier_from_rate(float(rate), settings)
            if tier == TOU_TIER_UNKNOWN:
                continue
            start_min = self._hhmm_minutes(str(row.get("start") or ""))
            end_min = self._hhmm_minutes(str(row.get("end") or ""))
            if start_min is None or end_min is None:
                continue
            days = {str(d) for d in row.get("days") or [] if isinstance(d, str)}
            for offset in range(-1, 3):
                day = base + timedelta(days=offset)
                if days and TOU_WEEKDAYS[day.weekday()] not in days:
                    continue
                start_dt = day.replace(hour=start_min // 60, minute=start_min % 60)
                end_dt = day.replace(hour=end_min // 60, minute=end_min % 60)
                if end_dt <= start_dt:
                    end_dt += timedelta(days=1)
                start = max(start_dt.timestamp(), now)
                end = min(end_dt.timestamp(), horizon)
                if start < end:
                    out.append((start, end, tier))
        return out

    async def _async_calendar_windows(self, entity_id: str, now: float, horizon: float) -> list[tuple[float, float]]:
        """Return [start, end] epoch windows of running/upcoming events, clamped to the horizon."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state == "unavailable":
            return []
        try:
            response = await self.hass.services.async_call(
                "calendar",
                "get_events",
                {
                    "entity_id": entity_id,
                    "start_date_time": dt_util.utc_from_timestamp(now).isoformat(),
                    "duration": {"minutes": TOU_TIMELINE_HORIZON_MINUTES},
                },
                blocking=True,
                return_response=True,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("calendar.get_events failed for %s: %r", entity_id, err)
            return []
        events: list[Any] = []
        if isinstance(response, dict):
            data = response.get(entity_id)
            if isinstance(data, dict):
                events = data.get("events") or []
        windows: list[tuple[float, float]] = []
        for ev in events:
            if not isinstance(ev, dict):
                continue
            start = self._event_ts(ev.get("start"))
            end = self._event_ts(ev.get("end"))
            if start is None or end is None or end <= now or start >= horizon:
                continue
            windows.append((max(start, now), min(end, horizon)))
        return windows

    @staticmethod
    def _event_ts(payload: Any) -> float | None:
        """Extract an epoch from a calendar event start/end payload."""
        if not isinstance(payload, dict):
            return None
        raw = payload.get("date_time") or payload.get("date")
        if not isinstance(raw, str):
            return None
        parsed = dt_util.parse_datetime(raw) or dt_util.parse_date(raw)
        if parsed is None:
            return None
        if isinstance(parsed, datetime):
            return float(parsed.timestamp())
        # All-day date: local midnight of that day.
        local = dt_util.start_of_local_day(parsed)
        return float(local.timestamp())

    @staticmethod
    def _paint_segments(
        now: float,
        horizon: float,
        current_tier: str,
        events: list[tuple[float, float, str]],
    ) -> list[TouSegment]:
        """Paint event windows over the flat current tier and merge equal neighbors."""
        points = {now, horizon}
        for start, end, _ in events:
            points.add(max(start, now))
            points.add(min(end, horizon))
        ordered = sorted(p for p in points if now <= p <= horizon)
        segments: list[TouSegment] = []
        for lo, hi in zip(ordered, ordered[1:], strict=False):
            if hi <= lo:
                continue
            mid = (lo + hi) / 2.0
            tier = current_tier
            # Events sorted by start: the most recently started covering event wins.
            for start, end, ev_tier in sorted(events, key=lambda e: e[0]):
                if start <= mid < end:
                    tier = ev_tier
            if segments and segments[-1].tier == tier and segments[-1].end == lo:
                segments[-1].end = hi
            else:
                segments.append(TouSegment(start=lo, end=hi, tier=tier))
        if not segments:
            segments = [TouSegment(start=now, end=horizon, tier=current_tier)]
        return segments

    # ------------------------------------------------------------------
    # Snapshot for entities / diagnostics
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Return the current tariff state for the panel, sensors and diagnostics."""
        timeline = self._timeline
        if timeline is None:
            return {"enabled": False, "tier": TOU_TIER_UNKNOWN, "rate": None, "rate_unit": self._rate_unit}
        next_ts, next_tier = self._next_change_from_timeline(timeline)
        return {
            "enabled": True,
            "tier": timeline.current_tier,
            "rate": timeline.current_rate,
            "rate_unit": self._rate_unit,
            "source": self._source_kind,
            "next_change": next_ts,
            "next_tier": next_tier,
            "segments": [[round(s.start, 1), round(s.end, 1), s.tier] for s in timeline.segments],
        }

    @staticmethod
    def _next_change_from_timeline(timeline: TouTimeline) -> tuple[float | None, str | None]:
        now = time.time()
        for b_ts, _, tier_after in timeline.boundaries():
            if b_ts > now:
                return b_ts, tier_after
        return None, None

    # ------------------------------------------------------------------
    # Target adjustment (the resolver wrapper)
    # ------------------------------------------------------------------

    def make_adjuster(
        self,
        room: dict[str, Any],
        settings: dict[str, Any],
        *,
        room_vacant: bool = False,
        presence_away: bool = False,
        mold_active: bool = False,
        decay: DecayFn | None = None,
    ) -> AdjustFn | None:
        """Build the per-room adjuster, or None when the room does not participate.

        The returned callable maps (timestamp, resolved targets) to TOU-adjusted
        targets and is composed into the schedule target resolver, so the whole
        MPC horizon — and the bang-bang "now" target — become tariff-aware.

        ``decay`` is the room's learned (alpha, outdoor_temp) pair when the
        thermal model is confident enough to derive "arrive" curves; None
        makes those records hold their flat target (classic coast behavior).
        """
        timeline = self._timeline
        mode = str(room.get("tou_mode", "off"))
        if timeline is None or mode == "off" or mode not in ("setback", "setback_boost"):
            return None
        if timeline.current_tier == TOU_TIER_UNKNOWN and not self._has_tiered_events(timeline):
            return None

        area_id = str(room.get("area_id", ""))
        records = self.schedules(settings) + self.row_records(room, self.rule_library(settings))
        tariff_windows = self._compile_schedules(area_id, settings, timeline, records)
        user_blocks = room.get("schedule_blocks") or []
        clamp_min = float(settings.get("tou_clamp_min", DEFAULT_TOU_CLAMP_MIN))
        clamp_max = float(settings.get("tou_clamp_max", DEFAULT_TOU_CLAMP_MAX))
        peak_floor = float(settings.get("tou_peak_heat_floor", DEFAULT_TOU_PEAK_HEAT_FLOOR))
        peak_ceiling = float(settings.get("tou_peak_cool_ceiling", DEFAULT_TOU_PEAK_COOL_CEILING))
        mid_delta = float(settings.get("tou_mid_setback_delta", DEFAULT_TOU_MID_SETBACK_DELTA))
        boost_delta = float(settings.get("tou_boost_delta", DEFAULT_TOU_BOOST_DELTA))
        boost_cap = room.get("tou_max_boost")
        boost_cap = float(boost_cap) if isinstance(boost_cap, (int, float)) else clamp_max
        boost_allowed = mode == "setback_boost"
        occupied = not room_vacant and not presence_away or bool(room.get("tou_boost_unoccupied", False))

        override_heat = room.get("override_heat")
        override_cool = room.get("override_cool")
        override_until = room.get("override_until")
        presence_clears = bool(settings.get("presence_clears_override", False))
        vacation_until = settings.get("vacation_until")
        vacation_temp = settings.get("vacation_temp")
        room_occupied = not room_vacant and not presence_away

        def adjust(ts: float, targets: TargetTemps) -> TargetTemps:
            heat, cool = targets.heat, targets.cool
            if heat is None and cool is None:
                return targets  # "off" stays off
            if (override_heat is not None or override_cool is not None) and (
                override_until is None or ts < override_until
            ):
                if not (presence_away and presence_clears):
                    return targets  # a manual override is never shaved
            vacation_now = vacation_until is not None and ts < float(vacation_until) and vacation_temp is not None
            # Unified target chain, weakest first: schedule (plain, already
            # resolved upstream) -> tier shaping -> tariff schedule records ->
            # user schedule blocks. Manual override, vacation and the mold /
            # climate-mode guards keep priority; off stays off.
            tier = timeline.tier_at(ts)
            shaped_heat, shaped_cool = heat, cool
            if heat is not None:
                if tier == TOU_TIER_PEAK and not mold_active:
                    shaped_heat = min(heat, peak_floor)
                elif tier == TOU_TIER_MID and not mold_active:
                    shaped_heat = max(heat - mid_delta, min(heat, clamp_min))
                elif tier == TOU_TIER_ULTRA_LOW and boost_allowed and not vacation_now and occupied:
                    shaped_heat = max(min(heat + boost_delta, boost_cap), heat)
            if cool is not None:
                if tier == TOU_TIER_PEAK:
                    shaped_cool = max(cool, peak_ceiling)
                elif tier == TOU_TIER_MID:
                    shaped_cool = min(cool + mid_delta, max(cool, clamp_max))
                elif tier == TOU_TIER_ULTRA_LOW and boost_allowed and not vacation_now and occupied:
                    shaped_cool = min(max(cool - boost_delta, min(cool, clamp_min)), cool)
            heat = _apply_tariff_windows(
                tariff_windows["heat"],
                ts,
                targets.heat,
                shaped_heat,  # type: ignore[arg-type]
                clamp_min,
                clamp_max,
                mold_active,
                vacation_now,
                True,
                decay,
            )
            cool = _apply_tariff_windows(
                tariff_windows["cool"],
                ts,
                targets.cool,
                shaped_cool,  # type: ignore[arg-type]
                clamp_min,
                clamp_max,
                mold_active,
                vacation_now,
                False,
                decay,
            )
            heat = _apply_user_blocks(
                user_blocks,
                "heat",
                ts,
                room_occupied,
                targets.heat,
                heat,  # type: ignore[arg-type]
                mold_active,
                vacation_now,  # type: ignore[arg-type]
            )
            cool = _apply_user_blocks(
                user_blocks,
                "cool",
                ts,
                room_occupied,
                targets.cool,
                cool,  # type: ignore[arg-type]
                mold_active,
                vacation_now,  # type: ignore[arg-type]
            )
            return TargetTemps(heat=heat, cool=cool)

        return adjust

    @staticmethod
    def _has_tiered_events(timeline: TouTimeline) -> bool:
        tiers = {seg.tier for seg in timeline.segments} - {TOU_TIER_UNKNOWN}
        return len(tiers) > 1

    @staticmethod
    def schedules(settings: dict[str, Any]) -> list[dict[str, Any]]:
        """Tariff schedule records from global settings (advanced/API authoring)."""
        records = settings.get("tou_schedules")
        if isinstance(records, list):
            return [r for r in records if isinstance(r, dict)]
        return []

    @staticmethod
    def rule_library(settings: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Shared tariff rule definitions (``settings["tou_rules"]``) keyed by id."""
        out: dict[str, dict[str, Any]] = {}
        for rule in settings.get("tou_rules") or []:
            if isinstance(rule, dict) and rule.get("id"):
                out[str(rule["id"])] = rule
        return out

    @staticmethod
    def row_records(room: dict[str, Any], rules: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        """Expand a room's schedule rows into engine records via the shared library.

        Rows are thin references ``{id, rule_id, enabled}``; the definition
        lives once in ``settings["tou_rules"]``, so editing a rule propagates
        to every room that references it — same name, same rule everywhere.
        A row with no rule (dangling reference, or the ignored pre-release
        inline format lacking a rule_id) compiles to nothing. The section
        toggle (``tou_schedule_enabled``) gates the whole schedule: a
        disabled section cannot steer the room, but its rows stay in place,
        so switching it back on brings the rules with it (temporary
        measures never lose data).
        """
        if not room.get("tou_schedule_enabled", False):
            return []
        area_id = str(room.get("area_id", ""))
        out: list[dict[str, Any]] = []
        for row in room.get("tou_overrides") or []:
            if not isinstance(row, dict) or not row.get("enabled", True):
                continue
            rule = rules.get(str(row.get("rule_id") or ""))
            if rule is None:
                continue
            out.extend(TouManager._rule_records(rule, area_id))
        return out

    @staticmethod
    def _rule_records(rule: dict[str, Any], area_id: str) -> list[dict[str, Any]]:
        """Fan one library rule out into at most one engine record per side.

        A blank side means the rule is skipped in that mode entirely, so a
        heating adjustment can never command cooling. "arrive" schedules the
        decay-derived action: the window runs from the tier boundary to the
        wall-clock arrival time, holding the flat temp until the learned
        model can derive the boundary temperature.
        """
        action = str(rule.get("action") or "")
        if action not in ("set", "raise", "lower", "arrive"):
            return []
        until = str(rule.get("until") or "tier_window")
        if action == "arrive":
            rec_until, rec_clock, rec_minutes = "clock", str(rule.get("clock") or ""), 0
            if not rec_clock:
                return []
        elif until == "minutes":
            rec_until, rec_clock, rec_minutes = "minutes", "", rule.get("minutes") or 0
        elif until == "clock":
            rec_until, rec_clock, rec_minutes = "clock", str(rule.get("clock") or ""), 0
        else:
            rec_until, rec_clock, rec_minutes = "window", "", 0
        rid = str(rule.get("id") or "r")
        out: list[dict[str, Any]] = []
        for side in ("heat", "cool"):
            temp = rule.get(side)
            if not isinstance(temp, (int, float)) or isinstance(temp, bool):
                continue  # blank side: the rule skips this mode
            if action == "set":
                value: dict[str, Any] = {"mode": "set", "temp": float(temp)}
            elif action == "arrive":
                value = {"mode": "arrive", "temp": float(temp)}
            else:
                value = {
                    "mode": "shift",
                    "direction": "raise" if action == "raise" else "lower",
                    "delta": abs(float(temp)),
                }
            out.append(
                {
                    "id": f"{rid}-{side}",
                    "name": str(rule.get("name") or ""),
                    "enabled": True,
                    "rooms": [area_id],
                    "side": side,
                    "tier": str(rule.get("tier") or "any"),
                    "from_tier": "",
                    "edge": str(rule.get("edge") or "start"),
                    "phase": "after",
                    "minutes": rec_minutes,
                    "until": rec_until,
                    "until_tier": "",
                    "clock": rec_clock,
                    "between": rule.get("between"),
                    "days": [d for d in (rule.get("days") or []) if isinstance(d, str)],
                    "guard": False,
                    "value": value,
                }
            )
        return out

    def _compile_schedules(
        self,
        area_id: str,
        settings: dict[str, Any],
        timeline: TouTimeline,
        records: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        """Expand records into per-side [start, end) windows; list order = priority."""
        cap = float(settings.get("tou_coast_max_minutes", DEFAULT_TOU_COAST_MAX_MINUTES))
        horizon_end = timeline.segments[-1].end if timeline.segments else time.time()
        out: dict[str, list[dict[str, Any]]] = {"heat": [], "cool": []}
        boundaries = timeline.boundaries()
        for rec in records:
            if not rec.get("enabled", True) or area_id not in (rec.get("rooms") or []):
                continue
            side = str(rec.get("side") or "")
            if side not in out:
                continue
            tier = str(rec.get("tier") or "any")
            from_tier = str(rec.get("from_tier") or "")
            edge = str(rec.get("edge") or "start")
            phase = str(rec.get("phase") or "after")
            days = [d for d in (rec.get("days") or []) if isinstance(d, str)]
            try:
                minutes = float(rec.get("minutes") or 0)
            except (TypeError, ValueError):
                continue
            for b_ts, b_from, b_to in boundaries:
                if tier != "any" and (b_to if edge == "start" else b_from) != tier:
                    continue
                if edge == "start" and from_tier and b_from != from_tier:
                    continue
                if days and TOU_WEEKDAYS[dt_util.as_local(dt_util.utc_from_timestamp(b_ts)).weekday()] not in days:
                    continue
                if not self._between_ok(b_ts, rec.get("between")):
                    continue

                if phase == "before":
                    if minutes <= 0:
                        continue
                    start, end = b_ts - minutes * 60.0, b_ts
                else:
                    start = b_ts
                    end = self._schedule_end(rec, b_ts, minutes, timeline, cap, horizon_end)  # type: ignore[assignment]
                    if end is None:
                        continue
                if start < horizon_end and end > start:
                    out[side].append({"rec": rec, "start": start, "end": min(end, horizon_end)})
        return out

    def _schedule_end(
        self,
        rec: dict[str, Any],
        b_ts: float,
        minutes: float,
        timeline: TouTimeline,
        cap: float,
        horizon_end: float,
    ) -> float | None:
        """End of an after-phase record: fixed minutes, window, clock, or tier."""
        until = str(rec.get("until") or "window")
        if until == "minutes":
            return b_ts + minutes * 60.0 if minutes > 0 else None
        if until in ("window", "next_change"):
            nxt = timeline.next_boundary_after(b_ts)
            return nxt if nxt is not None else horizon_end
        if until == "clock":
            return self._next_clock_ts(b_ts, str(rec.get("clock") or ""))
        if until == "next_tier_change":
            tier = str(rec.get("until_tier") or "")
            if not tier:
                return None
            nxt = timeline.next_boundary_after(b_ts, tier)
            return nxt if nxt is not None else min(b_ts + cap * 60.0, horizon_end)
        return None

    @staticmethod
    def _between_ok(b_ts: float, between: Any) -> bool:
        """True when the boundary's local time falls inside the optional range."""
        if not isinstance(between, dict):
            return True
        try:
            f_h, f_m = (int(p) for p in str(between.get("from", "")).split(":")[:2])
            t_h, t_m = (int(p) for p in str(between.get("to", "")).split(":")[:2])
        except (ValueError, TypeError):
            return True
        f, t = f_h * 60 + f_m, t_h * 60 + t_m
        if f == t or not (0 <= f <= 1439 and 0 <= t <= 1439):
            return True
        local = dt_util.as_local(dt_util.utc_from_timestamp(b_ts))
        at = local.hour * 60 + local.minute
        if f < t:
            return bool(f <= at < t)
        return bool(at >= f or at < t)  # wraps past midnight

    def preview(
        self,
        settings: dict[str, Any],
        area_id: str | None = None,
        rooms: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Tier segments + compiled tariff windows + per-record fire counts.

        *rooms* (the store's room map) adds the per-card ``tou_overrides``
        rows to the preview, mirroring what ``make_adjuster`` compiles.
        """
        timeline = self._timeline
        if timeline is None or not settings.get("tou_enabled", False):
            return None
        global_records = self.schedules(settings)
        rooms = rooms or {}
        rules = self.rule_library(settings)
        if area_id is None:
            areas = sorted(
                {str(a) for rec in global_records for a in (rec.get("rooms") or []) if str(a)}
                | {aid for aid, room in rooms.items() if isinstance(room, dict) and room.get("tou_overrides")}
            )
        else:
            areas = [area_id]
        areas_out: dict[str, Any] = {}
        fire_stats: dict[str, dict[str, Any]] = {}
        for area in areas:
            room = rooms.get(area)
            records = global_records + (self.row_records(room, rules) if isinstance(room, dict) else [])
            windows = self._compile_schedules(area, settings, timeline, records)


            rows: list[dict[str, Any]] = []
            for side in ("heat", "cool"):
                for w in windows[side]:
                    rec = w["rec"]
                    rid = str(rec.get("id", ""))
                    rows.append({"start": round(w["start"], 1), "end": round(w["end"], 1), "side": side, "id": rid})
                    stat = fire_stats.setdefault(rid, {"count": 0, "first": None})
                    stat["count"] += 1
                    if stat["first"] is None or w["start"] < stat["first"]:
                        stat["first"] = round(w["start"], 1)
            areas_out[area] = rows
        return {
            "now": round(time.time(), 1),
            "segments": [[round(s.start, 1), round(s.end, 1), s.tier] for s in timeline.segments],
            "areas": areas_out,
            "records": fire_stats,
        }

    @staticmethod
    def _next_clock_ts(after_ts: float, hhmm: str) -> float | None:
        """Next local occurrence of HH:MM strictly after *after_ts* (rolls past midnight)."""
        try:
            hour, minute = (int(p) for p in hhmm.split(":")[:2])
        except (ValueError, TypeError):
            return None
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        local = dt_util.as_local(dt_util.utc_from_timestamp(after_ts)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if local.timestamp() <= after_ts:
            local += timedelta(days=1)
        return float(local.timestamp())

    # ------------------------------------------------------------------
    # Price weighting for the MPC
    # ------------------------------------------------------------------

    def make_price_series_builder(
        self,
        room: dict[str, Any],
        settings: dict[str, Any],
    ) -> Callable[[float, int, float], list[float]] | None:
        """Return a (start_ts, n_blocks, dt_minutes) → multiplier-list builder.

        None when the room does not participate or price weighting is off,
        leaving the MPC optimizer at its flat ``w_energy`` cost.
        """
        timeline = self._timeline
        if timeline is None or not settings.get("tou_price_weighting", True):
            return None
        if str(room.get("tou_mode", "off")) == "off":
            return None

        def build(start_ts: float, n_blocks: int, dt_minutes: float) -> list[float]:
            step = dt_minutes * 60.0
            return [tou_price_multiplier(timeline.tier_at(start_ts + i * step)) for i in range(n_blocks)]

        return build
