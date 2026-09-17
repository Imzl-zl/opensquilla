import type { InjectionKey } from 'vue'

/**
 * Domain projection of one changed path in a project workspace. Staged and
 * unstaged state stay separate because a file can be in both: a staged edit
 * followed by a further worktree edit must not collapse into one badge.
 */
export interface WorkspaceChangeEntry {
  readonly path: string
  readonly previousPath: string | null
  readonly changeType: WorkspaceChangeType
  readonly staged: boolean
  readonly unstaged: boolean
  /** Null when lines are not countable (binary, or no stats for the path). */
  readonly addedLines: number | null
  readonly removedLines: number | null
}

export type WorkspaceChangeType =
  | 'added'
  | 'modified'
  | 'deleted'
  | 'renamed'
  | 'copied'
  | 'typeChanged'
  | 'unmerged'
  | 'untracked'
  | 'unknown'

/**
 * Why a workspace has no readable working-tree state. `available: false` is
 * separate from an empty change list so the panel can say "cannot tell"
 * instead of "nothing changed".
 */
export type WorkspaceChangesAvailability =
  | 'git_unavailable'
  | 'not_repository'
  | 'timed_out'
  | 'failed'

export interface WorkspaceChanges {
  readonly available: boolean
  readonly availabilityReason: WorkspaceChangesAvailability | null
  readonly branch: string | null
  readonly detached: boolean
  readonly upstream: string | null
  readonly ahead: number
  readonly behind: number
  readonly totalCount: number
  readonly truncated: boolean
  readonly addedLines: number
  readonly removedLines: number
  readonly entries: readonly WorkspaceChangeEntry[]
}

export interface WorkspaceFileDiff {
  readonly path: string
  readonly staged: boolean
  readonly text: string
  readonly truncated: boolean
  readonly binary: boolean
}

export interface WorkspaceChangesReader {
  readChanges(
    workspaceId: string,
    options?: { signal?: AbortSignal },
  ): Promise<WorkspaceChanges>
  readDiff(
    request: { workspaceId: string; path: string; staged?: boolean },
    options?: { signal?: AbortSignal },
  ): Promise<WorkspaceFileDiff>
}

export const WORKSPACE_CHANGES_KEY: InjectionKey<WorkspaceChangesReader> = Symbol('WorkspaceChanges')
