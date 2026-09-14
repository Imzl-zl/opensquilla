// Loaded only when saving a provider selection, never on the Desktop boot path.
import { parse, stringify } from 'smol-toml'
import {
  desktopRouterConfigPreambleLines,
  desktopRouterConfigTomlLines,
  normalizeRouterPresetBinding,
  routerConfigTomlLines,
  type DesktopRouterConfig,
  type DesktopRouterWriteIntent,
} from './desktop-router-config.js'
import { defaultRouterTiers } from './desktop-router-profiles.js'
import { normalizeRouterTiers, type RouterTier } from './router-tier-normalization.js'

export interface DesktopPrimaryProviderChange {
  expectedConfig: string
  router: DesktopRouterConfig
  modelRoutingMode: 'direct' | 'squilla_router' | 'llm_ensemble'
  routerPreamble: string[]
  routerLines: string[]
  ensembleLines: string[]
}

function table(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : {}
}

/** Resolve ownership from config.toml; the credential mirror may be stale. */
export function prepareDesktopPrimaryProviderChange(options: {
  existingRaw: string
  provider: string
  defaultTiers: Record<string, RouterTier>
  requestedRouter: DesktopRouterConfig & { writeIntent: DesktopRouterWriteIntent }
  requestedMode?: 'direct' | 'squilla_router' | 'llm_ensemble'
}): DesktopPrimaryProviderChange | null {
  let config: Record<string, unknown>
  try {
    config = parse(options.existingRaw, { integersAsBigInt: 'asNeeded' })
  } catch {
    // Parser diagnostics can contain credential-bearing source lines.
    throw new Error('Saved configuration is invalid; repair it before changing providers.')
  }
  const previousProvider = String(table(config.llm).provider || '').trim().toLowerCase()
  if (previousProvider === options.provider || !previousProvider) return null

  const saved = table(config.squilla_router)
  const ensemble = { ...table(config.llm_ensemble) }
  const requested = options.requestedRouter
  const explicitReplacement = requested.writeIntent === 'replace'
  const binding = explicitReplacement ? requested.routerPresetBinding
    : normalizeRouterPresetBinding(saved.preset_binding)
  const replaceTiers = explicitReplacement || binding === 'follow_primary'
  const enabled = options.requestedMode === undefined ? saved.enabled !== false
    : options.requestedMode === 'squilla_router'
  if (options.requestedMode !== undefined) ensemble.enabled = options.requestedMode === 'llm_ensemble'
  const defaultTier = explicitReplacement ? requested.routerDefaultTier : String(saved.default_tier || 'c1')
  if (!['c0', 'c1', 'c2', 'c3'].includes(defaultTier)) {
    throw new Error('Review the saved Router default tier in Model Routing before changing providers.')
  }
  const savedTiers = saved.tiers ?? defaultRouterTiers(
    String(saved.tier_profile || previousProvider), 'recommended',
  )
  const router: DesktopRouterConfig = {
    routerMode: enabled ? binding === 'follow_primary' ? 'recommended' : 'custom' : 'disabled',
    routerDefaultTier: defaultTier as DesktopRouterConfig['routerDefaultTier'],
    routerTiers: normalizeRouterTiers(
      replaceTiers ? explicitReplacement ? requested.routerTiers : options.defaultTiers : savedTiers,
      {},
    ),
    ...(binding ? { routerPresetBinding: binding } : {}),
  }
  if (enabled && ensemble.enabled !== true && saved.cross_provider_tiers !== true
    && Object.entries(router.routerTiers).some(([name, tier]) => (
      /^c[0-3]$/.test(name) && tier.provider && tier.provider !== options.provider
    ))) {
    throw new Error('Saved Router tiers use another provider. Disable Router, reset to recommended routes, or resolve the provider change in Model Routing.')
  }

  let routerLines: string[]
  let routerPreamble: string[] = []
  if (replaceTiers) {
    const recommended = table(parse(routerConfigTomlLines(router).join('\n')).squilla_router)
    const updated: Record<string, unknown> = { ...saved, enabled, default_tier: defaultTier,
      preset_binding: binding, tiers: recommended.tiers }
    delete updated.tier_profile
    routerLines = stringify({ squilla_router: updated }).trimEnd().split('\n')
  } else {
    const intent = enabled === (saved.enabled !== false) ? 'preserve' : 'toggle'
    routerPreamble = desktopRouterConfigPreambleLines(router, options.existingRaw, intent)
    routerLines = desktopRouterConfigTomlLines(router, options.existingRaw, intent)
  }
  return {
    expectedConfig: options.existingRaw,
    router,
    modelRoutingMode: ensemble.enabled === true ? 'llm_ensemble' : enabled ? 'squilla_router' : 'direct',
    routerPreamble,
    routerLines,
    ensembleLines: stringify({ llm_ensemble: ensemble }).trimEnd().split('\n'),
  }
}
