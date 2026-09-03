# Knowledge Research Tool Notes

Use the installed `knowledge-local-research` Skill for the workflow and recovery
rules. These notes explain tool outputs, not additional research quotas.

- Returned `scopeRefs`, `fileRefs`, `evidenceRefs`, and `tableRef` are research-local
  lookup handles; copy namespaces intact. They are not credentials, bibliographic
  facts, or proof of reading. Use the exposed schema and exact returned mappings.
- `mcp_search` interleaves files, at most five chunks per file.
  `mcp_searchByIds` ranks within the selected files; a grouping response performs
  no search. Neither results nor scope membership prove full-file reading.
- `mcp_researchNavigate` cursors resume a fixed snapshot, not a refreshed directory.
  `view="report"` returns keys/hashes and table mappings without paragraph prose.
  `view="review"` supplies submitted claim text with `claimKey`, `claimHash`, and
  `evidenceRefs`; exact bound evidence with `evidenceRef`, title, and locator; and
  table text with caption, page, and `tableHash`. Follow all fragments/cursors for
  review. This covers submitted content and cited excerpts, not entire documents;
  the service records material projection, never successful semantic verification.
- `mcp_researchReadEvidence` reads saved canonical evidence with continuations,
  not new upstream context. `mcp_getFileDetails` inventories detected tables;
  inventory completeness does not establish PDF extraction completeness.
  `mcp_getTable` preserves available table text and original crop metadata;
  materializing a crop does not mean the model saw it.
- Progress: `discoveredFileCount` means discovery. Search/scoped call counts are
  committed ledger calls; successful counts describe verified receipts, not useful
  findings. Grouping, cache replays, and unrecorded uncertain calls are excluded.
  `searchByIdsSelectedFileCount` counts selected files, not files read.
  `completeEvidenceProjectionCount` and `filesWithCompleteEvidenceProjectionCount`
  count prepared evidence ranges, not whole-file reading or comprehension.
  `modelDelivery="unknown"` must not be reinterpreted as successful reading.
- Finalize's `needs_review` checks are actionable gate failures with no manifest;
  `finalized` permits manifest publication but does not certify source semantics.
  Optional expected-item lists cover submitted items only.
- Tool receipts cannot observe model failures before execution or complete provider
  token/cost accounting. Do not fill missing usage with zero or call it a billed total.
