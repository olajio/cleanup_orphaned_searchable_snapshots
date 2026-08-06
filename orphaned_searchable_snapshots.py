#!/usr/bin/env python3
"""
orphaned_searchable_snapshots.py

Find, size, and (optionally) delete ORPHANED searchable snapshots -- snapshots in
the `found-snapshots` repository that are no longer referenced by any mounted
searchable-snapshot index AND are not managed by SLM. These are the leftovers from a
frozen index being deleted (or its ILM policy changed) without ILM running the delete
phase. Snapshots created by Snapshot Lifecycle Management (SLM) -- e.g. the periodic
cloud-snapshot-* backups -- are retired by SLM's own retention, so they are never
treated as orphans.

Single tool for the whole workflow:
  * default            -> DRY-RUN: list the orphans, change nothing.
  * --report-size      -> also report how much repository storage they occupy.
  * --check-ilm        -> also flag ILM policies that will create future orphans.
  * --ilm-review-file  -> log now-compliant policies that leaked orphans in the past.
  * --apply            -> delete the orphans (dry-run unless this is given).

How it works
------------
1. Collect the set of snapshots currently IN USE, from the settings of mounted
   searchable-snapshot indices (index.store.snapshot.snapshot_name), resolved with
   expand_wildcards=all and cross-checked against the cluster state.
2. List every snapshot in the repository (with its state and start time).
3. Orphans = all snapshots - in-use snapshots - SLM-managed snapshots (those whose
   metadata.policy names an SLM policy), optionally filtered by --pattern, then
   narrowed by the safety filters below.
4. --report-size: sum the orphans' storage. By default this uses the Get Snapshot
   API's index_details (snapshot metadata) for the total logical size -- fast and
   safe on large repos. Add --incremental for the dedup-aware "reclaimable" size
   from the _status API (slower/heavier; --incremental implies --report-size).
5. --apply: re-check the in-use set, then delete the orphans.

Why the safety filters exist (incident, 2026-07)
------------------------------------------------
A frozen index keeps no full local copy of its data -- the snapshot IS the data.
Deleting the snapshot a mounted index references destroys that index, and it does
so SILENTLY: Elasticsearch only reads the repository on shard start or a cache
miss, so the cluster stays green until a shard relocates or a node restarts, then
goes red with SnapshotMissingException. In one run this tool deleted six live
snapshots and the damage surfaced two weeks later.

The cause was index-name expansion. `GET /_all/_settings/...` does NOT match
hidden indices, and EVERY data-stream backing index is hidden -- including the
searchable-snapshot mount that replaces it (partial-.ds-*, restored-.ds-*). Those
mounts were invisible, so their live snapshots looked orphaned. The default
expand_wildcards=open hides closed indices for the same reason.

Guards now in place:
  * expand_wildcards=all on the settings query (hidden + closed included), plus a
    cluster-state cross-check that involves no wildcard expansion at all.
  * --min-snapshot-age-days (default 14): ILM creates a snapshot, then mounts it,
    then deletes the source index. Between the first two steps the snapshot has no
    referencing index. Young snapshots are never orphans in practice.
  * snapshots not in state SUCCESS are skipped (still being written).
  * indices ILM is mid-searchable_snapshot on are detected and their snapshots held
    back (--allow-ilm-in-flight to override).
  * the in-use set is re-read immediately before the delete (--skip-recheck to
    override), and the run aborts if that set shrank during the scan.
  * cluster health is reported after the delete, with a reminder that breakage can
    take weeks to appear.

Why not always use _status? The _status API reads every shard's file listing from
the repository (object storage) and is very slow on large repos -- on the QA repo it
timed out. index_details reads small per-index metadata blobs instead, so the fast
path avoids that. All requests also retry with backoff on read timeouts / 429 / 5xx
(see --timeout / --retries).

Snapshot names are placed in the request URL for both _status and DELETE. Because
Elasticsearch caps the HTTP request line at http.max_initial_line_length (default
4kb), requests are split into batches whose URL stays under MAX_URL_BYTES -- this
avoids the too_long_http_line_exception you hit with a naive fixed batch count.

Connection -- API key only (no basic auth)
------------------------------------------
Recommended: fetch credentials from AWS Secrets Manager with --cluster. The secret
is named `elastic/kibana/dataview_cleanup_<cluster>` and must contain the keys:
    es_url      -> the Elasticsearch endpoint
    es_api_key  -> the API key ("encoded" value)

    dev  -> elastic/kibana/dataview_cleanup_dev
    qa   -> elastic/kibana/dataview_cleanup_qa
    ccs  -> elastic/kibana/dataview_cleanup_ccs
    prod -> elastic/kibana/dataview_cleanup_prod

Resolution order for the endpoint and API key (first match wins):
    1. explicit --es-url / --api-key flags
    2. AWS Secrets Manager (when --cluster or --secret-name is given)
    3. environment variables ES_URL / ES_API_KEY

Usage
-----
  # DRY-RUN: list orphans for a cluster (credentials from AWS Secrets Manager)
  ./orphaned_searchable_snapshots.py --cluster prod

  # list + report storage occupied (read-only)
  ./orphaned_searchable_snapshots.py --cluster dev --report-size

  # actually delete only the 2023 orphans
  ./orphaned_searchable_snapshots.py --cluster qa --pattern '2023.*' --apply

  # or supply credentials directly / via environment variables
  ./orphaned_searchable_snapshots.py --es-url https://host:9243 --api-key "$KEY"
  ES_URL=... ES_API_KEY=... ./orphaned_searchable_snapshots.py

Options
-------
  --cluster {dev,qa,ccs,prod}  Load es_url/es_api_key from AWS Secrets Manager
                               secret elastic/kibana/dataview_cleanup_<cluster>.
  --secret-name NAME           Override the derived AWS secret name.
  --region NAME                AWS region for Secrets Manager (else default chain).
  --es-url URL      Elasticsearch endpoint (overrides secret and ES_URL)
  --api-key KEY     API key, "encoded" value (overrides secret and ES_API_KEY)
  --repo NAME       Snapshot repository (default: found-snapshots)
  --pattern GLOB    Only act on orphans whose name matches this glob (default: '*')
  --report-size     Report storage used by the orphans (fast; index_details metadata).
  --incremental     With --report-size, also compute the dedup-aware reclaimable size
                    via _status (slower). Implies --report-size.
  --check-ilm       Analyse ILM policies and flag culprits that create searchable
                    snapshots but won't let ILM delete them (source of future orphans).
                    Also writes a corrected PUT _ilm/policy body for each culprit to
                    corrected_ilm_policies/<cluster>/<policy>.json.
  --apply           Delete the orphans (without this, the tool is a dry run).
  --batch N         Max snapshots per request (also bounded by URL length; default 50)
  --timeout N       Per-request read timeout in seconds (default 120)
  --retries N       Retries with backoff on read timeouts / 429 / 5xx (default 3)
  --per-snapshot    Print the largest 25 orphans with their size (implies --report-size;
                    use --json for the full per-snapshot list).
  --audit-file PATH Write the FULL orphan list plus summary/analysis to a text file (the
                    on-screen output still shows only the top 25). For audit records.
  --ilm-review-file PATH
                    Write a review of ILM policies: the CURRENTLY-offending ones (that will
                    create new orphans) plus policies compliant now that appear to have
                    leaked in the past (with last-updated date; post-update leaks flagged
                    NEEDS REVIEW). Implies --check-ilm.
  --frozen-usage    Estimate what % of the frozen tier's searchable-snapshot storage the
                    orphans occupy (also sizes the in-use mounted snapshots, logical).
                    Implies --report-size.
  --frozen-tier-capacity [SIZE]
                    Report reclaimable (incremental) orphan storage as a % of the TOTAL
                    frozen-tier object-store storage. Pass a size inline (e.g. 60TiB,
                    units GiB/TiB) or give the flag with no value to be prompted. Read the
                    number from the Elastic Cloud console. Implies --incremental. If you run
                    with --incremental but omit this flag, you are prompted for the capacity
                    (skippable).
  --json            Emit the report as JSON instead of text
  --insecure        Skip TLS verification (not recommended)

Requires: Python 3.7+. AWS Secrets Manager lookups use boto3 if installed, else
fall back to the `aws` CLI.
"""

import argparse
import copy
import datetime
import fnmatch
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SECRET_PREFIX = "elastic/kibana/dataview_cleanup_"
VALID_CLUSTERS = ("dev", "qa", "ccs", "prod")
# Snapshot names go in the request URL; keep each request line under Elasticsearch's
# http.max_initial_line_length (default 4kb / 4096 bytes). 3500 leaves safe margin.
MAX_URL_BYTES = 3500
# Sentinel meaning "prompt for the value" when --frozen-tier-capacity is given without one.
PROMPT_SENTINEL = "__PROMPT__"
# Binary (1024-based) unit factors, matching the Elastic Cloud console and human().
CAPACITY_UNITS = {"gib": 1024 ** 3, "gb": 1024 ** 3, "tib": 1024 ** 4, "tb": 1024 ** 4}


def human(n):
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if n < 1024 or unit == "PiB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def url_batches(names, path_overhead, max_count, max_url_bytes=MAX_URL_BYTES):
    """Yield lists of names so that path_overhead + len(comma-joined names) stays
    under max_url_bytes, and each batch holds at most max_count names."""
    batch = []
    length = path_overhead
    for n in names:
        add = len(n) + (1 if batch else 0)  # +1 for the comma separator
        if batch and (len(batch) >= max_count or length + add > max_url_bytes):
            yield batch
            batch = []
            length = path_overhead
            add = len(n)
        batch.append(n)
        length += add
    if batch:
        yield batch


def _get_secret_string(secret_name, region):
    """Return the raw SecretString for secret_name, via boto3 or the aws CLI."""
    try:
        import boto3  # type: ignore
        from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
    except ImportError:
        boto3 = None
    if boto3 is not None:
        try:
            client = boto3.client("secretsmanager", **({"region_name": region} if region else {}))
            return client.get_secret_value(SecretId=secret_name)["SecretString"]
        except (BotoCoreError, ClientError) as e:
            sys.exit(f"ERROR: failed to read AWS secret '{secret_name}': {e}")

    import shutil
    import subprocess
    if not shutil.which("aws"):
        sys.exit("ERROR: reading AWS Secrets Manager needs either boto3 or the aws CLI, "
                 "and neither is available.")
    cmd = ["aws", "secretsmanager", "get-secret-value",
           "--secret-id", secret_name, "--query", "SecretString", "--output", "text"]
    if region:
        cmd += ["--region", region]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except subprocess.CalledProcessError as e:
        sys.exit(f"ERROR: aws CLI failed to read secret '{secret_name}':\n{e.stderr.strip()}")


def fetch_secret_creds(secret_name, region):
    """Return (es_url, es_api_key) from an AWS Secrets Manager JSON secret."""
    raw = _get_secret_string(secret_name, region)
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        sys.exit(f"ERROR: AWS secret '{secret_name}' is not valid JSON.")
    es_url = data.get("es_url")
    es_api_key = data.get("es_api_key")
    missing = [k for k, v in (("es_url", es_url), ("es_api_key", es_api_key)) if not v]
    if missing:
        sys.exit(f"ERROR: AWS secret '{secret_name}' is missing key(s): {', '.join(missing)}. "
                 "It must contain both 'es_url' and 'es_api_key'.")
    return es_url, es_api_key


# HTTP status codes worth retrying (transient / overloaded), rather than aborting.
RETRYABLE_STATUS = {429, 502, 503, 504}


class HttpError(str):
    """An HTTP failure that also carries the status code (str for easy messaging)."""

    def __new__(cls, code, body):
        self = str.__new__(cls, f"HTTP {code}\n{body}".rstrip())
        self.code = code
        return self


class ESClient:
    def __init__(self, url, api_key, insecure=False, timeout=120, retries=3):
        if not api_key:
            sys.exit("ERROR: no API key resolved. Use --cluster (AWS Secrets Manager), "
                     "--api-key, or the ES_API_KEY environment variable.")
        self.base = url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"ApiKey {api_key}",
        }
        self.ctx = None
        if insecure:
            self.ctx = ssl.create_default_context()
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def _attempt(self, method, path, timeout):
        """One HTTP attempt. Returns (result, error, retryable)."""
        req = urllib.request.Request(self.base + path, headers=self.headers, method=method)
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=timeout) as resp:
                body = resp.read().decode()
                return (json.loads(body) if body else {}), None, False
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            return None, HttpError(e.code, body), e.code in RETRYABLE_STATUS
        except (socket.timeout, TimeoutError):
            return None, f"read timed out after {timeout}s", True
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                return None, f"read timed out after {timeout}s", True
            return None, str(e.reason), False

    def _request(self, method, path, timeout=None, soft=False, ok_status=()):
        """Issue a request with retry/backoff.

        soft=False -> any unrecoverable failure exits the process. Used for every call
        whose result feeds the orphan decision: a silent empty result there would be
        read as "nothing references this snapshot", which is exactly how live data gets
        deleted. Failing loudly is the only safe behaviour.

        soft=True  -> returns None instead of exiting, for advisory/cross-check calls
        that the caller knows how to treat as "unknown" (never as "clear").
        """
        timeout = timeout or self.timeout
        last = None
        for attempt in range(self.retries + 1):
            result, err, retryable = self._attempt(method, path, timeout)
            if err is None:
                return result
            if isinstance(err, HttpError) and err.code in ok_status:
                return {}
            last = err
            if not retryable or attempt >= self.retries:
                break
            backoff = 2 ** (attempt + 1)
            sys.stderr.write(f"  (retry {attempt + 1}/{self.retries} after {err}; "
                             f"waiting {backoff}s)\n")
            time.sleep(backoff)
        if soft:
            return None
        hint = ""
        if isinstance(last, str) and "timed out" in last:
            hint = ("\nTry a smaller --batch, a larger --timeout, or scope with --pattern.")
        sys.exit(f"ERROR: {method} {path} -> {last}{hint}")

    def get(self, path):
        return self._request("GET", path)

    def delete(self, path, timeout=None):
        # 404 == the snapshot is already gone. That is the expected outcome when a
        # delete succeeded server-side but the response timed out and we retried, so
        # treat it as success rather than aborting a half-finished cleanup.
        return self._request("DELETE", path, timeout=timeout, ok_status=(404,))

    def get_optional(self, path, timeout=None):
        """GET that returns None (== "could not determine") instead of exiting.

        Retries exactly like get(). Callers MUST treat None as unknown, never as an
        empty/clear result -- a guard that silently passes when it could not run is
        worse than no guard.
        """
        return self._request("GET", path, timeout=timeout, soft=True)


# ---------------------------------------------------------------------------
# In-use (mounted) snapshot detection
#
# CRITICAL CORRECTNESS NOTE -- read before changing anything here.
#
# For a frozen/cold index the snapshot IS the data: deleting the snapshot that a
# mounted index references destroys that index permanently. So the "in use" set
# must be COMPLETE; any index we fail to see turns its live snapshot into a false
# orphan.
#
# Elasticsearch's index-name expansion hides two whole classes of index by default:
#   * HIDDEN indices -- every data-stream backing index sets index.hidden: true,
#     and so does the searchable-snapshot mount that replaces it
#     (partial-.ds-*, restored-.ds-*). `_all` and `*` DO NOT match hidden indices
#     unless expand_wildcards includes "hidden".
#   * CLOSED indices -- the default expand_wildcards=open skips them, yet a closed
#     mounted index still references its snapshot.
#
# Both are resolved by passing expand_wildcards=all. As a second line of defence
# the set is cross-checked against the cluster state, which lists index metadata
# directly and involves no wildcard expansion at all.
# ---------------------------------------------------------------------------

def _refs_from_settings(es, repo):
    """index -> snapshot_name, via the Get Settings API (expand_wildcards=all)."""
    data = es.get("/_all/_settings/index.store.snapshot.*"
                  "?flat_settings=true&expand_wildcards=all"
                  "&ignore_unavailable=true&allow_no_indices=true")
    refs = {}
    for index, body in (data or {}).items():
        s = (body or {}).get("settings", {})
        name = s.get("index.store.snapshot.snapshot_name")
        if not name:
            continue
        refs[index] = {
            "snapshot": name,
            "repo_name": s.get("index.store.snapshot.repository_name"),
            "repo_uuid": s.get("index.store.snapshot.repository_uuid"),
        }
    return refs


def _refs_from_cluster_state(es, repo):
    """index -> {snapshot, repo_name, repo_uuid}, read from the cluster state metadata.

    Independent cross-check: the cluster state enumerates index metadata directly,
    so it is immune to the hidden/closed wildcard-expansion rules that made the Get
    Settings call miss mounted indices. Returns None when the call is unavailable
    (e.g. the API key lacks the privilege) so the caller can degrade to a warning.
    """
    data = es.get_optional("/_cluster/state/metadata"
                           "?filter_path=metadata.indices.*.settings.index.store.snapshot")
    if data is None:
        return None
    indices = ((data.get("metadata") or {}).get("indices") or {})
    refs = {}
    for index, body in indices.items():
        snap = ((((body or {}).get("settings") or {}).get("index")
                 or {}).get("store") or {}).get("snapshot") or {}
        name = snap.get("snapshot_name")
        if not name:
            continue
        refs[index] = {
            "snapshot": name,
            "repo_name": snap.get("repository_name"),
            "repo_uuid": snap.get("repository_uuid"),
        }
    return refs


def collect_in_use(es, repo):
    """Snapshot names referenced by mounted searchable-snapshot indices.

    Returns (in_use:set, info:dict). `info` records what each source saw so the
    caller can report -- and refuse to act on -- a disagreement between them.

    Repository matching is by NAME *or* UUID. The same object-store bucket can be
    registered under more than one repository name; a mount recorded against the
    other name points at the very same blobs, so name-only matching would classify
    its live snapshot as an orphan.
    """
    settings_refs = _refs_from_settings(es, repo)
    state_refs = _refs_from_cluster_state(es, repo)

    # Any repository UUID seen behind the target repo name is the same bucket.
    all_refs = dict(settings_refs)
    if state_refs:
        all_refs.update(state_refs)
    repo_uuids = {r["repo_uuid"] for r in all_refs.values()
                  if r["repo_name"] == repo and r["repo_uuid"]}

    def matches(r):
        return r["repo_name"] == repo or (r["repo_uuid"] and r["repo_uuid"] in repo_uuids)

    def snaps(refs):
        return {r["snapshot"] for r in refs.values() if matches(r)}

    in_use = snaps(settings_refs)
    aliased = sorted({i for i, r in all_refs.items()
                      if r["repo_name"] != repo and matches(r)})
    info = {
        "settings_indices": len([r for r in settings_refs.values() if matches(r)]),
        "cluster_state_available": state_refs is not None,
        "cluster_state_indices": (len([r for r in state_refs.values() if matches(r)])
                                  if state_refs is not None else None),
        "only_in_cluster_state": [],
        "alias_repo_indices": aliased,
    }
    if state_refs is not None:
        # Union, never intersect: a reference seen by EITHER source means the
        # snapshot is live. Anything only the cluster state saw is a bug in the
        # settings query (this is exactly how hidden data-stream mounts were missed).
        state_snaps = snaps(state_refs)
        missed = {i: r["snapshot"] for i, r in state_refs.items()
                  if matches(r) and r["snapshot"] not in in_use}
        info["only_in_cluster_state"] = sorted(missed.items())
        in_use |= state_snaps
    info["total"] = len(in_use)
    return in_use, info


def in_progress_repo_operations(es, repo):
    """(snapshots_running, restores_running) for the repository.

    Either value is None when it could NOT be determined (timeout, 429, missing
    privilege). Callers must treat None as "unknown" and block, not as "idle" -- a
    guard that silently passes because its own request failed is worse than no guard.
    """
    running = es.get_optional(f"/_snapshot/{repo}/_current?ignore_unavailable=true")
    snaps = None if running is None else len(running.get("snapshots") or [])

    rec = es.get_optional("/_recovery?active_only=true&expand_wildcards=all"
                          "&filter_path=*.shards.type")
    if rec is None:
        return snaps, None
    restores = 0
    for body in rec.values():
        for shard in (body or {}).get("shards") or []:
            if (shard or {}).get("type") == "SNAPSHOT":
                restores += 1
    return snaps, restores


def ilm_indices_in_flight(es):
    """Indices ILM currently has inside the searchable_snapshot action.

    While that action runs there is a window where the snapshot already exists in
    the repository but nothing references it yet -- it looks exactly like an orphan.
    Returns a set of index names, or None when the call could not be made -- which the
    caller must treat as "unknown", not as "nothing in flight".
    """
    data = es.get_optional("/*/_ilm/explain?expand_wildcards=all&only_managed=true"
                           "&filter_path=indices.*.index,indices.*.action,indices.*.step")
    if data is None:
        return None
    out = set()
    for index, body in (data.get("indices") or {}).items():
        if (body or {}).get("action") == "searchable_snapshot":
            out.add((body or {}).get("index") or index)
    return out


def inflight_conflicts(orphans, inflight):
    """Candidate orphans whose name embeds an index ILM is mid-snapshot on.

    ILM names the snapshot it creates `<date>-<index>-<policy>-<uuid>`, so a
    candidate containing an in-flight index name is almost certainly that action's
    snapshot rather than a real orphan.
    """
    if not inflight:
        return []
    bases = set()
    for idx in inflight:
        base = idx
        for prefix in ("partial-", "restored-"):
            if base.startswith(prefix):
                base = base[len(prefix):]
        bases.add(base)
    return sorted(s for s in orphans if any(b in s for b in bases))


def list_all_snapshots(es, repo):
    """Return a list of dicts describing every snapshot in the repo.

    Each entry: {"name", "slm", "state", "start_ms"}.

    `slm` is the SLM policy managing the snapshot -- taken from the snapshot's
    metadata.policy, which Snapshot Lifecycle Management stamps onto snapshots it
    creates. It is None for snapshots not created by SLM (e.g. ILM searchable
    snapshots, manual snapshots). SLM-managed snapshots (e.g. the periodic
    cloud-snapshot-* backups) are retired by SLM's own retention, so they are NOT
    orphans even though no mounted index references them.

    `state` and `start_ms` drive the safety guards: a snapshot that is not SUCCESS
    is still being written, and a very young snapshot may be an ILM mount in flight.

    PAGINATION: Elasticsearch 8.3+ may TRUNCATE this response and report how many
    snapshots it withheld in `remaining`. A truncated listing is dangerous in both
    directions -- it hides snapshots from the orphan scan, and it makes a live
    snapshot look absent to the broken-index check -- so when the server reports
    more to come, the rest is fetched with the size/after cursor. Older versions
    that do not support those parameters never set `remaining`, so they never take
    the paginated path.
    """
    out = []
    seen = set()

    def absorb(data):
        for snap in data.get("snapshots", []):
            name = snap.get("snapshot")
            if not name or name in seen:
                continue
            seen.add(name)
            out.append({
                "name": name,
                "slm": (snap.get("metadata") or {}).get("policy"),
                "state": snap.get("state"),
                "start_ms": snap.get("start_time_in_millis"),
            })

    data = es.get(f"/_snapshot/{repo}/_all?ignore_unavailable=true")
    absorb(data)
    remaining = data.get("remaining")
    if remaining:
        sys.stderr.write(f"  (response truncated: {remaining} more snapshot(s); paginating)\n")
        after = data.get("next")
        guard = 0
        while after and guard < 1000:
            guard += 1
            page = es.get(f"/_snapshot/{repo}/_all?ignore_unavailable=true"
                          f"&size=1000&sort=start_time&after={urllib.parse.quote(after)}")
            before = len(out)
            absorb(page)
            after = page.get("next")
            if len(out) == before:
                break  # no progress -- stop rather than loop forever
        sys.stderr.write(f"  (paginated total: {len(out)} snapshot(s))\n")
    return out


def size_via_index_details(es, repo, names, batch, sleep=0.0):
    """Fast sizing: total (logical) size per snapshot from the Get Snapshot API's
    index_details (snapshot metadata), NOT the heavy _status shard scan.

    Returns (total_bytes, per_snapshot dict) where per[name] = {"total": bytes}.
    index_details reads small per-index metadata blobs, so it is far cheaper than
    _status and does not time out on large repositories. It does not expose the
    dedup-aware 'incremental' figure -- use --incremental (size_via_status) for that.
    """
    overhead = len(f"/_snapshot/{repo}/") + len("?index_details=true&ignore_unavailable=true")
    total = 0
    per = {}
    done = 0
    for chunk in url_batches(names, overhead, batch):
        csv = ",".join(chunk)
        data = es.get(f"/_snapshot/{repo}/{csv}?index_details=true&ignore_unavailable=true")
        for snap in data.get("snapshots", []):
            t = sum(idx.get("size_in_bytes", 0)
                    for idx in snap.get("index_details", {}).values())
            total += t
            per[snap.get("snapshot")] = {"total": t}
        done += len(chunk)
        sys.stderr.write(f"  ...sized {done}/{len(names)}\n")
        if sleep and done < len(names):
            time.sleep(sleep)
    return total, per


def size_via_status(es, repo, names, batch, sleep=0.0):
    """Precise sizing via _status: returns (total_bytes, incremental_bytes, per).
    per[name] = {"total": bytes, "incremental": bytes}. The _status API reads
    per-shard file listings from the repository and is SLOW/heavy on large repos --
    it is opt-in via --incremental and benefits from a smaller --batch."""
    overhead = len(f"/_snapshot/{repo}/") + len("/_status?ignore_unavailable=true")
    total = incr = 0
    per = {}
    done = 0
    for chunk in url_batches(names, overhead, batch):
        csv = ",".join(chunk)
        data = es.get(f"/_snapshot/{repo}/{csv}/_status?ignore_unavailable=true")
        for snap in data.get("snapshots", []):
            stats = snap.get("stats", {})
            t = stats.get("total", {}).get("size_in_bytes", 0)
            inc = stats.get("incremental", {}).get("size_in_bytes", 0)
            total += t
            incr += inc
            per[snap.get("snapshot")] = {"total": t, "incremental": inc}
        done += len(chunk)
        sys.stderr.write(f"  ...sized {done}/{len(names)}\n")
        if sleep and done < len(names):
            time.sleep(sleep)
    return total, incr, per


def delete_snapshots(es, repo, names, batch, timeout=None, sleep=0.0):
    """Delete the given snapshots in URL-length-bounded batches. Returns count.

    Snapshot deletion is serialised per repository and rewrites repository metadata,
    so a large batch can take far longer than an ordinary read -- hence the separate,
    longer timeout (--delete-timeout). A 404 is treated as success by the client: if
    the delete completed server-side but the response timed out, the retry finds the
    snapshot already gone, and aborting there would leave the cleanup half-finished.
    """
    overhead = len(f"/_snapshot/{repo}/")
    deleted = 0
    for chunk in url_batches(names, overhead, batch):
        csv = ",".join(chunk)
        es.delete(f"/_snapshot/{repo}/{csv}", timeout=timeout)
        deleted += len(chunk)
        sys.stderr.write(f"  ...deleted {deleted}/{len(names)}\n")
        if sleep and deleted < len(names):
            time.sleep(sleep)
    return deleted


def analyze_ilm_policies(es):
    """Fetch _ilm/policy and describe every policy that uses searchable_snapshot.

    Returns dict: policy_name -> {
        'modified_date': str|None,     # ILM policy's last-updated timestamp
        'ss_phases': [...],            # phases with a searchable_snapshot action
        'offending': bool,             # currently leaks (no delete phase / dss:false)
        'reason': str|None,            # why it is offending, else None
        'phases': {...},               # the policy's full phases (for building a fix)
    }

    delete_searchable_snapshot defaults to True, so a policy currently leaks only when it
    takes a searchable_snapshot but (a) has no delete phase/action, or (b) sets
    delete_searchable_snapshot: false.
    """
    data = es.get("/_ilm/policy")
    out = {}
    for name, body in data.items():
        phases = body.get("policy", {}).get("phases", {})
        ss_phases = [ph for ph, cfg in phases.items()
                     if "searchable_snapshot" in cfg.get("actions", {})]
        if not ss_phases:
            continue
        delete_actions = phases.get("delete", {}).get("actions", {})
        offending, reason = False, None
        if "delete" not in delete_actions:
            offending = True
            reason = "no delete phase -> ILM never deletes the searchable snapshot"
        elif delete_actions["delete"].get("delete_searchable_snapshot", True) is False:
            offending = True
            reason = "delete phase sets delete_searchable_snapshot: false"
        out[name] = {
            "modified_date": body.get("modified_date"),
            "ss_phases": ss_phases,
            "offending": offending,
            "reason": reason,
            "phases": phases,
        }
    return out


# Placeholder retention for a delete phase we ADD to a policy that has none. It must be a
# valid ILM duration so the file is applyable as-is, but it is a guess -- review before use.
DEFAULT_DELETE_MIN_AGE = "365d"


def build_corrected_policy(phases):
    """Return a corrected PUT _ilm/policy body ({"policy": {"phases": ...}}) that stops the
    policy leaking searchable snapshots, plus a note describing what was changed.

    - delete phase with delete_searchable_snapshot: false  -> flip it to true.
    - no delete phase / no delete action                   -> add a delete phase with
      delete_searchable_snapshot: true (min_age is a placeholder to review).
    Returns (body_dict, note_str).
    """
    fixed = copy.deepcopy(phases)
    delete_actions = fixed.get("delete", {}).get("actions", {})
    if "delete" in delete_actions:
        prev = delete_actions["delete"].get("delete_searchable_snapshot", True)
        delete_actions["delete"]["delete_searchable_snapshot"] = True
        note = ("set delete_searchable_snapshot: true in the existing delete phase "
                f"(was {json.dumps(prev)})")
    else:
        fixed["delete"] = {
            "min_age": DEFAULT_DELETE_MIN_AGE,
            "actions": {"delete": {"delete_searchable_snapshot": True}},
        }
        note = (f"added a delete phase (min_age {DEFAULT_DELETE_MIN_AGE} -- REVIEW this "
                "retention) with delete_searchable_snapshot: true")
    return {"policy": {"phases": fixed}}, note


def write_corrected_policies(cluster, ilm_index):
    """Write a corrected PUT _ilm/policy body for every offending policy to
    corrected_ilm_policies/<cluster>/<policy>.json. Returns list of (path, note)."""
    offenders = {n: v for n, v in ilm_index.items() if v["offending"]}
    if not offenders:
        return []
    out_dir = os.path.join("corrected_ilm_policies", cluster)
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        sys.stderr.write(f"WARNING: could not create '{out_dir}': {e}\n")
        return []
    written = []
    for name, info in sorted(offenders.items()):
        body, note = build_corrected_policy(info.get("phases", {}))
        path = os.path.join(out_dir, f"{name}.json")
        try:
            with open(path, "w") as fh:
                json.dump(body, fh, indent=2)
                fh.write("\n")
            written.append((path, note))
        except OSError as e:
            sys.stderr.write(f"WARNING: could not write '{path}': {e}\n")
    if written:
        sys.stderr.write(f"Wrote {len(written)} corrected ILM policy file(s) to {out_dir}/\n")
    return written


def offending_from_index(ilm_index):
    """Build the offending-policy list (shape used by print_ilm_section) from the index."""
    return [
        {"policy": n, "searchable_snapshot_phases": v["ss_phases"], "reason": v["reason"]}
        for n, v in sorted(ilm_index.items()) if v["offending"]
    ]


def _best_policy_match(name, policy_names):
    """Return the policy whose '-<policy>-' marker sits closest to the END of the snapshot
    name (rightmost wins; longer name breaks ties), or None. Searchable-snapshot names are
    {date}-{index}-{ilm-policy}-{uuid}, so the policy sits just before the uuid suffix."""
    best_p, best_pos = None, -1
    for p in policy_names:
        pos = name.rfind(f"-{p}-")
        if pos == -1:
            continue
        if pos > best_pos or (pos == best_pos and len(p) > len(best_p or "")):
            best_pos, best_p = pos, p
    return best_p


def attribute_orphans(orphans, policy_names, per_sizes=None):
    """Attribute each orphan to the policy embedded in its name.
    Returns dict: policy -> {"count": n, "bytes": b} (bytes 0 if per_sizes is None)."""
    result = {p: {"count": 0, "bytes": 0} for p in policy_names}
    for name in orphans:
        best_p = _best_policy_match(name, policy_names)
        if best_p is not None:
            result[best_p]["count"] += 1
            if per_sizes is not None:
                result[best_p]["bytes"] += per_sizes.get(name, {}).get("total", 0)
    return result


def _snapshot_date(name):
    """Leading YYYY.MM.DD of a snapshot name -> datetime.date, or None."""
    m = re.match(r"(\d{4})\.(\d{2})\.(\d{2})", name)
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def snapshot_age_days(entry, today=None):
    """Age of a snapshot in days, or None when it cannot be determined.

    Prefers the repository's own start_time_in_millis; falls back to the leading
    YYYY.MM.DD that ILM puts in the snapshot name.
    """
    today = today or datetime.date.today()
    start_ms = entry.get("start_ms")
    if start_ms:
        try:
            d = datetime.datetime.fromtimestamp(
                start_ms / 1000.0, datetime.timezone.utc).date()
            return (today - d).days
        except (OverflowError, OSError, ValueError):
            pass
    d = _snapshot_date(entry["name"])
    return (today - d).days if d else None


def _modified_date(iso):
    """Parse an ILM modified_date (e.g. 2023-06-21T13:14:53.420Z) -> datetime.date."""
    if not iso:
        return None
    try:
        return datetime.date.fromisoformat(iso[:10])
    except ValueError:
        return None


def formerly_leaking_policies(orphans, ilm_index, per_sizes=None):
    """Identify NON-offending searchable-snapshot policies that still have orphans -- i.e.
    they appear to have leaked in the past but are currently compliant.

    For each, records the policy's last-updated date, the orphan count/size, and the orphan
    date range. needs_review is True when at least one orphan was TAKEN (snapshot date)
    AFTER the policy's last update -- meaning it may still be leaking (or the index was
    deleted outside ILM), and a human should look.
    """
    policy_names = list(ilm_index.keys())
    buckets = {}
    for name in orphans:
        p = _best_policy_match(name, policy_names)
        if p is not None:
            buckets.setdefault(p, []).append(name)

    results = []
    for p, names in buckets.items():
        if ilm_index[p]["offending"]:
            continue  # current culprit -- reported in the offending section instead
        mod = _modified_date(ilm_index[p].get("modified_date"))
        dates = [d for d in (_snapshot_date(n) for n in names) if d]
        latest = max(dates) if dates else None
        earliest = min(dates) if dates else None
        total_bytes = (sum(per_sizes.get(n, {}).get("total", 0) for n in names)
                       if per_sizes is not None else None)
        results.append({
            "policy": p,
            "modified_date": ilm_index[p].get("modified_date"),
            "orphan_count": len(names),
            "orphan_bytes": total_bytes,
            "earliest_orphan": earliest.isoformat() if earliest else None,
            "latest_orphan": latest.isoformat() if latest else None,
            "needs_review": bool(mod and latest and latest > mod),
        })
    # NEEDS REVIEW first, then by orphan count desc, then name.
    results.sort(key=lambda r: (not r["needs_review"], -r["orphan_count"], r["policy"]))
    return results


def _parse_capacity(s):
    """Parse a size string like '60TiB', '60 tib', or '61440 GiB' -> (bytes, 'TiB'/'GiB').
    A bare number (no unit) returns None so the caller can ask for the unit."""
    m = re.match(r"^\s*([0-9]*\.?[0-9]+)\s*([a-zA-Z]+)\s*$", s or "")
    if not m:
        return None
    value, unit = float(m.group(1)), m.group(2).lower()
    if unit not in CAPACITY_UNITS:
        return None
    canonical = "TiB" if unit in ("tib", "tb") else "GiB"
    return int(value * CAPACITY_UNITS[unit]), canonical


def _prompt_capacity(skippable):
    """Prompt on the terminal for the frozen-tier object-store capacity.
    Returns (bytes, unit), or None if skippable and the user presses Enter / it can't parse.
    Exits when required (not skippable) and the input is unusable."""
    suffix = " (or press Enter to skip the reclaimable-% report)" if skippable else ""
    raw = input("Enter total frozen-tier object-store storage from the Elastic Cloud "
                f"console{suffix}: ").strip()
    if not raw and skippable:
        return None
    parsed = _parse_capacity(raw)
    if parsed is None and re.match(r"^\s*[0-9]*\.?[0-9]+\s*$", raw):
        unit = input("Unit? [TiB/GiB] (default TiB): ").strip() or "TiB"
        parsed = _parse_capacity(f"{raw}{unit}")
    if parsed is None:
        if skippable:
            sys.stderr.write("  (could not parse capacity; skipping the reclaimable-% report)\n")
            return None
        sys.exit(f"ERROR: could not parse frozen-tier capacity '{raw}'. "
                 "Use e.g. '60TiB' or '61440GiB'.")
    return parsed


def resolve_frozen_capacity(arg):
    """Resolve the frozen-tier object-store capacity from --frozen-tier-capacity.

    arg is: None (flag absent), PROMPT_SENTINEL (flag with no value -> ask), or a size
    string. Returns (bytes, unit) or None. Exits with a clear message on bad/missing input.
    """
    if arg is None:
        return None
    if arg != PROMPT_SENTINEL:
        parsed = _parse_capacity(arg)
        if parsed is None:
            sys.exit(f"ERROR: could not parse --frozen-tier-capacity '{arg}'. "
                     "Use e.g. '60TiB' or '61440GiB'.")
        return parsed
    if not sys.stdin.isatty():
        sys.exit("ERROR: --frozen-tier-capacity needs a value in non-interactive mode, "
                 "e.g. --frozen-tier-capacity 60TiB")
    return _prompt_capacity(skippable=False)


def resolve_credentials(args):
    """Resolve (es_url, api_key) using flags -> AWS secret -> environment."""
    url = args.es_url
    api_key = args.api_key

    if args.cluster or args.secret_name:
        secret_name = args.secret_name or (SECRET_PREFIX + args.cluster)
        sys.stderr.write(f"Loading credentials from AWS secret: {secret_name}\n")
        s_url, s_key = fetch_secret_creds(secret_name, args.region)
        url = url or s_url
        api_key = api_key or s_key

    url = url or os.environ.get("ES_URL")
    api_key = api_key or os.environ.get("ES_API_KEY")
    return url, api_key


def main():
    ap = argparse.ArgumentParser(
        description="Find, size, and optionally delete orphaned searchable snapshots.")
    ap.add_argument("--cluster", choices=VALID_CLUSTERS,
                    help="Load es_url/es_api_key from AWS Secrets Manager secret "
                         "elastic/kibana/dataview_cleanup_<cluster>.")
    ap.add_argument("--secret-name", help="Override the derived AWS secret name.")
    ap.add_argument("--region", help="AWS region for Secrets Manager (else default chain).")
    ap.add_argument("--es-url", help="Elasticsearch endpoint (overrides secret and ES_URL)")
    ap.add_argument("--api-key", help="API key, 'encoded' value (overrides secret and ES_API_KEY)")
    ap.add_argument("--repo", default="found-snapshots")
    ap.add_argument("--pattern", default="*")
    ap.add_argument("--report-size", action="store_true",
                    help="Report storage used by the orphans (read-only). Fast: uses the "
                         "Get Snapshot index_details metadata, not the heavy _status scan.")
    ap.add_argument("--incremental", action="store_true",
                    help="With --report-size, also compute the dedup-aware 'incremental' "
                         "(reclaimable) size via the _status API. Slower; can be heavy on "
                         "large repos -- pair with a smaller --batch / larger --timeout.")
    ap.add_argument("--check-ilm", action="store_true",
                    help="Also analyse ILM policies and flag 'culprit' policies that create "
                         "searchable snapshots but won't let ILM delete them (the source of "
                         "future orphans).")
    ap.add_argument("--apply", action="store_true",
                    help="Delete the orphans (without this, the tool is a dry run).")
    ap.add_argument("--min-snapshot-age-days", type=int, default=14, metavar="DAYS",
                    help="SAFETY: never treat a snapshot younger than this as an orphan "
                         "(default 14). A snapshot created minutes or days ago is far more "
                         "likely to be an ILM searchable_snapshot action still in flight "
                         "than a real leftover. Set 0 to disable (not recommended).")
    ap.add_argument("--allow-ilm-in-flight", action="store_true",
                    help="SAFETY OVERRIDE: proceed with --apply even when ILM is currently "
                         "running the searchable_snapshot action on an index whose snapshot "
                         "is in the candidate list.")
    ap.add_argument("--allow-empty-in-use", action="store_true",
                    help="SAFETY OVERRIDE: proceed with --apply even when the in-use scan "
                         "found zero mounted searchable-snapshot indices. Only use this when "
                         "you have confirmed the cluster genuinely has none.")
    ap.add_argument("--skip-recheck", action="store_true",
                    help="SAFETY OVERRIDE: skip re-reading the in-use set immediately before "
                         "deleting. The re-check closes the window between the scan and the "
                         "delete; only skip it if the re-read itself is failing.")
    ap.add_argument("--batch", type=int, default=50,
                    help="Max snapshots per request (also bounded by URL length).")
    ap.add_argument("--timeout", type=int, default=120,
                    help="Per-request read timeout in seconds (default 120).")
    ap.add_argument("--delete-timeout", type=int, default=600,
                    help="Read timeout in seconds for DELETE requests (default 600). Snapshot "
                         "deletion is serialised per repository and rewrites repository "
                         "metadata, so it can take far longer than a read.")
    ap.add_argument("--sleep", type=float, default=0.0, metavar="SECONDS",
                    help="Pause this long between batched sizing/delete requests, to pace load "
                         "on a busy cluster (default 0). Try 1-2 with --incremental on a large "
                         "repository.")
    ap.add_argument("--retries", type=int, default=3,
                    help="Retries with exponential backoff on read timeouts / 429 / 5xx (default 3).")
    ap.add_argument("--per-snapshot", action="store_true",
                    help="Print the largest 25 orphans with their size (implies "
                         "--report-size; use --json for the full list).")
    ap.add_argument("--audit-file", metavar="PATH",
                    help="Write the FULL orphan list plus summary and analysis to this text "
                         "file (the on-screen output still shows only the top 25).")
    ap.add_argument("--ilm-review-file", metavar="PATH",
                    help="Write a report of ILM policies that appear to have leaked orphans "
                         "in the past but are currently compliant, with each policy's last "
                         "update date. Policies with an orphan taken AFTER that date are "
                         "flagged NEEDS REVIEW. Implies --check-ilm.")
    ap.add_argument("--frozen-usage", action="store_true",
                    help="Estimate what percentage of the frozen tier's searchable-snapshot "
                         "storage the orphans occupy (also sizes the in-use mounted snapshots). "
                         "Uses logical sizes; heavier on clusters with many mounted snapshots. "
                         "Implies --report-size.")
    ap.add_argument("--frozen-tier-capacity", metavar="SIZE", nargs="?", const=PROMPT_SENTINEL,
                    help="Report reclaimable (incremental) orphan storage as a percentage of "
                         "the TOTAL frozen-tier object-store storage. Give the size inline "
                         "(e.g. --frozen-tier-capacity 60TiB, units GiB/TiB) or pass the flag "
                         "with no value to be prompted at runtime. Read this number from the "
                         "Elastic Cloud console (the ES API cannot supply it). Implies "
                         "--incremental.")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--insecure", action="store_true")
    args = ap.parse_args()

    if args.incremental or args.per_snapshot or args.frozen_usage:
        args.report_size = True  # these only make sense while reporting size
    if args.frozen_tier_capacity is not None:
        args.incremental = True   # the numerator is the dedup-aware reclaimable size
        args.report_size = True
    if args.ilm_review_file:
        args.check_ilm = True  # the review report needs the ILM policy data

    # Resolve (and, if needed, prompt for) the frozen-tier capacity up front, so the
    # interactive prompt is answered before the potentially long scan begins.
    frozen_capacity = resolve_frozen_capacity(args.frozen_tier_capacity)
    # If a reclaimable (incremental) size is being computed but no capacity was supplied,
    # offer the reclaimable-% report by prompting for the capacity (skippable, TTY only).
    if (frozen_capacity is None and args.frozen_tier_capacity is None
            and args.incremental and sys.stdin.isatty()):
        frozen_capacity = _prompt_capacity(skippable=True)

    url, api_key = resolve_credentials(args)
    if not url:
        sys.exit("ERROR: no Elasticsearch endpoint resolved. Use --cluster "
                 "(AWS Secrets Manager), --es-url, or the ES_URL environment variable.")
    es = ESClient(url, api_key=api_key, insecure=args.insecure,
                  timeout=args.timeout, retries=args.retries)

    mode = "APPLY (delete)" if args.apply else "DRY-RUN"
    sys.stderr.write(f"Repo    : {args.repo}\nPattern : {args.pattern}\nMode    : {mode}\n")
    # ORDER MATTERS. List the repository FIRST, then read the in-use set. ILM mounts
    # new searchable snapshots continuously; with the opposite order a snapshot created
    # AND mounted between the two calls appears in the repository listing but not in the
    # in-use set -- a false orphan. This way such a snapshot is simply not a candidate.
    sys.stderr.write(f"Listing all snapshots in {args.repo}...\n")
    all_snaps = list_all_snapshots(es, args.repo)
    sys.stderr.write(f"  total snapshots : {len(all_snaps)}\n")

    sys.stderr.write("Collecting in-use snapshots from mounted indices...\n")
    in_use, in_use_info = collect_in_use(es, args.repo)
    sys.stderr.write(f"  in-use snapshots: {len(in_use)}  "
                     f"(settings: {in_use_info['settings_indices']} mounted indices"
                     + (f", cluster state: {in_use_info['cluster_state_indices']}"
                        if in_use_info["cluster_state_available"] else ", cluster state: UNAVAILABLE")
                     + ")\n")
    if not in_use_info["cluster_state_available"]:
        sys.stderr.write("  WARNING: could not read the cluster state to cross-check the in-use\n"
                         "           set (needs the 'monitor' cluster privilege). Proceeding on\n"
                         "           the Get Settings result alone.\n")
    if in_use_info["only_in_cluster_state"]:
        # Should never happen now that expand_wildcards=all is used; if it does, the
        # settings query is under-resolving again and deleting would destroy live data.
        sys.stderr.write(f"  WARNING: {len(in_use_info['only_in_cluster_state'])} mounted index/indices "
                         "were visible ONLY in the cluster state:\n")
        for idx, snap in in_use_info["only_in_cluster_state"][:10]:
            sys.stderr.write(f"           {idx} -> {snap}\n")
        sys.stderr.write("           They have been counted as in-use. Investigate before deleting.\n")
    if in_use_info["alias_repo_indices"]:
        sys.stderr.write(f"  NOTE: {len(in_use_info['alias_repo_indices'])} mounted index/indices name a "
                         "DIFFERENT repository that resolves to the same\n"
                         "        repository UUID (same bucket). Counted as in-use.\n")

    # SLM-managed snapshots (metadata.policy set) are retired by SLM's own retention,
    # so they are never orphans -- exclude them from the candidate set.
    by_name = {s["name"]: s for s in all_snaps}
    slm_managed = {s["name"] for s in all_snaps if s["slm"]}
    if slm_managed:
        sys.stderr.write(f"  SLM-managed (excluded): {len(slm_managed)}\n")

    all_names = sorted(by_name)
    orphans = [s for s in all_names
               if s not in in_use
               and s not in slm_managed
               and fnmatch.fnmatch(s, args.pattern)]
    sys.stderr.write(f"  orphaned (match): {len(orphans)}\n")

    # ---- SAFETY FILTERS -------------------------------------------------------
    # A frozen index's snapshot IS its data, so a false positive here is destructive
    # and irreversible. Everything below removes candidates that a point-in-time
    # "nothing references it" test cannot safely call a leftover.

    # 1. Snapshots that are not SUCCESS are still being written (or failed midway);
    #    an IN_PROGRESS snapshot is by definition not yet referenced by anything.
    unfinished = [s for s in orphans if (by_name[s].get("state") or "SUCCESS") != "SUCCESS"]
    if unfinished:
        sys.stderr.write(f"  SAFETY: excluding {len(unfinished)} snapshot(s) not in state SUCCESS\n")
        orphans = [s for s in orphans if s not in set(unfinished)]

    # 2. Young snapshots. ILM creates the snapshot, then mounts it, then deletes the
    #    source index -- between the first two steps the snapshot has no referencing
    #    index and is indistinguishable from an orphan.
    too_young = []
    if args.min_snapshot_age_days > 0:
        ages = {s: snapshot_age_days(by_name[s]) for s in orphans}
        too_young = [s for s in orphans
                     if ages[s] is None or ages[s] < args.min_snapshot_age_days]
        if too_young:
            sys.stderr.write(f"  SAFETY: excluding {len(too_young)} snapshot(s) younger than "
                             f"{args.min_snapshot_age_days} day(s) (or of unknown age)\n")
            for s in too_young[:10]:
                sys.stderr.write(f"          age={ages[s]}d  {s}\n")
            orphans = [s for s in orphans if s not in set(too_young)]

    # 3. ILM mid-flight on an index whose snapshot is in the candidate list.
    inflight = ilm_indices_in_flight(es)
    ilm_unknown = inflight is None
    if ilm_unknown:
        sys.stderr.write("  SAFETY: could not read _ilm/explain, so ILM in-flight mounts cannot be\n"
                         "          checked (needs read_ilm). This blocks --apply.\n")
    conflicts = inflight_conflicts(orphans, inflight or set())
    if conflicts:
        sys.stderr.write(f"  SAFETY: {len(conflicts)} candidate(s) match an index ILM is currently "
                         "running the searchable_snapshot action on:\n")
        for s in conflicts[:10]:
            sys.stderr.write(f"          {s}\n")
        if args.allow_ilm_in_flight:
            sys.stderr.write("          --allow-ilm-in-flight given; keeping them in the set.\n")
        else:
            orphans = [s for s in orphans if s not in set(conflicts)]
            sys.stderr.write("          Excluded (pass --allow-ilm-in-flight to keep them).\n")

    # 4. Plausibility guard. If the in-use scan came back empty while the repository is
    #    full of ILM-style searchable snapshots, the scan is far more likely to be broken
    #    than the cluster to be genuinely unmounted -- that is the shape of the
    #    hidden-index bug. It IS legitimate on a cluster whose frozen indices were all
    #    deleted, so --allow-empty-in-use exists for that case.
    implausible = (not in_use) and len(orphans) > 0 and not args.allow_empty_in_use
    if (not in_use) and orphans:
        sys.stderr.write("  SAFETY: the in-use scan found ZERO mounted searchable-snapshot indices\n"
                         f"          while {len(orphans)} candidate orphan(s) exist. That usually means the\n"
                         "          scan is failing, not that nothing is mounted.\n")
        if args.allow_empty_in_use:
            sys.stderr.write("          --allow-empty-in-use given; continuing.\n")

    # 5. Concurrent repository activity.
    #    A running SNAPSHOT is a concurrent write to the repository -- deleting under it
    #    can fail or interfere, so it blocks --apply.
    #    Shards recovering FROM a snapshot are only reported: on a cluster doing ILM
    #    frozen mounts this is routinely non-zero, and a correctly-identified orphan is
    #    by definition not the snapshot any recovering shard is reading.
    running_snaps, running_restores = in_progress_repo_operations(es, args.repo)
    if running_snaps is None:
        sys.stderr.write("  SAFETY: could not determine whether a snapshot is running against "
                         f"{args.repo}. This blocks --apply.\n")
    elif running_snaps:
        sys.stderr.write(f"  SAFETY: {running_snaps} snapshot(s) currently running against "
                         f"{args.repo} -- this blocks --apply.\n")
    if running_restores:
        sys.stderr.write(f"  NOTE: {running_restores} shard(s) recovering from snapshot "
                         "(normal during ILM frozen mounts; not a blocker).\n")
    report_activity = {"snapshots_running": running_snaps, "restores_running": running_restores}

    if unfinished or too_young or conflicts:
        sys.stderr.write(f"  orphans after safety filters: {len(orphans)}\n")

    report = {
        "repo": args.repo,
        "pattern": args.pattern,
        "total_snapshots": len(all_snaps),
        "in_use": len(in_use),
        "in_use_detection": {
            "settings_indices": in_use_info["settings_indices"],
            "cluster_state_available": in_use_info["cluster_state_available"],
            "cluster_state_indices": in_use_info["cluster_state_indices"],
            "only_in_cluster_state": [i for i, _ in in_use_info["only_in_cluster_state"]],
            "alias_repo_indices": in_use_info["alias_repo_indices"],
        },
        "repo_activity": report_activity,
        "slm_managed_excluded": len(slm_managed),
        "safety_excluded": {
            "not_success": unfinished,
            "younger_than_days": args.min_snapshot_age_days,
            "too_young": too_young,
            "ilm_in_flight": [] if args.allow_ilm_in_flight else conflicts,
        },
        "orphan_count": len(orphans),
        "applied": bool(args.apply),
    }

    # Optional: analyse ILM policies for culprits that will create future orphans.
    ilm_index = None
    if args.check_ilm:
        sys.stderr.write("Analysing ILM policies for searchable-snapshot culprits...\n")
        ilm_index = analyze_ilm_policies(es)
        report["offending_ilm_policies"] = offending_from_index(ilm_index)
        sys.stderr.write(f"  offending ILM policies: {len(report['offending_ilm_policies'])}\n")
        # Write a ready-to-apply corrected policy for each offender.
        write_corrected_policies(args.cluster or "cluster", ilm_index)

    if not orphans:
        if args.ilm_review_file and ilm_index is not None:
            write_ilm_review_file(args.ilm_review_file, args,
                                  formerly_leaking_policies([], ilm_index, None),
                                  report.get("offending_ilm_policies", []))
        if args.audit_file:
            write_audit_file(args.audit_file, args, report, [], None)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print("No orphaned snapshots found.")
            print_ilm_section(args, report)
        return

    # List the orphans (to stderr so stdout stays clean for the report / JSON).
    for name in orphans:
        sys.stderr.write(f"  orphan: {name}\n")

    # Optional: size the orphans.
    per = None
    if args.report_size:
        if args.incremental:
            sys.stderr.write(f"Sizing {len(orphans)} orphan(s) via _status "
                             f"(dedup-aware, batch<= {args.batch})...\n")
            total, incr, per = size_via_status(es, args.repo, orphans, args.batch, args.sleep)
            report["incremental_bytes"] = incr
            report["incremental_human"] = human(incr)
        else:
            sys.stderr.write(f"Sizing {len(orphans)} orphan(s) via index_details "
                             f"(fast, batch<= {args.batch})...\n")
            total, per = size_via_index_details(es, args.repo, orphans, args.batch, args.sleep)
        report.update({
            "measured_count": len(per),
            "total_bytes": total,
            "total_human": human(total),
            "size_method": "status" if args.incremental else "index_details",
        })
        if args.per_snapshot:
            report["per_snapshot"] = [
                dict({"snapshot": n, "total_bytes": v["total"]},
                     **({"incremental_bytes": v["incremental"]} if "incremental" in v else {}))
                for n, v in sorted(per.items(), key=lambda kv: kv[1]["total"], reverse=True)
            ]

    # Optional: what share of the frozen tier's searchable-snapshot storage is orphaned?
    # Frozen-tier snapshot storage = the snapshots backing mounted (in-use) searchable
    # indices + the orphans. Sizes are LOGICAL (index_details) so the ratio is a consistent
    # share of frozen-tier data; the exact freed-on-delete space is the dedup-aware
    # 'incremental' figure, which can be much smaller.
    if args.frozen_usage:
        in_use_names = sorted(in_use)
        sys.stderr.write(f"Sizing {len(in_use_names)} in-use searchable snapshot(s) for "
                         f"frozen-tier % (index_details)...\n")
        in_use_total, _ = size_via_index_details(es, args.repo, in_use_names, args.batch, args.sleep)
        orphan_total = report.get("total_bytes", 0)
        frozen_total = orphan_total + in_use_total
        pct = (100.0 * orphan_total / frozen_total) if frozen_total else 0.0
        report["frozen_tier"] = {
            "basis": "logical (index_details)",
            "in_use_count": len(in_use_names),
            "in_use_bytes": in_use_total, "in_use_human": human(in_use_total),
            "orphan_bytes": orphan_total, "orphan_human": human(orphan_total),
            "total_bytes": frozen_total, "total_human": human(frozen_total),
            "orphan_pct_of_frozen": round(pct, 2),
        }
        sys.stderr.write(f"  frozen-tier orphan share: {pct:.2f}%  "
                         f"({human(orphan_total)} of {human(frozen_total)})\n")

    # Optional: reclaimable orphan bytes as a % of the TOTAL frozen-tier object-store
    # storage (a number supplied by the user -- the ES API cannot provide it). The
    # numerator is the dedup-aware 'incremental' size (what actually frees on deletion).
    if frozen_capacity is not None:
        cap_bytes, cap_unit = frozen_capacity
        reclaimable = report.get("incremental_bytes", 0)
        pct = (100.0 * reclaimable / cap_bytes) if cap_bytes else 0.0
        report["frozen_reclaim"] = {
            "capacity_bytes": cap_bytes,
            "capacity_unit": cap_unit,
            "capacity_human": human(cap_bytes),
            "reclaimable_bytes": reclaimable,
            "reclaimable_human": human(reclaimable),
            "reclaimable_pct_of_capacity": round(pct, 2),
        }
        sys.stderr.write(f"  reclaimable is {pct:.2f}% of frozen-tier object-store storage "
                         f"({human(reclaimable)} of {human(cap_bytes)})\n")

    # Optional: attribute orphans to the offending ILM policies that produced them.
    if args.check_ilm and report.get("offending_ilm_policies"):
        attrib = attribute_orphans(
            orphans, [p["policy"] for p in report["offending_ilm_policies"]], per)
        for p in report["offending_ilm_policies"]:
            a = attrib[p["policy"]]
            p["orphan_count"] = a["count"]
            if args.report_size:
                p["orphan_bytes"] = a["bytes"]
                p["orphan_human"] = human(a["bytes"])

    # Optional: log offending + formerly-leaking ILM policies to a review file.
    if args.ilm_review_file and ilm_index is not None:
        review = formerly_leaking_policies(orphans, ilm_index, per)
        report["formerly_leaking_ilm_policies"] = review
        write_ilm_review_file(args.ilm_review_file, args, review,
                              report.get("offending_ilm_policies", []))

    # Optional: delete the orphans.
    if args.apply:
        # Hard blocks: conditions under which a delete is not safe to attempt at all.
        if implausible:
            sys.exit("ERROR: refusing to delete -- the in-use scan found zero mounted "
                     "searchable-snapshot indices. Verify with\n"
                     "  GET _cluster/state/metadata?filter_path=metadata.indices.*.settings.index.store.snapshot\n"
                     "If the cluster genuinely has no mounted searchable snapshots, re-run "
                     "with --allow-empty-in-use.")
        if not in_use_info["cluster_state_available"]:
            sys.exit("ERROR: refusing to delete -- the cluster-state cross-check is unavailable, "
                     "so the in-use set cannot be independently verified. Grant the API key the "
                     "'monitor' cluster privilege and re-run.")
        if in_use_info["only_in_cluster_state"]:
            sys.exit("ERROR: refusing to delete -- indices were visible only in the cluster state, "
                     "meaning the settings query is under-resolving. Investigate before deleting.")
        if ilm_unknown:
            sys.exit("ERROR: refusing to delete -- _ilm/explain could not be read, so a "
                     "searchable_snapshot action in flight cannot be ruled out. Grant the "
                     "API key 'read_ilm' and re-run.")
        if running_snaps is None:
            sys.exit("ERROR: refusing to delete -- could not determine whether a snapshot is "
                     f"running against {args.repo}. Re-run when the cluster is responsive.")
        if running_snaps:
            sys.exit(f"ERROR: refusing to delete -- {running_snaps} snapshot(s) currently "
                     f"running against {args.repo}. Wait for them to finish and re-run.")

        # Final guard: re-read the in-use set immediately before deleting. Sizing and
        # ILM analysis can take a long time on a big repo, and ILM keeps mounting new
        # searchable snapshots while we work -- a candidate may have become live since
        # the scan started.
        if not args.skip_recheck:
            sys.stderr.write("Re-checking the in-use set immediately before deletion...\n")
            fresh_in_use, fresh_info = collect_in_use(es, args.repo)
            became_live = sorted(set(orphans) & fresh_in_use)
            if became_live:
                sys.stderr.write(f"  SAFETY: {len(became_live)} candidate(s) are now referenced by a "
                                 "mounted index and will NOT be deleted:\n")
                for s in became_live[:10]:
                    sys.stderr.write(f"          {s}\n")
                orphans = [s for s in orphans if s not in set(became_live)]
                report["safety_excluded"]["became_live_before_delete"] = became_live
            if fresh_info["total"] < in_use_info["total"]:
                sys.stderr.write(f"  WARNING: the in-use set SHRANK during the run "
                                 f"({in_use_info['total']} -> {fresh_info['total']}). Indices may be "
                                 "closing or being deleted concurrently; aborting is safer.\n")
                sys.exit("ERROR: aborting --apply because the in-use set shrank mid-run. "
                         "Re-run the scan and review before deleting.")

        if not orphans:
            sys.stderr.write("Nothing left to delete after the safety re-check.\n")
            report["deleted"] = 0
        else:
            sys.stderr.write(f"Deleting {len(orphans)} orphan(s) (batch<= {args.batch})...\n")
            report["deleted"] = delete_snapshots(es, args.repo, orphans, args.batch,
                                             timeout=args.delete_timeout, sleep=args.sleep)

        # Post-delete health check. Deleting a snapshot a mounted index needs does not
        # fail loudly -- Elasticsearch only notices on the next cache miss (shard
        # relocation, node restart), so the cluster can stay green for weeks and then
        # go red. This is the earliest signal available; the follow-up check matters.
        health = es.get_optional("/_cluster/health") or {}
        status = health.get("status")
        unassigned = health.get("unassigned_shards")
        report["post_delete_health"] = {"status": status, "unassigned_shards": unassigned}
        sys.stderr.write(f"Post-delete cluster health: {status}, "
                         f"unassigned shards: {unassigned}\n")
        if status and status != "green":
            sys.stderr.write("  WARNING: cluster is not green after the delete. Check\n"
                             "           GET _cluster/allocation/explain for SnapshotMissingException.\n")
        sys.stderr.write("  NOTE: a broken frozen index may not surface for days or weeks (the\n"
                         "        failure appears on the first cache miss). Re-check cluster health\n"
                         "        after the next node restart or shard relocation.\n")

    # Optional: write the FULL orphan list + analysis to an audit file.
    if args.audit_file:
        write_audit_file(args.audit_file, args, report, orphans, per)

    if args.json:
        print(json.dumps(report, indent=2))
        return

    # Human-readable summary.
    print("\n==================== ORPHANED SEARCHABLE SNAPSHOTS ====================")
    print(f"  repository         : {args.repo}")
    print(f"  name pattern       : {args.pattern}")
    print(f"  orphaned snapshots : {len(orphans)}")
    if args.report_size:
        print(f"  total (logical)    : {report['total_human']}   ({report['total_bytes']} bytes)")
        if args.incremental:
            print(f"  incremental (free) : {report['incremental_human']}   ({report['incremental_bytes']} bytes)")
    print("======================================================================")
    if args.report_size:
        print(f"  size method        : {report['size_method']}")
        if args.incremental:
            print("  'incremental (free)' is the dedup-aware estimate of space freed by")
            print("  deleting these snapshots.")
        else:
            print("  'total (logical)' sums each snapshot's index sizes (upper bound; shared")
            print("  blobs counted once per snapshot). Add --incremental for the dedup-aware")
            print("  reclaimable figure.")
        print("  For the exact repository bill, also check the backing object-storage")
        print("  bucket metrics (S3/GCS/Azure).")
    if "frozen_tier" in report:
        ft = report["frozen_tier"]
        print("\n  -- FROZEN-TIER STORAGE (logical) --")
        print(f"  in-use searchable snapshots : {ft['in_use_human']}  ({ft['in_use_count']} snapshots)")
        print(f"  orphaned searchable snapshots: {ft['orphan_human']}")
        print(f"  total frozen-tier snapshots  : {ft['total_human']}")
        print(f"  >> orphans are {ft['orphan_pct_of_frozen']:.2f}% of frozen-tier "
              "searchable-snapshot storage (logical)")
        print("  Note: logical share; space actually freed on deletion is the dedup-aware")
        print("  'incremental' figure (run --incremental) and can be much smaller when orphans")
        print("  share blobs with live snapshots.")
    if "frozen_reclaim" in report:
        fr = report["frozen_reclaim"]
        print("\n  -- RECLAIMABLE vs FROZEN-TIER OBJECT-STORE STORAGE --")
        print(f"  frozen-tier object-store storage : {fr['capacity_human']}  (provided)")
        print(f"  reclaimable (incremental) orphans: {fr['reclaimable_human']}")
        print(f"  >> deleting orphans reclaims {fr['reclaimable_pct_of_capacity']:.2f}% of "
              "total frozen-tier object-store storage")
    if args.apply:
        print(f"  DELETED {report['deleted']} orphaned snapshot(s).")
    else:
        print("  DRY-RUN: nothing deleted. Re-run with --apply to delete the above.")
    if args.report_size and args.per_snapshot:
        rows = report["per_snapshot"]
        has_incr = any("incremental_bytes" in r for r in rows)
        cols = "total, incremental" if has_incr else "total"
        shown = min(25, len(rows))
        print(f"\n  Largest {shown} orphan(s) by size ({cols}):")
        for row in rows[:25]:
            size = f"{human(row['total_bytes']):>12}"
            if has_incr:
                size += f"  {human(row.get('incremental_bytes', 0)):>12}"
            print(f"    {size}  {row['snapshot']}")
        if len(rows) > 25:
            print(f"    ... and {len(rows) - 25} more (use --json for the full list)")

    print_ilm_section(args, report)


def print_ilm_section(args, report):
    """Print the offending-ILM-policies section (text mode) if --check-ilm ran."""
    if not args.check_ilm:
        return
    offending = report.get("offending_ilm_policies", [])
    print("\n==================== OFFENDING ILM POLICIES ==========================")
    if not offending:
        print("  None. Every policy that creates searchable snapshots also lets ILM")
        print("  delete them (delete phase present, delete_searchable_snapshot not false).")
        print("======================================================================")
        return
    print(f"  {len(offending)} policy(ies) create searchable snapshots that ILM will NOT")
    print("  clean up -- these are the source of future orphans:")
    attributed_bytes = 0
    for p in offending:
        phases = ",".join(p["searchable_snapshot_phases"])
        print(f"    - {p['policy']}  (searchable_snapshot in: {phases})")
        print(f"        {p['reason']}")
        if "orphan_count" in p:
            if "orphan_human" in p:
                attributed_bytes += p.get("orphan_bytes", 0)
                print(f"        orphaned snapshots from this policy: {p['orphan_count']}"
                      f"  ({p['orphan_human']})")
            else:
                print(f"        orphaned snapshots from this policy: {p['orphan_count']}"
                      "  (add --report-size for their size)")
    print("======================================================================")
    if any("orphan_human" in p for p in offending):
        print(f"  total orphaned storage attributable to these policies: {human(attributed_bytes)}")
    print("  Fix: add a delete phase with delete_searchable_snapshot: true (its default),")
    print("  or set it to true where it is currently false.")


def write_audit_file(path, args, report, orphans, per):
    """Write the FULL orphan list plus a summary/analysis to a text file for auditing.
    Unlike the on-screen output (top 25), this lists EVERY orphan."""
    L = []
    def w(s=""):
        L.append(s)

    w("=" * 70)
    w("ORPHANED SEARCHABLE SNAPSHOT AUDIT")
    w("=" * 70)
    w(f"Generated (UTC) : {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
    w(f"Cluster         : {args.cluster or '(from --es-url / ES_URL)'}")
    w(f"Repository      : {report['repo']}")
    w(f"Name pattern    : {report['pattern']}")
    w(f"Mode            : {'APPLY (snapshots deleted)' if args.apply else 'DRY-RUN (nothing deleted)'}")
    w("")
    w("SUMMARY")
    w(f"  total snapshots in repo : {report.get('total_snapshots', '?')}")
    w(f"  in-use (mounted)        : {report.get('in_use', '?')}")
    w(f"  SLM-managed (excluded)  : {report.get('slm_managed_excluded', 0)}")
    w(f"  orphaned snapshots      : {report['orphan_count']}")
    det = report.get("in_use_detection") or {}
    if det:
        w("")
        w("IN-USE DETECTION (how the live snapshots were identified)")
        w(f"  mounted indices via _settings (expand_wildcards=all) : {det.get('settings_indices', '?')}")
        if det.get("cluster_state_available"):
            w(f"  mounted indices via cluster state (cross-check)      : {det.get('cluster_state_indices')}")
        else:
            w("  cluster-state cross-check                            : UNAVAILABLE (no privilege)")
        only = det.get("only_in_cluster_state") or []
        if only:
            w(f"  !! {len(only)} index/indices seen ONLY in the cluster state -- the settings")
            w("     query is under-resolving. These were counted as in-use:")
            for idx in only[:20]:
                w(f"       {idx}")
    sf = report.get("safety_excluded") or {}
    if sf:
        held = [("not in state SUCCESS", sf.get("not_success") or []),
                (f"younger than {sf.get('younger_than_days')} day(s)", sf.get("too_young") or []),
                ("ILM searchable_snapshot in flight", sf.get("ilm_in_flight") or []),
                ("became live before delete", sf.get("became_live_before_delete") or [])]
        held = [(label, names) for label, names in held if names]
        if held:
            w("")
            w("HELD BACK BY SAFETY FILTERS (candidates NOT treated as orphans)")
            for label, names in held:
                w(f"  {label}: {len(names)}")
                for n in names[:25]:
                    w(f"    {n}")
                if len(names) > 25:
                    w(f"    ...and {len(names) - 25} more")
    if "post_delete_health" in report:
        h = report["post_delete_health"]
        w("")
        w("POST-DELETE CLUSTER HEALTH")
        w(f"  status           : {h.get('status')}")
        w(f"  unassigned shards: {h.get('unassigned_shards')}")
        w("  NOTE: a frozen index whose snapshot was deleted stays green until the first")
        w("        cache miss (shard relocation / node restart), which can be weeks later.")
        w("        Re-check health after the next restart before considering this clean.")
    if "total_bytes" in report:
        w(f"  total (logical)         : {report['total_human']}  ({report['total_bytes']} bytes)")
        if "incremental_bytes" in report:
            w(f"  incremental (reclaim)   : {report['incremental_human']}  ({report['incremental_bytes']} bytes)")
        w(f"  size method             : {report.get('size_method')}")
    if "deleted" in report:
        w(f"  deleted                 : {report['deleted']}")
    if "frozen_tier" in report:
        ft = report["frozen_tier"]
        w("")
        w("FROZEN-TIER STORAGE (logical, from index_details)")
        w(f"  in-use searchable snapshots  : {ft['in_use_human']}  ({ft['in_use_count']} snapshots)")
        w(f"  orphaned searchable snapshots: {ft['orphan_human']}")
        w(f"  total frozen-tier snapshots  : {ft['total_human']}")
        w(f"  orphans are {ft['orphan_pct_of_frozen']:.2f}% of frozen-tier searchable-snapshot storage")
        w("  (logical share; actual freed space on deletion is the dedup-aware 'incremental'")
        w("   figure and can be much smaller when orphans share blobs with live snapshots.)")
    if "frozen_reclaim" in report:
        fr = report["frozen_reclaim"]
        w("")
        w("RECLAIMABLE vs FROZEN-TIER OBJECT-STORE STORAGE")
        w(f"  frozen-tier object-store storage : {fr['capacity_human']}  (provided)")
        w(f"  reclaimable (incremental) orphans: {fr['reclaimable_human']}")
        w(f"  reclaimable is {fr['reclaimable_pct_of_capacity']:.2f}% of total frozen-tier "
          "object-store storage")
    w("")

    if args.check_ilm:
        for line in render_offending_lines(report.get("offending_ilm_policies", [])):
            w(line)
        w("")

    w("NOTES")
    w("  - Orphan = in repo, not referenced by any mounted index, NOT SLM-managed, and")
    w("    past every safety filter (SLM-managed snapshots are retired by SLM's own")
    w("    retention).")
    w("  - The in-use set is resolved with expand_wildcards=all: data-stream backing")
    w("    indices and their searchable-snapshot mounts (partial-.ds-*, restored-.ds-*)")
    w("    are HIDDEN, and _all/* do not match hidden indices by default. Missing them")
    w("    turns a live snapshot into a false orphan -- and deleting it destroys the")
    w("    index, because for a frozen index the snapshot IS the data.")
    w("  - 'total (logical)' sums each snapshot's index sizes and OVERCOUNTS blobs shared")
    w("    between snapshots -- treat it as an upper bound.")
    w("  - 'incremental (reclaim)' (only with --incremental) is the dedup-aware estimate of")
    w("    space freed on deletion; actual freed can be lower when blobs are shared with")
    w("    retained (in-use or SLM) snapshots. The exact figure is the object-store bucket")
    w("    size before vs. after.")
    w("")

    if per:
        rows = sorted(orphans, key=lambda n: per.get(n, {}).get("total", 0), reverse=True)
        has_incr = any("incremental" in per.get(n, {}) for n in orphans)
        w(f"FULL ORPHAN LIST ({len(orphans)}) -- largest first:")
        head = f"  {'total':>12}"
        if has_incr:
            head += f"  {'incremental':>12}"
        head += "  snapshot"
        w(head)
        for n in rows:
            v = per.get(n, {})
            line = f"  {human(v.get('total', 0)):>12}"
            if has_incr:
                line += f"  {human(v.get('incremental', 0)):>12}"
            line += f"  {n}"
            w(line)
    else:
        w(f"FULL ORPHAN LIST ({len(orphans)}):")
        for n in sorted(orphans):
            w(f"  {n}")
    w("")

    try:
        with open(path, "w") as fh:
            fh.write("\n".join(L) + "\n")
        sys.stderr.write(f"Audit file written: {path} ({len(orphans)} orphan(s))\n")
    except OSError as e:
        sys.stderr.write(f"WARNING: could not write audit file '{path}': {e}\n")


def render_offending_lines(offending):
    """Return the OFFENDING-ILM-POLICIES section as a list of text lines (shared by the
    audit file and the ILM review file)."""
    lines = ["OFFENDING ILM POLICIES (create searchable snapshots ILM will not delete)"]
    if not offending:
        lines.append("  None.")
        return lines
    for p in offending:
        phases = ",".join(p["searchable_snapshot_phases"])
        lines.append(f"  - {p['policy']}  (searchable_snapshot in: {phases}); {p['reason']}")
        if "orphan_count" in p:
            extra = f"  ({p['orphan_human']})" if "orphan_human" in p else ""
            lines.append(f"      orphans from this policy: {p['orphan_count']}{extra}")
    return lines


def write_ilm_review_file(path, args, review, offending):
    """Write the ILM policy review to a text file: the CURRENTLY-offending policies (that
    will create new orphans) plus the formerly-leaking-but-now-compliant policies -- so all
    potentially-offending ILM policies are reviewable in one place."""
    L = []
    def w(s=""):
        L.append(s)

    needs = [r for r in review if r["needs_review"]]
    ok = [r for r in review if not r["needs_review"]]

    def fmt(r):
        sz = human(r["orphan_bytes"]) if r.get("orphan_bytes") is not None else "n/a"
        dates = f"{r['earliest_orphan']} .. {r['latest_orphan']}"
        return sz, dates

    w("=" * 70)
    w("ILM POLICY REVIEW -- SEARCHABLE SNAPSHOT LEAKS")
    w("=" * 70)
    w(f"Generated (UTC) : {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
    w(f"Cluster         : {args.cluster or '(from --es-url / ES_URL)'}")
    w("")
    w("SECTION 1 -- CURRENTLY misconfigured policies that will create NEW orphans")
    w("(no delete phase, or delete_searchable_snapshot: false):")
    w("")
    for line in render_offending_lines(offending):
        w(line)
    w("")
    w("-" * 70)
    w("SECTION 2 -- policies compliant NOW that appear to have leaked in the PAST")
    w("-" * 70)
    w("")
    w("A policy is listed here if it currently HAS a delete phase with")
    w("delete_searchable_snapshot enabled, yet orphaned searchable snapshots exist whose")
    w("name embeds this policy -- i.e. it leaked in the past but looks compliant now")
    w("(likely updated after the leak).")
    w("")
    w("'NEEDS REVIEW' = at least one leaked snapshot was TAKEN (snapshot date) AFTER the")
    w("policy's last update, so it may still be leaking, or the index was deleted outside")
    w("ILM. Those should be reviewed by a human.")
    w("")

    w(f"NEEDS REVIEW -- leaked AFTER last update ({len(needs)}):")
    if not needs:
        w("  (none)")
    for r in needs:
        sz, dates = fmt(r)
        w(f"  - {r['policy']}")
        w(f"      last updated : {r['modified_date']}")
        w(f"      orphans      : {r['orphan_count']}  ({sz})")
        w(f"      orphan dates : {dates}")
        w(f"      !! latest orphan {r['latest_orphan']} is AFTER last update "
          f"{(r['modified_date'] or '')[:10]} -> REVIEW")
    w("")

    w(f"LIKELY ALREADY FIXED -- all leaked snapshots predate last update ({len(ok)}):")
    if not ok:
        w("  (none)")
    for r in ok:
        sz, dates = fmt(r)
        w(f"  - {r['policy']}  (last updated {(r['modified_date'] or 'unknown')[:10]}; "
          f"orphans {r['orphan_count']}, {sz}; dates {dates})")
    w("")

    try:
        with open(path, "w") as fh:
            fh.write("\n".join(L) + "\n")
        sys.stderr.write(f"ILM review file written: {path} "
                         f"({len(needs)} need review, {len(ok)} likely fixed)\n")
    except OSError as e:
        sys.stderr.write(f"WARNING: could not write ILM review file '{path}': {e}\n")


if __name__ == "__main__":
    main()
