# TOU / plugin preview (local, gitignored)

Three ways to look at the Time-of-Use integration — or the plugin in general —
without touching a real installation:

## 1. `ha-test.sh` — real HA as your user, no sudo, no sim noise

```bash
dev/sim/local/ha-test.sh up        # http://127.0.0.1:8123 (first visit: onboarding)
dev/sim/local/ha-test.sh logs -f   # watch the HA log
dev/sim/local/ha-test.sh down      # stop
dev/sim/local/ha-test.sh reset     # stop + delete all instance data
```

Runs Home Assistant **2026.10** from the sim's existing venv
(`~/.roommind-sim/venvs/ha-2026.10.0`, created by any `dev/sim` run; make one
with `dev/sim/bin/sim venv add 2026.10.0`) with a fresh config in
`~/.roommind-ha-test/` and `custom_components/roommind` symlinked from this
checkout — so code edits show up after an HA restart, and nothing from the
sim harness (virtual clock, simhome) is involved. Real onboarding, real
"Add Integration" flow, real sidebar panel.

Seeded entities in its `configuration.yaml` (delete the file or `reset` to
re-seed):

- `demo:` — a fake house: climate/weather/calendar/sensor/cover entities to
  create rooms from, so RoomMind has something to control.
- `input_number.strompreis` + `input_select.tarifperiode` + 
  `input_datetime.naechster_wechsel` — the TOU sources: bind them in
  **Settings → Time-of-Use control** (cost thresholds 0.20 / 0.12 / 0.06).
  Flip `input_select.tarifperiode` in the UI (or with
  `dev/sim/bin/sim`-style `ha.service` calls / dev-tools → Actions) and watch
  tier shaping, coast-rule windows and the `sensor.roommind_tou_*` entities
  follow. The demo `calendar.*` entities also work as calendar sources.

Version knobs: `ROOMMIND_HA_TEST_VERSION=2026.11.0` (add that venv first),
`ROOMMIND_HA_TEST_PORT`, `ROOMMIND_HA_TEST_HOST=0.0.0.0` to reach it from a
phone on the LAN.

## 2. `docker-compose.yml` — official HA container (closer to a VM)

Needs docker access once: `sudo usermod -aG docker $USER` + re-login
(or run the compose commands with sudo). Image `ghcr.io/home-assistant/home-
assistant:stable`, plugin bind-mounted read-only, same config folder as above:

```bash
docker compose -f dev/sim/local/docker-compose.yml up -d
```

If you want a literal VM (HA OS with Supervisor/HACS), the usual route is
Quickemu/VirtualBox with the HA OS qcow2 image — needs installation rights;
the two options above cover plugin testing without it.

## 3. `preview.sh` — scripted TOU scenario in the sim harness

```bash
dev/sim/local/preview.sh          # bring up instance "tou", catch up to now
# browse the printed URLs (http://127.0.0.1:<port>/roommind/settings ...)
dev/sim/local/preview.sh down     # stop and delete it
```

What you see:

- **Settings → Time-of-Use control** — enabled, cost sensor
  (`input_number.strompreis`, threshold-classified) + period sensor
  (`input_select.tarifperiode`), price-weighted planning on, two coast rules
  ("Coast through peaks" 18.5 °C / 300 min for wohnzimmer+kueche,
  "Carry the pre-charge until 07:00" for schlafzimmer).
- **Room detail → Time-of-Use** — wohnzimmer/büro on `setback + boost`,
  kueche/schlafzimmer on `setback`.
- **Global tariff sensors** (`sensor.roommind_tou_*`) with the current tier.

After catch-up the simulated clock runs in real time and sits in a fresh
**peak** window (~2 h in), so the living rooms show live coasting; tier
shaping (peak 18 °C floor) is visible in the room targets. The tariff would
have kept cycling every few hours — see the `timeline:` in
`tou-preview.yaml`.

Tweaks:

- Edit `tou-preview.yaml`, then `preview.sh down` and rerun (or
  `dev/sim/bin/sim up tou --fresh --scenario dev/sim/local/tou-preview.yaml --catch-up`).
- Watch the tariff move faster: `dev/sim/bin/sim clock tou speed 60`,
  or jump: `sim clock tou ff 6h --then realtime`.
- Push a tier change by hand:
  `sim do tou ha.service service=input_select.select_option data={entity_id:input_select.tarifperiode,option:Off-Peak}`
  (or in the HA UI: Settings → Entities).
- Screenshots: `dev/sim/bin/sim shot tou --path /roommind/settings --full`.

The frontend bundle (`custom_components/roommind/frontend/roommind-panel.js`)
is gitignored; `sim up` rebuilds it automatically when `frontend/src` is newer.

Caveat: this machine's systemd lets `sim up` start HA **as root**, which leaves
root-owned `token`/state files that later `sim status/shot/down` runs can't
read (`Permission denied`). Options: run the sim commands with `sudo -E`, or
clean up once with `sudo rm -rf ~/.roommind-sim/instances/{tou,main}` and
prefer routes 1/2 above, which stay in your own user account.
