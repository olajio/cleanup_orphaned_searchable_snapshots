#!/usr/bin/env python3
"""Regression tests for the orphan-detection safety guards.

Offline -- no cluster needed. Replays the July 2026 incident (live searchable
snapshots deleted because hidden data-stream mounts were invisible to the in-use
scan) against the current code, and exercises every guard added in response.

Run:  python3 test_safety_guards.py     (exit 0 = all pass)
"""
import datetime
import importlib.util
import os
import sys

spec = importlib.util.spec_from_file_location(
    "oss", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "orphaned_searchable_snapshots.py"))
oss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oss)

REPO = "found-snapshots"
# The exact snapshot the script deleted, which broke a live frozen index.
BROKEN_SNAP = ("2026.07.19-.ds-metrics-apm.service_summary.60m-default-2026.06.19-000112"
               "-metrics-apm.service_summary_60m_metrics-default_policy-5sj_o9ept-q2uda5nf4zpa")
BROKEN_IDX = "partial-.ds-metrics-apm.service_summary.60m-default-2026.06.19-000112"
# A genuinely old orphan from the same audit run.
OLD_SNAP = ("2024.08.21-metricbeat-7.17.9-2024.08.15-000094-metricbeat-7.17.9"
            "-qxiufpxmszyg8xkl2cfp1q")


class FakeES:
    """Mimics ES wildcard semantics: hidden indices only appear with expand_wildcards=all."""

    def __init__(self, hidden_indices, visible_indices, state_available=True):
        self.hidden = hidden_indices
        self.visible = visible_indices
        self.state_available = state_available
        self.paths = []

    def get(self, path):
        self.paths.append(path)
        if "_settings" in path:
            out = dict(self.visible)
            if "expand_wildcards=all" in path:
                out.update(self.hidden)
            return {
                idx: {"settings": {
                    "index.store.snapshot.snapshot_name": snap,
                    "index.store.snapshot.repository_name": REPO,
                }} for idx, snap in out.items()
            }
        raise AssertionError("unexpected get: " + path)

    def get_optional(self, path):
        self.paths.append(path)
        if "_cluster/state" in path:
            if not self.state_available:
                return None
            allidx = dict(self.visible)
            allidx.update(self.hidden)  # cluster state has no wildcard expansion
            return {"metadata": {"indices": {
                idx: {"settings": {"index": {"store": {"snapshot": {
                    "snapshot_name": snap, "repository_name": REPO,
                }}}}} for idx, snap in allidx.items()
            }}}
        if "_ilm/explain" in path:
            return {"indices": {}}
        return {}


HIDDEN = {BROKEN_IDX: BROKEN_SNAP}
VISIBLE = {"restored-metricbeat-7.17.9-2024.01.01-000001": "2024.01.01-someotherthing-x"}

fails = []


def check(label, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + label + (("  -- " + detail) if detail else ""))
    if not cond:
        fails.append(label)


print("=== 1. Reproduce the bug: old query (no expand_wildcards) misses hidden mounts ===")
es = FakeES(HIDDEN, VISIBLE)
old_style = es.get("/_all/_settings/index.store.snapshot.*?flat_settings=true")
old_in_use = {b["settings"]["index.store.snapshot.snapshot_name"] for b in old_style.values()}
check("old query misses the live snapshot (this caused the outage)",
      BROKEN_SNAP not in old_in_use)

print()
print("=== 2. Fixed settings query sees hidden data-stream mounts ===")
es = FakeES(HIDDEN, VISIBLE)
refs = oss._refs_from_settings(es, REPO)
check("expand_wildcards=all present in the request", "expand_wildcards=all" in es.paths[0])
check("hidden partial-.ds-* mount is now resolved",
      (refs.get(BROKEN_IDX) or {}).get("snapshot") == BROKEN_SNAP)

print()
print("=== 3. collect_in_use unions both sources ===")
es = FakeES(HIDDEN, VISIBLE)
in_use, info = oss.collect_in_use(es, REPO)
check("live snapshot classified as IN USE", BROKEN_SNAP in in_use)
check("cluster-state cross-check ran", info["cluster_state_available"] is True)
check("sources agree (no under-resolution)", info["only_in_cluster_state"] == [])

print()
print("=== 4. Cross-check catches a regression if the settings query breaks again ===")


class RegressedES(FakeES):
    def get(self, path):  # simulate the old, broken behaviour
        return FakeES.get(self, path.replace("expand_wildcards=all", "expand_wildcards=open"))


es = RegressedES(HIDDEN, VISIBLE)
in_use, info = oss.collect_in_use(es, REPO)
check("union still marks the snapshot in-use", BROKEN_SNAP in in_use)
check("discrepancy is reported to the operator",
      [i for i, _ in info["only_in_cluster_state"]] == [BROKEN_IDX],
      str(info["only_in_cluster_state"]))

print()
print("=== 5. Cluster state unavailable degrades gracefully ===")
es = FakeES(HIDDEN, VISIBLE, state_available=False)
in_use, info = oss.collect_in_use(es, REPO)
check("still works without the cluster-state privilege", BROKEN_SNAP in in_use)
check("unavailability is flagged", info["cluster_state_available"] is False)

print()
print("=== 6. Age guard: independent second line of defence ===")
scan_day = datetime.date(2026, 7, 21)  # the real audit date
entry_new = {"name": BROKEN_SNAP, "start_ms": None}
entry_old = {"name": OLD_SNAP, "start_ms": None}
age_new = oss.snapshot_age_days(entry_new, today=scan_day)
age_old = oss.snapshot_age_days(entry_old, today=scan_day)
check("2026.07.19 snapshot is 2 days old", age_new == 2, f"age={age_new}")
check("held back by the default 14-day guard", age_new < 14)
check("genuine 2024 orphan is unaffected", age_old > 14, f"age={age_old}")

epoch_ms = int(datetime.datetime(2026, 7, 19).timestamp() * 1000)
check("start_time_in_millis is preferred when present",
      oss.snapshot_age_days({"name": "no-date-prefix", "start_ms": epoch_ms},
                            today=scan_day) == 2)
check("unknown age returns None (treated as too young)",
      oss.snapshot_age_days({"name": "no-date-prefix", "start_ms": None}) is None)

print()
print("=== 7. ILM in-flight conflict detection ===")
conf = oss.inflight_conflicts([BROKEN_SNAP, OLD_SNAP], {BROKEN_IDX})
check("strips partial-/restored- prefix and matches the snapshot", conf == [BROKEN_SNAP], str(conf))
check("no false match on unrelated orphans", OLD_SNAP not in conf)
check("empty in-flight set is a no-op", oss.inflight_conflicts([BROKEN_SNAP], set()) == [])

print()
print("=== 8. list_all_snapshots captures state + start time ===")


class SnapES:
    def get(self, path):
        return {"snapshots": [
            {"snapshot": BROKEN_SNAP, "state": "SUCCESS", "start_time_in_millis": epoch_ms},
            {"snapshot": "in-progress-one", "state": "IN_PROGRESS", "start_time_in_millis": epoch_ms},
            {"snapshot": "cloud-snapshot-x", "state": "SUCCESS",
             "metadata": {"policy": "cloud-snapshot-policy"}},
        ]}


snaps = oss.list_all_snapshots(SnapES(), REPO)
by = {s["name"]: s for s in snaps}
check("state captured", by["in-progress-one"]["state"] == "IN_PROGRESS")
check("SLM policy still detected", by["cloud-snapshot-x"]["slm"] == "cloud-snapshot-policy")
check("start time captured", by[BROKEN_SNAP]["start_ms"] == epoch_ms)

print()
print("=== 9. Repository aliasing: same bucket registered under two names ===")
ALIAS_IDX = "partial-.ds-other-mount"
ALIAS_SNAP = "2026.01.01-.ds-other-snapshot"


class AliasES(FakeES):
    """One mount names 'found-snapshots', another names 'backup-repo' -- same UUID."""

    def get(self, path):
        return {
            BROKEN_IDX: {"settings": {
                "index.store.snapshot.snapshot_name": BROKEN_SNAP,
                "index.store.snapshot.repository_name": REPO,
                "index.store.snapshot.repository_uuid": "UUID-A"}},
            ALIAS_IDX: {"settings": {
                "index.store.snapshot.snapshot_name": ALIAS_SNAP,
                "index.store.snapshot.repository_name": "backup-repo",
                "index.store.snapshot.repository_uuid": "UUID-A"}},
        }

    def get_optional(self, path):
        if "_cluster/state" in path:
            return {"metadata": {"indices": {}}}
        return {}


in_use, info = oss.collect_in_use(AliasES({}, {}), REPO)
check("snapshot behind the aliased repo name counted as in-use", ALIAS_SNAP in in_use)
check("aliased mount reported to the operator", info["alias_repo_indices"] == [ALIAS_IDX],
      str(info["alias_repo_indices"]))


class ForeignES(AliasES):
    def get(self, path):
        d = AliasES.get(self, path)
        d[ALIAS_IDX]["settings"]["index.store.snapshot.repository_uuid"] = "UUID-B"
        return d


in_use, info = oss.collect_in_use(ForeignES({}, {}), REPO)
check("a genuinely different repository is NOT counted", ALIAS_SNAP not in in_use)

print()
print("=== 10. In-progress repository activity is detected ===")


class BusyES:
    def get_optional(self, path):
        if "_current" in path:
            return {"snapshots": [{"snapshot": "running-now"}]}
        if "_recovery" in path:
            return {"idx": {"shards": [{"type": "SNAPSHOT"}, {"type": "PEER"}]}}
        return {}


s, r = oss.in_progress_repo_operations(BusyES(), REPO)
check("running snapshot counted", s == 1, f"snapshots={s}")
check("only SNAPSHOT-type recoveries counted", r == 1, f"restores={r}")


class IdleES:
    def get_optional(self, path):
        return {"snapshots": []} if "_current" in path else {}


check("idle cluster reports no activity", oss.in_progress_repo_operations(IdleES(), REPO) == (0, 0))



print()
print("=== 11. Truncated snapshot listing is paginated, not silently accepted ===")


class PagedES:
    """ES 8.3+ truncates GET _snapshot/<repo>/_all and reports `remaining`."""

    def __init__(self):
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        if "after=" not in path:
            return {"snapshots": [{"snapshot": "snap-a", "state": "SUCCESS"}],
                    "total": 3, "remaining": 2, "next": "cursor-1"}
        if "cursor-1" in path:
            return {"snapshots": [{"snapshot": "snap-b", "state": "SUCCESS"}],
                    "total": 3, "remaining": 1, "next": "cursor-2"}
        return {"snapshots": [{"snapshot": "snap-c", "state": "SUCCESS"}],
                "total": 3, "remaining": 0}


es = PagedES()
snaps = oss.list_all_snapshots(es, REPO)
names = [s["name"] for s in snaps]
check("all pages fetched", names == ["snap-a", "snap-b", "snap-c"], str(names))
check("cursor passed via after=", any("after=cursor-1" in c for c in es.calls))


class UnpagedES:
    """Older ES: no `remaining` key -- must not attempt pagination."""

    def __init__(self):
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        return {"snapshots": [{"snapshot": "only-one", "state": "SUCCESS"}]}


es = UnpagedES()
snaps = oss.list_all_snapshots(es, REPO)
check("no pagination attempted when `remaining` absent", len(es.calls) == 1)
check("single page returned intact", [s["name"] for s in snaps] == ["only-one"])


class LoopES:
    """Server keeps returning the same page -- must not spin forever."""

    def get(self, path):
        return {"snapshots": [{"snapshot": "dup"}], "remaining": 99, "next": "same"}


snaps = oss.list_all_snapshots(LoopES(), REPO)
check("no infinite loop on a non-advancing cursor", [s["name"] for s in snaps] == ["dup"])

print()
print("=== 12. Restore activity does not block; running snapshot does ===")


class RestoringES:
    def get_optional(self, path):
        if "_current" in path:
            return {"snapshots": []}
        if "_recovery" in path:
            return {"i": {"shards": [{"type": "SNAPSHOT"}, {"type": "SNAPSHOT"}]}}
        return {}


s, r = oss.in_progress_repo_operations(RestoringES(), REPO)
check("frozen shard recovery counted but snapshots idle", (s, r) == (0, 2), f"{(s, r)}")
check("a routine ILM frozen mount would NOT block --apply", s == 0)

print()
print("=" * 60)
if fails:
    print(f"{len(fails)} CHECK(S) FAILED: {fails}")
    sys.exit(1)
print(f"ALL CHECKS PASSED")
