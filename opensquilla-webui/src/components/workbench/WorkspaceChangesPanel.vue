<template>
  <div class="wb-changes">
    <header class="wb-changes__bar">
      <div class="wb-changes__meta">
        <span v-if="branchLabel" class="wb-changes__branch">{{ branchLabel }}</span>
        <span v-if="divergenceLabel" class="wb-changes__divergence">{{ divergenceLabel }}</span>
        <span v-if="changes?.available" class="wb-changes__count">
          {{ t('workbench.changes.fileCount', { count: changes.totalCount }) }}
        </span>
        <span
          v-if="changes?.available"
          class="wb-changes__count"
          :title="t('workbench.changes.diffStats', { added: changes.addedLines, removed: changes.removedLines })"
          :aria-label="t('workbench.changes.diffStats', { added: changes.addedLines, removed: changes.removedLines })"
        >
          <span class="wb-changes__added">+{{ changes.addedLines }}</span>
          <span class="wb-changes__removed">-{{ changes.removedLines }}</span>
        </span>
      </div>
      <button
        type="button"
        class="wb-changes__action"
        :disabled="loading"
        @click="reload()"
      >
        <Icon name="refresh" :size="13" />
        <span>{{ t('workbench.changes.refresh') }}</span>
      </button>
    </header>

    <p v-if="loading && !changes" class="wb-changes__note" role="status">
      {{ t('workbench.changes.loading') }}
    </p>

    <div v-else-if="errorMessage" class="wb-changes__note wb-changes__note--error" role="alert">
      <span>{{ errorMessage }}</span>
      <button type="button" class="wb-changes__action" @click="reload()">
        {{ t('workbench.changes.retry') }}
      </button>
    </div>

    <div v-else-if="changes && !changes.available" class="wb-changes__note" role="status">
      <strong>{{ t('workbench.changes.unavailableTitle') }}</strong>
      <span>{{ unavailableDetail }}</span>
    </div>

    <p v-else-if="changes && changes.entries.length === 0" class="wb-changes__note" role="status">
      {{ t('workbench.changes.empty') }}
    </p>

    <p
      v-else-if="changes && changes.truncated"
      class="wb-changes__note wb-changes__note--truncated"
      role="status"
    >
      {{ t('workbench.changes.truncated', {
        count: changes.entries.length,
        total: changes.totalCount,
      }) }}
    </p>

    <div v-if="changes && changes.entries.length > 0" class="wb-changes__body">
      <div class="wb-changes__list" role="list" :aria-label="t('workbench.changes.listLabel')">
        <section v-for="group in groups" :key="group.key" class="wb-changes__group">
          <h4 class="wb-changes__group-head">
            <Icon :name="group.icon" :size="12" />
            <span>{{ group.label }}</span>
            <span class="wb-changes__group-count">{{ group.entries.length }}</span>
          </h4>
          <button
            v-for="entry in group.entries"
            :key="entryKey(entry)"
            type="button"
            class="wb-changes__entry"
            :class="{ 'is-selected': entryKey(entry) === selectedKey }"
            :aria-pressed="entryKey(entry) === selectedKey"
            :data-entry-key="entryKey(entry)"
            @click="select(entry)"
            @keydown.down.prevent="focusSibling(entry, 1)"
            @keydown.up.prevent="focusSibling(entry, -1)"
            @keydown.home.prevent="focusEdge('first')"
            @keydown.end.prevent="focusEdge('last')"
          >
            <span
              class="wb-changes__type"
              :data-type="entry.changeType"
              :aria-label="t(`workbench.changes.types.${entry.changeType}`)"
              :title="t(`workbench.changes.types.${entry.changeType}`)"
            >{{ typeLetter(entry.changeType) }}</span>
            <span class="wb-changes__path">
              <span v-if="pathParts(entry.path).dir" class="wb-changes__path-dir">{{ pathParts(entry.path).dir }}</span>
              <span class="wb-changes__path-base">{{ pathParts(entry.path).base }}</span>
            </span>
            <span v-if="entryStats(entry)" class="wb-changes__stats">
              <span class="wb-changes__added">+{{ entry.addedLines }}</span>
              <span class="wb-changes__removed">-{{ entry.removedLines }}</span>
            </span>
            <span v-if="entry.staged && entry.unstaged" class="wb-changes__both">
              {{ t('workbench.changes.bothHalves') }}
            </span>
            <Icon v-else-if="entry.staged" name="check" :size="12" class="wb-changes__check" />
          </button>
        </section>
      </div>

      <section
        class="wb-changes__diff"
        :aria-busy="diffLoading"
        :aria-label="t('workbench.changes.diffLabel')"
      >
        <p v-if="!selectedEntry" class="wb-changes__note">{{ t('workbench.changes.selectPrompt') }}</p>
        <p v-else-if="diffLoading" class="wb-changes__note" role="status">
          {{ t('workbench.changes.diffLoading') }}
        </p>
        <div v-else-if="diffError" class="wb-changes__note wb-changes__note--error" role="alert">
          {{ diffError }}
        </div>
        <p v-else-if="diff && diff.binary" class="wb-changes__note" role="status">
          {{ t('workbench.changes.diffBinary') }}
        </p>
        <p v-else-if="diff && !diff.text.trim()" class="wb-changes__note" role="status">
          {{ t('workbench.changes.diffEmpty') }}
        </p>
        <template v-else-if="diff">
          <div class="wb-changes__diff-head">
            <span class="wb-changes__diff-path">{{ diff.path }}</span>
            <span class="wb-changes__diff-side">
              {{ diff.staged ? t('workbench.changes.staged') : t('workbench.changes.unstaged') }}
            </span>
            <span
              class="wb-changes__diff-lines"
              :title="t('workbench.changes.diffStats', { added: addedLines, removed: removedLines })"
              :aria-label="t('workbench.changes.diffStats', { added: addedLines, removed: removedLines })"
            >
              <span class="wb-changes__added">+{{ addedLines }}</span>
              <span class="wb-changes__removed">-{{ removedLines }}</span>
            </span>
          </div>
          <p v-if="diff.truncated" class="wb-changes__note" role="status">
            {{ t('workbench.changes.diffTruncated') }}
          </p>
          <div class="wb-changes__code">
            <!-- Two number columns are the convention, but they are not
                 self-explanatory, so the columns are labelled once here. -->
            <div class="wb-changes__line wb-changes__line--head" aria-hidden="true">
              <span class="wb-changes__gutter">{{ t('workbench.changes.oldLineNumber') }}</span>
              <span class="wb-changes__gutter">{{ t('workbench.changes.newLineNumber') }}</span>
              <span class="wb-changes__marker" />
            </div>
            <div
              v-for="(line, index) in diffLines"
              :key="index"
              class="wb-changes__line"
              :data-kind="line.kind"
            >
              <template v-if="hasGutters(line)">
                <span class="wb-changes__gutter" aria-hidden="true">{{ line.oldNumber ?? '' }}</span>
                <span class="wb-changes__gutter" aria-hidden="true">{{ line.newNumber ?? '' }}</span>
                <span class="wb-changes__marker" aria-hidden="true">{{ line.marker }}</span>
              </template>
              <code class="wb-changes__line-code">{{ line.content }}</code>
            </div>
          </div>
        </template>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, inject, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import {
  WORKSPACE_CHANGES_KEY,
  type WorkspaceChangeEntry,
  type WorkspaceChangeType,
  type WorkspaceChanges,
  type WorkspaceChangesReader,
  type WorkspaceFileDiff,
} from '@/modules/workspaceChanges'

const props = defineProps<{
  workspaceId: string
  workspaceName?: string
}>()

const { t } = useI18n()
const reader = inject<WorkspaceChangesReader | null>(WORKSPACE_CHANGES_KEY, null)

const changes = ref<WorkspaceChanges | null>(null)
const loading = ref(false)
const errorMessage = ref('')
const selectedKey = ref('')
const diff = ref<WorkspaceFileDiff | null>(null)
const diffLoading = ref(false)
const diffError = ref('')

// One glyph per change type keeps the row scannable; the localized name stays
// available through the chip's accessible label.
const TYPE_LETTER: Record<WorkspaceChangeType, string> = {
  added: 'A',
  modified: 'M',
  deleted: 'D',
  renamed: 'R',
  copied: 'C',
  typeChanged: 'T',
  unmerged: 'U',
  untracked: '?',
  unknown: '·',
}

type DiffLineKind = 'context' | 'added' | 'removed' | 'hunk' | 'meta'

interface DiffLine {
  kind: DiffLineKind
  oldNumber: number | null
  newNumber: number | null
  marker: string
  content: string
}

const HUNK_HEADER_RE = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/
const FILE_HEADER_PREFIXES = [
  'diff --git ',
  'index ',
  'new file mode ',
  'deleted file mode ',
  'old mode ',
  'new mode ',
  'similarity index ',
  'rename from ',
  'rename to ',
  'Binary files ',
  'GIT binary patch',
]

let requestToken = 0

const selectedEntry = computed<WorkspaceChangeEntry | null>(() => {
  if (!changes.value || !selectedKey.value) return null
  return changes.value.entries.find(entry => entryKey(entry) === selectedKey.value) || null
})

const branchLabel = computed(() => {
  const value = changes.value
  if (!value || !value.available) return ''
  if (value.detached) return t('workbench.changes.detached')
  return value.branch || ''
})

const divergenceLabel = computed(() => {
  const value = changes.value
  if (!value || !value.available) return ''
  if (!value.ahead && !value.behind) return ''
  return t('workbench.changes.divergence', {
    ahead: value.ahead,
    behind: value.behind,
  })
})

const groups = computed(() => {
  const value = changes.value
  if (!value) return []
  const untracked: WorkspaceChangeEntry[] = []
  const staged: WorkspaceChangeEntry[] = []
  const unstaged: WorkspaceChangeEntry[] = []
  for (const entry of value.entries) {
    if (entry.changeType === 'untracked') untracked.push(entry)
    else if (entry.staged) staged.push(entry)
    else unstaged.push(entry)
  }
  return [
    { key: 'staged', icon: 'check' as const, label: t('workbench.changes.groupStaged'), entries: staged },
    { key: 'unstaged', icon: 'pencil' as const, label: t('workbench.changes.groupUnstaged'), entries: unstaged },
    { key: 'untracked', icon: 'plus' as const, label: t('workbench.changes.groupUntracked'), entries: untracked },
  ].filter(group => group.entries.length > 0)
})

/**
 * Split a unified diff into renderable rows.
 *
 * Line numbers come from the hunk headers rather than being counted from the
 * top, because a hunk never starts at line 1. Each row carries its kind so the
 * stylesheet can tint the whole row instead of only its text — a tinted row is
 * what makes a patch readable at a glance, and it keeps the code itself in the
 * normal foreground colour.
 */
const diffLines = computed<DiffLine[]>(() => {
  const value = diff.value
  if (!value || value.binary || !value.text) return []
  const rows: DiffLine[] = []
  let oldNumber = 0
  let newNumber = 0
  const push = (
    kind: DiffLineKind,
    text: string,
    oldLine: number | null,
    newLine: number | null,
  ) => {
    rows.push({
      kind,
      oldNumber: oldLine,
      newNumber: newLine,
      marker: kind === 'added' ? '+' : kind === 'removed' ? '-' : text.slice(0, 1) === ' ' ? ' ' : '',
      content: kind === 'added' || kind === 'removed' ? text.slice(1) : text,
    })
  }
  for (const raw of value.text.split('\n')) {
    const hunk = HUNK_HEADER_RE.exec(raw)
    if (hunk) {
      oldNumber = Number(hunk[1])
      newNumber = Number(hunk[2])
      push('hunk', raw, null, null)
      continue
    }
    if (raw.startsWith('---') || raw.startsWith('+++')) {
      push('meta', raw, null, null)
      continue
    }
    if (FILE_HEADER_PREFIXES.some(prefix => raw.startsWith(prefix))) {
      push('meta', raw, null, null)
      continue
    }
    if (raw.startsWith('+')) {
      push('added', raw, null, newNumber++)
      continue
    }
    if (raw.startsWith('-')) {
      push('removed', raw, oldNumber++, null)
      continue
    }
    if (raw.startsWith(' ')) {
      push('context', raw, oldNumber++, newNumber++)
      continue
    }
    // Trailing "\ No newline at end of file" and any other stray line.
    push('meta', raw, null, null)
  }
  return rows
})

const addedLines = computed(() => countLines(diff.value?.text, '+'))
const removedLines = computed(() => countLines(diff.value?.text, '-'))

const unavailableDetail = computed(() => {
  const reason = changes.value?.availabilityReason
  switch (reason) {
    case 'git_unavailable':
      return t('workbench.changes.reasonGitUnavailable')
    case 'not_repository':
      return t('workbench.changes.reasonNotRepository')
    case 'timed_out':
      return t('workbench.changes.reasonTimedOut')
    default:
      return t('workbench.changes.reasonFailed')
  }
})

function typeLetter(type: WorkspaceChangeType): string {
  return TYPE_LETTER[type] ?? '·'
}

/** Count only real diff content lines, not the `+++`/`---` file headers. */
function countLines(text: string | undefined, marker: '+' | '-'): number {
  if (!text) return 0
  let total = 0
  for (const line of text.split('\n')) {
    if (!line.startsWith(marker)) continue
    if (line.startsWith(`${marker}${marker}${marker}`)) continue
    total += 1
  }
  return total
}

/** Counts are shown only when both halves are known; `0/0` for a binary file
 * would claim a measurement Git did not make. */
function entryStats(entry: WorkspaceChangeEntry): boolean {
  return Number.isFinite(entry.addedLines) && Number.isFinite(entry.removedLines)
}

/** File headers and hunk bands span the row: an empty gutter reads as a
 * misaligned column. */
function hasGutters(line: DiffLine): boolean {
  return line.kind === 'context' || line.kind === 'added' || line.kind === 'removed'
}

function entryButtons(): HTMLButtonElement[] {
  return [...document.querySelectorAll<HTMLButtonElement>('.wb-changes__entry')]
}

function focusSibling(entry: WorkspaceChangeEntry, offset: number) {
  const buttons = entryButtons()
  const index = buttons.findIndex(button => button.dataset.entryKey === entryKey(entry))
  const next = buttons[index + offset]
  next?.focus()
}

function focusEdge(edge: 'first' | 'last') {
  const buttons = entryButtons()
  const target = edge === 'first' ? buttons[0] : buttons[buttons.length - 1]
  target?.focus()
}

function pathParts(path: string): { dir: string; base: string } {
  const index = path.lastIndexOf('/')
  if (index === -1) return { dir: '', base: path }
  return { dir: path.slice(0, index + 1), base: path.slice(index + 1) }
}

function entryKey(entry: WorkspaceChangeEntry): string {
  return `${entry.path}::${entry.staged ? 'staged' : 'unstaged'}`
}

async function reload(preserveSelection = false) {
  const activeReader = reader
  if (!activeReader) {
    errorMessage.value = t('workbench.changes.readerUnavailable')
    return
  }
  const token = ++requestToken
  loading.value = true
  if (!preserveSelection) {
    selectedKey.value = ''
    diff.value = null
    diffError.value = ''
  }
  try {
    const result = await activeReader.readChanges(props.workspaceId)
    if (token !== requestToken) return
    changes.value = result
    errorMessage.value = ''
    if (
      selectedKey.value
      && !result.entries.some(entry => entryKey(entry) === selectedKey.value)
    ) {
      selectedKey.value = ''
      diff.value = null
    }
  } catch (error) {
    if (token !== requestToken) return
    changes.value = null
    errorMessage.value = error instanceof Error
      ? error.message
      : t('workbench.changes.loadFailed')
  } finally {
    if (token === requestToken) loading.value = false
  }
}

async function select(entry: WorkspaceChangeEntry) {
  const activeReader = reader
  const key = entryKey(entry)
  selectedKey.value = key
  diff.value = null
  diffError.value = ''
  if (!activeReader) {
    diffError.value = t('workbench.changes.readerUnavailable')
    return
  }
  const token = ++requestToken
  diffLoading.value = true
  try {
    // The staged flag decides which half of the change is shown; an untracked
    // file has only worktree content, so it is always read unstaged.
    const result = await activeReader.readDiff({
      workspaceId: props.workspaceId,
      path: entry.path,
      staged: entry.staged && !entry.unstaged,
    })
    if (token !== requestToken) return
    diff.value = result
  } catch (error) {
    if (token !== requestToken) return
    diffError.value = error instanceof Error
      ? error.message
      : t('workbench.changes.diffFailed')
  } finally {
    if (token === requestToken) diffLoading.value = false
  }
}

watch(() => props.workspaceId, () => { void reload() }, { immediate: true })
</script>

<style scoped>
.wb-changes {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  height: 100%;
  min-height: 0;
  padding: 0.5rem 0.75rem 0.75rem;
  color: var(--text);
  font-size: 0.8125rem;
}

.wb-changes__bar {
  display: flex;
  flex: none;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  min-height: 1.75rem;
}

.wb-changes__meta {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  min-width: 0;
}

.wb-changes__branch {
  overflow: hidden;
  color: var(--text-muted);
  font-family: var(--font-mono);
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* Small labels on a surface need the secondary text token: --text-dim is a
   low-emphasis tier that drops below 4.5:1 on several themes. */
.wb-changes__divergence,
.wb-changes__count {
  flex: none;
  color: var(--text-muted);
}

.wb-changes__action {
  display: inline-flex;
  flex: none;
  gap: 0.25rem;
  align-items: center;
  padding: 0.125rem 0.5rem;
  color: var(--text-muted);
  font: inherit;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  cursor: pointer;
}

.wb-changes__action:disabled {
  cursor: default;
  opacity: 0.6;
}

.wb-changes__action:focus-visible,
.wb-changes__entry:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}

.wb-changes__note {
  display: flex;
  flex: none;
  flex-wrap: wrap;
  gap: 0.5rem;
  align-items: center;
  margin: 0;
  color: var(--text-muted);
}

.wb-changes__note--error {
  color: var(--danger);
}

.wb-changes__body {
  display: grid;
  flex: 1;
  gap: 0.625rem;
  grid-template-rows: minmax(0, 2fr) minmax(0, 3fr);
  min-height: 0;
}

.wb-changes__list {
  overflow-y: auto;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
}

.wb-changes__group-head {
  display: flex;
  position: sticky;
  top: 0;
  z-index: 1;
  gap: 0.375rem;
  align-items: center;
  margin: 0;
  padding: 0.25rem 0.5rem;
  color: var(--text-muted);
  font-size: 0.6875rem;
  font-weight: 600;
  letter-spacing: 0.02em;
  text-transform: uppercase;
  background: var(--bg-elevated);
  border-bottom: 1px solid var(--border);
}

.wb-changes__group-count {
  margin-left: auto;
  padding: 0 0.3125rem;
  font-weight: 500;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}

.wb-changes__entry {
  display: flex;
  gap: 0.5rem;
  align-items: center;
  width: 100%;
  padding: 0.25rem 0.5rem;
  color: var(--text);
  font: inherit;
  text-align: left;
  background: none;
  border: 0;
  border-left: 2px solid transparent;
  cursor: pointer;
}

.wb-changes__entry:hover {
  background: var(--bg-elevated);
}

.wb-changes__entry.is-selected {
  background: var(--bg-elevated);
  border-left-color: var(--accent);
}

.wb-changes__type {
  flex: none;
  width: 1.125rem;
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-weight: 600;
  text-align: center;
}

.wb-changes__type[data-type="added"],
.wb-changes__type[data-type="untracked"] {
  color: var(--syntax-string);
}

.wb-changes__type[data-type="deleted"],
.wb-changes__type[data-type="unmerged"] {
  color: var(--danger);
}

.wb-changes__type[data-type="renamed"],
.wb-changes__type[data-type="copied"] {
  color: var(--accent);
}

.wb-changes__path {
  display: flex;
  overflow: hidden;
  min-width: 0;
  font-family: var(--font-mono);
  white-space: nowrap;
}

.wb-changes__path-dir {
  overflow: hidden;
  color: var(--text-muted);
  text-overflow: ellipsis;
}

.wb-changes__path-base {
  flex: none;
  color: var(--text);
}

.wb-changes__stats {
  display: flex;
  flex: none;
  gap: 0.25rem;
  margin-left: auto;
  font-family: var(--font-mono);
  font-size: 0.6875rem;
}

.wb-changes__stats + .wb-changes__both,
.wb-changes__stats + .wb-changes__check {
  margin-left: 0.375rem;
}

.wb-changes__both {
  flex: none;
  margin-left: auto;
  padding: 0 0.3125rem;
  color: var(--text-muted);
  font-size: 0.6875rem;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}

.wb-changes__check {
  flex: none;
  margin-left: auto;
  color: var(--syntax-string);
}

.wb-changes__diff {
  display: flex;
  flex-direction: column;
  gap: 0.375rem;
  min-height: 0;
}

.wb-changes__diff-head {
  display: flex;
  flex: none;
  gap: 0.5rem;
  align-items: center;
  padding: 0 0.125rem;
  font-family: var(--font-mono);
  font-size: 0.75rem;
}

.wb-changes__diff-path {
  overflow: hidden;
  color: var(--text);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.wb-changes__diff-side {
  flex: none;
  padding: 0 0.3125rem;
  color: var(--text-muted);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}

.wb-changes__diff-lines {
  display: flex;
  flex: none;
  gap: 0.375rem;
  margin-left: auto;
}

/* The +N/-N counts are metadata; the patch itself carries the add/remove
   colour. Colouring 12px counts with --syntax-string lands at 4.46:1 on one
   theme, just under the 4.5:1 floor for body text. */
.wb-changes__added,
.wb-changes__removed {
  color: var(--text-muted);
}

.wb-changes__code {
  overflow: auto;
  flex: 1;
  min-height: 0;
  padding: 0.25rem 0;
  font-family: var(--font-mono);
  font-size: 0.75rem;
  line-height: 1.5;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
}

.wb-changes__line {
  display: flex;
  align-items: baseline;
  white-space: pre;
}

/* The row tint carries add/remove, so the code itself stays in the normal
   foreground colour (green text on a green tint is the case that fails). */
.wb-changes__line[data-kind="added"] {
  background: color-mix(in srgb, var(--syntax-string) 14%, transparent);
}

.wb-changes__line[data-kind="removed"] {
  background: color-mix(in srgb, var(--danger) 14%, transparent);
}

.wb-changes__line[data-kind="hunk"] {
  margin: 0.125rem 0;
  /* --syntax-comment is a comment tier: it falls to 1.68:1 on a light
     elevated band, so the hunk header uses the secondary text token. */
  color: var(--text-muted);
  background: var(--bg-elevated);
}

.wb-changes__line[data-kind="meta"] {
  color: var(--text-muted);
}

.wb-changes__line--head {
  position: sticky;
  top: 0;
  z-index: 1;
  color: var(--text-muted);
  font-size: 0.6875rem;
  background: var(--bg-surface);
  border-bottom: 1px solid var(--border);
}

.wb-changes__gutter {
  flex: none;
  width: 2.125rem;
  padding-right: 0.375rem;
  color: var(--text-muted);
  text-align: right;
  user-select: none;
}

.wb-changes__marker {
  flex: none;
  width: 0.75rem;
  text-align: center;
  user-select: none;
}

/* The marker keeps the row's own colour instead of an add/remove accent: a
   green marker on a green tint (and red on red) measures as low as 3.71:1,
   because those two accent tokens are chosen against the plain surface. */

.wb-changes__line-code {
  padding-right: 0.5rem;
  font: inherit;
  color: inherit;
}
</style>
