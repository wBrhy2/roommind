/**
 * Core type definitions for RoomMind frontend.
 */

export type ClimateMode = "auto" | "heat_only" | "cool_only";

export type RoomMode = "idle" | "heating" | "cooling";

export type OverrideType = "boost" | "eco" | "custom";

export interface NotificationTarget {
  entity_id: string;
  person_entity: string;
  notify_when: "always" | "home_only";
}

export interface ScheduleEntry {
  entity_id: string;
}

export interface CoverScheduleEntry {
  entity_id: string;
  mode?: "force" | "gate";
}

export interface RoomLiveData {
  current_temp: number | null;
  current_humidity: number | null;
  target_temp: number | null;
  heat_target: number | null;
  cool_target: number | null;
  mode: RoomMode;
  heating_power: number; // 0-100
  device_setpoint: number | null; // Device target temp in Full Control mode
  override_active: boolean;
  override_type: OverrideType | null;
  override_heat: number | null;
  override_cool: number | null;
  override_until: number | null;
  override_suppressed: boolean;
  active_schedule_index: number;
  window_open: boolean;
  confidence: number | null;
  mpc_active: boolean;
  presence_away: boolean;
  occupancy_vacant?: boolean;
  mold_risk_level: "ok" | "warning" | "critical";
  mold_surface_rh: number | null;
  mold_surface_temp: number | null;
  mold_prevention_active: boolean;
  mold_prevention_delta: number;
  blind_position: number | null;
  cover_auto_paused: boolean;
  cover_override_until?: number | null;
  cover_forced_reason: string;
  cover_reason?: string;
  active_cover_schedule_index: number;
  active_heat_sources: string | null;
  learning_paused_reason: "outdoor_unavailable" | null;
  compressor_protection_active: boolean;
  compressor_protection_reason: "min_off" | "min_run" | null;
  compressor_protection_until: number | null;
  coil_dry_active: boolean;
  coil_dry_phase: "drain" | "blow" | null;
  coil_dry_until: number | null;
  coil_dry_entities: string[];
  schedule_temp_warnings: ScheduleTempWarning[];
}

/** A schedule block temperature the backend discarded as implausible (#395). */
export interface ScheduleTempWarning {
  day: string; // lowercase English weekday, e.g. "monday"
  from: string; // "HH:MM:SS"
  field: "temperature" | "heat_temperature" | "cool_temperature";
  value: string; // the offending raw value; may not be numeric
}

export type DeviceType = "trv" | "ac";
export type DeviceRole = "primary" | "secondary" | "auto";

export interface DeviceConfig {
  entity_id: string;
  type: DeviceType;
  role: DeviceRole;
  heating_system_type?: string;
  idle_action?: "off" | "fan_only" | "setback" | "low"; // default "off"
  idle_fan_mode?: string; // default "low"
  setback_auto_off?: boolean; // setback ends in off after an hour without demand, default off (#255)
  setpoint_mode?: "proportional" | "direct"; // default "proportional"
  setpoint_offset?: number; // °C added to the room target in direct mode, default 0
  coil_dry?: "inherit" | "on" | "off"; // default "inherit"
  coil_dry_minutes?: number; // 0 = inherit global
  coil_dry_mode?: "" | "fan_only" | "dry"; // "" = inherit global
  coil_dry_fan_mode?: string; // "" = inherit global, "__keep__" = keep current
  valve_opening_entity?: string; // number entity for direct valve opening degree (0-100%)
  valve_closing_entity?: string; // number entity for direct valve closing degree (0-100%)
  valve_closing_mode?: "closing_degree" | "idle_opening"; // idle_opening: the second entity is the opening while idle (SONOFF TRV-ZBT)
}

export type ConflictResolution =
  "heating_priority" | "cooling_priority" | "majority" | "outdoor_temp";

/** Normalized tariff tier used by the Time-of-Use module. */
export type TouTier = "peak" | "mid" | "off" | "ultra_low";

/** Global store-layer limits (Settings -> Time-of-Use), shipped to the room
 * cards so the store toggle can show the band and swing the room's economy
 * will actually work in. °C internally like every other temperature in the
 * store. */
export interface TouLimits {
  heat_min: number;
  heat_max: number;
  cool_min: number;
  cool_max: number;
  swing: number;
  store_gap: number;
}

/** Per-room overrides of the global limits; null/undefined = inherit. */
export type TouRoomLimits = Partial<Record<keyof TouLimits, number | null>>;

/** One time-of-day schedule row: a clock window on chosen weekdays that steers
 * one or both modes. Room-local blocks (`schedule_blocks`) and the blocks of a
 * base schedule (`schedules_lib`) share this shape and the same resolver. */
export interface ScheduleBlock {
  id: string;
  name: string;
  enabled?: boolean;
  when: { start: string; end: string; days?: string[] };
  gate?: "always" | "occupied" | "away";
  side?: "heat" | "cool" | "both";
  value: {
    mode: "set" | "shift" | "comfort" | "eco";
    temp?: number;
    direction?: "raise" | "lower";
    delta?: number;
  };
}

/** A reusable house/floor schedule (settings["schedules_lib"]); rooms bind it
 * explicitly. Its blocks resolve under each room's own schedule_blocks, so a
 * house schedule steers every binder while each room keeps the last word. */
export interface BaseSchedule {
  id: string;
  name: string;
  scope: "house" | "floor";
  floor_id?: string;
  blocks: ScheduleBlock[];
}

/** A room's thin reference into the base-schedule library. */
export interface BaseScheduleRef {
  id: string;
  enabled?: boolean;
}

/** Per-tier confirmation data from the bound tariff sources (see
 * TouManager.tier_info): the price the sources know for that tier and the
 * raw period vocabulary that resolves to it. Kind marks where the number
 * came from: live now, provider's next-period price, persisted last known
 * (source currently down), manual window, or configured cutoff. */
export interface TouTierInfoTier {
  rate: number | null;
  kind: "live" | "next" | "last" | "window" | "cutoff" | null;
  min?: number | null;
  max?: number | null;
  count?: number;
  ts?: number | null;
  names: string[];
  current: boolean;
}

export interface TouTierInfo {
  unit: string;
  tiers: Partial<Record<TouTier, TouTierInfoTier>>;
  unmapped: { state: string; count: number }[];
  current: { tier: TouTier; rate: number; stale: boolean } | null;
  next: { ts: number | null; tier: TouTier; rate: number | null; stale: boolean } | null;
  /** Why the future view is incomplete while its sensors are bound:
   * "no_tier" (nothing says which period follows), "no_time" (the change
   * sensor is not a timestamp), "no_price" (no price for the coming tier). */
  next_hint?: "no_tier" | "no_time" | "no_price" | "";
}

/** Which source class answers the current tier ("auto" = legacy cascade). */
export type TouTierSource = "auto" | "period" | "rate" | "flags";

/** Which source provides the future-tier timeline ("auto" = calendars, then
 * next-change; "next_change_rate" = classify the provider's next-period rate
 * with the cost thresholds; "none" = shape only, no pre-conditioning/coasting
 * planning). */
export type TouLookahead = "auto" | "calendar" | "next_change_rate" | "next_change" | "none";

/** What an override row does to the resolved schedule target. "arrive" is the
 * decay-derived action: the room must coast to the value exactly at the
 * arrival clock time with conditioning stopped from the tier boundary; until
 * the thermal model is learned the rule holds the flat temperature. */
export type TouOverrideAction = "set" | "raise" | "lower" | "arrive";

/** How long a non-arrive rule holds after its tier boundary. */
export type TouOverrideUntil = "tier_window" | "minutes" | "clock";

/** One entry of the shared tariff rule library (`settings.tou_rules`).
 * The rule's name is its identity: same name means the same rule in every
 * room. Editing it from one card propagates; renaming to a fresh name forks
 * a derivative; renaming onto a taken name merges (incoming wins).
 * Backend expansion: TouManager.row_records. */
export interface TouRule {
  id: string;
  name: string;
  /** Which tariff tier's boundary anchors the rule; "any" = every change. */
  tier: TouTier | "any";
  /** Whether the rule fires when the tier starts or when it ends. */
  edge: "start" | "end";
  action: TouOverrideAction;
  /** °C target for set/arrive (or "comfort"/"eco" = the room's own target),
   * Δ°C for raise/lower; null/undefined = skip this side. */
  heat?: number | "comfort" | "eco" | null;
  cool?: number | "comfort" | "eco" | null;
  until: TouOverrideUntil;
  /** Duration when until = "minutes". */
  minutes?: number;
  /** End time "HH:MM" when until = "clock", or the arrival time for arrive. */
  clock?: string;
  /** Restrict to boundaries on these weekdays; empty = every day. */
  days?: string[];
  /** Restrict to boundaries whose local time falls in this clock range. */
  between?: { from: string; to: string } | null;
}

/** The rule's fields without its library id — what a card `definition`
 * carries while creating/editing (the server strips/owns ids). */
export type TouRuleDef = Omit<TouRule, "id">;

/** One row on the device card: a thin reference into the shared library.
 * `definition` rides along only while the card creates or edits the rule
 * (the server upserts it into the library and strips it from the room). */
export interface TouOverrideRow {
  id: string;
  rule_id: string;
  enabled: boolean;
  definition?: TouRuleDef;
}

export interface TouCalendarBinding {
  entity_id: string;
  tier: TouTier;
}

/** Manual tariff-window row: a rate applied between two wall-clock times on
 * the chosen weekdays. Empty days list = every day; an end not after the
 * start wraps past midnight. Maps to a tier through the cost thresholds. */
export interface TouRateWindow {
  id: string;
  rate: number;
  start: string;
  end: string;
  days: string[];
}

/** A stored room whose Home Assistant area was deleted; it rests until the area is back (#494). */
export interface RoomWithoutArea {
  area_id: string;
  display_name: string;
}

export interface CompressorGroup {
  id: string;
  name: string;
  members: string[];
  min_run_minutes: number;
  min_off_minutes: number;
  master_entity: string;
  conflict_resolution: ConflictResolution;
  action_script: string;
  enforce_uniform_mode: boolean;
}

export interface RoomConfig {
  area_id: string;
  thermostats: string[];
  acs: string[];
  devices?: DeviceConfig[];
  temperature_sensor: string;
  humidity_sensor: string;
  occupancy_sensors?: string[];
  occupancy_eco_enabled?: boolean;
  occupancy_eco_delay?: number;
  preheat_enabled?: boolean;
  window_sensors: string[];
  window_open_delay: number;
  window_close_delay: number;
  climate_mode: ClimateMode;
  schedules: ScheduleEntry[];
  schedule_selector_entity: string;
  /** Room-owned time-of-day blocks; override bound base schedules on overlap. */
  schedule_blocks?: ScheduleBlock[];
  /** Explicit opt-in bindings into the shared base-schedule library. */
  base_schedules?: BaseScheduleRef[];
  comfort_temp?: number;
  eco_temp?: number;
  comfort_heat: number;
  comfort_cool: number;
  eco_heat: number;
  eco_cool: number;
  override_heat?: number | null;
  override_cool?: number | null;
  override_until?: number | null;
  override_type?: OverrideType | null;
  presence_persons?: string[];
  display_name?: string;
  heating_system_type?: string;
  covers?: string[];
  covers_auto_enabled?: boolean;
  covers_deploy_threshold?: number;
  covers_min_position?: number;
  covers_override_minutes?: number;
  cover_schedules?: CoverScheduleEntry[];
  cover_schedule_selector_entity?: string;
  cover_orientations?: Record<string, number>;
  covers_outdoor_min_temp?: number | null;
  covers_night_close?: boolean;
  covers_night_position?: number;
  covers_night_close_elevation?: number;
  covers_night_close_offset_minutes?: number;
  covers_snap_deploy?: boolean;
  covers_window_hold?: boolean;
  cover_min_positions?: Record<string, number>;
  ignore_presence?: boolean;
  is_outdoor?: boolean;
  valve_protection_exclude?: string[];
  heat_source_orchestration?: boolean;
  heat_source_primary_delta?: number;
  heat_source_outdoor_threshold?: number;
  heat_source_ac_min_outdoor?: number;
  climate_control_enabled?: boolean;
  /** The store layer arbitrages tariff periods for this room (see
   * managers/tou_manager.py): relax across expensive stretches, pre-charge
   * before them, bounded by the limits below. */
  tou_store_enabled?: boolean;
  /** Optional per-room overrides of the global store limits (blank = inherit). */
  tou_heat_min?: number | null;
  tou_heat_max?: number | null;
  tou_cool_min?: number | null;
  tou_cool_max?: number | null;
  tou_swing?: number | null;
  tou_store_gap?: number | null;
  /** Section gate: off hides the schedule and the rows cannot steer the room. */
  tou_schedule_enabled?: boolean;
  /** Tariff-schedule rows: references into the shared rule library. */
  tou_overrides?: TouOverrideRow[];
  live?: RoomLiveData;
}

export interface GlobalSettings {
  outdoor_temp_sensor: string;
  outdoor_humidity_sensor: string;
  outdoor_cooling_min?: number;
  outdoor_heating_max?: number;
  control_mode?: "mpc" | "bangbang";
  comfort_weight?: number;
  weather_entity?: string;
  outdoor_unavailable_notify?: boolean;
  climate_control_active?: boolean;
  learning_disabled_rooms?: string[];
  hidden_rooms?: string[];
  prediction_enabled?: boolean;
  vacation_temp?: number;
  vacation_until?: number | null;
  presence_enabled?: boolean;
  presence_persons?: string[];
  presence_away_action?: "eco" | "off";
  presence_clears_override?: boolean;
  schedule_off_action?: "eco" | "off";
  valve_protection_enabled?: boolean;
  valve_protection_interval_days?: number;
  coil_dry_enabled?: boolean;
  coil_dry_minutes?: number;
  coil_dry_mode?: "fan_only" | "dry";
  coil_dry_fan_mode?: string;
  coil_dry_min_cooling_minutes?: number;
  coil_dry_drain_minutes?: number;
  mold_detection_enabled?: boolean;
  mold_humidity_threshold?: number;
  mold_f_rsi?: number;
  mold_sustained_minutes?: number;
  mold_notification_cooldown?: number;
  mold_notifications_enabled?: boolean;
  mold_notification_targets?: NotificationTarget[];
  mold_prevention_enabled?: boolean;
  mold_prevention_intensity?: "light" | "medium" | "strong";
  mold_prevention_notify_enabled?: boolean;
  mold_prevention_notify_targets?: NotificationTarget[];
  compressor_groups?: CompressorGroup[];
  /** Time-of-Use tariff awareness (managers/tou_manager.py). */
  tou_enabled?: boolean;
  tou_tier_source?: TouTierSource;
  tou_lookahead?: TouLookahead;
  tou_rate_entity?: string;
  tou_period_entity?: string;
  tou_next_change_entity?: string;
  tou_next_rate_entity?: string;
  /** Optional sensor publishing the *name* of the next period (e.g.
   * "ulo_mid_peak"); its vocabulary maps to the coming tier without
   * thresholds or a fixed dropdown. */
  tou_next_period_entity?: string;
  /** "" = no lookahead (the next-change step is disabled). */
  tou_next_change_tier?: TouTier | "";
  tou_calendar_entities?: TouCalendarBinding[];
  tou_rate_windows?: TouRateWindow[];
  tou_flag_entities?: Partial<Record<TouTier, string>>;
  tou_price_weighting?: boolean;
  tou_peak_threshold?: number | null;
  tou_mid_threshold?: number | null;
  tou_ultra_threshold?: number | null;
  tou_heat_min?: number;
  tou_heat_max?: number;
  tou_cool_min?: number;
  tou_cool_max?: number;
  tou_swing?: number;
  tou_store_gap?: number;
  tou_coast_max_minutes?: number;
  /** User-assigned period vocabulary: raw state (lower-cased) -> tier. */
  tou_period_aliases?: Record<string, string>;
  /** Shared tariff rule library; cards reference entries by rule_id. */
  tou_rules?: TouRule[];
  room_order?: string[];
  group_by_floor?: boolean;
  boost_applied_at?: Record<string, number>;
}

// HA types for panel integration
export interface HassConnection {
  addEventListener(event: string, callback: () => void): void;
  removeEventListener(event: string, callback: () => void): void;
}

export interface HomeAssistant {
  callWS: <T>(msg: Record<string, unknown>) => Promise<T>;
  callService: (domain: string, service: string, data?: Record<string, unknown>) => Promise<void>;
  states: Record<string, HassEntity>;
  areas: Record<string, HassArea>;
  floors?: Record<string, HassFloor>;
  entities: Record<string, HassEntityRegistryEntry>;
  devices: Record<string, HassDeviceRegistryEntry>;
  language: string;
  config: { unit_system: { temperature: string } };
  connection?: HassConnection;
}

export interface HassArea {
  area_id: string;
  name: string;
  picture: string | null;
  floor_id: string | null;
}

export interface HassFloor {
  floor_id: string;
  name: string;
  level: number | null;
}

export interface HassEntityRegistryEntry {
  entity_id: string;
  area_id: string | null;
  device_id: string | null;
  platform: string;
}

export interface HassDeviceRegistryEntry {
  id: string;
  area_id: string | null;
}

export interface HassEntity {
  entity_id: string;
  state: string;
  attributes: Record<string, unknown>;
}

export interface AnalyticsDataPoint {
  ts: number;
  room_temp: number | null;
  outdoor_temp: number | null;
  target_temp: number | null;
  mode: string;
  predicted_temp: number | null;
  window_open: boolean;
  heating_power: number | null;
  solar_irradiance: number | null;
  blind_position?: number | null;
  cover_reason?: string;
  device_setpoint?: number | null;
}

export interface AnalyticsData {
  detail: AnalyticsDataPoint[];
  history: AnalyticsDataPoint[];
  forecast?: AnalyticsDataPoint[];
  model: {
    confidence: number;
    model: {
      C: number;
      U: number;
      Q_heat: number;
      Q_cool: number;
      Q_solar: number;
      Q_occupancy: number;
      Q_fast?: number;
    };
    n_samples: number;
    n_observations: number;
    n_heating: number;
    n_cooling: number;
    applicable_modes: string[];
    mpc_active: boolean;
    sigma_e: number;
    prediction_std_idle: number;
    prediction_std_heating: number;
    has_occupancy_sensors: boolean;
    solar_gain_now?: number;
    solar_gain_today_max?: number;
  };
}

export type TimeRange = "12h" | "24h" | "2d" | "7d" | "14d" | "30d" | "90d";
