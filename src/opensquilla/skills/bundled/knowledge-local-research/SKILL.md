---
name: knowledge-local-research
description: Research only the local Knowledge corpus and deliver a cited HTML/PDF report with verified PDF table screenshots. Use for local knowledge-base investigation, multi-document synthesis, or evidence-grounded financial research. Never use internet sources or model memory as evidence.
---

# Local Knowledge Research

Use only local Knowledge facts. Never use web, memory, or workspace files as
evidence. The MCP sidecar owns verification, citations, table media, rendering,
and provenance; do not recreate those jobs.

## Workflow

1. Call `mcp_researchBegin`; pass its internal `researchId` unchanged thereafter.
2. Run materially different `mcp_search` queries. Target 30-50 relevant files when
   supported; stop after two useful variants add no files. Never pad sources.
3. Use `mcp_searchByIds` on exact returned IDs, at most 20 files per call. Cover
   selected files and seek numbers, corroboration, conflicts, drivers, and outlook.
4. For useful PDFs call `mcp_getFileDetails` once; pagination is automatic. Call
   `mcp_getTable` only for tables used. Never request or copy screenshot base64.
5. Draft substantive report paragraphs in reading order. Submit all paragraphs in
   one `mcp_researchAddClaims` call; every claim needs exact verified evidence IDs,
   and major claims should use independent files when available.
6. Add only verified tables with `mcp_researchAddTable`, then call
   `mcp_researchFinalize` and check coverage.
7. Publish exactly the manifest's `report.html`, `report.pdf`, and
   `provenance.json`, once each with `bundle="none"`.

## Hard Rules

- Internal IDs belong only in MCP arguments and `provenance.json`, never human text.
- Every factual paragraph needs verified evidence; repeat retrieval after rejection.
- Create no drafts or custom reports. Final chat only confirms the three artifacts.
