// @vitest-environment happy-dom

import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import i18n from '@/i18n'
import ChatComposer from './ChatComposer.vue'

const BASE_PROPS = {
  modelValue: 'describe this image',
  'onUpdate:modelValue': () => {},
  attachments: [],
  busySendMode: 'queue',
  hasSendContent: true,
  isStreaming: false,
  canStop: false,
  isNewLanding: false,
  placeholder: 'Send a message',
  sendButtonTitle: 'Send',
  runMode: 'safe',
  allowedRunModes: ['safe', 'full'],
  runModeLocked: false,
  runModeLockMessage: '',
  sessionRoutingMode: 'llm_ensemble',
  sessionRoutingBusy: false,
  routerVisualEffectsEnabled: true,
  codingModeEnabled: false,
  codingModeSettingsBusy: false,
  voiceBusy: false,
  voiceRecording: false,
  voiceReady: true,
}

afterEach(() => {
  document.body.innerHTML = ''
  i18n.global.locale.value = 'en'
})

describe('ChatComposer image-send guard', () => {
  it('announces Sending and disables duplicate submission while receipt is pending', async () => {
    const onSend = vi.fn()
    const el = document.createElement('div')
    document.body.appendChild(el)
    const app = createApp(ChatComposer, { ...BASE_PROPS, sendPending: true, onSend })
    app.use(i18n)
    app.mount(el)
    await nextTick()
    const send = el.querySelector<HTMLButtonElement>('.chat-send-btn')!
    expect(send.disabled).toBe(true)
    expect(send.getAttribute('aria-busy')).toBe('true')
    expect(el.querySelector('.chat-composer-send-pending')?.textContent).toContain('Sending')
    expect(el.querySelector('.chat-composer-send-pending [role="status"]')).toBeTruthy()
    send.click()
    expect(onSend).not.toHaveBeenCalled()
    app.unmount()
  })

  it('keeps Stop available while a pending send waits for its receipt', async () => {
    const onStop = vi.fn()
    const el = document.createElement('div')
    document.body.appendChild(el)
    const app = createApp(ChatComposer, { ...BASE_PROPS, sendPending: true, canStop: true, onStop })
    app.use(i18n)
    app.mount(el)
    await nextTick()
    const stop = el.querySelector<HTMLButtonElement>('.chat-send-btn')!
    expect(stop.disabled).toBe(false)
    expect(el.querySelector('.chat-composer-send-pending')?.textContent).toContain('Sending')
    stop.click()
    expect(onStop).toHaveBeenCalledOnce()
    app.unmount()
  })

  it('announces the block accessibly and prevents the send control from firing', async () => {
    const onSend = vi.fn()
    const message = 'Ensemble image input is unavailable.'
    const el = document.createElement('div')
    document.body.appendChild(el)
    const app = createApp(ChatComposer, {
      ...BASE_PROPS,
      sendBlockedMessage: message,
      onSend,
    })
    app.use(i18n)
    app.mount(el)
    await nextTick()

    const status = el.querySelector<HTMLElement>('#chat-composer-send-status')
    const textarea = el.querySelector<HTMLTextAreaElement>('.chat-textarea')
    const send = el.querySelector<HTMLButtonElement>('.chat-send-btn')

    expect(status?.textContent).toBe(message)
    expect(status?.getAttribute('role')).toBe('status')
    expect(status?.getAttribute('aria-live')).toBe('polite')
    expect(status?.getAttribute('aria-atomic')).toBe('true')
    expect(textarea?.getAttribute('aria-describedby')).toBe(status?.id)
    expect(send?.getAttribute('aria-describedby')).toBe(status?.id)
    expect(send?.title).toBe(message)
    expect(send?.disabled).toBe(true)
    send?.click()
    expect(onSend).not.toHaveBeenCalled()

    app.unmount()
  })

  it('keeps the send control enabled when no guard message is present', async () => {
    const onSend = vi.fn()
    const el = document.createElement('div')
    document.body.appendChild(el)
    const app = createApp(ChatComposer, { ...BASE_PROPS, onSend })
    app.use(i18n)
    app.mount(el)
    await nextTick()

    const send = el.querySelector<HTMLButtonElement>('.chat-send-btn')
    expect(el.querySelector('#chat-composer-send-status')).toBeNull()
    expect(send?.disabled).toBe(false)
    send?.click()
    expect(onSend).toHaveBeenCalledOnce()

    app.unmount()
  })

  it('disables sending during a routing mutation without mounting a status row', async () => {
    const onSend = vi.fn()
    const el = document.createElement('div')
    document.body.appendChild(el)
    const app = createApp(ChatComposer, {
      ...BASE_PROPS,
      sessionRoutingBusy: true,
      onSend,
    })
    app.use(i18n)
    app.mount(el)
    await nextTick()

    const textarea = el.querySelector<HTMLTextAreaElement>('.chat-textarea')
    const send = el.querySelector<HTMLButtonElement>('.chat-send-btn')
    expect(el.querySelector('#chat-composer-send-status')).toBeNull()
    expect(textarea?.getAttribute('aria-describedby')).toBeNull()
    expect(send?.disabled).toBe(true)
    expect(send?.getAttribute('aria-busy')).toBe('true')
    expect(send?.title).toBe('Model routing is being updated. Wait before sending.')
    send?.click()
    expect(onSend).not.toHaveBeenCalled()

    app.unmount()
  })
})

describe('ChatComposer selected skill queue controls', () => {
  const selectedSkills = [{ name: 'synthetic-table', instanceId: 'instance-a', digest: 'digest-a' }]

  async function mountBusyComposer(overrides: Record<string, unknown> = {}) {
    const onSend = vi.fn()
    const onStop = vi.fn()
    const el = document.createElement('div')
    document.body.appendChild(el)
    const app = createApp(ChatComposer, {
      ...BASE_PROPS,
      selectedSkills,
      isStreaming: true,
      canStop: true,
      busySendMode: 'steer',
      sendButtonTitle: 'Steer the current response',
      onSend,
      onStop,
      ...overrides,
    })
    app.use(i18n)
    app.mount(el)
    await nextTick()
    return { app, el, onSend, onStop }
  }

  it('allows a busy skill draft to send to the queue and keeps Stop available', async () => {
    const { app, el, onSend, onStop } = await mountBusyComposer()
    const send = el.querySelector<HTMLButtonElement>('.chat-send-btn.btn--primary')
    const stop = el.querySelector<HTMLButtonElement>('.chat-send-btn.btn--danger')
    expect(send?.disabled).toBe(false)
    expect(send?.title).toBe(i18n.global.t('chat.sendQueues'))
    expect(stop?.disabled).toBe(false)
    send?.click()
    expect(onSend).toHaveBeenCalledOnce()
    expect(onStop).not.toHaveBeenCalled()
    stop?.click()
    expect(onStop).toHaveBeenCalledOnce()
    app.unmount()
  })

  it.each([
    { sendPending: true },
    { sessionRoutingBusy: true },
    { inputDisabled: true },
    { sendBlockedMessage: 'Synthetic send restriction' },
  ])('preserves Stop while the skill queue send is blocked: %j', async overrides => {
    const { app, el, onSend, onStop } = await mountBusyComposer(overrides)
    const send = el.querySelector<HTMLButtonElement>('.chat-send-btn.btn--primary')
    const stop = el.querySelector<HTMLButtonElement>('.chat-send-btn.btn--danger')
    expect(send?.disabled).toBe(true)
    send?.click()
    expect(onSend).not.toHaveBeenCalled()
    expect(stop?.disabled).toBe(false)
    stop?.click()
    expect(onStop).toHaveBeenCalledOnce()
    app.unmount()
  })

  it.each([
    { selectedSkills: [] },
    { modelValue: '', hasSendContent: false },
    { stopTargetsPlanRun: true },
    { replanActive: true },
  ])('retains the existing stop-only controls outside skill queue input: %j', async overrides => {
    const { app, el, onStop } = await mountBusyComposer(overrides)
    expect(el.querySelector('.chat-send-btn.btn--primary')).toBeNull()
    el.querySelector<HTMLButtonElement>('.chat-send-btn.btn--danger')?.click()
    expect(onStop).toHaveBeenCalledOnce()
    app.unmount()
  })
})
