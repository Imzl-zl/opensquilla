import { describe, expect, it } from 'vitest'

import appSource from './App.vue?raw'

/**
 * The Workbench dock can be collapsed and, before this control existed, a
 * collapsed panel was only recoverable by opening a new item. These assertions
 * keep a visible toggle (and its shortcut) wired to the same store state the
 * host renders from.
 */
describe('App workbench dock toggle contract', () => {
  it('renders a toggle bound to the workbench host it controls', () => {
    const start = appSource.indexOf('data-testid="workbench-toggle"')
    expect(start).toBeGreaterThan(-1)
    const button = appSource.slice(
      appSource.lastIndexOf('<button', start),
      appSource.indexOf('</button>', start),
    )

    expect(button).toContain('aria-controls="workbench-panel"')
    expect(button).toContain(':aria-expanded="workbenchStore.expanded"')
    expect(button).toContain(':aria-keyshortcuts="workbenchToggleAriaShortcut"')
    expect(button).toContain('@click="toggleWorkbench()"')
    // The icon reflects the state instead of relying on the user's memory.
    expect(button).toContain("workbenchStore.expanded ? 'panel-right-close' : 'panel-right-open'")
  })

  it('hides the toggle when no panel can be shown', () => {
    const computedStart = appSource.indexOf('const workbenchToggleVisible')
    const computedEnd = appSource.indexOf('const workbenchToggleTitle')
    expect(computedEnd).toBeGreaterThan(computedStart)
    const gating = appSource.slice(computedStart, computedEnd)

    expect(gating).toContain('appStore.features.artifactWorkbench === true')
    expect(gating).toContain('workbenchStore.items.length > 0')
  })

  it('toggles the same store flag the host renders from', () => {
    const start = appSource.indexOf('function toggleWorkbench()')
    const end = appSource.indexOf('}', start)
    expect(appSource.slice(start, end)).toContain(
      'workbenchStore.setExpanded(!workbenchStore.expanded)',
    )
  })

  it('binds the toggle-workbench shortcut to the same function', () => {
    // Bound twice on purpose: once for the button's aria-keyshortcuts, once for
    // the keydown handler, so the last occurrence is the handler branch.
    const start = appSource.lastIndexOf("shortcutsStore.effectiveBinding('toggle-workbench')")
    expect(start).toBeGreaterThan(-1)
    const branch = appSource.slice(start, start + 400)

    expect(branch).toContain('bindingMatches(e, toggleWorkbenchBinding, isMac)')
    expect(branch).toContain('toggleWorkbench()')
  })
})
