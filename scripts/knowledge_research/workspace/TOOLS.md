# Knowledge Research Tool Notes

- `mcp_search`: full-corpus discovery interleaves files, with at most five chunks
  per file. Returned scope/file/evidence references are assigned by the sidecar.
- `mcp_searchByIds`: score-ranked search within at most 20 selected files. Supply
  exactly one of `scopeRefs`, `fileRefs`, or legacy `fileIds`. For larger scopes,
  explicitly select returned group references or a smaller subset. A grouping
  response is not evidence that a search already executed.
- `mcp_researchNavigate`: bounded snapshot, reference mapping, coverage, and
  continuation navigation. Short references are research-namespaced mappings,
  not authentication credentials or proof of model reading.
- `mcp_researchReadEvidence`: canonical evidence text from this research's ledger.
  Follow continuation ranges when needed. Distinguish stored content, returned
  ranges, and unknown model delivery/understanding.
- `mcp_getFileDetails`: automatically collected PDF table inventory with bounded
  model projections. Follow available continuations. Inventory completeness only
  concerns detected entries. Markdown is normally text-only.
- `mcp_getTable`: extracted text and original PDF crop metadata. The crop is
  materialized without copying base64 through the model. Respect truncation and
  quality fields: a valid text/image hash does not verify extraction completeness,
  claim support, or visual inspection. Crops are limited to available extracted
  tables; unavailable crops are not proof of a complete visual inventory.
- `mcp_researchBegin`, `mcp_researchAddClaims`, `mcp_researchAddTable`, and
  `mcp_researchFinalize`: source-bound report assembly using only evidence
  registered in this research. Prefer 3-5 paragraphs per batch with `batchKey`
  and stable `claimKey` values. Use `expectedClaimHash` for an explicit revision.
  A paragraph timeout with unknown commit status requires the original batchKey
  and original payload on retry. Correct rejected batches using error
  locations and exact reference mappings. Do not invent identifiers.
- `mcp_researchAddTable` has no batchKey: identical tableId and section/caption
  replay the original item; changed content returns a conflict. When the Navigate
  report view is available in the schema, inspect already-added tables there.
  Otherwise retry the original table-add arguments unchanged.
- Finalize renders the submitted content and returns its three-file manifest.
  Optional expected-item lists check submitted items, not an undisclosed draft.
  Coverage counts are not a semantic correctness or research-completeness score.
  Material gaps, unsupported assertions, or known incomplete key tables affecting
  conclusions require more evidence or paragraph revision. Revise an existing
  paragraph with the same `claimKey`, current `expectedClaimHash`, and a new
  `batchKey`; remove unsupported assertions from its text while retaining non-empty,
  evidence-grounded prose, then finalize again. There is no paragraph-deletion API;
  do not submit empty text. Unknown visual review alone is not a blanket blocker.
- `publish_artifact`: publish only the manifest's `report.html`, `report.pdf`, and
  `provenance.json`, each with `bundle="none"`. Do not publish a directory, draft,
  screenshot asset, or private state. Keep raw IDs and short references out of
  human-facing text; use the sidecar's readable citations.
  Preserve successful receipts and retry only explicitly failed files. For unknown
  status, use a supported query if available; otherwise disclose uncertainty and
  stop, without claiming success or repeatedly resending. The three calls are not
  an atomic three-file publication transaction.
- Tool receipts describe observable tool activity. Model failures before tool
  execution and provider token/cost completeness are not observable here; do not
  fill missing usage with zero or present an estimate as the total billed amount.
