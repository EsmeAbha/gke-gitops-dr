# gke-gitops-dr

Postgres on GKE across two regions, managed entirely through Git with Argo CD. The disaster-recovery plan has measured targets. A drill destroys data, and then a whole region, on every CI run and every Monday, and fails the build if recovery misses its targets.

```
                Git (this repo) ── the only way changes reach the clusters, including failover
               ╱                ╲
   GKE asia-southeast1 (sg)        GKE asia-east1 (tw)
   Argo CD ── reconciles itself    Argo CD ── reconciles itself       no cross-region control plane:
   app-db-sg  PRIMARY, 3 zones     app-db-tw  REPLICA CLUSTER, 3 zones  losing sg doesn't take the
        │  WAL every ≤60 s               ▲  replays sg's WAL             tooling needed to recover
        ▼                                │
   gs://…-pg-backups  (multi-region ASIA, versioned, soft delete, Workload Identity only)
```

## What's in it

| Path | What |
|---|---|
| `infra/terraform/` | VPC, two private Autopilot clusters (with maintenance windows on different days), Cloud NAT, the backup bucket, and a backup identity scoped to that bucket and to the two Postgres service accounts |
| `platform/` | cert-manager, CloudNativePG 1.30 and the Barman Cloud plugin, each pinned to a release; an upgrade is a one-line PR |
| `database/` | The two `Cluster`s in CNPG's distributed topology, nightly backups, and storage components for GCS or S3 |
| `deploy/` | One overlay per cluster (`gke-sg`, `gke-tw`, plus `kind-*` for CI) |
| `argocd/` | AppProjects, app-of-apps per cluster, bootstrap |
| `drill/` | The DR drill: measures RTO and RPO for real, against real clusters |
| `scripts/check_gitops.py` | Invariants checked on every PR, before anything reaches a cluster |
| `monitoring/` | Alerts for the two things that silently break DR, with promtool tests |
| `docs/dr-plan.md` | Targets, failover, failback, point-in-time-restore runbooks |

## Decisions

**Each region runs its own Argo CD.** A single central Argo CD is simpler. But it lives in one region, and losing that region would remove the tool you need to fail over. Here, each cluster pulls its own config from Git.

**Failover is a PR.** The failover changes `replica.primary` in both clusters' manifests. Argo CD's self-heal would revert a `kubectl patch`, which is the point: there's one source of truth, and the change is reviewed and recorded in the incident timeline. CI rejects a PR that flips only one region, because that would create two primaries.

**Cross-region replication goes through object storage, not streaming.** There's no network path between regions to secure or to stall the primary during a partition. The cost is an RPO of up to `archive_timeout` (60 s) plus upload time. `docs/dr-plan.md` explains why that's the right trade for this service, and the drill measures it.

**Argo CD can't delete the database.** The `Cluster` objects carry `Prune=false,Delete=false`, so deleting a file or renaming an Application can't delete data volumes. The `database` AppProject may create only CNPG kinds in namespace `db`, so a bad commit there can't touch CRDs or RBAC.

**No key files.** Backups use Workload Identity. The Google service account can reach one bucket, and only the two Postgres service accounts can act as it.

## The drill

```
python -m drill pitr        --cluster app-db-sg@db --rpo 0  --rto 600
python -m drill region-loss --primary app-db-sg@db@gke-sg --standby app-db-tw@db@gke-tw \
                            --confirm-destroy app-db-sg --rpo 120 --rto 1800 [--promote gitops]
```

**Region loss:**
1. Write a heartbeat row every second and record each one the primary acknowledged.
2. Delete the primary region's database.
3. Promote the other region.
4. Measure the time until writes succeed and stay up for 30 seconds (RTO, counted from the start of that run, because a promoted instance can accept a write and then restart), and which acknowledged writes survived (RPO).

Writes are matched on ID and timestamp. Postgres logs sequences in batches, so a promoted standby can reuse IDs of writes that were lost, and matching on ID alone would undercount the loss. A test reproduces that case.

**Point-in-time restore:**
1. Write rows, mark a target time, keep writing, then delete everything.
2. Restore a copy to the target time.
3. Check that every row up to the target is there and nothing after it.

**Safety:**
- Nothing is destroyed unless `--confirm-destroy` names the cluster.
- The drill aborts before any damage if:
  - a cluster is unhealthy;
  - WAL archiving is failing;
  - the standby is already writable;
  - the standby is too far behind for a fair measurement.
- Exit codes: 0 passed, 1 a target missed, 2 aborted.

**In CI** (`ci/kind-drill.sh`):
- Two namespaces on kind stand in for the regions, and an S3-compatible store stands in for the bucket.
- Everything else is the same operator, plugin and Cluster specs that Argo CD deploys.
- The report goes in the job summary and is kept as an artifact.

A second job installs a real Argo CD in kind and syncs the platform apps from the commit under test.

## Tests and checks

```
pip install -r requirements.txt -r requirements-dev.txt
python scripts/check_gitops.py      # DR invariants
pytest -q                           # 23 tests
```

**`check_gitops.py`** renders the overlays and rejects:
- two primaries;
- a replica or restore source that doesn't exist;
- two regions archiving to one path;
- more than one region initialising its own database;
- a Cluster without prune protection;
- an Application pointing at a missing path or deploying kinds its project forbids.

Each of these has a test that makes the change and expects the checker to fail.

**The drill logic** is tested against a simulated pair of regions that reproduces:
- archive delay;
- promotion time;
- sequence reuse;
- a restore that ignores its target.

**CI also runs** kubeconform on every rendered overlay, `terraform validate`, and promtool on the alerts.

## Status

- **Verified locally:**
  - the unit tests and the invariant checks;
  - `terraform validate`.
- **Runs in GitHub Actions** (not yet run):
  - the kind drill;
  - the Argo CD sync;
  - schema validation;
  - promtool.
- **Not done on real GKE:** applying the Terraform. That needs a billing-enabled project, at roughly two Autopilot clusters plus a small bucket. The first GKE game day goes in `docs/dr-plan.md`.
