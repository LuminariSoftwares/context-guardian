
export const IDLE_DEFAULTS = { idleRatio: 0, cooldownMs: 60000, appendOnly: false };

function coerceNum(v, dflt) {
  if (v === undefined) return dflt;
  const n = Number(v);
  return Number.isNaN(n) ? 0 : n;
}

export function planIdle(input = {}) {
  const src = input && typeof input === "object" ? input : {};
  const idleRatio = coerceNum(src.idleRatio, IDLE_DEFAULTS.idleRatio);
  const cooldownMs = coerceNum(src.cooldownMs, IDLE_DEFAULTS.cooldownMs);
  const appendOnly = src.appendOnly === undefined ? IDLE_DEFAULTS.appendOnly : !!src.appendOnly;
  const tokens = coerceNum(src.tokens, 0);
  const floorTokens = coerceNum(src.floorTokens, 0);
  const lastIdleMs = coerceNum(src.lastIdleMs, 0);
  const nowMs = coerceNum(src.nowMs, 0);
  const hasCompactRegion = !!src.hasCompactRegion;
  const surface = Array.isArray(src.surface) ? src.surface : [];
  const pressure = src.pressure === undefined || src.pressure === null ? NaN : Number(src.pressure);

  if (idleRatio <= 0) return { action: "skip", reason: "idle-off", start: null, end: null };
  if (Number.isNaN(pressure)) return { action: "skip", reason: "no-measure", start: null, end: null };
  if (pressure < idleRatio) return { action: "skip", reason: "below-ratio", start: null, end: null };
  if (tokens < floorTokens) return { action: "skip", reason: "below-floor", start: null, end: null };
  if (nowMs - lastIdleMs < cooldownMs) return { action: "skip", reason: "cooldown", start: null, end: null };
  if (!appendOnly) return { action: "compactNow", reason: "chain-off", start: null, end: null };
  if (!hasCompactRegion) return { action: "compactNow", reason: "no-compactRegion", start: null, end: null };
  if (surface.length < 2) return { action: "compactNow", reason: "surface-too-small", start: null, end: null };
  return { action: "compactRegion", reason: "chain", start: surface[0], end: surface[surface.length - 1] };
}

export function idleRecord(plan, before, after) {
  return {
    event: "idle-compaction",
    route: plan.action,
    reason: plan.reason,
    before: before ?? null,
    after: after ?? null,
    compacted: after !== null && after !== undefined && before !== null && before !== undefined && after < before,
  };
}

export function describe(plan) {
  let line = `idle: ${plan.action} (${plan.reason})`;
  if (plan.action === "compactRegion") line += ` seq ${plan.start}..${plan.end}`;
  return line;
}

function _selftest() {
  const checks = [
    ["defaults_no_overrides_is_idle_off", () => JSON.stringify(planIdle()) === JSON.stringify({ action: "skip", reason: "idle-off", start: null, end: null })],
    ["no_measure_when_pressure_null", () => {
      const p = planIdle({ idleRatio: 0.5 });
      return p.action === "skip" && p.reason === "no-measure";
    }],
    ["below_ratio", () => {
      const p = planIdle({ idleRatio: 0.5, pressure: 0.2 });
      return p.action === "skip" && p.reason === "below-ratio";
    }],
    ["below_floor", () => {
      const p = planIdle({ idleRatio: 0.5, pressure: 0.6, tokens: 10, floorTokens: 20 });
      return p.action === "skip" && p.reason === "below-floor";
    }],
    ["cooldown_boundary", () => {
      const atBoundary = planIdle({ idleRatio: 0.5, pressure: 0.6, tokens: 100, floorTokens: 0, nowMs: 60000, lastIdleMs: 0, cooldownMs: 60000 });
      const insideCooldown = planIdle({ idleRatio: 0.5, pressure: 0.6, tokens: 100, floorTokens: 0, nowMs: 59999, lastIdleMs: 0, cooldownMs: 60000 });
      return atBoundary.reason !== "cooldown" && insideCooldown.action === "skip" && insideCooldown.reason === "cooldown";
    }],
    ["chain_off_goes_compactNow", () => {
      const p = planIdle({ idleRatio: 0.5, pressure: 0.6, tokens: 100, floorTokens: 0, nowMs: 60000, cooldownMs: 0 });
      return p.action === "compactNow" && p.reason === "chain-off" && p.start === null && p.end === null;
    }],
    ["no_compactRegion_goes_compactNow", () => {
      const p = planIdle({ idleRatio: 0.5, pressure: 0.6, tokens: 100, floorTokens: 0, nowMs: 60000, cooldownMs: 0, appendOnly: true, hasCompactRegion: false });
      return p.action === "compactNow" && p.reason === "no-compactRegion";
    }],
    ["surface_too_small_one_node", () => {
      const p = planIdle({ idleRatio: 0.5, pressure: 0.6, tokens: 100, floorTokens: 0, nowMs: 60000, cooldownMs: 0, appendOnly: true, hasCompactRegion: true, surface: [42] });
      return p.action === "compactNow" && p.reason === "surface-too-small";
    }],
    ["chain_route_start_end_exact", () => {
      const p = planIdle({ idleRatio: 0.5, pressure: 0.6, tokens: 100, floorTokens: 0, nowMs: 60000, cooldownMs: 0, appendOnly: true, hasCompactRegion: true, surface: [12, 40, 77] });
      return p.action === "compactRegion" && p.reason === "chain" && p.start === 12 && p.end === 77;
    }],
    ["non_array_surface_is_empty", () => {
      const p = planIdle({ idleRatio: 0.5, pressure: 0.6, tokens: 100, floorTokens: 0, nowMs: 60000, cooldownMs: 0, appendOnly: true, hasCompactRegion: true, surface: "nope" });
      return p.action === "compactNow" && p.reason === "surface-too-small";
    }],
    ["idle_record_exact_compacted_true", () => {
      const plan = { action: "compactRegion", reason: "chain", start: 1, end: 9 };
      const r = idleRecord(plan, 500, 200);
      return JSON.stringify(r) === JSON.stringify({ event: "idle-compaction", route: "compactRegion", reason: "chain", before: 500, after: 200, compacted: true });
    }],
    ["idle_record_null_after_is_not_compacted", () => {
      const plan = { action: "compactNow", reason: "chain-off", start: null, end: null };
      const r = idleRecord(plan, 500, null);
      return r.after === null && r.compacted === false;
    }],
    ["describe_line_format", () => {
      const skipLine = describe({ action: "skip", reason: "idle-off", start: null, end: null });
      const chainLine = describe({ action: "compactRegion", reason: "chain", start: 12, end: 77 });
      return skipLine === "idle: skip (idle-off)" && chainLine === "idle: compactRegion (chain) seq 12..77";
    }],
  ];
  let passed = 0;
  for (const [name, fn] of checks) {
    let ok = false;
    try {
      ok = !!fn();
      console.log((ok ? "  ok   " : "  FAIL ") + name);
    } catch (e) {
      console.log(`  FAIL ${name}  -- raised ${e && e.name}: ${e && e.message}`);
    }
    if (ok) passed += 1;
  }
  const total = checks.length;
  const failed = total - passed;
  console.log(`cg_idle_route selftest: ${total} checks, ${passed} passed, ${failed} failed`);
  return total > 0 && failed === 0 ? 0 : 1;
}

if (process.argv.includes("--selftest")) {
  process.exit(_selftest());
}
