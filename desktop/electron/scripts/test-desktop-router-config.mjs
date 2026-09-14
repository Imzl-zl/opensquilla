import { strict as assert } from 'node:assert'
import {
  desktopRouterConfigTomlLines,
  desktopRouterConfigPreambleLines,
  desktopRouterPreambleLineIndexes,
  desktopTomlSectionNames,
  normalizeRouterPresetBinding,
  resolveDesktopRouterUpdate,
  routerConfigTomlLines,
} from '../dist/desktop-router-config.js'
import { defaultRouterTiers } from '../dist/desktop-router-profiles.js'
import { normalizeRouterTiers } from '../dist/router-tier-normalization.js'

const defaults = {
  c0: { provider: 'tokenrhythm', model: 'deepseek-v4-flash-0731' },
  c1: { provider: 'tokenrhythm', model: 'deepseek-v4-pro-0813' },
  c2: { provider: 'tokenrhythm', model: 'kimi-k2.7-code' },
  c3: { provider: 'tokenrhythm', model: 'glm-5.2', ensembleEnabled: true },
}
assert.equal(defaultRouterTiers('openrouter', 'recommended').c1.provider, 'openrouter')
assert.equal(defaultRouterTiers('tokenrhythm', 'recommended').c3.ensembleEnabled, true)
const mutableDefaults = defaultRouterTiers('openrouter', 'recommended')
mutableDefaults.c1.model = 'caller-edit'
assert.notEqual(defaultRouterTiers('openrouter', 'recommended').c1.model, 'caller-edit')
const legacy = { routerMode: 'recommended', routerDefaultTier: 'c1', routerTiers: defaults }
function update(payload = {}, existing = legacy, extra = {}) {
  return resolveDesktopRouterUpdate({
    payload, existing, routerMode: existing?.routerMode ?? 'recommended',
    routerDefaultTier: payload.routerDefaultTier ?? existing?.routerDefaultTier ?? 'c1',
    defaultTiers: defaults, freshConfig: false, ...extra,
  })
}

for (const invalid of [undefined, null, '', 'recommended', 'FOLLOW_PRIMARY', {}, true]) {
  assert.equal(normalizeRouterPresetBinding(invalid), undefined)
}
for (const binding of ['follow_primary', 'custom']) assert.equal(normalizeRouterPresetBinding(binding), binding)

// Historical recommended/equal ladders, regenerated defaults and untrusted IPC
// markers never acquire ownership just because a current catalog matches them.
for (const existing of [legacy, { ...legacy, routerTiers: undefined }, null]) {
  for (const payload of [{}, { routerTiers: defaults }, { routerPresetBinding: 'follow_primary' }]) {
    const result = update(payload, existing)
    assert.equal(Object.hasOwn(result, 'routerPresetBinding'), false)
    assert.doesNotMatch(routerConfigTomlLines(result).join('\n'), /preset_binding|tier_profile/)
  }
}
const customPayload = {
  routerPresetBinding: 'follow_primary',
  routerTiers: { ...defaults, c1: { ...defaults.c1, model: 'operator-choice' } },
}
assert.equal(update(customPayload).routerPresetBinding, 'custom')
for (const options of [{ freshConfig: true }, {}]) {
  const result = update({ ...customPayload, routerResetToRecommended: true }, null, options)
  assert.equal(result.routerPresetBinding, 'follow_primary')
  assert.equal(result.routerTiers.c1.model, defaults.c1.model, 'main must ignore renderer tiers on reset')
}
const fresh = update(customPayload, null, { freshConfig: true })
assert.equal(fresh.routerPresetBinding, 'follow_primary')
assert.equal(fresh.routerTiers.c1.model, defaults.c1.model)

for (const routerPresetBinding of [undefined, 'follow_primary', 'custom']) {
  const existing = { ...legacy, ...(routerPresetBinding ? { routerPresetBinding } : {}) }
  for (const payload of [{ apiKey: 'synthetic-new-key' }, { searchProvider: 'duckduckgo' },
    { routerTiers: structuredClone(defaults) }, { routerPresetBinding: 'custom' }]) {
    const result = update(payload, existing)
    assert.equal(result.routerPresetBinding, routerPresetBinding)
    assert.equal(result.writeIntent, 'preserve')
  }
  const disabled = update({}, existing, { routerMode: 'disabled' })
  assert.equal(disabled.writeIntent, 'toggle')
  assert.equal(disabled.routerPresetBinding, routerPresetBinding)
  assert.equal(disabled.routerTiers.c3.ensembleEnabled, true)
  const enabled = update({}, disabled, { routerMode: 'recommended' })
  assert.equal(enabled.routerPresetBinding, routerPresetBinding)
  assert.deepEqual(enabled.routerTiers, disabled.routerTiers)
}

for (const edit of [
  { model: 'changed' }, { provider: 'openrouter' }, { thinkingLevel: 'high' },
  { thinking_level: 'high' }, { ensembleEnabled: true }, { ensemble_enabled: true },
  { ensemble_selection_mode: 'operator-pool' }, { image_only: true },
  { extra: { temperature: 0.3, stops: ['end'] } },
]) {
  const result = update({ routerTiers: { ...defaults, c1: { ...defaults.c1, ...edit } } }, fresh)
  assert.equal(result.routerPresetBinding, 'custom', JSON.stringify(edit))
  assert.equal(result.writeIntent, 'replace')
}
assert.equal(update({ routerDefaultTier: 'c2' }, fresh).routerPresetBinding, 'custom')
const withExtra = update({ routerTiers: { ...defaults, c1: { ...defaults.c1, extra: { temperature: 0.3 } } } }, fresh)
assert.equal(update({ routerTiers: defaults }, withExtra).writeIntent, 'replace', 'removing an extra field is an edit')
assert.equal(update({ routerTiers: { ...defaults, c1: { ...defaults.c1, description: 'display only' } } }, fresh).writeIntent, 'preserve')

const actualRouter = [
  '["squilla_router"] # Control UI owns this config',
  'enabled = true',
  'preset_binding = "custom"',
  'cross_provider = true',
  'operator_note = """',
  '[llm]',
  'enabled = this is text, not a boolean',
  '"""',
  "[ squilla_router . 'tiers' . c1 ] # actual operator tier",
  'provider = "openrouter"',
  'model = "operator-model"',
  'thinking_level = "high"',
  'ensemble_enabled = false',
  '[squilla_router.budget_gate]',
  'action = "cap"',
  'limit_usd = 2.5',
].join('\n')
const actualConfig = '[llm]\nprovider = "tokenrhythm"\n' + actualRouter + '\n[channels]\nenabled = true\n'
assert.deepEqual(desktopRouterConfigTomlLines(fresh, actualConfig, 'preserve'), actualRouter.split('\n'))
const toggled = desktopRouterConfigTomlLines({ ...fresh, routerMode: 'disabled' }, actualConfig, 'toggle').join('\n')
assert.equal(toggled, actualRouter.replace('enabled = true', 'enabled = false'))
assert.equal(desktopRouterConfigTomlLines(fresh, toggled, 'toggle').join('\n'), actualRouter)
assert.deepEqual(desktopRouterConfigTomlLines(fresh, '[llm]\nprovider = "x"', 'preserve'), [])
assert.deepEqual(desktopTomlSectionNames('[squilla_router.tiers.c1]\nmodel="""\n[llm]\n"""\n["squilla_router.fake"]'),
  ['squilla_router\0tiers\0c1', null, null, null, 'squilla_router.fake'])
for (const preamble of [
  'squilla_router.enabled = true\n"squilla_router".preset_binding = "custom"\nsquilla_router.tiers.c1.model = "operator"',
  'squilla_router = { tiers = { c1 = { enabled = true, model = "operator" } }, preset_binding = "custom", enabled = true }',
  "'squilla_router'.enabled = true\nsquilla_router.extra = [\n  'first',\n  'second',\n]\nsquilla_router.note = '''\n[llm]\n'''",
]) {
  const raw = preamble + '\n[llm]\nprovider = "x"\n'
  assert.equal(desktopRouterConfigPreambleLines(fresh, raw).join('\n'), preamble)
  assert.equal(desktopRouterPreambleLineIndexes(raw).size, preamble.split('\n').length)
  assert.deepEqual(desktopRouterConfigTomlLines(fresh, raw), [])
  const off = { ...fresh, routerMode: 'disabled' }
  const toggledPreamble = desktopRouterConfigPreambleLines(off, raw, 'toggle').join('\n')
  // The nested tier.enabled stays true; only the Router's enabled bit changes.
  assert.match(toggledPreamble, /enabled = false/)
  assert.equal(desktopRouterConfigPreambleLines(fresh, toggledPreamble, 'toggle').join('\n'), preamble)
  assert.deepEqual(desktopRouterConfigTomlLines(off, raw, 'toggle'), [])
}
const inlineWithoutEnabled = 'squilla_router = { preset_binding = "custom", tiers = { c1 = { model = "operator" } } }'
assert.match(desktopRouterConfigPreambleLines({ ...fresh, routerMode: 'disabled' }, inlineWithoutEnabled, 'toggle').join('\n'),
  /squilla_router = \{ enabled = false,\s+preset_binding/)
assert.deepEqual(desktopRouterConfigPreambleLines(fresh, inlineWithoutEnabled, 'replace'), [])

const serialized = routerConfigTomlLines({ ...withExtra, routerMode: 'disabled' }).join('\n')
assert.match(serialized, /enabled = false/)
assert.match(serialized, /preset_binding = "custom"/)
assert.match(serialized, /provider = "tokenrhythm"/)
assert.match(serialized, /"extra" = \{ "temperature" = 0.3 \}/)
assert.doesNotMatch(serialized, /tier_profile|supports_image/)
assert.throws(() => routerConfigTomlLines({ ...fresh, routerTiers: { 'bad]name': defaults.c1 } }), /tier name/)
const normalized = normalizeRouterTiers({ ...defaults, c1: { ...defaults.c1,
  ensemble_selection_mode: 'custom_b5', extra: { temperature: 0.3 }, supports_image: true } }, defaults)
assert.equal(normalized.c1.ensembleSelectionMode, 'custom_b5')
assert.deepEqual(normalized.c1.extra, { temperature: 0.3 })
assert.doesNotMatch(routerConfigTomlLines({ ...fresh, routerTiers: normalized }).join('\n'), /supports_image/)

console.log(JSON.stringify({ ok: true, ownership: true, actualRouterPreserved: true, inlineSerializer: true }))
