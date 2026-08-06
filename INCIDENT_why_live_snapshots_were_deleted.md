# Why live searchable snapshots were mistaken for orphans

**Incident:** dev cluster went RED on 2026-08-05 · **Cause introduced:** 2026-07-22 cleanup
· **Elastic case:** 02127001

This is written to be shared with the team. No prior knowledge of the cleanup script is
assumed.

---

## 1. The one-sentence version

The script asked Elasticsearch "show me every index that is using a snapshot" in a way that
**could not see some of the indices**. Those indices were using their snapshots the whole
time; the script just never saw them, concluded the snapshots were unused, and deleted them.

### In three steps

1. **The test for "orphan" was: is any index using this snapshot?** If nothing came back, the
   snapshot was treated as a leftover and deleted.
2. **The question was asked with `GET _all/_settings/…`.** In Elasticsearch, `_all` skips any
   index marked `index.hidden: true`. You have to ask for those explicitly with
   `expand_wildcards=all`.
3. **The six indices in question were marked hidden.** They were alive and pointing at those
   snapshots. The question simply never reached them.

---

## 2. Background you need

**A frozen index has no local copy of its data.** This is the part that makes the bug so
damaging. In a normal (hot/warm) index, the data sits on the node's disk and a snapshot is a
*backup* of it. In a **frozen** index it is the other way round: the data lives only in the
S3 snapshot, and the node keeps a small **cache** of recently-read parts.

> For a frozen index, **the snapshot *is* the data.** Deleting it is not "deleting a backup" —
> it is deleting the index's contents.

**What "orphaned" was supposed to mean.** ILM moves an index to the frozen tier by taking a
snapshot and mounting it. When the index later ages out, ILM should delete both the index and
its snapshot. If the ILM policy is missing `delete_searchable_snapshot: true`, the index goes
but the snapshot stays behind forever, costing S3 money and serving nothing. *That* is an
orphan, and cleaning those up is what the script was built to do.

**How the script identified orphans.** Three-way test — a snapshot is an orphan if it is in
the repository, is **not referenced by any live index**, and is not an SLM backup. The whole
thing hinges on that middle test being correct.

---

## 3. The bug

To find which snapshots are in use, the script ran:

```
GET /_all/_settings/index.store.snapshot.*
```

The intent: "for every index, tell me which snapshot it's mounted from."

**The problem is `_all`.** In Elasticsearch, `_all` and `*` **do not match hidden indices.**
You have to ask for them explicitly with `expand_wildcards=all`. This is the same reason
`GET _cat/indices` doesn't show your `.ds-*` indices unless you add that parameter.

**And the frozen mounts backing our data streams are hidden.** Our APM, metrics, logs and
traces data lives in data streams. When ILM freezes one of those indices, the resulting
mounted index (`partial-.ds-…`) carries `index.hidden: true`.

So the query returned only the mounts that were not hidden. The frozen data-stream mounts
were invisible to it.

> **Important nuance — do not judge by the name.** "Hidden" is a **per-index setting**
> (`index.hidden`), not a naming convention. A `.ds-` prefix is just the naming convention for
> data stream backing indices; it does **not** by itself mean an index is hidden, and on our
> dev cluster some `.ds-*` indices are visible to plain `_all` while others are not. (An index
> removed from its data stream, or restored under a `.ds-` name, may carry no `index.hidden`
> at all.)
>
> This makes the problem **worse**, not better: you cannot predict from an index's name
> whether a query will see it. Only the setting decides. That is precisely why tooling must
> ask for everything rather than assume.
>
> Check any index with:
> ```
> GET <index>/_settings?flat_settings=true&filter_path=**.hidden
> ```
> and list every mount with its hidden flag and snapshot together:
> ```
> GET _all/_settings/index.hidden,index.store.snapshot.snapshot_name?expand_wildcards=all&filter_path=**.hidden,**.snapshot_name
> ```

There is one further wrinkle worth knowing: a wildcard pattern that itself **starts with a
dot** (e.g. `.ds-*`) *does* match hidden indices whose names start with a dot — a
backwards-compatibility rule so dot-index patterns keep working. `_all` and `*` do not start
with a dot, so they get no such exemption. This is why `GET _cat/indices/.ds-*` and
`GET _cat/indices/*` can return different sets.

### The proof, from the affected index

Elastic pulled the settings of one of the red indices. Two lines matter:

```json
"partial-.ds-metrics-apm.service_summary.60m-default-2026.06.19-000112": {
  "settings": { "index": {

    "hidden": "true",                            ← this made it invisible to _all

    "store": { "type": "snapshot", "snapshot": {
      "snapshot_name": "2026.07.19-.ds-metrics-apm.service_summary.60m-default
                        -2026.06.19-000112-…-5sj_o9ept-q2uda5nf4zpa",   ← the snapshot we deleted
      "repository_name": "found-snapshots"
    }}
  }}}
```

The index was alive and actively pointing at that snapshot. The script's question just never
reached it.

**All six red indices are `partial-.ds-*`, and all six carry `index.hidden: true`.** Verified
from the index settings, not inferred from the names. Not a coincidence — it is the signature
of the bug.

### A second, smaller version of the same bug

The default `expand_wildcards=open` also excludes **closed** indices. A closed searchable-snapshot
index still owns its snapshot. Same fix.

---

## 4. Why nothing broke for two weeks

The snapshots were deleted on **22 July**. The cluster went red on **5 August**.

Elasticsearch reads the snapshot repository in exactly two situations: when a **shard starts**,
and on a **cache miss**. At no other time does a frozen index check that its snapshot still
exists. So after the deletion, the six indices kept serving queries from their local cache and
the cluster stayed green — the damage was invisible.

On 5 August something caused those shards to be reallocated. Shard start = repository read =
`SnapshotMissingException` = red cluster.

Elastic's own words:

> *"if you delete the underlying searchable snapshot Elasticsearch will continue to operate
> normally until the first cache miss. This may be much later, for instance when a shard
> relocates to a different node, or when the node holding the shard restarts."*

**Consequence for us:** a green cluster after a cleanup proves nothing. Verification has to
happen after the next restart or relocation, not immediately.

---

## 5. Why the backups didn't save us

The obvious recovery is "restore from the nightly `cloud-snapshot-*` SLM backup." It doesn't
work here, and this is worth understanding because it is counter-intuitive:

> **A snapshot of a mounted searchable-snapshot index does not contain the data — only a
> pointer to the original snapshot.**

The SLM backups faithfully backed up all six indices. But each backup is a pointer to the
snapshot we deleted. Delete the target, and every pointer to it is worthless.

**Implication:** for frozen-tier data, the searchable snapshot is the *only* copy. There is no
second line of defence. This is why the safety bar for this cleanup has to be so high.

---

## 6. What was changed in the script

Seven layers, so that no single mistake can repeat this. Layers 1–2 fix the actual bug; 3–7
are defence in depth.

| # | Layer | What it does |
|---|-------|--------------|
| 1 | **`expand_wildcards=all`** | The in-use query now includes **hidden** and **closed** indices. This is the actual fix. |
| 2 | **Cluster-state cross-check** | The in-use set is *also* read from `_cluster/state/metadata`, which lists index metadata directly and does no wildcard expansion at all. The two answers are **unioned** — an index seen by either one counts as in use. If they disagree, the run warns loudly and `--apply` **refuses to proceed**. |
| 3 | **Minimum age (`--min-snapshot-age-days`, default 14)** | ILM creates a snapshot, *then* mounts it, *then* deletes the old index. In that gap the snapshot genuinely has no index pointing at it and looks exactly like an orphan. Snapshots younger than 14 days — or of unknown age — are never deleted. |
| 4 | **Snapshot state check** | Snapshots not in state `SUCCESS` are still being written; skipped. |
| 5 | **ILM in-flight check** | If ILM is currently running the `searchable_snapshot` action on an index, that index's snapshot is held back. |
| 6 | **Pre-delete re-check** | Sizing a large repository takes a long time, and ILM keeps mounting new snapshots meanwhile. The in-use set is re-read immediately before deleting; anything that became live is dropped. If the in-use set *shrank* during the run, the delete **aborts**. |
| 7 | **Post-delete health check** | Reports cluster health afterwards, with an explicit reminder that green now means nothing (see §4). |

Additional hardening found during the review:

- **Repository matched by UUID as well as name.** The same S3 bucket can be registered under
  two repository names; a mount recorded against the second name points at the same blobs.
  Name-only matching would have called its live snapshot an orphan.
- **Collection order reversed.** The repository is now listed *before* the in-use set is read.
  In the old order, a snapshot created *and* mounted between the two calls would appear in the
  listing but not the in-use set — a false orphan. In the new order it simply isn't a candidate.
- **Concurrent activity blocks deletion.** A running snapshot or an in-flight restore against
  the repository aborts `--apply`.
- **Plausibility guard.** If the in-use scan returns *zero* mounted indices while orphan
  candidates exist, the tool refuses to delete — that is the shape of a broken scan, not an
  idle cluster. (Had this existed, it would not have caught this incident, because 177 mounts
  *were* found; the hidden ones were simply missing from that count.)
- **`--apply` now requires the `monitor` cluster privilege** so the cross-check in layer 2 is
  always available.
- **Full transparency in the audit file.** Every snapshot held back by a safety layer is
  listed under `HELD BACK BY SAFETY FILTERS`, and the audit records how the in-use set was
  resolved and how many indices each source saw.

### Verified against the real incident

Replaying the 21 July dev scan through the fixed code:

| Scenario | Live snapshots wrongly selected for deletion |
|---|---|
| Old code | **6 of 6** — the incident |
| Fixed code | **0 of 6** — and both control orphans still correctly found |
| Layer 1 deliberately re-broken, layer 3 disabled | 0 of 6 — layer 2 catches all six |
| Layers 1 *and* 2 both broken | 4 of 6 saved by the age guard alone |

---

## 7. New tool: `find_broken_searchable_snapshots.py`

Because breakage is silent (§4), **cluster health cannot tell you how many indices are already
damaged.** Six have gone red; others may be broken and simply haven't been touched yet.

This read-only tool answers that directly. It lists every mounted searchable-snapshot index
(hidden ones included) and checks whether its snapshot still exists in the repository:

```bash
./find_broken_searchable_snapshots.py --cluster dev
```

Anything it reports is an index that *will* fail on its next shard start. **Run this on dev
now, and on qa/prod before and after any cleanup.**

---

## 8. What the team should take away

1. **Frozen-tier snapshots are not backups — they are the data.** Deleting one is equivalent
   to deleting the index.
2. **Green does not mean safe.** Damage surfaces on the next restart or relocation, which can
   be weeks later. Verify then, not immediately.
3. **SLM backups do not protect frozen indices.** They store pointers, not data.
4. **Hidden indices are easy to miss.** Any tooling that enumerates indices must pass
   `expand_wildcards=all`, or it is quietly working with a partial list. This applies well
   beyond this script.
5. **You cannot tell from an index's name whether a query will see it.** Visibility comes from
   the `index.hidden` setting, and two indices with similar names can differ. Never reason
   about coverage from naming patterns — verify with the setting.
6. **Cross-check destructive decisions against a second, independent source.** One query
   returning a plausible-looking number (177 mounted indices) is not verification.
7. **An absent result is not evidence of absence.** "No index references this snapshot" was
   treated as fact when it actually meant "my query found nothing" — which can equally mean
   the query was wrong. For destructive actions, an empty answer deserves more scrutiny than
   a full one.

---

## 9. References

- Elastic support case **02127001** — Cluster in Red state, unassigned primary shards
- Elastic support case **02118106** — the original orphaned-snapshot investigation
- [Searchable snapshots — reliability and backup](https://www.elastic.co/docs/deploy-manage/tools/snapshot-and-restore/searchable-snapshots#back-up-restore-searchable-snapshots)
- Repository docs: [`README.md`](README.md) · [`HOWTO_orphaned_searchable_snapshots.md`](HOWTO_orphaned_searchable_snapshots.md)
