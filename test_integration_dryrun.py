#!/usr/bin/env python3
"""Integration test: run the real main() against a simulated cluster.

Offline -- no real cluster. A fake ESClient answers every endpoint main() calls,
modelling the dev cluster as it actually was on 2026-07-21, including the hidden
partial-.ds-* mounts that the original code could not see.

Asserts end to end that:
  * no live snapshot reaches the orphan list (dry-run), and
  * no live snapshot reaches a DELETE request (--apply).

Run:  python3 test_integration_dryrun.py     (exit 0 = all pass)
"""

import importlib.util
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "oss", os.path.join(HERE, "orphaned_searchable_snapshots.py"))
oss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oss)

REPO = "found-snapshots"

# Live frozen indices and the snapshots they are mounted from. All are hidden
# data-stream mounts -- the exact shape the original code missed.
LIVE = {
    "partial-.ds-metrics-apm.service_summary.60m-default-2026.06.19-000112":
        "2026.07.19-.ds-metrics-apm.service_summary.60m-default-2026.06.19-000112"
        "-metrics-apm.service_summary_60m_metrics-default_policy-5sj_o9ept-q2uda5nf4zpa",
    "partial-.ds-metrics-apm.service_destination.60m-default-2026.06.19-000112":
        "2026.07.19-.ds-metrics-apm.service_destination.60m-default-2026.06.19-000112"
        "-metrics-apm.service_destination_60m_metrics-default_policy-hbsboxqerkawqlnm7bh9kq",
    "partial-.ds-.logs-elasticsearch.deprecation-default-2026.04.20-000001":
        "2026.06.03-.ds-.logs-elasticsearch.deprecation-default-2026.04.20-000001"
        "-.deprecation-indexing-ilm-policy-3sz1hkqrr_un4ztgxjtsqa",
}
# Genuine orphans: old, nothing references them.
TRUE_ORPHANS = [
    "2024.08.21-metricbeat-7.17.9-2024.08.15-000094-metricbeat-7.17.9-qxiufpxmszyg8xkl2cfp1q",
    "2023.09.23-.ds-metricbeat-8.8.1-mt-rnd-2023.09.17-000027-metricbeat-8.8.1-mt-rnd-ypbzpvnntngqb5ocwizqbg",
]
SLM_SNAP = "cloud-snapshot-2026.08.03-ki7f_xz8rek4qn3mlpqmdq"
# A snapshot ILM created moments ago and has not finished mounting.
YOUNG_SNAP = "2026.07.20-.ds-traces-apm-default-2026.07.20-004200-apm-rollover-30-days-aaaaaaaa"

fails = []


def check(label, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + label + (("  -- " + detail) if detail else ""))
    if not cond:
        fails.append(label)


class FakeClient:
    """Answers every endpoint main() touches. Records DELETEs."""

    def __init__(self, honour_expand_wildcards=True):
        self.honour = honour_expand_wildcards
        self.deleted = []

    # -- helpers ---------------------------------------------------------
    def _settings_body(self, path):
        mounts = dict(LIVE) if (self.honour and "expand_wildcards=all" in path) else {}
        return {i: {"settings": {
            "index.store.snapshot.snapshot_name": s,
            "index.store.snapshot.repository_name": REPO,
            "index.store.snapshot.repository_uuid": "UUID-1",
        }} for i, s in mounts.items()}

    def _all_snapshots(self):
        snaps = [{"snapshot": s, "state": "SUCCESS",
                  "start_time_in_millis": 1_750_000_000_000} for s in LIVE.values()]
        snaps += [{"snapshot": s, "state": "SUCCESS",
                   "start_time_in_millis": 1_700_000_000_000} for s in TRUE_ORPHANS]
        snaps.append({"snapshot": SLM_SNAP, "state": "SUCCESS",
                      "metadata": {"policy": "cloud-snapshot-policy"},
                      "start_time_in_millis": 1_754_000_000_000})
        snaps.append({"snapshot": YOUNG_SNAP, "state": "SUCCESS",
                      "start_time_in_millis": int(__import__("time").time() * 1000) - 86_400_000})
        return {"snapshots": snaps}

    # -- client surface --------------------------------------------------
    def get(self, path):
        if "_settings" in path:
            return self._settings_body(path)
        if path.startswith(f"/_snapshot/{REPO}/_all"):
            return self._all_snapshots()
        if "index_details=true" in path or "/_status" in path:
            names = path.split(f"/_snapshot/{REPO}/")[1].split("?")[0].replace("/_status", "")
            return {"snapshots": [
                {"snapshot": n,
                 "index_details": {"i": {"size_in_bytes": 1024}},
                 "stats": {"total": {"size_in_bytes": 1024},
                           "incremental": {"size_in_bytes": 512}}}
                for n in names.split(",")]}
        if path.startswith("/_ilm/policy"):
            return {}
        raise AssertionError("unexpected GET " + path)

    def get_optional(self, path, timeout=None):
        if "_cluster/state" in path:
            return {"metadata": {"indices": {
                i: {"settings": {"index": {"store": {"snapshot": {
                    "snapshot_name": s, "repository_name": REPO,
                    "repository_uuid": "UUID-1"}}}}}
                for i, s in LIVE.items()}}}
        if "_ilm/explain" in path:
            return {"indices": {}}
        if "_current" in path:
            return {"snapshots": []}
        if "_recovery" in path:
            return {}
        if "_cluster/health" in path:
            return {"status": "green", "unassigned_shards": 0}
        return {}

    def delete(self, path, timeout=None):
        names = path.split(f"/_snapshot/{REPO}/")[1]
        self.deleted.extend(names.split(","))
        return {"acknowledged": True}


def run_main(argv, client):
    """Invoke the real main() with the fake client injected."""
    real_client, real_creds = oss.ESClient, oss.resolve_credentials
    oss.ESClient = lambda *a, **k: client
    oss.resolve_credentials = lambda args: ("https://fake:9243", "fake-key")
    out, err = io.StringIO(), io.StringIO()
    real_out, real_err, real_argv = sys.stdout, sys.stderr, sys.argv
    sys.stdout, sys.stderr, sys.argv = out, err, ["prog"] + argv
    code = 0
    try:
        oss.main()
    except SystemExit as e:
        code = e.code
    finally:
        sys.stdout, sys.stderr, sys.argv = real_out, real_err, real_argv
        oss.ESClient, oss.resolve_credentials = real_client, real_creds
    return code, out.getvalue(), err.getvalue()


print("=== A. Dry-run against a cluster with hidden live mounts ===")
c = FakeClient()
code, out, err = run_main(["--report-size"], c)
leaked = [s for s in LIVE.values() if f"orphan: {s}" in err]
check("no live snapshot listed as an orphan", not leaked, str(leaked))
check("both genuine orphans found", all(f"orphan: {s}" in err for s in TRUE_ORPHANS))
check("SLM snapshot excluded", f"orphan: {SLM_SNAP}" not in err)
check("young snapshot held back by the age guard", f"orphan: {YOUNG_SNAP}" not in err)
check("hidden mounts counted as in-use", f"in-use snapshots: {len(LIVE)}" in err,
      next((l.strip() for l in err.splitlines() if "in-use snapshots:" in l), "(not found)"))
check("nothing deleted in dry-run", c.deleted == [])

print()
print("=== B. --apply: only genuine orphans are actually DELETEd ===")
c = FakeClient()
code, out, err = run_main(["--apply", "--yes"], c)
live_deleted = [s for s in LIVE.values() if s in c.deleted]
check("no live snapshot deleted", not live_deleted, str(live_deleted))
check("SLM snapshot not deleted", SLM_SNAP not in c.deleted)
check("young snapshot not deleted", YOUNG_SNAP not in c.deleted)
check("genuine orphans deleted", sorted(c.deleted) == sorted(TRUE_ORPHANS), str(c.deleted))
check("post-delete health reported", "Post-delete cluster health: green" in err)

print()
print("=== C. --apply blocked when the settings query regresses ===")
c = FakeClient(honour_expand_wildcards=False)
code, out, err = run_main(["--apply", "--yes"], c)
check("run aborted", code != 0, f"exit={code}")
check("nothing deleted", c.deleted == [], str(c.deleted))
check("reason names the under-resolving query",
      "only in the cluster state" in str(code) or "only in the cluster state" in err + out,
      str(code))

print()
print("=== D. --apply blocked while a snapshot is running ===")


class BusyClient(FakeClient):
    def get_optional(self, path, timeout=None):
        if "_current" in path:
            return {"snapshots": [{"snapshot": "running"}]}
        return FakeClient.get_optional(self, path)


c = BusyClient()
code, out, err = run_main(["--apply", "--yes"], c)
check("run aborted", code != 0, f"exit={code}")
check("nothing deleted", c.deleted == [])

print()
print("=== E. Routine ILM frozen mount (shard recovering) does NOT block ===")


class RecoveringClient(FakeClient):
    def get_optional(self, path, timeout=None):
        if "_recovery" in path:
            return {"idx": {"shards": [{"type": "SNAPSHOT"}]}}
        return FakeClient.get_optional(self, path)


c = RecoveringClient()
code, out, err = run_main(["--apply", "--yes"], c)
check("run completed", code in (0, None), f"exit={code}")
check("genuine orphans still deleted", sorted(c.deleted) == sorted(TRUE_ORPHANS), str(c.deleted))

print()
print("=== F. --min-snapshot-age-days 0 still cannot delete a live snapshot ===")
c = FakeClient()
code, out, err = run_main(["--apply", "--yes", "--min-snapshot-age-days", "0"], c)
live_deleted = [s for s in LIVE.values() if s in c.deleted]
check("live snapshots still protected by the in-use scan", not live_deleted, str(live_deleted))
check("young snapshot now deletable (guard disabled, as documented)", YOUNG_SNAP in c.deleted)

print()
print("=== G. Guards FAIL CLOSED when their own request fails ===")


class FlakyClient(FakeClient):
    """The advisory endpoints time out -> get_optional returns None (unknown)."""

    def __init__(self, break_paths):
        FakeClient.__init__(self)
        self.break_paths = break_paths

    def get_optional(self, path, timeout=None):
        if any(p in path for p in self.break_paths):
            return None          # what the real client returns after exhausting retries
        return FakeClient.get_optional(self, path, timeout)


for label, paths in [("_ilm/explain unavailable", ["_ilm/explain"]),
                     ("_snapshot/_current unavailable", ["_current"]),
                     ("cluster state unavailable", ["_cluster/state"])]:
    c = FlakyClient(paths)
    code, out, err = run_main(["--apply", "--yes"], c)
    check(f"{label}: --apply refuses", code != 0, f"exit={code}")
    check(f"{label}: nothing deleted", c.deleted == [], str(c.deleted))

c = FlakyClient(["_ilm/explain"])
code, out, err = run_main([], c)          # dry-run must still work
check("dry-run still runs when an advisory endpoint is down", code in (0, None), f"exit={code}")


print()
print("=== H. Confirmation is required before deleting ===")
c = FakeClient()
code, out, err = run_main(["--apply"], c)   # no --yes, stdin not a tty
check("non-interactive --apply without --yes refuses", code != 0, f"exit={code}")
check("nothing deleted without confirmation", c.deleted == [], str(c.deleted))

print()
print("=== I. Plan file ties what you reviewed to what gets deleted ===")
import json as _json
import tempfile

plan_path = os.path.join(tempfile.mkdtemp(), "plan.json")
c = FakeClient()
code, out, err = run_main(["--plan-file", plan_path], c)     # dry-run writes the plan
plan = _json.load(open(plan_path))
check("plan lists exactly the genuine orphans",
      sorted(plan["snapshots"]) == sorted(TRUE_ORPHANS), str(plan["snapshots"]))
check("plan records repo/cluster/version for traceability",
      plan["repo"] == REPO and "tool_version" in plan and "generated_utc" in plan)
check("dry-run with --plan-file deletes nothing", c.deleted == [])

# Apply the plan, but the cluster has since grown a new orphan.
NEW_ORPHAN = "2020.01.01-.ds-appeared-since-the-plan-somepolicy-zzzz"
_orig = FakeClient._all_snapshots


def _with_extra(self):
    d = _orig(self)
    d["snapshots"].append({"snapshot": NEW_ORPHAN, "state": "SUCCESS",
                           "start_time_in_millis": 1_600_000_000_000})
    return d


FakeClient._all_snapshots = _with_extra
c = FakeClient()
code, out, err = run_main(["--apply", "--yes", "--plan-file", plan_path], c)
check("snapshot that appeared after the plan is NOT deleted", NEW_ORPHAN not in c.deleted)
check("only the reviewed snapshots are deleted",
      sorted(c.deleted) == sorted(TRUE_ORPHANS), str(c.deleted))
check("the new arrival is reported to the operator", NEW_ORPHAN in err)
FakeClient._all_snapshots = _orig

# A plan built for another cluster must be rejected outright.
plan["cluster"] = "some-other-cluster"
_json.dump(plan, open(plan_path, "w"))
c = FakeClient()
code, out, err = run_main(["--apply", "--yes", "--plan-file", plan_path], c)
check("plan from a different cluster is rejected", code != 0, f"exit={code}")
check("nothing deleted from a mismatched plan", c.deleted == [])

print()
print("=== J. --max-delete caps the blast radius ===")
c = FakeClient()
code, out, err = run_main(["--apply", "--yes", "--max-delete", "1"], c)
check("run aborted when selection exceeds the cap", code != 0, f"exit={code}")
check("nothing deleted", c.deleted == [], str(c.deleted))
c = FakeClient()
code, out, err = run_main(["--apply", "--yes", "--max-delete", "5"], c)
check("run proceeds when within the cap", sorted(c.deleted) == sorted(TRUE_ORPHANS))

print()
print("=== K. Audit file survives a crash mid-delete ===")
audit_path = os.path.join(tempfile.mkdtemp(), "audit.txt")


class ExplodingClient(FakeClient):
    def delete(self, path, timeout=None):
        raise RuntimeError("connection reset mid-delete")


c = ExplodingClient()
try:
    run_main(["--apply", "--yes", "--audit-file", audit_path], c)
except RuntimeError:
    pass
check("audit file exists despite the crash", os.path.exists(audit_path))
body = open(audit_path).read() if os.path.exists(audit_path) else ""
check("audit records the full attempted set",
      all(s in body for s in TRUE_ORPHANS), f"{len(body)} bytes")
check("audit is stamped with the tool version", "Tool version" in body)

print()
print("=" * 62)
if fails:
    print(f"{len(fails)} CHECK(S) FAILED: {fails}")
    sys.exit(1)
print("ALL INTEGRATION CHECKS PASSED")
