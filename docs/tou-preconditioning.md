# Time-of-Use Control & Pre-conditioning

RoomMind can link to an electricity tariff source and shape each room's target
temperature by time of use: easing demand while energy is expensive and
pre-charging the room while it is ultra-cheap, so the expensive window is
coasted on stored heat instead of bought energy.

## Setting up a tariff source

Open `Settings -> Time-of-Use control` and enable the feature. The panel then
asks two questions, one source per answer — anything you bind but do not
select is kept in settings, just ignored:

**1. Where the current tier comes from:**

- **Cost sensor** — pick this when you have a numeric price but no period
  text (Tibber, Nord Pool, spot rates). A numeric price entity (e.g.
  `sensor.hydro_one_networks_inc_ar_residential_electricity_rate` or an
  `input_number`) plus **Cost thresholds** (peak from, mid-peak from,
  ultra-low up to) map rates onto tiers.
- **Period sensor** — pick this when an integration names the active tier.
  Words like `on-peak`, `mid-peak`, `off-peak`, `overnight`/`ultra`/`super-off`
  are recognized (Hydro One's `…_active_peak` states such as `ulo_on_peak`
  map automatically).
- **Period flags** — fallback for setups where automations toggle one
  `input_boolean`/`binary_sensor` per tier.

**2. How far RoomMind can see into the future** (this is what enables
pre-conditioning and coast rules — with no future view, only tier shaping of
the currently active tier applies):

- **Tariff calendars** (recommended) — bind up to four `calendar.*` entities,
  one per tier. Their events give RoomMind the *entire future tariff
  timeline*: exact peak windows in both directions, which is what coasting
  and pre-conditioning plan against. Works with the built-in **Local
  Calendar** integration — no automations required.
- **Next-change rate sensor** (default) — some providers publish not only
  *when* the next change starts but *what the price will be* after it (a
  "next rate" entity). Bind it alongside the next-change timestamp and
  RoomMind classifies that price with the same cost thresholds to learn the
  incoming tier — no fixed "leads into" guess needed. If the reading is
  unavailable or the thresholds are missing, the lookahead simply stays off.
- **Next change sensor** — for integrations that only publish a timestamp
  for the next tier boundary (e.g. Hydro One's `…_next_peak_starts`).
  RoomMind sees *one step* ahead: the sensor gives the when, the **Tier
  after the next change** selector gives the what — the change leads into
  that tier, and everything after it stays there. That combination is
  exactly right for a "next *peak* starts" sensor (pick Peak), which always
  changes into peak; pick **Not set** to disable the lookahead again. A
  generic "next change" timestamp that alternates between tiers cannot be
  pinned this way — use calendars instead.
- **No future view** — shaping only.

When nothing resolves (sensor deleted, unavailable, no thresholds), the tier
is unknown and RoomMind behaves exactly as before: no shaping, no coasting.

Manual overrides, vacation and all safety limits keep priority: a boost you
started is never shaved by a tariff, and vacation setback is never boosted.

### Helpers as an escape hatch

If your tariff source is unreliable — the integration's entity goes
`unavailable`, uses wording RoomMind cannot map, or a calendar misbehaves —
bind Home Assistant **helpers** instead and let an automation keep them
current:

- `input_number` for the cost sensor, `input_select` or a text-state sensor
  for the period, `input_boolean`/`binary_sensor` for flags, and
  `input_datetime` (**Date & time** type) for the next-change timestamp.
  RoomMind reads helpers exactly like real sensors; an unparseable or stale
  value simply degrades to "no shaping / no lookahead", never to wrong
  behaviour.
- **Microgrid / multi-fuel setups**: the tiers are only labels for *expensive
  now* versus *cheap later*. If your effective energy cost follows something
  other than a utility timetable — the genset just started, the battery is
  full, you are islanded with wind blowing — an automation that watches the
  dispatch situation can publish the effective tier, price and expected
  window end into helpers, and RoomMind will shape, pre-charge and coast
  against that as if it were the utility tariff. The "Tier after the next
  change" selector then simply states which dispatch state your automation
  flips to next. Pre-condition quality depends on your automation's
  boundary predictions being right; nothing else in the chain changes.

### Storing excess solar as thermal mass

The same machinery is a neat fit for one case that has nothing to do with a
utility timetable: **free energy you would otherwise export or curtail**.
Midday photovoltaic surplus is exactly what the `ultra_low` tier means to
RoomMind — *cheap now, use it* — so an automation that watches your
inverter/meter can publish the surplus state instead of a tariff:

- Write an ultra-low price (or select the ultra-low period) into the cost or
  period helper whenever production exceeds consumption, and the normal
  tariff state back when it does not. Bind that helper as your tariff source.
- Rooms on `setback + boost` respond as they would to an ultra-cheap window:
  the MPC raises heating targets up to each room's **Highest pre-charge
  target** and runs the heat pump early, so the building's mass — walls,
  screed, furniture — soaks the surplus up as heat. Cooling works mirrored:
  a surplus window may pre-chill instead, storing coolth for the afternoon.
- Tick **Boost even when nobody is here** on rooms you want charging while
  empty — the whole point is that the sun shines whether or not you are in
  the room.
- When the surplus ends (clouds, battery full, feed-in capped), the tier
  falls back and the stored heat carries the house into the evening; a coast
  rule ending **Any tier change** or at a wall-clock time can hold it
  through the peak that follows.
- Predictable patterns — "solar surplus roughly 10:00–15:00 in spring" — can
  be expressed with a calendar on the ultra-low tier, and the MPC will plan
  the soak-up around its forecast; the helper approach captures the actual,
  dynamic surplus. The two can coexist: calendars give the future, the
  period/cost helper corrects the present.

Treat it as comfort-buffered storage: the clamps (`Lowest/Highest allowed
target`) and per-room boost caps are what stop a bright March noon from
turning the living room into a greenhouse — set them where you would still
want to sit at 17:00.

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
directly: peak 18 °C heating floor / 28 °C cooling ceiling, mid ±2 °C. These
are bounds on the room's **indoor target temperature** — during a peak the
heating setpoint never rises above 18 °C and the cooling setpoint never drops
below 28 °C. They are not device-output limits, and they are separate from
the global *Maximum outdoor temp for heating* / *Minimum outdoor temp for
cooling* gates under Control, which block conditioning by outdoor weather
regardless of tier.

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
  time (e.g. `07:30` — the departure time the comfort must last until), at
  the next change into a chosen tier (the cheap window that ends the peak;
  with a fallback to the **Maximum coast duration** if that tier never arrives
  in the visible horizon), or at **Any tier change** — the next boundary no
  matter which tier it leads into, which suits tariffs whose future beyond
  one step is unpredictable.
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
