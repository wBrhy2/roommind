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
pre-conditioning and the tariff schedule rows — with no future view, only tier
shaping of the currently active tier applies):

- **Tariff calendars** (recommended) — bind up to four `calendar.*` entities,
  one per tier. Their events give RoomMind the *entire future tariff
  timeline*: exact peak windows in both directions, which is what coasting
  and pre-conditioning plan against. Works with the built-in **Local
  Calendar** integration — no automations required. No calendar entities?
  Define **manual tariff windows** in the same branch instead: rows of
  rate + start + end + weekdays, repeating every matching day (an end
  before the start runs past midnight), with a spare `+` row to add more.
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
  falls back and the stored heat carries the house into the evening; a card
  schedule row holding to the **next period change** or to a wall-clock time
  can keep it through the peak that follows.
- Predictable patterns — "solar surplus roughly 10:00–15:00 in spring" — can
  be expressed with a calendar on the ultra-low tier, and the MPC will plan
  the soak-up around its forecast; without calendar entities, the same
  pattern fits a **manual tariff window** (a near-zero rate, those hours,
  those weekdays). The helper approach captures the actual, dynamic surplus.
  The two can coexist: calendars or windows give the future, the period/cost
  helper corrects the present.

Treat it as comfort-buffered storage: the clamps (`Lowest/Highest allowed
target`) and per-room boost caps are what stop a bright March noon from
turning the living room into a greenhouse — set them where you would still
want to sit at 17:00.

## Room participation

Each room (except outdoor rooms) has a **Time-of-Use mode**:

- `Off` — ignore tariffs (default; upgrades are unaffected).
- `Pre-cool` (`setback`) — the target eases during mid-peak and peak tiers,
  never above the schedule.
- `Pre-cool + pre-heat` (`setback_boost`) — additionally pre-charges during
  ultra-low tiers: heat rises up to **Highest pre-heat target**, cooling
  dips, so the following peak runs on stored comfort.
- `Pre-heat only` (`boost_only`) — the reverse posture: ultra-low tiers
  pre-heat exactly as `Pre-cool + pre-heat` does, but mid/peak tiers leave
  the schedule untouched — the room charges cheap and never relaxes on its
  own.

The names in brackets are the stored mode ids; only the labels were renamed.

How far a participating room actually shifts is not per-room: the shaping
defaults come from `Settings -> Time-of-Use` (fine-tune), and the card shows
the current numbers under the mode selector so you can see what the posture
will do. The only per-room shaping number is the pre-heat ceiling
(**Highest pre-heat target**).

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

## Tariff schedule (device card)

Scheduling by time of use happens on the room's device card, under its
Time-of-Use section. The section is gated by a toggle (like the outdoor-area
switch): when it is off, no rule of the schedule acts on the room — the rows
stay saved so switching it off is a safe temporary measure, never a data loss.
When it is on, each row is a temporary target override anchored on a tariff
*period* instead of the clock: pick a tier and whether the row fires when the
period **starts** or **ends**, choose an action and a heating and/or cooling
value, and say how long the hold lasts. The normal schedule and every other
rule keep controlling the room — a row only bends the target while it holds.

Rows reference **shared rules** (see below) — the card edits one named rule
and rooms pick it from the list.

For every matching change in the timeline the row computes a window:

- **Time of use**: which tier's boundary starts the row (`Any period change`
  works too), optionally restricted to weekdays and to a wall-clock range the
  change itself must fall inside.
- **Action**: `Override target temperature to`, `Increase target
  temperature temporarily by`, `Decrease target temperature temporarily by`,
  or `Reach target temperature by time` (see below).
- **Heating / Cooling**: one value per mode, each optional. A blank side
  means the row is skipped in that mode — a heating-only row leaves cooling
  entirely alone (plain tier shaping still applies there).
- **Holds**: until the next period change, for a fixed number of minutes, or
  until a wall-clock time.

Because the window is part of the MPC horizon, the optimizer decides *when* to
start charging so the room arrives at the hold target by the boundary and how
much of the expensive window it can coast through — the boost hours you once
hard-coded ("02:00 in the loft") become a consequence of the tariff timeline
and the learned room model. During a row window the schedule is *not* silently
abandoned: any schedule target on the wrong side of the row value is ignored
while the window holds, and everything else (manual override, vacation,
windows, mold, compressor protection) keeps its priority.

Cooling mirrors heating: relax the cooling target during the peak and let the
room drift up, or pre-cool in a cheap window.

### Shared rules

A rule is authored once and referenced by every room's row: definitions live
in the integration's store (the shared rule library, `settings.tou_rules`),
rooms keep thin references `{row, rule_id, enabled}`. Same name always means
the same rule:

A new rule starts as a **draft on the card** — blank name, nothing sent.
A name is mandatory: saving an unnamed rule asks whether to discard it, and
moving away without answering (tab switch, reload) discards it by default.

- **Editing a rule** from any card updates the single shared definition — the
  card shows "used in N rooms" so the blast radius is visible before you
  type. Edits are *staged* on the card and reach the server as one save
  (collapse the row, tap Save, or close the dialog), so the save is atomic:
  the name travels with the fields, and editing and *then* renaming forks a
  derivative instead of clobbering the shared rule.
- **The name field doubles as the rule picker**: picking an existing rule's
  name switches the row onto that shared rule (its configuration loads into
  the editor, the library stays untouched); typing a fresh name renames, and
  a rename to a name nobody uses forks a derivative — this room keeps the
  edited rule, the others keep the original. (The server's merge rule stays
  as the API-level invariant: a definition saved under a name that already
  belongs to another rule overwrites it and repoints every reference; the
  card avoids that case by adopting instead.)
- **The row toggle** (enabled) stays per-room — one room can park a shared
  rule without switching it off in the others.

A rule is pruned automatically when the last reference to it disappears —
deleting the row, or deleting the room. Switching a room's schedule (or its
whole Time-of-Use mode) off keeps every reference: a temporary measure never
loses rules.

### Reach target temperature by time

The decay-derived action for the classic "heat during the cheap window, arrive
comfortable at the door" case. Example: ultra-low ends at 07:00, you leave at
09:00 and the room must be at 20 °C when you go — but heating should *stop* at
07:00, not trickle through the morning. The row says `(ultra-low · ends)`
`(reach 20 °C by 09:00)` `(heating 20 °C)` and RoomMind derives the
temperature the room must already have at the boundary from your learned room
model:

    needed(t) = outdoor + (20 − outdoor) · e^(α · (09:00 − t))

with α the room's learned heat-loss rate. A room losing a degree every ten
hours against a −2 °C morning needs roughly 21.4 °C at 07:00 to coast to
exactly 20 °C by 09:00. That derived value is the *indirect* 07:00 target —
you never schedule it. The MPC replans it every cycle against the forecast,
so the pre-charge lands in the cheap window and the schedule series stays
continuous at the arrival instant. If the room decays faster than the model
predicted, small top-ups are allowed rather than arriving cold: comfort at
09:00 is the promise, zero energy in between is the best effort.

While the room's thermal model is still immature the row degrades gracefully
to holding the flat target (classic coast behavior), and the card says so.

### Advanced global records

The engine's native record format (`tou_schedules`) stays available through
the websocket API for edge cases the card does not expose (a separate lead
window before the boundary, ending on the change *into* a specific tier with
a maximum-hold fallback, or the one-way guard semantics of the old coast
rules). Coast rules themselves were removed in 1.9.0-beta.8; the feature was
beta-only, so leftover stored rules are dropped on load.


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
5. Card schedule row "Evening peak push-out": (Peak · starts) (Override target
   temperature to) heating 18 °C (cooling blank), holds until the next period
   change.
6. Card schedule row "Warm for the door": (Ultra-low · ends) (Reach target
   temperature by time) heating 20 °C, arrive by 09:00 — RoomMind derives the
   boundary temperature from the learned decay so heating stops when the
   cheap window ends and the room is exactly 20 °C at departure.
