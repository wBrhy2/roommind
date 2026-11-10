"""Residual heat transition tracking for RoomMind."""

from __future__ import annotations

import time

from ..const import MODE_COOLING, MODE_HEATING
from ..control.residual_heat import advance_rebound, compute_residual_heat


class ResidualHeatTracker:
    """Tracks heating on/off transitions and computes residual heat."""

    def __init__(self) -> None:
        self._off_since: dict[str, float] = {}
        self._off_power: dict[str, float] = {}
        self._on_since: dict[str, float] = {}

    def get_q_residual(self, area_id: str, system_type: str, previous_mode: str) -> float:
        """Compute residual heat from previous cycle state."""
        if not system_type or area_id not in self._off_since or previous_mode == MODE_HEATING:
            return 0.0
        elapsed = (time.time() - self._off_since[area_id]) / 60.0
        heat_dur = (self._off_since[area_id] - self._on_since.get(area_id, self._off_since[area_id])) / 60.0
        last_pf = self._off_power.get(area_id, 1.0)
        return compute_residual_heat(elapsed, system_type, last_pf, heat_dur)

    def update(
        self, area_id: str, mode: str, power_fraction: float, previous_mode: str, q_residual: float = 0.0
    ) -> None:
        """Update heating transition state based on current mode."""
        if mode == MODE_HEATING:
            self._off_since.pop(area_id, None)
            self._off_power[area_id] = power_fraction
            if previous_mode != MODE_HEATING:
                self._on_since[area_id] = time.time()
        elif previous_mode == MODE_HEATING:
            self._off_since[area_id] = time.time()
        elif q_residual == 0.0 and area_id in self._off_since:
            self._off_since.pop(area_id, None)
            self._off_power.pop(area_id, None)
            self._on_since.pop(area_id, None)

    def remove_room(self, area_id: str) -> None:
        """Clean up state for a removed room."""
        self._off_since.pop(area_id, None)
        self._off_power.pop(area_id, None)
        self._on_since.pop(area_id, None)

    def clear_room(self, area_id: str) -> None:
        """Clear state for a room (thermal reset)."""
        self.remove_room(area_id)

    def clear_all(self) -> None:
        """Clear state for all rooms."""
        self._off_since.clear()
        self._off_power.clear()
        self._on_since.clear()


class AcReboundTracker:
    """Tracks the AC rebound charge per room (air pulled below the building mass).

    Driven by the mode the EKF trains with, so the charge always matches the
    labels the model learns from.
    """

    def __init__(self) -> None:
        self._charge: dict[str, float] = {}
        self._cooling: dict[str, bool] = {}
        self._at: dict[str, float] = {}

    def get_charge(self, area_id: str) -> float:
        """Current charge, advanced to now with the last recorded mode."""
        if area_id not in self._at:
            return 0.0
        elapsed = (time.time() - self._at[area_id]) / 60.0
        return advance_rebound(self._charge[area_id], 1.0 if self._cooling[area_id] else 0.0, elapsed)

    def update(self, area_id: str, mode: str | None) -> None:
        """Record the mode from now on (None: unknown, treated as not cooling)."""
        charge = self.get_charge(area_id)
        cooling = mode == MODE_COOLING
        if charge <= 0.0 and not cooling:
            self.remove_room(area_id)
            return
        self._charge[area_id] = charge
        self._cooling[area_id] = cooling
        self._at[area_id] = time.time()

    def seed_running(self, area_id: str) -> None:
        """Start fully charged: a cooling run already in progress (after a restart)."""
        self._charge[area_id] = 1.0
        self._cooling[area_id] = True
        self._at[area_id] = time.time()

    def remove_room(self, area_id: str) -> None:
        """Clean up state for a removed room."""
        self._charge.pop(area_id, None)
        self._cooling.pop(area_id, None)
        self._at.pop(area_id, None)

    def clear_all(self) -> None:
        """Clear state for all rooms."""
        self._charge.clear()
        self._cooling.clear()
        self._at.clear()
