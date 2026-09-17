import type { TransportCallOptions as RpcCallOptions } from './transportTypes'
import type { RpcRequester as WorkspaceChangesTransport } from './privateTransports'
import {
  WORKSPACES_GIT_DIFF_METHOD,
  type WorkspacesGitDiffParams,
  type WorkspacesGitDiffResult,
} from '@/contracts/generated/v4/workspacesGitDiff'
import { validateWorkspacesGitDiffResult } from '@/contracts/generated/v4/workspacesGitDiffValidators.mjs'
import {
  WORKSPACES_GIT_STATUS_METHOD,
  type WorkspacesGitStatusParams,
  type WorkspacesGitStatusResult,
} from '@/contracts/generated/v4/workspacesGitStatus'
import { validateWorkspacesGitStatusResult } from '@/contracts/generated/v4/workspacesGitStatusValidators.mjs'
import {
  WORKSPACES_GIT_STAGE_METHOD,
  type WorkspacesGitStageParams,
  type WorkspacesGitStageResult,
} from '@/contracts/generated/v4/workspacesGitStage'
import {
  validateWorkspacesGitStageParams,
  validateWorkspacesGitStageResult,
} from '@/contracts/generated/v4/workspacesGitStageValidators.mjs'
import type {
  WorkspaceChanges,
  WorkspaceChangesReader,
  WorkspaceIndexChange,
} from '@/modules/workspaceChanges'

function optionsFor(signal?: AbortSignal): RpcCallOptions | undefined {
  return signal ? { signal, abortAction: 'reject', timeoutAction: 'reject' } : undefined
}

function requireResult<T>(value: unknown, valid: (candidate: unknown) => boolean, method: string): T {
  if (!valid(value)) throw new Error(`${method} returned an invalid response`)
  return value as T
}

/** The outgoing direction is validated too: a request that cannot satisfy its
 * Contract (an empty path list, say) should not reach the Gateway at all. */
function requireParams<T>(value: unknown, valid: (candidate: unknown) => boolean, method: string): T {
  if (!valid(value)) throw new Error(`${method} received params that violate its contract`)
  return value as T
}

/**
 * Gateway-backed reader for the read-only working-tree surface.
 *
 * The generated validator runs at this boundary, so a mixed-version Gateway
 * that answers with an unexpected shape fails here instead of reaching the
 * panel as a half-populated model.
 */
export function createV4WorkspaceChanges(
  transport: WorkspaceChangesTransport,
): WorkspaceChangesReader {
  return {
    async readChanges(workspaceId, options): Promise<WorkspaceChanges> {
      const params: WorkspacesGitStatusParams = { workspaceId }
      const result = requireResult<WorkspacesGitStatusResult>(
        await transport.request(
          WORKSPACES_GIT_STATUS_METHOD,
          params as unknown as Record<string, unknown>,
          optionsFor(options?.signal),
        ),
        validateWorkspacesGitStatusResult,
        WORKSPACES_GIT_STATUS_METHOD,
      )
      return {
        available: result.available,
        availabilityReason: result.availabilityReason,
        branch: result.branch,
        detached: result.detached,
        upstream: result.upstream,
        ahead: result.ahead,
        behind: result.behind,
        totalCount: result.totalCount,
        truncated: result.truncated,
        addedLines: result.addedLines,
        removedLines: result.removedLines,
        entries: result.entries.map(entry => ({
          path: entry.path,
          previousPath: entry.previousPath,
          changeType: entry.changeType,
          staged: entry.staged,
          unstaged: entry.unstaged,
          addedLines: entry.addedLines,
          removedLines: entry.removedLines,
        })),
      }
    },

    async readDiff(request, options) {
      const params: WorkspacesGitDiffParams = {
        workspaceId: request.workspaceId,
        path: request.path,
        ...(request.staged === undefined ? {} : { staged: request.staged }),
      }
      const result = requireResult<WorkspacesGitDiffResult>(
        await transport.request(
          WORKSPACES_GIT_DIFF_METHOD,
          params as unknown as Record<string, unknown>,
          optionsFor(options?.signal),
        ),
        validateWorkspacesGitDiffResult,
        WORKSPACES_GIT_DIFF_METHOD,
      )
      return {
        path: result.path,
        staged: result.staged,
        text: result.text,
        truncated: result.truncated,
        binary: result.binary,
      }
    },

    async stagePaths(request, options): Promise<WorkspaceIndexChange> {
      const params = requireParams<WorkspacesGitStageParams>(
        {
          workspaceId: request.workspaceId,
          staged: request.staged,
          paths: [...request.paths],
        },
        validateWorkspacesGitStageParams,
        WORKSPACES_GIT_STAGE_METHOD,
      )
      const result = requireResult<WorkspacesGitStageResult>(
        await transport.request(
          WORKSPACES_GIT_STAGE_METHOD,
          params as unknown as Record<string, unknown>,
          optionsFor(options?.signal),
        ),
        validateWorkspacesGitStageResult,
        WORKSPACES_GIT_STAGE_METHOD,
      )
      return {
        staged: result.staged,
        affectedPaths: result.affectedPaths,
      }
    },
  }
}
