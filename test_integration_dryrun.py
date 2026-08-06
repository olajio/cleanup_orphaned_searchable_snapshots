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

    def get_optional(self, path):
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

    def delete(self, path):
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
code, out, err = run_main(["--apply"], c)
live_deleted = [s for s in LIVE.values() if s in c.deleted]
check("no live snapshot deleted", not live_deleted, str(live_deleted))
check("SLM snapshot not deleted", SLM_SNAP not in c.deleted)
check("young snapshot not deleted", YOUNG_SNAP not in c.deleted)
check("genuine orphans deleted", sorted(c.deleted) == sorted(TRUE_ORPHANS), str(c.deleted))
check("post-delete health reported", "Post-delete cluster health: green" in err)

print()
print("=== C. --apply blocked when the settings query regresses ===")
c = FakeClient(honour_expand_wildcards=False)
code, out, err = run_main(["--apply"], c)
check("run aborted", code != 0, f"exit={code}")
check("nothing deleted", c.deleted == [], str(c.deleted))
check("reason names the under-resolving query",
      "only in the cluster state" in str(code) or "only in the cluster state" in err + out,
      str(code))

print()
print("=== D. --apply blocked while a snapshot is running ===")


class BusyClient(FakeClient):
    def get_optional(self, path):
        if "_current" in path:
            return {"snapshots": [{"snapshot": "running"}]}
        return FakeClient.get_optional(self, path)


c = BusyClient()
code, out, err = run_main(["--apply"], c)
check("run aborted", code != 0, f"exit={code}")
check("nothing deleted", c.deleted == [])

print()
print("=== E. Routine ILM frozen mount (shard recovering) does NOT block ===")


class RecoveringClient(FakeClient):
    def get_optional(self, path):
        if "_recovery" in path:
            return {"idx": {"shards": [{"type": "SNAPSHOT"}]}}
        return FakeClient.get_optional(self, path)


c = RecoveringClient()
code, out, err = run_main(["--apply"], c)
check("run completed", code in (0, None), f"exit={code}")
check("genuine orphans still deleted", sorted(c.deleted) == sorted(TRUE_ORPHANS), str(c.deleted))

print()
print("=== F. --min-snapshot-age-days 0 still cannot delete a live snapshot ===")
c = FakeClient()
code, out, err = run_main(["--apply", "--min-snapshot-age-days", "0"], c)
live_deleted = [s for s in LIVE.values() if s in c.deleted]
check("live snapshots still protected by the in-use scan", not live_deleted, str(live_deleted))
check("young snapshot now deletable (guard disabled, as documented)", YOUNG_SNAP in c.deleted)

print()
print("=" * 62)
if fails:
    print(f"{len(fails)} CHECK(S) FAILED: {fails}")
    sys.exit(1)
print("ALL INTEGRATION CHECKS PASSED")
