// @vitest-environment happy-dom

import { createApp, h, nextTick, reactive } from 'vue'
import type { Component } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, describe, expect, it, vi } from 'vitest'
import en from '@/locales/en.json'
import {
  WORKSPACE_CHANGES_KEY,
  type WorkspaceChanges,
  type WorkspaceChangesReader,
  type WorkspaceFileDiff,
} from '@/modules/workspaceChanges'
import WorkspaceChangesPanel from './WorkspaceChangesPanel.vue'

function changes(overrides: Partial<WorkspaceChanges> = {}): WorkspaceChanges {
  return {
    available: true,
    availabilityReason: null,
    branch: 'main',
    detached: false,
    upstream: null,
    ahead: 0,
    behind: 0,
    totalCount: 1,
    truncated: false,
    entries: [
      {
        path: 'src/a.ts',
        previousPath: null,
        changeType: 'modified',
        staged: false,
        unstaged: true,
      },
    ],
    ...overrides,
  }
}

function diff(overrides: Partial<WorkspaceFileDiff> = {}): WorkspaceFileDiff {
  return {
    path: 'src/a.ts',
    staged: false,
    text: '@@ -1 +1 @@\n-const a = 1\n+const a = 2\n',
    truncated: false,
    binary: false,
    ...overrides,
  }
}

function reader(overrides: Partial<WorkspaceChangesReader> = {}): WorkspaceChangesReader {
  return {
    readChanges: vi.fn(async () => changes()),
    readDiff: vi.fn(async () => diff()),
    ...overrides,
  }
}

async function settle() {
  for (let index = 0; index < 6; index += 1) {
    await Promise.resolve()
    await nextTick()
  }
}

function mountPanel(
  port: WorkspaceChangesReader | null,
  props: Record<string, unknown> = { workspaceId: 'workspace-1', workspaceName: 'Project A' },
) {
  const element = document.createElement('div')
  document.body.append(element)
  const state = reactive({ ...props })
  const app = createApp({
    render: () => h(WorkspaceChangesPanel as Component, state),
  })
  app.use(createI18n({ legacy: false, locale: 'en', messages: { en } }))
  if (port) app.provide(WORKSPACE_CHANGES_KEY, port)
  app.mount(element)
  return {
    element,
    unmount: () => {
      app.unmount()
      element.remove()
    },
  }
}

function clickEntry(element: HTMLElement, path: string) {
  const button = [...element.querySelectorAll<HTMLButtonElement>('.wb-changes__entry')]
    .find(candidate => candidate.textContent?.includes(path))
  if (!button) throw new Error(`no entry button for ${path}`)
  button.click()
}

afterEach(() => {
  document.body.innerHTML = ''
})

describe('WorkspaceChangesPanel', () => {
  it('lists changed files and shows the selected diff', async () => {
    const port = reader({
      readChanges: vi.fn(async () => changes({
        entries: [
          { path: 'src/a.ts', previousPath: null, changeType: 'modified', staged: false, unstaged: true },
          { path: 'src/new.ts', previousPath: null, changeType: 'untracked', staged: false, unstaged: true },
        ],
        totalCount: 2,
      })),
    })
    const mounted = mountPanel(port)
    await settle()

    expect(mounted.element.textContent).toContain('src/a.ts')
    expect(mounted.element.textContent).toContain('src/new.ts')
    expect(mounted.element.textContent).toContain('main')
    expect(mounted.element.textContent).toContain('Select a file to review its diff.')
    // The truncation notice is a status for a bounded list, not a permanent banner.
    expect(mounted.element.textContent).not.toContain('Showing')

    clickEntry(mounted.element, 'src/new.ts')
    await settle()

    expect(port.readDiff).toHaveBeenCalledWith({
      workspaceId: 'workspace-1',
      path: 'src/new.ts',
      staged: false,
    })
    expect(mounted.element.textContent).toContain('const a = 2')
    mounted.unmount()
  })

  it('reads the staged half when a file is only staged', async () => {
    const port = reader({
      readChanges: vi.fn(async () => changes({
        entries: [
          { path: 'src/staged.ts', previousPath: null, changeType: 'added', staged: true, unstaged: false },
        ],
      })),
    })
    const mounted = mountPanel(port)
    await settle()

    clickEntry(mounted.element, 'src/staged.ts')
    await settle()

    expect(port.readDiff).toHaveBeenCalledWith({
      workspaceId: 'workspace-1',
      path: 'src/staged.ts',
      staged: true,
    })
    mounted.unmount()
  })

  it('explains an unavailable repository instead of showing an empty list', async () => {
    const mounted = mountPanel(reader({
      readChanges: vi.fn(async () => changes({
        available: false,
        availabilityReason: 'not_repository',
        branch: null,
        totalCount: 0,
        entries: [],
      })),
    }))
    await settle()

    expect(mounted.element.textContent).toContain('Git status is unavailable')
    expect(mounted.element.textContent).toContain('This workspace is not a Git repository.')
    mounted.unmount()
  })

  it('reports an unreadable working tree and offers a retry', async () => {
    const readChanges = vi.fn()
      .mockRejectedValueOnce(new Error('gateway offline'))
      .mockResolvedValueOnce(changes())
    const mounted = mountPanel(reader({ readChanges }))
    await settle()

    expect(mounted.element.textContent).toContain('gateway offline')
    const retry = [...mounted.element.querySelectorAll<HTMLButtonElement>('button')]
      .find(button => button.textContent?.includes('Retry'))
    retry?.click()
    await settle()

    expect(readChanges).toHaveBeenCalledTimes(2)
    expect(mounted.element.textContent).toContain('src/a.ts')
    mounted.unmount()
  })

  it('renders an empty working tree as a status, not an error', async () => {
    const mounted = mountPanel(reader({
      readChanges: vi.fn(async () => changes({ totalCount: 0, entries: [] })),
    }))
    await settle()

    expect(mounted.element.textContent).toContain('No changes in this workspace.')
    expect(mounted.element.querySelector('[role="alert"]')).toBeNull()
    mounted.unmount()
  })

  it('renders the patch as tinted rows with line numbers from the hunk header', async () => {
    const mounted = mountPanel(reader({
      readDiff: vi.fn(async () => diff({
        text: [
          'diff --git a/src/a.ts b/src/a.ts',
          'index 1111111..2222222 100644',
          '--- a/src/a.ts',
          '+++ b/src/a.ts',
          '@@ -10,2 +10,2 @@',
          ' const keep = 1',
          '-const a = 1',
          '+const a = 2',
          '',
        ].join('\n'),
      })),
    }))
    await settle()
    clickEntry(mounted.element, 'src/a.ts')
    await settle()

    const row = (kind: string) => [...mounted.element.querySelectorAll('.wb-changes__line')]
      .find(node => node.getAttribute('data-kind') === kind)
    const gutters = (kind: string) => [...(row(kind)?.querySelectorAll('.wb-changes__gutter') || [])]
      .map(node => node.textContent)

    // The hunk starts at line 10, then one context line advances both sides.
    expect(row('removed')?.textContent).toContain('const a = 1')
    expect(gutters('removed')).toEqual(['11', ''])
    expect(gutters('added')).toEqual(['', '11'])
    expect(row('added')?.querySelector('.wb-changes__marker')?.textContent).toBe('+')
    expect(row('removed')?.querySelector('.wb-changes__marker')?.textContent).toBe('-')
    // File headers are their own row kind so they are not tinted as content.
    expect(row('meta')?.textContent).toContain('diff --git a/src/a.ts b/src/a.ts')
    mounted.unmount()
  })

  it('labels the two number columns and keeps hunk rows aligned across the row', async () => {
    const mounted = mountPanel(reader({
      readChanges: vi.fn(async () => changes({
        entries: [
          { path: 'src/a.ts', previousPath: null, changeType: 'modified', staged: false, unstaged: true },
          { path: 'src/b.ts', previousPath: null, changeType: 'modified', staged: false, unstaged: true },
        ],
        totalCount: 2,
      })),
      readDiff: vi.fn(async () => diff({
        text: '@@ -1,1 +1,1 @@\n-const a = 1\n+const a = 2\n',
      })),
    }))
    await settle()
    clickEntry(mounted.element, 'src/a.ts')
    await settle()

    const head = mounted.element.querySelector('.wb-changes__line--head')
    expect(head?.textContent).toContain('Old')
    expect(head?.textContent).toContain('New')

    // A hunk band spans the row instead of leaving an empty gutter column.
    const hunk = [...mounted.element.querySelectorAll('.wb-changes__line')]
      .find(node => node.getAttribute('data-kind') === 'hunk')
    expect(hunk?.querySelectorAll('.wb-changes__gutter').length).toBe(0)
    expect(hunk?.textContent).toContain('@@ -1,1 +1,1 @@')
    mounted.unmount()
  })

  it('moves between files with the arrow keys', async () => {
    const mounted = mountPanel(reader({
      readChanges: vi.fn(async () => changes({
        entries: [
          { path: 'src/a.ts', previousPath: null, changeType: 'modified', staged: false, unstaged: true },
          { path: 'src/b.ts', previousPath: null, changeType: 'modified', staged: false, unstaged: true },
        ],
        totalCount: 2,
      })),
    }))
    await settle()

    const buttons = [...mounted.element.querySelectorAll<HTMLButtonElement>('.wb-changes__entry')]
    buttons[0].focus()
    buttons[0].dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    await nextTick()
    expect(document.activeElement).toBe(buttons[1])

    buttons[1].dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowUp', bubbles: true }))
    await nextTick()
    expect(document.activeElement).toBe(buttons[0])
    mounted.unmount()
  })

  it('reports a binary file without rendering a text diff', async () => {
    const mounted = mountPanel(reader({
      readDiff: vi.fn(async () => diff({ text: 'Binary files a and b differ', binary: true })),
    }))
    await settle()

    clickEntry(mounted.element, 'src/a.ts')
    await settle()

    expect(mounted.element.textContent).toContain('This file is binary; no text diff is available.')
    expect(mounted.element.querySelector('.wb-changes__code')).toBeNull()
    mounted.unmount()
  })

  it('notes a truncated change list', async () => {
    const mounted = mountPanel(reader({
      readChanges: vi.fn(async () => changes({ totalCount: 12, truncated: true })),
    }))
    await settle()

    expect(mounted.element.textContent).toContain('Showing 1 of 12 changed files.')
    mounted.unmount()
  })

  it('notes truncation without hiding the patch', async () => {
    const mounted = mountPanel(reader({
      readDiff: vi.fn(async () => diff({ truncated: true })),
    }))
    await settle()

    clickEntry(mounted.element, 'src/a.ts')
    await settle()

    expect(mounted.element.textContent)
      .toContain('This diff was truncated to keep the panel responsive.')
    expect(mounted.element.querySelector('.wb-changes__code')).not.toBeNull()
    mounted.unmount()
  })

  it('stays honest when no reader is provided', async () => {
    const mounted = mountPanel(null)
    await settle()

    expect(mounted.element.textContent).toContain('Workspace changes are unavailable.')
    mounted.unmount()
  })
})
