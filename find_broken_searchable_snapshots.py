#!/usr/bin/env python3
"""
find_broken_searchable_snapshots.py

Find every mounted searchable-snapshot index whose backing snapshot is MISSING
from the repository -- i.e. indices that have already been broken by a snapshot
deletion, whether or not they have gone red yet.

Why this exists
---------------
For a frozen index the snapshot IS the data; the node holds only a cache.
Elasticsearch reads the repository when a shard starts or on a cache miss, and at
no other time. So deleting a snapshot a mounted index needs breaks that index
*silently*: the cluster stays green until a shard relocates or a node restarts --
possibly weeks later -- and only then goes red with SnapshotMissingException.

Cluster health therefore tells you nothing about how many indices are already
broken. This tool compares, directly:

    every index's index.store.snapshot.snapshot_name   (resolved with
        expand_wildcards=all AND cross-checked against the cluster state, so
        hidden data-stream mounts are included)
  vs
    every snapshot actually present in the repository

Anything in the first list and not the second is a broken index waiting to fail.

This is READ-ONLY. It never deletes or modifies anything.

Usage
-----
    ./find_broken_searchable_snapshots.py --cluster dev
    ./find_broken_searchable_snapshots.py --cluster dev --json
    ./find_broken_searchable_snapshots.py --es-url https://host:9243 --api-key "$KEY"

Credentials resolve exactly as in orphaned_searchable_snapshots.py:
--cluster reads es_url / es_api_key from AWS Secrets Manager secret
elastic/kibana/dataview_cleanup_<cluster>. API key only.

Required privileges: monitor (cluster), view_index_metadata, and read access to
the snapshot repository.

Exit codes: 0 = nothing broken, 1 = broken indices found, 2 = error.
"""

import argparse
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "_oss", os.path.join(HERE, "orphaned_searchable_snapshots.py"))
_oss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_oss)


def mounted_refs(es, repo):
    """index -> snapshot_name for every mounted searchable-snapshot index.

    Unions the Get Settings result (expand_wildcards=all) with the cluster state,
    so hidden data-stream mounts (partial-.ds-*, restored-.ds-*) and closed
    indices are both included.
    """
    refs = dict(_oss._refs_from_settings(es, repo))
    state = _oss._refs_from_cluster_state(es, repo)
    if state:
        refs.update(state)
    uuids = {r["repo_uuid"] for r in refs.values()
             if r["repo_name"] == repo and r["repo_uuid"]}
    return {i: r["snapshot"] for i, r in refs.items()
            if r["repo_name"] == repo or (r["repo_uuid"] and r["repo_uuid"] in uuids)}, \
        state is not None


def data_stream_of(es, index):
    """The data stream an index backs, or None. Used to build the repair steps."""
    data = es.get_optional("/_data_stream?expand_wildcards=all"
                           "&filter_path=data_streams.name,data_streams.indices.index_name") or {}
    for ds in data.get("data_streams") or []:
        for member in ds.get("indices") or []:
            if member.get("index_name") == index:
                return ds.get("name")
    return None


def main():
    ap = argparse.ArgumentParser(
        description="Find mounted searchable-snapshot indices whose backing snapshot "
                    "is missing from the repository (read-only).")
    ap.add_argument("--cluster", choices=["dev", "qa", "ccs", "prod"],
                    help="Load es_url/es_api_key from AWS Secrets Manager secret "
                         "elastic/kibana/dataview_cleanup_<cluster>.")
    ap.add_argument("--secret-name")
    ap.add_argument("--region")
    ap.add_argument("--es-url")
    ap.add_argument("--api-key")
    ap.add_argument("--repo", default="found-snapshots")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--insecure", action="store_true")
    args = ap.parse_args()

    url, api_key = _oss.resolve_credentials(args)
    if not url:
        sys.exit("ERROR: no Elasticsearch endpoint resolved. Use --cluster, --es-url, "
                 "or the ES_URL environment variable.")
    es = _oss.ESClient(url, api_key=api_key, insecure=args.insecure,
                       timeout=args.timeout, retries=args.retries)

    sys.stderr.write("Reading mounted searchable-snapshot indices...\n")
    refs, state_ok = mounted_refs(es, args.repo)
    sys.stderr.write(f"  mounted searchable-snapshot indices: {len(refs)}\n")
    if not state_ok:
        sys.stderr.write("  WARNING: the cluster state could not be read (needs the 'monitor'\n"
                         "           cluster privilege). Hidden indices may be under-reported --\n"
                         "           this result is NOT authoritative without it.\n")

    sys.stderr.write(f"Listing snapshots present in {args.repo}...\n")
    present = {s["name"] for s in _oss.list_all_snapshots(es, args.repo)}
    sys.stderr.write(f"  snapshots in repository: {len(present)}\n")

    broken = sorted((idx, snap) for idx, snap in refs.items() if snap not in present)

    health = es.get_optional("/_cluster/health") or {}
    result = {
        "repo": args.repo,
        "cluster_state_crosscheck": state_ok,
        "mounted_indices": len(refs),
        "snapshots_in_repo": len(present),
        "broken_count": len(broken),
        "cluster_status": health.get("status"),
        "unassigned_shards": health.get("unassigned_shards"),
        "broken": [{"index": i, "missing_snapshot": s} for i, s in broken],
    }

    if args.json:
        print(json.dumps(result, indent=2))
        return 1 if broken else 0

    print("\n============ MOUNTED INDICES WITH A MISSING SNAPSHOT ============")
    print(f"  repository                       : {args.repo}")
    print(f"  mounted searchable-snapshot idx  : {len(refs)}")
    print(f"  snapshots present in repository  : {len(present)}")
    print(f"  cluster-state cross-check        : {'yes' if state_ok else 'NO (not authoritative)'}")
    print(f"  cluster status                   : {health.get('status')} "
          f"({health.get('unassigned_shards')} unassigned shards)")
    print(f"  BROKEN indices                   : {len(broken)}")
    print("=================================================================")

    if not broken:
        print("\nNo mounted index is missing its backing snapshot.")
        print("Every searchable-snapshot index can still find its data in the repository.")
        return 0

    print("\nThese indices reference a snapshot that no longer exists. They will fail")
    print("on their next shard start or cache miss (node restart, shard relocation),")
    print("even if the cluster is green right now.\n")
    for idx, snap in broken:
        ds = data_stream_of(es, idx)
        print(f"  index          : {idx}")
        print(f"  missing snapshot: {snap}")
        print(f"  data stream    : {ds or '(none)'}")
        print()

    print("NEXT STEPS")
    print("  1. Check whether any snapshot taken BEFORE the deletion still contains the")
    print("     index's data:")
    print(f"       GET _snapshot/{args.repo}/_all?index_details=true")
    print("     NOTE: an SLM cloud-snapshot-* taken while the index was already mounted")
    print("     stores only a POINTER to the searchable snapshot, not the data. If the")
    print("     original snapshot is gone, those backups cannot rebuild the index.")
    print("  2. If a usable snapshot exists, follow the repair sequence: remove the")
    print("     backing index from its data stream, delete the index, clone that snapshot,")
    print("     re-mount with storage=shared_cache, move ILM to frozen/complete, then")
    print("     re-add the index to the data stream.")
    print("  3. If no usable snapshot exists, the data is unrecoverable and the index")
    print("     must be deleted to return the cluster to green.")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(2)
