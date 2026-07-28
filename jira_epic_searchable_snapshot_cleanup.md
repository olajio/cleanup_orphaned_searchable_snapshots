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
compound. In our environment they have accumulated to **~65 TiB logical** (with
**~9.3 TiB genuinely reclaimable** after accounting for deduplication) — all of it paying
full object-store billing rates with zero query value in return.

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
safe to fix. All assets live in: **`olajio/cleanup_orphaned_searchable_snapshots`**

### Tool: `orphaned_searchable_snapshots.py`

| Capability | Flag | Notes |
|------------|------|-------|
| Orphan detection | (default) | 3-way exclusion: not mounted, not SLM-managed, in repo |
| Credentials from AWS Secrets Manager | `--cluster {dev,qa,ccs,prod}` | Loads `es_url` + `es_api_key` from `elastic/kibana/dataview_cleanup_<cluster>` |
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
| `corrected_ilm_policies/` | Ready-to-apply corrected `PUT _ilm/policy` bodies |
| `searchable_snapshot_ilm_findings.md` | Full audit write-up |
| `{dev,qa,prod,ccs}_ilm_policy` | Exported ILM policy snapshots per cluster (input to `analyze_ilm.py`) |

---

## Audit Findings — Current State (July 2026)

| Cluster | Orphans | Logical size | Reclaimable (dedup-aware) | Currently-leaking ILM policies | Policies needing review |
|---------|--------:|-------------:|-------------:|-------------------------------|------------------------:|
| **PROD** | 1,291 | 51.44 TiB | ~220 GiB | `cost` (0 orphans yet — latent) | 2 |
| **QA** | 527 | 10.34 TiB | ~8.47 TiB | none currently flagged | 6 |
| **DEV** | 231 | 3.30 TiB | ~639 GiB | `solarwinds-test` (0 orphans yet — latent) | 14 |
| **CCS** | 0 | — | — | none | 0 |
| **TOTAL** | **2,049** | **~65 TiB** | **~9.3 TiB** | | |

**~91% of reclaimable storage is concentrated in QA.**

### Critical Finding

`apm-rollover-30-days` is the dominant offender — it produced 405 orphans (8.38 TiB logical)
on QA and 127 orphans (2.00 TiB logical) on DEV. Orphan creation dates extend to
**2026-02-04**, after the policy's last update on both clusters, indicating the root cause is
either still active in the policy or the index is being removed outside ILM's delete phase.
Elastic support engagement is required to confirm.

> **Note on PROD's 51 TiB logical vs. ~220 GiB reclaimable:** PROD has an extremely high
> dedup ratio (~239×) — most blobs in the orphaned snapshots are still pinned by live
> snapshots. The true bytes freed would only be confirmed by comparing the object-store bucket
> size before and after deletion.

---

## Acceptance Criteria / Story Breakdown

### Phase 1 — Root-cause triage (before any deletion)

- [ ] Engage Elastic support to determine why `apm-rollover-30-days` frozen snapshots are
      orphaning on QA and DEV after the policy was last updated. Confirm whether the policy
      is the source or whether an external process is removing indices before ILM's delete
      phase runs.

### Phase 2 — Fix leaking ILM policies

- [ ] **DEV:** Apply corrected `solarwinds-test` policy
      (`corrected_ilm_policies/dev/solarwinds-test.json`). Review the `min_age: 365d`
      placeholder against actual retention requirements before applying.
- [ ] **PROD:** Apply corrected `cost` policy (`corrected_ilm_policies/prod/cost.json`).
      Same `min_age` review applies. Schedule for a maintenance window.
- [ ] **QA / DEV:** Apply resolution for `apm-rollover-30-days` per Phase 1 outcome.
- [ ] Monitor each cluster for 30 days post-fix to confirm no new orphans are generated.

### Phase 3 — Orphan deletion (dry-run before `--apply`)

- [ ] **QA** — highest priority (~8.47 TiB reclaimable). Run dry-run, review output, then
      apply during a low-traffic window.
- [ ] **DEV** — (~639 GiB reclaimable). Tool already validated here; proceed to `--apply`.
- [ ] **PROD** — schedule for a maintenance window (~220 GiB reclaimable). Note the high
      dedup ratio; actual billing reduction confirmed from Cloud console bucket size.
- [ ] **CCS** — no action required.

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
- Audit write-up: `searchable_snapshot_ilm_findings.md` (in repo)
- Full per-cluster orphan lists: `{dev,qa,prod,ccs}_orphans_audit.txt` (in repo)
- Full ILM policy review: `{dev,qa,prod,ccs}_ilm_review.txt` (in repo)
