# Jira Epic: Searchable Snapshot Orphan Cleanup — Elastic Cloud

## Summary

Identify, quantify, and eliminate orphaned searchable snapshots accumulating in the
`found-snapshots` object-storage repository across all Elastic Cloud clusters; fix the ILM
policies producing them; and establish a process to prevent future accumulation.

---

## Why This Matters — Business & Billing Impact

### How Elastic Cloud Bills for Snapshot Storage

Elastic Cloud bills for storage across two distinct dimensions:

1. **Node (local) storage** — SSD/spinning disk on hot and warm tier nodes, billed per node
   configuration. This is the expensive tier.

2. **Snapshot / object-store storage** — the `found-snapshots` repository backed by cloud
   object storage (S3, GCS, or Azure Blob). This is what the **cold and frozen tiers**
   physically live in, and it is billed on **total bytes stored in the repository**,
   regardless of whether a given snapshot is referenced by a live index or not.

Reference: [Elastic Cloud Hosted Deployment Billing Dimensions — Storage](https://www.elastic.co/docs/deploy-manage/cloud-organization/billing/cloud-hosted-deployment-billing-dimensions#storage)

### Why Orphaned Snapshots Drive Up the Bill

A **searchable snapshot** is how Elastic implements the cold and frozen tiers: the index data
lives in the object-store snapshot, not on local disk, which is what makes those tiers cheap
per GB. When an index is removed — by an ILM delete phase, a manual delete, or a policy
error — the snapshot backing it **should** be deleted at the same time via
`delete_searchable_snapshot: true` in the ILM delete phase.

When that flag is missing or the delete phase does not exist, the snapshot stays in the
repository permanently:

- **No live index references it** — it serves no queries and provides no value.
- **Elastic still bills for every byte it occupies** — the repository metering does not
  distinguish orphaned blobs from live ones.
- **It accumulates silently** — there is no dashboard alert, no automatic expiry, and no
  quota enforcement.

Over time, across a fleet of indices cycling through the frozen tier, orphaned snapshots
compound into terabytes of dead data — all paying full object-store billing rates with zero
query value in return.

### Additional Operational Impacts

Beyond the direct billing cost, orphaned snapshots cause:

- **Slower snapshot/SLM operations** — repository housekeeping (SLM retention scans,
  `_cleanup`, health checks) must enumerate all snapshots; a bloated repository with thousands
  of orphans degrades these operations.
- **Inflated repository metadata** — the snapshot repository index grows with every orphan;
  at scale this increases the memory and time cost of every snapshot API call.
- **Cluttered snapshot lists** — auditing backups and diagnosing restore issues becomes harder
  when thousands of dead snapshots appear alongside live ones.
- **Masking of root-cause policy defects** — without tooling, there is no way to know which
  ILM policy is responsible for the accumulation or whether a fix applied months ago actually
  stopped the leak.

---

## Background

Elastic's ILM `searchable_snapshot` action (in the cold or frozen phase) creates a snapshot
and mounts it as the live index. The ILM delete phase, via `delete_searchable_snapshot: true`
(the default), removes that snapshot when the index ages out. Two failure modes produce
orphans:

1. **Missing delete phase** — the policy has no delete phase at all, so the searchable
   snapshot is never cleaned up.
2. **`delete_searchable_snapshot: false`** — the delete phase exists but explicitly opts out
   of cleaning up the snapshot.

SLM (`cloud-snapshot-*`) periodic backups are not affected — SLM manages its own retention
independently and those snapshots are excluded from the orphan definition.

---

## What Was Built

A Python tool and supporting documentation were developed to make this problem measurable and
safe to fix.

### Tool: `orphaned_searchable_snapshots.py`

| Capability | Flag | Notes |
|------------|------|-------|
| Orphan detection | (default) | 3-way exclusion: not mounted, not SLM-managed, in repo |
| Credentials from AWS Secrets Manager | `--cluster` | Loads `es_url` + `es_api_key` from a named secret per cluster |
| Fast logical sizing | `--report-size` | Uses `index_details` metadata; no timeout risk |
| Dedup-aware reclaimable sizing | `--incremental` | Uses `_status` API; URL-length batched to avoid HTTP 400 |
| Per-snapshot breakdown (top 25) | `--per-snapshot` | Implies `--report-size` |
| Pattern filter | `--pattern GLOB` | Scope to a year, index prefix, etc. |
| ILM culprit analysis | `--check-ilm` | Flags offending policies + orphan count/size per policy |
| Auto-generated corrected policies | (with `--check-ilm`) | Writes `corrected_ilm_policies/<cluster>/<policy>.json` |
| ILM review file | `--ilm-review-file PATH` | Section 1: currently offending; Section 2: formerly-leaking with NEEDS REVIEW flag |
| Frozen-tier logical share | `--frozen-usage` | Orphans as % of frozen-tier mounted storage (from ES metadata) |
| Frozen-tier object-store share | `--frozen-tier-capacity [SIZE]` | Reclaimable as % of total object-store capacity (user-provided from Cloud console); prompts interactively if `--incremental` used without this flag |
| Full audit record | `--audit-file PATH` | Complete orphan list + offending policies + sizing summary |
| Safe deletion | `--apply` | Dry-run by default; batched + retried with exponential backoff |
| Machine-readable output | `--json` | Full data for downstream tooling |

### Supporting Files

| File | Purpose |
|------|---------|
| `README.md` | Concepts, glossary, quick-start, all-options reference |
| `HOWTO_orphaned_searchable_snapshots.md` | Detailed usage guide, recipes, troubleshooting |
| `analyze_ilm.py` | Offline auditor for exported ILM policy JSON files |
| `corrected_ilm_policies/` | Ready-to-apply corrected `PUT _ilm/policy` bodies per cluster |
| `searchable_snapshot_ilm_findings.md` | Full audit write-up |

---

## Audit Findings — Current State

Run the tool against each cluster to populate this table before beginning any remediation:

| Cluster | Orphans | Logical size | Reclaimable (dedup-aware) | Currently-leaking ILM policies | Policies needing review |
|---------|--------:|-------------:|-------------:|-------------------------------|------------------------:|
| Cluster 1 | TBD | TBD | TBD | TBD | TBD |
| Cluster 2 | TBD | TBD | TBD | TBD | TBD |
| Cluster N | TBD | TBD | TBD | TBD | TBD |
| **TOTAL** | | | | | |

> **Note on logical vs. reclaimable size:** Snapshots are deduplicated — the logical size
> over-counts blobs shared with live snapshots. The reclaimable (`--incremental`) figure is
> the dedup-aware estimate of space actually freed. The exact bytes reclaimed are only
> confirmed by comparing the object-store bucket size in the Elastic Cloud console before and
> after deletion.

### Key Things to Investigate per Cluster

When reviewing the `--check-ilm` and `--ilm-review-file` output, flag any policy where:

- Orphan creation dates fall **after** the policy's last-updated date — the policy may still
  be leaking even after a previous remediation attempt (shown as `NEEDS REVIEW`).
- A policy creates searchable snapshots but has **no delete phase** — it will keep producing
  orphans until fixed.
- A policy has a delete phase with **`delete_searchable_snapshot: false`** — the snapshot is
  intentionally left behind; confirm this is deliberate.

---

## Acceptance Criteria / Story Breakdown

### Phase 1 — Root-cause triage (before any deletion)

- [ ] Engage Elastic support to determine why frozen snapshots continue to orphan on clusters
      where orphan creation dates post-date the relevant policy's last update. Confirm whether
      the policy itself is the source or whether an external process is removing indices before
      ILM's delete phase runs.

### Phase 2 — Fix leaking ILM policies

- [ ] For each non-production cluster with a currently-offending policy: apply the corrected
      policy body from `corrected_ilm_policies/<cluster>/<policy>.json`. Review the
      `min_age: 365d` placeholder against actual retention requirements before applying.
- [ ] For each production cluster with a currently-offending policy: apply the corrected
      policy body. Same `min_age` review applies. Schedule for a maintenance window.
- [ ] For any cluster where orphans post-date the last policy update: apply the resolution
      determined in Phase 1.
- [ ] Monitor each cluster for 30 days post-fix to confirm no new orphans are generated by
      the corrected policies.

### Phase 3 — Orphan deletion (dry-run before `--apply`)

- [ ] Highest-priority cluster (largest reclaimable storage) — run dry-run, review output,
      then apply during a low-traffic window.
- [ ] Remaining non-production clusters — run dry-run and proceed to `--apply` once
      confirmed.
- [ ] Production clusters — schedule for a maintenance window. Note that a high dedup ratio
      means actual billing reduction may be lower than the logical size; confirm from the
      Cloud console bucket size before and after.
- [ ] Any cluster with zero orphans — no action required.

### Phase 4 — Verification & handoff

- [ ] Confirm object-storage bucket size in Elastic Cloud console decreased by expected
      amount after each cluster's cleanup (bucket size before vs. after is the definitive
      measure).
- [ ] Re-run the tool in read-only mode 30 days after deletion to confirm no new orphan
      accumulation.
- [ ] Document final reclaimable-vs-actual results and close this epic.

---

## Required API Key Privileges

| Operation | Permissions required |
|-----------|---------------------|
| Reporting / listing / sizing | `monitor`, `view_index_metadata` |
| `--check-ilm` / `--ilm-review-file` | `read_ilm` |
| `--apply` (delete orphans) | `manage` or `cluster:admin/snapshot/delete` |

---

## References

- Elastic Cloud billing — storage dimensions:
  https://www.elastic.co/docs/deploy-manage/cloud-organization/billing/cloud-hosted-deployment-billing-dimensions#storage
- Tool repository: `olajio/cleanup_orphaned_searchable_snapshots`
- Tool documentation: `README.md` and `HOWTO_orphaned_searchable_snapshots.md` (in repo)
