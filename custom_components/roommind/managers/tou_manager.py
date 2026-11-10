"""Time-of-Use (TOU) tariff manager for RoomMind.

Reads the configured tariff source (rate/period/next-change sensors, tariff
calendars, or plain tier flags), builds a per-cycle tier timeline, and exposes:

* ``make_adjuster``  — a target-temperature modifier spliced into the schedule
  target resolver, applying tier setback/boost shaping and coast-rule windows.
  A coast window behaves exactly like a synthesized schedule block: the MPC
  treats it as the next scheduled target, so conditioning stops at the tier
  boundary and the learned thermal model coasts the room through the expensive
  window on stored heat (mirrored for cooling).
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
    TargetTemps,
    tou_price_multiplier,
)

_LOGGER = logging.getLogger(__name__)

AdjustFn = Callable[[float, TargetTemps], TargetTemps]


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
            next_rate = self._read_float_state(str(settings.get("tou_next_rate_entity") or ""))
            tier = self._tier_from_rate(next_rate, settings)
            if tier == TOU_TIER_UNKNOWN:
                return None, TOU_TIER_UNKNOWN
            return raw.timestamp(), tier
        tier = str(settings.get("tou_next_change_tier") or "")
        if tier not in (TOU_TIER_PEAK, TOU_TIER_MID, TOU_TIER_OFF, TOU_TIER_ULTRA_LOW):
            if lookahead == "auto" and settings.get("tou_next_rate_entity"):
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
    ) -> AdjustFn | None:
        """Build the per-room adjuster, or None when the room does not participate.

        The returned callable maps (timestamp, resolved targets) to TOU-adjusted
        targets and is composed into the schedule target resolver, so the whole
        MPC horizon — and the bang-bang "now" target — become tariff-aware.
        """
        timeline = self._timeline
        mode = str(room.get("tou_mode", "off"))
        if timeline is None or mode == "off" or mode not in ("setback", "setback_boost"):
            return None
        if timeline.current_tier == TOU_TIER_UNKNOWN and not self._has_tiered_events(timeline):
            return None

        area_id = str(room.get("area_id", ""))
        windows = self._coast_windows(area_id, settings, timeline)
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
            # A coast window is a synthesized schedule block: inside it the rule's
            # temperature REPLACES tier shaping (never raising demand, though).
            coast: float | None = None
            for w_start, w_end, w_coast in windows:
                if w_start <= ts < w_end:
                    coast = w_coast
                    break
            if coast is None:
                tier = timeline.tier_at(ts)
                if tier == TOU_TIER_PEAK:
                    if heat is not None and not mold_active:
                        heat = min(heat, peak_floor)
                    if cool is not None:
                        cool = max(cool, peak_ceiling)
                elif tier == TOU_TIER_MID:
                    if heat is not None and not mold_active:
                        heat = max(heat - mid_delta, min(heat, clamp_min))
                    if cool is not None:
                        cool = min(cool + mid_delta, max(cool, clamp_max))
                elif tier == TOU_TIER_ULTRA_LOW:
                    if boost_allowed and not vacation_now and occupied:
                        if heat is not None:
                            heat = max(min(heat + boost_delta, boost_cap), heat)
                        if cool is not None:
                            cool = min(max(cool - boost_delta, min(cool, clamp_min)), cool)
            else:
                if heat is not None and coast < heat and not mold_active:
                    heat = coast
                elif cool is not None and coast > cool:
                    cool = coast
            return TargetTemps(heat=heat, cool=cool)

        return adjust

    @staticmethod
    def _has_tiered_events(timeline: TouTimeline) -> bool:
        tiers = {seg.tier for seg in timeline.segments} - {TOU_TIER_UNKNOWN}
        return len(tiers) > 1

    def _coast_windows(
        self,
        area_id: str,
        settings: dict[str, Any],
        timeline: TouTimeline,
    ) -> list[tuple[float, float, float]]:
        """Materialize [start, end, coast_temp] windows for one room from its rules."""
        rules = settings.get("tou_coast_rules") or []
        windows: list[tuple[float, float, float]] = []
        horizon_end = timeline.segments[-1].end if timeline.segments else time.time()
        for rule in rules:
            if not isinstance(rule, dict) or not rule.get("enabled", True):
                continue
            if area_id not in (rule.get("rooms") or []):
                continue
            coast = rule.get("coast_temp")
            if not isinstance(coast, (int, float)):
                continue
            trigger = rule.get("trigger") or {}
            from_tier = str(trigger.get("from_tier", ""))
            to_tier = str(trigger.get("to_tier", ""))
            end_cfg = rule.get("end") or {}
            end_mode = str(end_cfg.get("mode", "duration"))
            end_value = end_cfg.get("value")
            post_hold = str(rule.get("post_hold") or "schedule")
            cap_minutes = float(settings.get("tou_coast_max_minutes", DEFAULT_TOU_COAST_MAX_MINUTES))
            for b_ts, b_from, b_to in timeline.boundaries():
                if (from_tier and b_from != from_tier) or (to_tier and b_to != to_tier):
                    continue
                end = self._window_end(b_ts, end_mode, end_value, timeline, cap_minutes, horizon_end)
                if end is None or end <= b_ts:
                    continue
                if post_hold == "stay":
                    nxt = timeline.next_boundary_after(end)
                    end = nxt if nxt is not None else horizon_end
                windows.append((b_ts, min(end, horizon_end), float(coast)))
                break  # one window per rule: the first matching boundary in the horizon
        windows.sort(key=lambda w: w[0])
        return windows

    def _window_end(
        self,
        boundary_ts: float,
        end_mode: str,
        end_value: Any,
        timeline: TouTimeline,
        cap_minutes: float,
        horizon_end: float,
    ) -> float | None:
        """Resolve a rule's end condition into a timestamp after the boundary."""
        if end_mode == "duration":
            try:
                minutes = float(end_value)
            except (TypeError, ValueError):
                return None
            return boundary_ts + minutes * 60.0
        if end_mode == "clock":
            return self._next_clock_ts(boundary_ts, str(end_value or ""))
        if end_mode == "tier_window":
            tier = str(end_value or "")
            nxt = timeline.next_boundary_after(boundary_ts, tier or None)
            if nxt is not None:
                return nxt
            return min(boundary_ts + cap_minutes * 60.0, horizon_end)
        return None

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
