"""Time-of-Use (TOU) tariff manager for RoomMind.

Reads the configured tariff source (rate/period/next-change sensors, tariff
calendars, or plain tier flags), builds a per-cycle tier timeline, and exposes:

* ``make_adjuster``  — a target-temperature modifier spliced into the schedule
  target resolver, applying the store layer and tariff-schedule windows. The
  store layer (``tou_store_enabled``) relaxes targets across future expensive
  stretches and sizes a pre-charge so a coasting room lands on its floor as
  the stretch ends — bounded by the consent band, the max deviation (swing)
  and gated by the price ratio that justifies it (gap). A schedule window
  behaves exactly like a synthesized schedule block: the MPC treats it as the
  next scheduled target, so conditioning stops at the tier boundary and the
  learned thermal model coasts the room through the expensive window on
  stored heat (mirrored for cooling).
  Windows come from two sources: the advanced global ``tou_schedules``
  records and the room's ``tou_overrides`` rows — thin references
  (``{id, rule_id, enabled}``) into the shared rule library
  (``settings["tou_rules"]``) that the user authors on the device card as
  (action) (anchor) (heat) (cool) (end); same name means same rule in every
  room. The per-room ``tou_schedule_enabled`` toggle gates the whole
  schedule.
* ``make_price_builder`` — per-block energy-price multipliers that scale the
  MPC optimizer's ``w_energy`` term, biasing runs into cheap tiers.
* ``snapshot`` — current tier/rate/next-change for the global energy sensors
  and diagnostics.

Safety rules encoded by the adjuster:
* a manual override at the timestamp is never shaved by tariff logic;
* "off" targets (None sides) stay off;
* the store layer never relaxes past the schedule's own eco target, never
  leaves the consent band, and skips its raise during vacation;
* heat-relaxing steps are skipped while mold prevention is active;
* every shift is bounded by the per-side band without overriding user
  targets that already sit outside it.
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
    DEFAULT_COMFORT_COOL,
    DEFAULT_COMFORT_HEAT,
    DEFAULT_ECO_COOL,
    DEFAULT_ECO_HEAT,
    DEFAULT_TOU_COAST_MAX_MINUTES,
    DEFAULT_TOU_COOL_MAX,
    DEFAULT_TOU_COOL_MIN,
    DEFAULT_TOU_HEAT_MAX,
    DEFAULT_TOU_HEAT_MIN,
    DEFAULT_TOU_STORE_GAP,
    DEFAULT_TOU_SWING,
    DOMAIN,
    TOU_ARRIVAL_EXPONENT_CAP,
    TOU_LAST_RATE_TIER_MAX_AGE_S,
    TOU_PRICE_HISTORY_SAVE_MIN_INTERVAL_S,
    TOU_PRICE_HISTORY_WINDOW_S,
    TOU_SOURCE_EXTRA,
    TOU_SOURCE_FLAGS,
    TOU_SOURCE_NONE,
    TOU_SOURCE_PERIOD,
    TOU_SOURCE_RATE,
    TOU_TIER_MID,
    TOU_TIER_OFF,
    TOU_TIER_PEAK,
    TOU_TIER_ULTRA_LOW,
    TOU_TIER_UNKNOWN,
    TOU_TIERS,
    TOU_TIMELINE_HORIZON_MINUTES,
    TOU_WEEKDAYS,
    TargetTemps,
    tier_entry,
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


def _value_candidate(value: Any, plain: float, comfort: float | None = None, eco: float | None = None) -> float | None:
    """Resolve a record/block value against the plain schedule target.

    "comfort" and "eco" name the room's own configured targets (the same
    values the schedule section edits), so a rule tracks the room instead of
    freezing a number; the caller passes the side-appropriate pair.
    """
    if not isinstance(value, dict):
        return None
    mode = str(value.get("mode") or "set")
    try:
        if mode == "shift":
            delta = abs(float(value.get("delta")))  # type: ignore[arg-type]
            if str(value.get("direction") or "raise") == "lower":
                delta = -delta
            return plain + delta
        if mode in ("comfort", "eco"):
            base = comfort if mode == "comfort" else eco
            return None if base is None else float(base)
        return float(value.get("temp"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _arrive_candidate(
    value: Any,
    window: dict[str, Any],
    ts: float,
    decay: DecayFn | None,
    is_heat: bool,
    comfort: float | None = None,
    eco: float | None = None,
) -> float | None:
    """Decay-derived floor for an ``arrive`` record at *ts*.

    The room must coast to ``temp`` exactly at the window end with
    conditioning stopped. With the learned loss rate alpha and outdoor
    temperature t_out, an idle room decays as
    ``T(t) = t_out + (T(t0) - t_out) * e^(-alpha*(t - t0))``, so the
    temperature needed right now is
    ``t_out + (temp - t_out) * e^(alpha*(t_end - ts))`` — the "indirect
    target" the user schedules, replanned every cycle and continuous at the
    arrival instant. Mirrors automatically for the cooling side. A ``to``
    of "comfort"/"eco" aims at the room's own configured target. Without a
    confident learned model (``decay`` None) — or when the outdoors would
    drift the room the wrong way (above the heating target / below the
    cooling target) — this degrades to the flat temperature, i.e. classic
    coast behavior.
    """
    if not isinstance(value, dict) or str(value.get("mode")) != "arrive":
        return None
    named = str(value.get("to") or "")
    if named in ("comfort", "eco"):
        temp = comfort if named == "comfort" else eco
    else:
        try:
            temp = float(value.get("temp"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
    if temp is None:
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
    comfort: float | None = None,
    eco: float | None = None,
) -> float | None:
    """Tariff schedule records replace tier shaping on their side (chain step 4)."""
    if plain is None:
        return None  # off stays off: no tariff record activates a side
    active = _active_window(windows, ts)
    if active is None:
        return shaped
    rec = active["rec"]
    value = rec.get("value")
    cand = _arrive_candidate(value, active, ts, decay, is_heat, comfort, eco)
    if cand is None:
        cand = _value_candidate(value, plain, comfort, eco)
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
    comfort: float | None = None,
    eco: float | None = None,
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
    cand = _value_candidate(active.get("value"), plain, comfort, eco)
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
        # Tier-dictionary observations (in-memory, rebuilt as cycles read the
        # sources): raw period states -> what they mapped to. They feed the
        # settings panel's "tiers as your source feeds them" block; a restart
        # simply clears them.
        self._observed_states: dict[str, dict[str, Any]] = {}
        # Persisted price history per tier (settings["tou_price_history"]): the
        # last valid cost, a rolling min/max window and a count. Seeded from
        # settings on the first build and written back (throttled) when values
        # change, so a provider outage cannot blind the panel — or the rate
        # tier fallback.
        self._price_history: dict[str, dict[str, Any]] = {}
        self._history_loaded = False
        self._history_dirty = False
        self._history_saved = 0.0
        # Extra tiers (generator, solar…): seconds-sustained-true counters
        # keyed by tier id, and the underlying grid reading the extras ride
        # over — reported beside the effective tier so a dropped flag resumes
        # exactly where the grid tariff left off.
        self._tier_edges: dict[str, float] = {}
        self._grid_tier: str = TOU_TIER_UNKNOWN
        self._grid_rate: float | None = None

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

        self._load_price_history(settings)
        current, rate, unit, source = self._resolve_current_tier(settings)
        # The grid tariff keeps being tracked underneath; an active extra
        # tier (generator running, solar producing) overrides it for this
        # cycle's state, price and rule anchoring — nothing about the grid
        # reading is lost when the flag drops.
        self._grid_tier, self._grid_rate = current, rate
        current, rate, unit, source = self._apply_extra_tiers(settings, current, rate, unit, source, now)
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
            next_ts, next_tier, next_rate, next_unit = self._resolve_next_view(settings, lookahead)
            if next_ts is not None and next_tier is not None and now < next_ts < horizon:
                points.add(next_ts)
                events.append((next_ts, horizon, next_tier))
            if next_tier is not None and next_rate is not None:
                # The provider's price for the upcoming period is a real cost
                # observation of that tier: keep it now, so the tier row can
                # answer "(last known)" before the period ever starts.
                self._observe_rate(next_tier, next_rate, next_unit)

        segments = self._paint_segments(now, horizon, current, events)
        timeline = TouTimeline(segments, current_tier=current, current_rate=rate)
        self._timeline = timeline
        await self._async_persist_history()
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
                tier = self._normalize_period(state.state, settings)
                self._observe_period_state(state.state, tier)
                if tier != TOU_TIER_UNKNOWN:
                    rate, unit = self._read_rate(settings)
                    if rate is None:
                        # Cost gone: keep showing/using the last valid price.
                        found = self._last_rate_for_tier(tier)
                        if found is not None:
                            rate, unit = found[0], found[1]
                    else:
                        self._observe_rate(tier, rate, unit)
                    return tier, rate, unit, TOU_SOURCE_PERIOD

        flags = settings.get("tou_flag_entities") or {}
        if isinstance(flags, dict) and mode in ("auto", "flags"):
            for tier, entity_id in flags.items():
                if not entity_id or not isinstance(entity_id, str):
                    continue
                state = self.hass.states.get(entity_id)
                if state is not None and state.state == "on":
                    rate, unit = self._read_rate(settings)
                    if rate is None:
                        found = self._last_rate_for_tier(str(tier))
                        if found is not None:
                            rate, unit = found[0], found[1]
                    else:
                        self._observe_rate(str(tier), rate, unit)
                    return str(tier), rate, unit, TOU_SOURCE_FLAGS

        rate, unit = self._read_rate(settings)
        if mode not in ("auto", "rate"):
            return TOU_TIER_UNKNOWN, rate, unit, TOU_SOURCE_NONE
        tier = self._tier_from_rate(rate, settings)
        if tier == TOU_TIER_UNKNOWN and rate is None:
            # Provider blip: keep classifying from the last valid price while
            # it is fresh; beyond the grace window the tariff stays unknown and
            # shapes nothing — a stale price must not guess a tier.
            found = self._last_known_rate(TOU_LAST_RATE_TIER_MAX_AGE_S)
            if found is not None:
                guessed = self._tier_from_rate(found[0], settings)
                if guessed != TOU_TIER_UNKNOWN:
                    return guessed, found[0], found[1], TOU_SOURCE_RATE
        if tier != TOU_TIER_UNKNOWN:
            self._observe_rate(tier, rate, unit)
            return tier, rate, unit, TOU_SOURCE_RATE
        return TOU_TIER_UNKNOWN, rate, unit, TOU_SOURCE_NONE

    @staticmethod
    def _normalize_period(state: str, settings: dict[str, Any]) -> str:
        """``normalize_period_state`` with the user's vocabulary consulted first.

        ``tou_period_aliases`` maps a lower-cased raw period state onto a tier
        and overrides the built-in word recognition — the fix lever for
        provider wording the normalizer cannot read. The settings panel
        surfaces those unreads as unmapped strings the user can assign.
        """
        key = (state or "").strip().lower()
        aliases = settings.get("tou_period_aliases")
        if isinstance(aliases, dict):
            mapped = aliases.get(key)
            if mapped in (TOU_TIER_PEAK, TOU_TIER_MID, TOU_TIER_OFF, TOU_TIER_ULTRA_LOW):
                return str(mapped)
        return normalize_period_state(state)

    def _observe_period_state(self, raw: str, tier: str) -> None:
        """Remember a period state and what it mapped to (known or not)."""
        key = (raw or "").strip().lower()
        if not key:
            return
        entry = self._observed_states.setdefault(key, {"tier": tier, "count": 0})
        entry["tier"] = tier
        entry["count"] = int(entry.get("count", 0)) + 1
        entry["ts"] = time.time()
        if len(self._observed_states) > 64:  # provider vocabularies are small; stay bounded
            oldest = min(self._observed_states.items(), key=lambda kv: kv[1].get("ts", 0.0))[0]
            self._observed_states.pop(oldest, None)

    def _observe_rate(self, tier: str, rate: float | None, unit: str) -> None:
        """Record a valid cost observation for *tier* (persisted history)."""
        if rate is None or tier in (TOU_TIER_UNKNOWN, ""):
            return
        now = time.time()
        entry = self._price_history.get(str(tier))
        if not isinstance(entry, dict) or now - float(entry.get("ts", 0.0)) > TOU_PRICE_HISTORY_WINDOW_S:
            # First observation, or the rolling window expired (seasons move
            # prices): start fresh so min/max stay representative.
            self._price_history[str(tier)] = {
                "rate": rate,
                "unit": unit,
                "ts": now,
                "count": 1,
                "min": rate,
                "max": rate,
            }
        else:
            entry["rate"] = rate
            entry["unit"] = unit or entry.get("unit", "")
            entry["ts"] = now
            entry["count"] = int(entry.get("count", 0)) + 1
            entry["min"] = min(float(entry.get("min", rate)), rate)
            entry["max"] = max(float(entry.get("max", rate)), rate)
        self._history_dirty = True

    def _load_price_history(self, settings: dict[str, Any]) -> None:
        """Seed the in-memory history from persisted settings once per process."""
        if self._history_loaded:
            return
        self._history_loaded = True
        raw = settings.get("tou_price_history")
        if not isinstance(raw, dict):
            return
        for tier in (TOU_TIER_PEAK, TOU_TIER_MID, TOU_TIER_OFF, TOU_TIER_ULTRA_LOW):
            entry = raw.get(tier)
            if isinstance(entry, dict) and isinstance(entry.get("rate"), (int, float)):
                self._price_history[tier] = dict(entry)

    async def _async_persist_history(self) -> None:
        """Write the price history back, throttled; keeps the dirty flag."""
        if not self._history_dirty:
            return
        now = time.time()
        if now - self._history_saved < TOU_PRICE_HISTORY_SAVE_MIN_INTERVAL_S:
            return
        data = getattr(self.hass, "data", None)
        if not isinstance(data, dict):  # harness without a real hass.data: nothing to persist
            return
        store = data.get(DOMAIN, {}).get("store")
        if store is None:
            return
        await store.async_save_settings({"tou_price_history": self._price_history})
        self._history_saved = now
        self._history_dirty = False

    def _last_rate_for_tier(self, tier: str, max_age: float | None = None) -> tuple[float, str, float] | None:
        """(rate, unit, ts) of the last valid cost seen in *tier*, if in age."""
        entry = self._price_history.get(tier)
        if not isinstance(entry, dict) or not isinstance(entry.get("rate"), (int, float)):
            return None
        ts = float(entry.get("ts", 0.0))
        if max_age is not None and time.time() - ts > max_age:
            return None
        return float(entry["rate"]), str(entry.get("unit", "")), ts

    def _last_known_rate(self, max_age: float | None = None) -> tuple[float, str, float] | None:
        """The freshest valid cost across all tiers (fallback when the source died)."""
        best: tuple[float, str, float] | None = None
        for tier in (TOU_TIER_PEAK, TOU_TIER_MID, TOU_TIER_OFF, TOU_TIER_ULTRA_LOW):
            found = self._last_rate_for_tier(tier, max_age)
            if found is not None and (best is None or found[2] > best[2]):
                best = found
        return best

    def tier_info(self, settings: dict[str, Any]) -> dict[str, Any]:
        """The tariff as the bound sources feed it, pulled at view time.

        Per tier: a price — preferring the live cost sensor (current tier) and
        the provider's next-period price sensor, then the persisted last-known
        value, then the manual-window rate, then the configured threshold
        cutoff (kind-marked, so a cutoff never masquerades as a reading) — and
        the raw period vocabulary that resolves to it. A ``current`` and a
        ``next`` block say what is active and what is coming, with cost,
        without waiting for cycles to observe it.
        """
        window_rate: dict[str, float] = {}
        for row in settings.get("tou_rate_windows") or []:
            if not isinstance(row, dict):
                continue
            try:
                rate = float(row.get("rate"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            window_rate.setdefault(self._tier_from_rate(rate, settings), rate)
        cutoff: dict[str, Any] = {
            TOU_TIER_PEAK: settings.get("tou_peak_threshold"),
            TOU_TIER_MID: settings.get("tou_mid_threshold"),
            TOU_TIER_ULTRA_LOW: settings.get("tou_ultra_threshold"),
        }
        # Panel can open before the first cycle ran (or right after a restart):
        # seed from persisted history so the dictionary is never needlessly blank.
        self._load_price_history(settings)
        current = self._timeline.current_tier if self._timeline else TOU_TIER_UNKNOWN
        # Pull, don't recall: read the bound entities fresh on every view.
        live_rate, live_unit = self._read_rate(settings)
        if not live_unit:
            any_last = self._last_known_rate()
            live_unit = any_last[1] if any_last else ""
        next_ts, next_tier = (
            self._next_change_from_timeline(self._timeline) if self._timeline is not None else (None, None)
        )
        next_rate: float | None = None
        next_stale = False
        next_hint = ""
        lookahead_mode = str(settings.get("tou_lookahead") or "auto")
        future_bound = bool(
            str(settings.get("tou_next_change_entity") or "")
            or str(settings.get("tou_next_period_entity") or "")
            or str(settings.get("tou_next_rate_entity") or "")
        )
        if lookahead_mode != "none":
            v_ts, v_tier, v_rate, _v_unit = self._resolve_next_view(settings, lookahead_mode)
            if next_ts is None:
                next_ts = v_ts
            if not next_tier:
                next_tier = v_tier
            if next_tier:
                next_rate = v_rate
                if next_rate is None:
                    found = self._last_rate_for_tier(str(next_tier))
                    if found is not None:
                        next_rate, next_stale = found[0], True
            if future_bound:
                if not next_tier:
                    next_hint = "no_tier"
                elif next_ts is None:
                    next_hint = "no_time"
                elif next_rate is None:
                    next_hint = "no_price"
        raw_aliases = settings.get("tou_period_aliases")
        aliases: dict[str, Any] = raw_aliases if isinstance(raw_aliases, dict) else {}
        tiers: dict[str, Any] = {}
        for tier in (TOU_TIER_PEAK, TOU_TIER_MID, TOU_TIER_OFF, TOU_TIER_ULTRA_LOW):
            hist = self._price_history.get(tier)
            rate_out: float | None = None
            kind: str | None = None
            if tier == current and live_rate is not None:
                rate_out, kind = live_rate, "live"
            elif tier == next_tier and next_rate is not None:
                rate_out, kind = next_rate, "next"
            elif isinstance(hist, dict) and isinstance(hist.get("rate"), (int, float)):
                rate_out, kind = float(hist["rate"]), "last"
            elif tier in window_rate:
                rate_out, kind = window_rate[tier], "window"
            elif isinstance(cutoff.get(tier), (int, float)):
                rate_out, kind = float(cutoff[tier]), "cutoff"
            names = sorted(state for state, v in self._observed_states.items() if v.get("tier") == tier)
            names += [str(a) for a, t in aliases.items() if t == tier and str(a) not in names]
            tiers[tier] = {
                "rate": rate_out,
                "kind": kind,
                "min": hist.get("min") if isinstance(hist, dict) else None,
                "max": hist.get("max") if isinstance(hist, dict) else None,
                "count": hist.get("count", 0) if isinstance(hist, dict) else 0,
                "ts": hist.get("ts") if isinstance(hist, dict) else None,
                "names": names[:8],
                "current": tier == current,
                "label": None,
            }
        # Extra tiers (generator, solar…): the same currency, a different
        # evidence. Their price is the user's typed cost, their vocabulary is
        # their sensor — and "current" means they are holding right now.
        for entry in settings.get("tou_extra_tiers") or []:
            if not isinstance(entry, dict):
                continue
            tid = str(entry.get("id") or "")
            if not tid or tid in tiers:
                continue
            cost = entry.get("cost")
            rate_e = float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None
            tiers[tid] = {
                "rate": rate_e,
                "kind": "source" if rate_e is not None else None,
                "min": None,
                "max": None,
                "count": 0,
                "ts": None,
                "names": [],
                "current": tid == current,
                "label": str(entry.get("name") or tid),
            }
        current_block: dict[str, Any] | None = None
        if current in tiers:
            entry_c = tier_entry(settings, current)
            cost_c = entry_c.get("cost") if entry_c else None
            if isinstance(cost_c, (int, float)) and not isinstance(cost_c, bool):
                # An extra tier holds: its typed cost IS the effective price.
                current_block = {"tier": current, "rate": float(cost_c), "stale": False}
            else:
                rate_c: float | None = live_rate
                if rate_c is None:
                    found_c = self._last_rate_for_tier(current)
                    rate_c = found_c[0] if found_c else None
                if rate_c is not None:
                    current_block = {"tier": current, "rate": rate_c, "stale": live_rate is None}
        next_block: dict[str, Any] | None = None
        if next_tier in tiers and (next_ts is not None or next_rate is not None):
            next_block = {
                "ts": round(next_ts, 1) if next_ts is not None else None,
                "tier": str(next_tier),
                "rate": next_rate,
                "stale": next_stale,
            }
        observed_unmapped: list[dict[str, int | str]] = [
            {"state": state, "count": int(v.get("count", 0))}
            for state, v in self._observed_states.items()
            if v.get("tier") == TOU_TIER_UNKNOWN
        ]
        observed_unmapped.sort(key=lambda e: -int(e["count"]))
        return {
            "unit": self._rate_unit or live_unit,
            "tiers": tiers,
            "unmapped": observed_unmapped[:16],
            "current": current_block,
            "next": next_block,
            "next_hint": next_hint,
        }

    def _read_rate(self, settings: dict[str, Any]) -> tuple[float | None, str]:
        return self._read_rate_entity(str(settings.get("tou_rate_entity") or ""))

    def _read_rate_entity(self, entity_id: str) -> tuple[float | None, str]:
        """Numeric state and unit of any bound rate-like entity, or (None, "")."""
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

    def _resolve_next_view(
        self, settings: dict[str, Any], lookahead: str
    ) -> tuple[float | None, str | None, float | None, str]:
        """The upcoming period pulled from the bound future sensors.

        Returns ``(ts, tier, rate, unit)``: the change time from
        ``tou_next_change_entity``, the price from ``tou_next_rate_entity``,
        and the tier from whichever future source can answer — the
        next-period sensor's vocabulary (provider names like ``ulo_mid_peak``
        mapped by the same recognition as the current period), the cost
        thresholds applied to the next rate, or the fixed tier dropdown, in
        the order ``tou_lookahead`` trusts them. A next-change slot fed a
        period *name* instead of a timestamp is understood as vocabulary
        rather than discarded: the tier and price still arrive, minus the
        time. No source is ever guessed past its evidence.
        """
        ts: float | None = None
        slot_tier: str | None = None
        change_id = str(settings.get("tou_next_change_entity") or "")
        if change_id:
            state = self.hass.states.get(change_id)
            if state is not None and state.state not in ("unavailable", "unknown"):
                raw: datetime | None
                if isinstance(state.state, datetime):
                    raw = state.state
                else:
                    raw = dt_util.parse_datetime(str(state.state))
                if raw is not None:
                    ts = raw.timestamp()
                else:
                    guessed = self._normalize_period(str(state.state), settings)
                    slot_tier = guessed if guessed != TOU_TIER_UNKNOWN else None

        period_tier: str | None = None
        period_id = str(settings.get("tou_next_period_entity") or "")
        if period_id:
            state = self.hass.states.get(period_id)
            if state is not None and state.state not in ("unavailable", "unknown"):
                guessed = self._normalize_period(state.state, settings)
                period_tier = guessed if guessed != TOU_TIER_UNKNOWN else None

        next_rate, next_unit = self._read_rate_entity(str(settings.get("tou_next_rate_entity") or ""))
        rate_tier: str | None = None
        if next_rate is not None and self._thresholds_present(settings):
            rate_tier = self._tier_from_rate(next_rate, settings)

        dropdown = str(settings.get("tou_next_change_tier") or "")
        if dropdown not in (TOU_TIER_PEAK, TOU_TIER_MID, TOU_TIER_OFF, TOU_TIER_ULTRA_LOW):
            dropdown = ""

        if lookahead == "next_change_rate":
            # The rate decides in this mode; a fixed dropdown must not outrank it.
            order: list[str] = [p for p in (period_tier, rate_tier, slot_tier) if p]
        else:
            order = [p for p in (period_tier, dropdown, rate_tier, slot_tier) if p]
        tier = order[0] if order else None
        return ts, tier, next_rate, next_unit

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
        rate = timeline.current_rate
        rate_stale = False
        if rate is None:
            # Keep going with the last known valid value rather than blanking
            # the sensors when the provider integration drops.
            found = self._last_known_rate()
            if found is not None:
                rate, rate_stale = found[0], True
        return {
            "enabled": True,
            "tier": timeline.current_tier,
            "rate": rate,
            "rate_stale": rate_stale,
            "rate_unit": self._rate_unit,
            "source": self._source_kind,
            # The grid tariff underneath, so a dropped extra tier resumes
            # visibly and automations can still read the utility state.
            "grid_tier": self._grid_tier,
            "grid_rate": self._grid_rate,
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

        Participation means: the store toggle is on, or the room has tariff
        rows, or it has user schedule blocks. The returned callable maps
        (timestamp, resolved targets) to TOU-adjusted targets and is composed
        into the schedule target resolver, so the whole MPC horizon — and the
        bang-bang "now" target — become tariff-aware.

        ``decay`` is the room's learned (alpha, outdoor_temp) pair when the
        thermal model is confident enough to derive coast curves; None keeps
        the store layer's pre-charge and "arrive" records at their flat
        target (classic coast behavior).
        """
        timeline = self._timeline
        if timeline is None:
            return None
        store_enabled = bool(room.get("tou_store_enabled", False))
        area_id = str(room.get("area_id", ""))
        records = self.schedules(settings) + self.row_records(room, self.rule_library(settings))
        user_blocks = self.merged_user_blocks(room, settings)
        if not store_enabled and not records and not user_blocks:
            return None
        if timeline.current_tier == TOU_TIER_UNKNOWN and not self._has_tiered_events(timeline):
            return None
        tariff_windows = self._compile_schedules(area_id, settings, timeline, records)

        def _num(value: Any, fallback: float) -> float:
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                return fallback
            try:
                return float(value)
            except ValueError:
                return fallback

        # Consent band per side plus the auto layer's reach: room overrides
        # win over the global limits, blanks inherit.
        heat_min = _num(room.get("tou_heat_min"), _num(settings.get("tou_heat_min"), DEFAULT_TOU_HEAT_MIN))
        heat_max = _num(room.get("tou_heat_max"), _num(settings.get("tou_heat_max"), DEFAULT_TOU_HEAT_MAX))
        cool_min = _num(room.get("tou_cool_min"), _num(settings.get("tou_cool_min"), DEFAULT_TOU_COOL_MIN))
        cool_max = _num(room.get("tou_cool_max"), _num(settings.get("tou_cool_max"), DEFAULT_TOU_COOL_MAX))
        swing = max(0.0, _num(room.get("tou_swing"), _num(settings.get("tou_swing"), DEFAULT_TOU_SWING)))
        gap = max(1.0, _num(room.get("tou_store_gap"), _num(settings.get("tou_store_gap"), DEFAULT_TOU_STORE_GAP)))
        eco_heat = _num(room.get("eco_heat", room.get("eco_temp")), DEFAULT_ECO_HEAT)
        eco_cool = _num(room.get("eco_cool"), DEFAULT_ECO_COOL)
        # Named-value resolution ("set to Comfort/Eco"): the room's own
        # configured targets, same sources the schedule resolver reads.
        comfort_heat = _num(room.get("comfort_heat", room.get("comfort_temp")), DEFAULT_COMFORT_HEAT)
        comfort_cool = _num(room.get("comfort_cool"), DEFAULT_COMFORT_COOL)
        store_runs = self._store_runs(timeline, gap, settings) if store_enabled else []

        override_heat = room.get("override_heat")
        override_cool = room.get("override_cool")
        override_until = room.get("override_until")
        presence_clears = bool(settings.get("presence_clears_override", False))
        vacation_until = settings.get("vacation_until")
        vacation_temp = settings.get("vacation_temp")
        room_occupied = not room_vacant and not presence_away

        def _heat_floor(plain: float) -> float:
            # Relaxation bound: swing from the schedule, never below the eco
            # target, never outside the band, never above a lower plain target.
            return min(plain, max(plain - swing, heat_min, eco_heat))

        def _cool_ceiling(plain: float) -> float:
            return max(plain, min(plain + swing, cool_max, eco_cool))

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
            # resolved upstream) -> store layer -> tariff schedule records ->
            # user schedule blocks. Manual override, vacation and the mold /
            # climate-mode guards keep priority; off stays off.
            shaped_heat, shaped_cool = heat, cool
            if store_runs:
                run_now = next((r for r in store_runs if r[0] <= ts < r[1]), None)
                run_next = None if run_now is not None else next((r for r in store_runs if r[0] > ts), None)
                if heat is not None:
                    if run_now is not None:
                        if not mold_active:
                            shaped_heat = _heat_floor(heat)
                    elif run_next is not None and decay is not None and not vacation_now:
                        # Pre-charge: the temperature a coasting room needs
                        # *now* to land on the floor as the run ends — the
                        # same back-cast the arrive rows use, bounded by the
                        # swing the room consented to.
                        floor_end = _heat_floor(heat)
                        alpha, t_out = decay
                        if t_out < floor_end:
                            exponent = min(alpha * (run_next[1] - ts) / 3600.0, TOU_ARRIVAL_EXPONENT_CAP)
                            needed = t_out + (floor_end - t_out) * math.exp(exponent)
                            shaped_heat = min(max(needed, heat), heat + swing, heat_max)
                if cool is not None:
                    if run_now is not None:
                        shaped_cool = _cool_ceiling(cool)
                    elif run_next is not None and decay is not None and not vacation_now:
                        ceil_end = _cool_ceiling(cool)
                        alpha, t_out = decay
                        if t_out > ceil_end:
                            exponent = min(alpha * (run_next[1] - ts) / 3600.0, TOU_ARRIVAL_EXPONENT_CAP)
                            needed = t_out + (ceil_end - t_out) * math.exp(exponent)
                            shaped_cool = max(min(needed, cool), cool - swing, cool_min)
            heat = _apply_tariff_windows(
                tariff_windows["heat"],
                ts,
                targets.heat,
                shaped_heat,  # type: ignore[arg-type]
                heat_min,
                heat_max,
                mold_active,
                vacation_now,
                True,
                decay,
                comfort_heat,
                eco_heat,
            )
            cool = _apply_tariff_windows(
                tariff_windows["cool"],
                ts,
                targets.cool,
                shaped_cool,  # type: ignore[arg-type]
                cool_min,
                cool_max,
                mold_active,
                vacation_now,
                False,
                decay,
                comfort_cool,
                eco_cool,
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
                comfort_heat,
                eco_heat,
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
                comfort_cool,
                eco_cool,
            )
            return TargetTemps(heat=heat, cool=cool)

        return adjust

    @staticmethod
    def _store_runs(
        timeline: TouTimeline, gap: float, settings: dict[str, Any] | None = None
    ) -> list[tuple[float, float]]:
        """Merge future stretches worth arbitraging across the timeline.

        A segment is expensive when it costs above the off-peak reference
        (multiplier 1.0), a cheaper window exists to arbitrage against, and
        the ratio clears ``gap``. The reference is the cheapest other segment;
        with a single-tier horizon (no future view) it falls back to 1.0, so
        the active expensive tier still relaxes — shaping without lookahead —
        while nothing gets pre-charged (there is no later boundary to aim at).
        Contiguous expensive segments merge into one run: the store layer
        relaxes targets across the whole run and sizes a pre-charge so a
        coasting room lands on its floor as the run ends.
        """
        segs = timeline.segments
        mults = [tou_price_multiplier(s.tier, settings) for s in segs]
        runs: list[tuple[float, float]] = []
        for i, seg in enumerate(segs):
            m = mults[i]
            others = mults[:i] + mults[i + 1 :]
            reference = min(others) if others else 1.0
            cheaper_exists = reference < m if others else m > 1.0
            if m > 1.0 and cheaper_exists and m >= gap * reference:
                if runs and abs(runs[-1][1] - seg.start) < 1.0:
                    runs[-1] = (runs[-1][0], max(runs[-1][1], seg.end))
                else:
                    runs.append((seg.start, seg.end))
        return runs

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

    # ------------------------------------------------------------------
    # Extra tiers: on-site energy sources as first-class tariff states
    # ------------------------------------------------------------------

    def _extra_tier_active(self, entry: dict[str, Any], now: float) -> bool:
        """Is this extra tier's sensor evidence holding right now?

        ``for`` debounces by seconds of sustained truth, counted from when the
        manager last saw it flip — a generator reaching steady speed, a relay
        chattering at a price boundary, or an inverter hovering at its standby
        threshold must not strobe the tariff (the Victron dead-band lesson:
        a flapping state is useless as an indicator, and here it would also
        anchor phantom rule windows).
        """
        entity_id = str(entry.get("entity_id") or "")
        if not entity_id:
            return False
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            self._tier_edges.pop(str(entry.get("id") or ""), None)
            return False
        kind = str(entry.get("kind") or "binary_on")
        raw_active = False
        if kind == "state":
            wanted = {str(s).strip().lower() for s in (entry.get("states") or []) if str(s).strip()}
            raw_active = bool(wanted) and str(state.state).strip().lower() in wanted
        elif kind in ("numeric_above", "numeric_below"):
            try:
                value = float(state.state)
            except (TypeError, ValueError):
                self._tier_edges.pop(str(entry.get("id") or ""), None)
                return False
            threshold = entry.get("threshold")
            if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
                return False
            raw_active = value > float(threshold) if kind == "numeric_above" else value < float(threshold)
        else:  # binary_on
            raw_active = state.state in ("on", "true", "1")
        return self._debounce(str(entry.get("id") or ""), raw_active, float(entry.get("for_seconds") or 0.0), now)

    def _debounce(self, tier: str, raw_active: bool, hold: float, now: float) -> bool:
        """Smooth a raw truth into a sustained one: on after *hold* seconds
        of continuous truth; off is instant (stop generating means stop)."""
        if not raw_active:
            self._tier_edges.pop(tier, None)
            return False
        since = self._tier_edges.get(tier)
        if since is None:
            self._tier_edges[tier] = now
            since = now
        return hold <= 0.0 or now - since >= hold

    def _apply_extra_tiers(
        self, settings: dict[str, Any], grid_tier: str, rate: float | None, unit: str, source: str, now: float
    ) -> tuple[str, float | None, str, str]:
        """Let a holding extra tier override the grid tier.

        Returns ``(tier, rate, unit, source)``: while an extra tier holds, the
        tariff state and price are the entry's (its cost is the price the room
        should optimize against); the grid tier and rate are still reported
        beside them (snapshot's ``grid_tier``/``grid_rate``) and the grid
        history keeps recording — a dropped generator flag must resume exactly
        where the grid tariff left off. An extra tier without a cost is state
        without economics: the grid rate stays the effective price.
        """
        for entry in settings.get("tou_extra_tiers") or []:
            if not isinstance(entry, dict) or not entry.get("enabled", True):
                continue
            tier = str(entry.get("id") or "")
            if not tier or tier in TOU_TIERS or tier == TOU_TIER_UNKNOWN:
                continue
            if not self._extra_tier_active(entry, now):
                continue
            cost = entry.get("cost")
            if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                entry_unit = str(entry.get("unit") or "") or unit
                return tier, float(cost), entry_unit, TOU_SOURCE_EXTRA
            return tier, rate, unit, TOU_SOURCE_EXTRA
        return grid_tier, rate, unit, source

    @staticmethod
    def merged_user_blocks(room: dict[str, Any], settings: dict[str, Any]) -> list[dict[str, Any]]:
        """The room's schedule blocks with bound base schedules expanded underneath.

        Blocks from each base schedule the room opted into (``base_schedules``
        references into ``settings["schedules_lib"]``, in listed order), then
        the room's own ``schedule_blocks`` — the resolver lets the later row
        win on overlap, so a house/floor schedule steers every binder while
        the room keeps the last word. Disabled references and dangling ids
        compile to nothing.
        """
        out: list[dict[str, Any]] = []
        library = {
            str(s.get("id")): s for s in (settings.get("schedules_lib") or []) if isinstance(s, dict) and s.get("id")
        }
        for ref in room.get("base_schedules") or []:
            if not isinstance(ref, dict) or not ref.get("enabled", True):
                continue
            entry = library.get(str(ref.get("id") or ""))
            if entry is None:
                continue
            out.extend(b for b in (entry.get("blocks") or []) if isinstance(b, dict))
        out.extend(b for b in (room.get("schedule_blocks") or []) if isinstance(b, dict))
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
        decay-derived action in two modes: by a wall-clock time (window runs
        from the charge period's end to the clock) or coasting through the
        anchored period (window = the period itself, arrival at its end).
        Both hold the flat temp until the learned model can derive the
        boundary temperature.
        """
        action = str(rule.get("action") or "")
        if action not in ("set", "raise", "lower", "arrive"):
            return []
        until = str(rule.get("until") or "tier_window")
        edge = str(rule.get("edge") or "start")
        if action == "arrive":
            # Two arrival modes share the decay curve, keyed by whether an
            # arrival clock exists: "reach by HH:MM" coasts from the charge
            # period's end to that time (anchor = the period ending — heating
            # stops at the boundary and stored heat carries the room), while a
            # period-end arrival rides the whole anchored period and lands at
            # the next change. A fixed duration is meaningless for an arrival
            # promise, and an empty "clock" end has nothing to reach.
            rec_clock = str(rule.get("clock") or "")
            if rec_clock:
                rec_until, rec_minutes = "clock", 0
                edge = "end"
            elif until == "tier_window":
                rec_until, rec_clock, rec_minutes = "window", "", 0
                edge = "start"
            else:
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
            named = temp if isinstance(temp, str) and temp in ("comfort", "eco") else None
            if named is not None:
                # A named target tracks the room's own configured temperature
                # (resolved at apply time, so settings changes heal next cycle).
                if action == "set":
                    value: dict[str, Any] = {"mode": named}
                elif action == "arrive":
                    value = {"mode": "arrive", "to": named}
                else:
                    continue  # raise/lower are deltas; a named value cannot express one
            elif not isinstance(temp, (int, float)) or isinstance(temp, bool):
                continue  # blank side: the rule skips this mode
            elif action == "set":
                value = {"mode": "set", "temp": float(temp)}
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
                    "edge": edge,
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
        # A period already holding when the horizon opens has no start
        # transition in the segments — yet "Peak · starts" describes being
        # *in* peak, not only watching it begin. Seed an in-progress anchor at
        # the horizon edge so rules fire while their period is active — and so
        # sensor-driven extras (a generator that is already running) work at
        # all: their "starts" moment is exactly the moment they are observed.
        # The from-tier of an in-progress anchor is unknown, so it equals the
        # tier and only "starts" matching may read it.
        first = timeline.segments[0] if timeline.segments else None
        if first is not None and first.tier != TOU_TIER_UNKNOWN:
            boundaries = [(first.start, first.tier, first.tier), *boundaries]
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
                if b_from == b_to and edge == "end":
                    continue  # in-progress seed: a period has not ended at "now"
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
        if not room.get("tou_store_enabled", False) and not (
            self.schedules(settings) or self.row_records(room, self.rule_library(settings))
        ):
            return None

        def build(start_ts: float, n_blocks: int, dt_minutes: float) -> list[float]:
            step = dt_minutes * 60.0
            return [tou_price_multiplier(timeline.tier_at(start_ts + i * step), settings) for i in range(n_blocks)]

        return build
