# Time-of-Use Control & Pre-conditioning

RoomMind can link to an electricity tariff source and shape each room's target
temperature by time of use: easing demand while energy is expensive and
pre-charging the room while it is ultra-cheap, so the expensive window is
coasted on stored heat instead of bought energy.

## Setting up a tariff source

Open `Settings -> Time-of-Use control` and enable the feature, then bind at
least one source. RoomMind reads whatever you link and auto-detects the kind:

- **Cost sensor** — a numeric price entity (e.g.
  `sensor.hydro_one_networks_inc_ar_residential_electricity_rate`, Tibber,
  Nord Pool, an `input_number`). Give it **Cost thresholds** (peak from,
  mid-peak from, ultra-low up to) so rates map onto tiers.
- **Period sensor** — an entity whose state names the active tier. Words like
  `on-peak`, `mid-peak`, `off-peak`, `overnight`/`ultra`/`super-off` are
  recognized (Hydro One's `…_active_peak` states such as `ulo_on_peak` map
  automatically).
- **Next change sensor** — a timestamp entity (e.g.
  `…_next_peak_starts`) marking the next tier boundary; pair it with the
  **Tier after the next change** selector.
- **Tariff calendars** — bind up to four `calendar.*` entities, one per tier.
  Their events give RoomMind the *entire future tariff timeline*: exact peak
  windows in both directions, which is what coasting and pre-conditioning
  plans against. This is the richest source and works with the built-in
  **Local Calendar** integration — no automations required.
- **Period flags** — fallback for setups where automations toggle one
  `input_boolean`/`binary_sensor` per tier. Flags carry no future information,
  so calendar or next-change bindings are still needed for coasting to see
  past the current tier.

When nothing resolves (sensor deleted, unavailable, no thresholds), the tier is
unknown and RoomMind behaves exactly as before: no shaping, no coasting.

Manual overrides, vacation and all safety limits keep priority: a boost you
started is never shaved by a tariff, and vacation setback is never boosted.

## Room participation

Each room (except outdoor rooms) has a **Time-of-Use mode**:

- `Off` — ignore tariffs (default; upgrades are unaffected).
- `Setback only` — the target eases during mid-peak and peak tiers, never
  above the schedule.
- `Setback + boost` — additionally pre-charges during ultra-low tiers: heat
  rises up to **Highest pre-charge target**, cooling dips, so the following
  peak runs on stored comfort.

`Eco when the room is empty` and away-presence eco still cap the result —
unless you tick **Boost even when nobody is here**, which lets an ultra-low
pre-charge warm an empty room (your loft scenario: charge at 02:00 so the
07:00 peak drifts down through comfort while you are still there).

Tier shaping defaults (peak heating/cooling limits, mid setback, boost step,
clamp band) live in the settings and match the familiar Ontario numbers
directly: peak 18 °C heating floor / 28 °C cooling ceiling, mid ±2 °C.

## Price-weighted planning

With a tariff timeline, the MPC weights its energy term by the tier price of
every future block. In practice: a run that would burn energy inside the peak
costs more, so the optimizer starts it during the cheap window before the
boundary and lets the learned room model coast through the start of the peak.
Turn it off if you only want the plain tier setback/boost.

## Coast rules

A coast rule turns a tier *change* into a scheduled temperature event, the
same way your own automations did: when the tariff flips (say ultra-low to
peak), the room stops conditioning and drifts to the **coast temperature**,
holding it for as long as the rule says, then either returns to the schedule
or keeps holding.

For every matching change in the timeline the rule computes a window:

- **Trigger**: which change starts it (`from tier → to tier`, either side may
  stay "Any tier"), across the rooms you tick.
- **Coast ends after**: a fixed duration (change + X minutes), at a wall-clock
  time (e.g. `07:30` — the departure time the comfort must last until), or at
  the next change into a chosen tier (the cheap window that ends the peak;
  with a fallback to the **Maximum coast duration** if that tier never arrives
  in the visible horizon).
- **When the hold ends**: `Return to schedule` hands the target back (the MPC
  then treats the rise like any scheduled block and pre-heats toward it), or
  `Keep holding` holds the coast temperature until the next tier change.

Because the window is part of the MPC horizon, the optimizer decides *when* to
start charging so the room arrives warm at the coast boundary and how much of
the peak it can coast through — the boost hours you once hard-coded
("02:00 in the loft") become a consequence of the tariff timeline and the
learned room model. During a coast window the schedule is *not* silently
abandoned: any schedule target above the coast temperature is ignored while
the window holds, and everything else (override, vacation, windows, mold,
compressor protection) keeps its priority.

Cooling coasts the mirrored way: pre-cool in a cheap window, stop at the
boundary, and let the room drift up through the peak.

## Reading it back

Three global entities reflect the tariff: `sensor.roommind_energy_rate`
(current cost from the source), `sensor.roommind_energy_period`
(`peak`/`mid`/`off`/`ultra_low`) and `sensor.roommind_energy_next_change`
(next change, with the tier as attribute). Diagnostics exports the current
tier timeline; the panel shows the active tier.

## Example: Hydro One ULO with local calendars

1. Install the Hydro One integration; bind `…_electricity_rate` as the cost
   sensor and `…_active_peak` as the period sensor.
2. Create four Local Calendars (`ULTOU-Peak`, `ULTOU-Mid`, `ULTOU-Low`,
   `ULTOU-UL`) and bind them as tariff calendars with the matching tier.
   (The calendars are the schedule of record; RoomMind reads the events —
   no `Power-*` automations or `input_boolean` flags needed.)
3. Settings: peak floor 18 °C, peak ceiling 28 °C, mid ±2 °C, boost +2 °C,
   clamp 17–28 °C, price weighting on.
4. Rooms: `Setback + boost` for the loft/boys/dining rooms that should
   pre-charge overnight; `Setback only` where comfort during peaks matters.
5. Coast rule "Evening peak push-out": trigger off→peak, coast 18 °C, end
   `tier_window → off` (cap 480 min), post-hold `Return to schedule`.
