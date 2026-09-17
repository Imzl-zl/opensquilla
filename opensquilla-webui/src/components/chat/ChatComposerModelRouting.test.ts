// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive, type App } from 'vue'
import i18n from '@/i18n'
import ChatComposerModelRouting from './ChatComposerModelRouting.vue'

const apps: App[] = []
beforeEach(() => {
  i18n.global.locale.value = 'en'
  document.body.innerHTML = ''
})
afterEach(() => {
  apps.splice(0).forEach((app) => app.unmount())
  vi.restoreAllMocks()
})
async function mount(overrides: Record<string, unknown> = {}) {
  const selected = vi.fn(),
    mode = vi.fn(),
    close = vi.fn(),
    settings = vi.fn(),
    refresh = vi.fn()
  const props = reactive({
    modelRoutingMode: 'off',
    busy: false,
    newTaskModelAvailable: true,
    newTaskModels: [
      { id: 'shared-model', name: 'Model Alpha', provider: 'provider-a' },
      { id: 'shared-model', name: 'Model Beta', provider: 'provider-b' },
    ],
    onSelectNewTaskModel: selected,
    onSetSessionRoutingMode: mode,
    onClose: close,
    onOpenModelSettings: settings,
    onRefreshNewTaskModels: refresh,
    ...overrides,
  })
  const el = document.createElement('div')
  document.body.append(el)
  const app = createApp({ render: () => h(ChatComposerModelRouting, props as any) })
  apps.push(app)
  app.use(i18n)
  app.mount(el)
  await nextTick()
  return { props, selected, mode, close, settings, refresh }
}
const query = <T extends HTMLElement = HTMLElement>(selector: string) =>
  document.body.querySelector<T>(selector)!
async function key(element: HTMLElement, value: string, extra: KeyboardEventInit = {}) {
  element.dispatchEvent(new KeyboardEvent('keydown', { key: value, bubbles: true, ...extra }))
  await nextTick()
}
async function search(value: string) {
  const input = query<HTMLInputElement>('input')
  input.value = value
  input.dispatchEvent(new Event('input', { bubbles: true }))
  await nextTick()
  return input
}

describe('Native cascading model routing menu', () => {
  it('keeps the compact primary target mounted until a tap or click activates it', async () => {
    vi.spyOn(window, 'innerWidth', 'get').mockReturnValue(390)
    await mount()
    const single = query<HTMLButtonElement>('.routing-mode')
    single.dispatchEvent(new PointerEvent('pointerenter', { pointerType: 'touch' }))
    await nextTick()
    expect(query('[role="listbox"]')).toBeNull()
    single.click()
    await nextTick()
    expect(query('[role="listbox"]')).toBeTruthy()
    query<HTMLButtonElement>('[aria-label="Back to routing modes"]').click()
    await nextTick()
    expect(query('[role="listbox"]')).toBeNull()
  })
  it('preserves provider identity when two providers expose the same model id', async () => {
    const { selected } = await mount()
    expect(document.querySelectorAll('[role="option"]')).toHaveLength(3)
    const input = await search('provider-b')
    await key(input, 'ArrowDown')
    await key(input, 'Enter')
    expect(selected).toHaveBeenCalledExactlyOnceWith({
      model: 'shared-model',
      provider: 'provider-b',
    })
  })
  it('starts ArrowUp at the last available model when search has no active result', async () => {
    const { selected } = await mount()
    const input = query<HTMLInputElement>('input')
    await key(input, 'ArrowUp')
    await key(input, 'Enter')
    expect(selected).toHaveBeenCalledWith({ model: 'shared-model', provider: 'provider-b' })
  })
  it('does not change routing when merely opening or hovering the model submenu', async () => {
    const { selected, mode } = await mount({ modelRoutingMode: 'squilla_router' })
    query('.routing-mode').dispatchEvent(new Event('pointerenter'))
    await nextTick()
    expect(query('[role="listbox"]')).toBeTruthy()
    expect(selected).not.toHaveBeenCalled()
    expect(mode).not.toHaveBeenCalled()
  })
  it('lets a model selection request the direct-mode handoff from router mode', async () => {
    const { selected } = await mount({
      modelRoutingMode: 'squilla_router',
      newTaskModelDisabledReason: 'routing',
    })
    query<HTMLButtonElement>('.routing-mode').click()
    await nextTick()
    query<HTMLButtonElement>('[role="option"]:nth-child(2)').click()
    expect(selected).toHaveBeenCalledWith({ model: 'shared-model', provider: 'provider-a' })
  })
  it('preserves the single model default choice and explicit routing alternatives', async () => {
    const { selected, mode } = await mount()
    query<HTMLButtonElement>('[role="option"]').click()
    expect(selected).toHaveBeenCalledWith(null)
    query<HTMLButtonElement>('.routing-mode:nth-child(2)').click()
    expect(mode).toHaveBeenCalledWith('squilla_router')
    query<HTMLButtonElement>('.routing-mode:nth-child(3)').click()
    expect(mode).toHaveBeenCalledWith('llm_ensemble')
  })
  it('uses Escape to return one level first, then close, and ignores IME confirmation', async () => {
    const { close, selected } = await mount()
    const input = await search('Alpha')
    input.focus()
    await key(input, 'ArrowDown')
    await key(input, 'Enter', { isComposing: true })
    expect(selected).not.toHaveBeenCalled()
    await key(input, 'Escape')
    expect(query('[role="listbox"]')).toBeNull()
    expect(document.activeElement).toBe(query('.routing-mode'))
    expect(close).not.toHaveBeenCalled()
    await key(query('.routing-mode'), 'Escape')
    expect(close).toHaveBeenCalledOnce()
  })
  it('keeps unavailable selected models visible and prevents accidental replacement during partial failure', async () => {
    const { selected, refresh } = await mount({
      newTaskModelSelection: { model: 'offline-model', provider: 'provider-c' },
      newTaskModelsProviderErrors: [{ provider: 'provider-c', error: 'offline' }],
    })
    const missing = query<HTMLButtonElement>('[aria-selected="true"]')
    expect(missing.textContent).toContain('offline-model')
    expect(missing.getAttribute('aria-disabled')).toBe('true')
    missing.click()
    expect(selected).not.toHaveBeenCalled()
    expect(query('.routing-issue').textContent).toContain('provider-c')
    query<HTMLButtonElement>('.routing-retry').click()
    expect(refresh).toHaveBeenCalledOnce()
  })
  it('guards busy model and route controls without discarding focused elements', async () => {
    const { props, mode, selected } = await mount()
    const row = query<HTMLButtonElement>('.routing-mode:nth-child(2)')
    row.focus()
    props.busy = true
    await nextTick()
    expect(document.activeElement).toBe(row)
    row.click()
    query<HTMLButtonElement>('[role="option"]:nth-child(2)').click()
    expect(mode).not.toHaveBeenCalled()
    expect(selected).not.toHaveBeenCalled()
  })
  it('retains all routing modes and settings for an existing task without exposing a new-task picker', async () => {
    const { mode, settings } = await mount({ newTaskModelAvailable: false })
    expect(document.querySelectorAll('[role="menuitemradio"]')).toHaveLength(3)
    expect(query('[role="listbox"]')).toBeNull()
    query<HTMLButtonElement>('.routing-mode').click()
    expect(mode).toHaveBeenCalledWith('off')
    query<HTMLButtonElement>('.routing-settings').click()
    expect(settings).toHaveBeenCalledOnce()
  })
})
