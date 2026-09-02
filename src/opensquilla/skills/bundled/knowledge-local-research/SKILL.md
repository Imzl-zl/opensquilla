---
name: knowledge-local-research
description: Research only the local Knowledge corpus and deliver a cited HTML/PDF report with verified PDF table screenshots. Use for local knowledge-base investigation, multi-document synthesis, or evidence-grounded financial research. Never use internet sources or model memory as evidence.
---

# Local Knowledge Research

Use Knowledge as the only factual source. The MCP sidecar owns evidence
verification, pagination, citations, table media, report rendering, and the public
artifact allowlist. Do not recreate that logic with files, shell commands, or JSON.

## Workflow

1. Call `mcp_researchBegin` with the report title. Keep its `researchId` internal and
   pass it unchanged to every Knowledge and research call below.
2. Discover broadly with materially different `mcp_search` query families. For a
   deep report, target 30-50 genuinely relevant, evidence-bearing files when the
   corpus supports them; stop only after two useful query variants add no new
   relevant file. Never pad the source count, fall back to the web, or use memory as
   evidence. Collect exact returned file IDs and evidence IDs without guessing.
3. Select relevant files, then call `mcp_searchByIds` with focused queries and no
   more than 20 exact file IDs per call. Cover every selected file. Seek
   corroboration, disagreements, numbers, causal drivers, and forward-looking
   evidence. Do not cite a result outside the requested file set.
4. For each selected PDF that may contain useful tables, call
   `mcp_getFileDetails` once with `fileId` and `researchId`; pagination is automatic.
   Call `mcp_getTable` only for tables actually used in the report. Do not request or
   copy screenshot base64.
5. Build a substantive report in reading order: executive findings, observed
   performance, driver chains, multi-source support, conflicting evidence, and
   conditional outlook. Call `mcp_researchAddClaim` once per paragraph with plain
   human-facing prose and the exact verified `evidenceIds` that support it. Assign
   every reference to visible analysis; use multiple independent files for major
   claims when available. Call `mcp_researchAddTable` only after its inventory and
   full table were verified.
6. Call `mcp_researchFinalize`. Check its coverage instead of counting a
   References-only file as support. It creates human-readable inline citations and
   references, renders parsed tables plus original PDF crops in HTML, and renders
   original table crops in PDF.
7. From `publicArtifactManifest.files`, call `publish_artifact` exactly once for each
   of `report.html`, `report.pdf`, and `provenance.json`, using the supplied path,
   name, MIME type, and `bundle="none"`. Publish no directory or other file.

## Hard Rules

- Internal research, file, evidence, and table IDs may appear only in MCP arguments
  and `provenance.json`. Never place them in claims, chat, HTML, or PDF.
- Every factual paragraph needs verified evidence. Never replace a rejected ID with
  a similar-looking ID; repeat the relevant Knowledge call instead.
- Do not create or publish notes, drafts, source dumps, intermediate HTML, or custom
  provenance. Private state lives under `.codex/knowledge-research` and must remain
  private.
- The final chat response only confirms that the three artifacts are ready. Do not
  repeat internal IDs, workspace paths, or raw provenance.
