# DR plan: app-db

## Targets

| Scenario | RPO target | RTO target | How it's met | Tested by |
|---|---|---|---|---|
| A zone fails | seconds at most (async replicas) | < 1 min | 3 instances spread across zones; CNPG promotes the most advanced replica in the region | CNPG's own failover (not drilled here) |
| A region is lost | ≤ 2 min | ≤ 30 min | Replica cluster in the other region replays WAL from the multi-region bucket; failover by PR | `drill region-loss` |
| Bad change destroys data | 0 (to the chosen time) | ≤ 60 min | 14 days of base backups and WAL; restore a copy to any second in the window | `drill pitr` |
| Backups deleted or corrupted | n/a | n/a | Bucket versioning and 7-day soft delete; the backup identity can reach this bucket only | Not drilled yet (see Gaps) |

**Why the region-loss RPO is 2 minutes, not 0.**
- WAL reaches the other region through object storage, at least every `archive_timeout` (60 s), plus upload time.
- Synchronous cross-region replication would give RPO 0. But every commit would then wait about 50 ms for the round trip, and a network partition would stall the primary.
- For this service, losing up to two minutes in a region-wide disaster is the better trade. The figure is measured on every drill, not assumed.

**Why the RTO is 30 minutes when the technical failover takes about a minute.** The time is spent on:
- noticing the problem;
- deciding it really is a region loss and not a blip, because failing over on a blip creates a split brain;
- merging the failover PR.

The drill measures the technical part on every CI run. A game day with `--promote gitops` measures the whole process.

## Failover: a region is lost

1. **Confirm it's regional, not a blip.** The GCP status page and the regional uptime check both agree, and the outage has lasted 10 minutes or more. If the primary region might come back on its own, waiting is cheaper than rebuilding.
2. **Make sure the old primary can't come back as primary.** If the API server of the lost region is reachable at all, scale its Argo CD down and delete its `Cluster` object: `kubectl --context gke-sg -n db delete cluster app-db-sg`. This deletes only that region's copy; the archive in the bucket stays.
3. **Open the failover PR.** It changes `replica.primary` to `app-db-tw` in **both** `database/regions/sg/cluster.yaml` and `database/regions/tw/cluster.yaml`. `scripts/check_gitops.py` fails the PR if only one of them changes, because that is two primaries. Merge it with the incident commander's approval.
4. **Argo CD in Taiwan syncs and CNPG promotes `app-db-tw`.** Watch with `kubectl --context gke-tw -n db get cluster app-db-tw -w` until the phase is `Cluster in healthy state`.
5. **Point the apps at Taiwan** (DNS or global load balancer weight). Record the time: this is the RTO.
6. **Record the RPO.** Find the last write the application recorded as committed, and the newest row in `app-db-tw`.

## Failback: Singapore is back

There's no rush, so do it during working hours.

1. Re-sync Singapore from Git: `app-db-sg` comes back as a replica of `app-db-tw` (`bootstrap.recovery` from Taiwan's archive). This requires changing sg's bootstrap source to `app-db-tw` in the same PR that rebuilds it.
2. When its lag is low, do a **controlled switchover**, not a failover:
   - set `replica.primary: app-db-sg` on `app-db-tw` (demote);
   - read the demotion token: `kubectl get cluster app-db-tw -o jsonpath='{.status.demotionToken}'`;
   - set it as `promotionToken` on `app-db-sg` in the same PR.

   Done this way, nothing is lost and nothing has to be rebuilt.

## Point-in-time restore: a bad change

1. **Find the time just before the bad change** (from the deploy log, the migration timestamp or the audit log). Use UTC with an explicit offset.
2. **Restore a copy next to the live database. Never restore in place.**
   - `python -m drill pitr` shows the exact manifest: `drill.region.restore_manifest`.
   - In production, commit a `Cluster` with `bootstrap.recovery.recoveryTarget.targetTime` to `database/restores/`.
3. **Copy back only what was destroyed**, using `pg_dump -t` from the copy. Then delete the copy.

The restored copy never archives WAL, so it can't write into the live database's backup path.

## When the alerts fire

### WAL archiving is failing

The other region and every point-in-time restore are frozen at the last archived segment, so RPO grows every minute.

1. Check the barman-cloud sidecar logs on the primary pod.
2. Most often the cause is credentials: the Workload Identity binding or the bucket IAM. Check that `cnpg-backup@` still has `roles/storage.objectAdmin` on the bucket.
3. The disk keeps filling with unarchived WAL until this is fixed. Watch PVC usage.

### A replica is far behind

1. Check whether there are any writes. On an idle database, replay lag grows with nothing wrong.
2. For the DR region: is WAL archiving healthy on the primary (see above)?
3. Can the Taiwan pods read the bucket? Check the restore logs of the designated primary.

## Drills

| When | What | Where |
|---|---|---|
| Every push and every Monday | `pitr` + `region-loss` with `--promote patch` | CI on kind (`ci/kind-drill.sh`); the report is in the job summary |
| Quarterly game day | `region-loss --promote gitops` on GKE, with the on-call engineer following this document | Staging project |

A drill that misses a target fails the build. Update the targets in this document only with the evidence that justified the change.

## Gaps (known, not yet done)

- **Backup-deletion drill.** Restore from soft-deleted objects after a simulated malicious deletion.
- **Application tier.** Traffic failover (Cloud DNS or global load balancer) is described, not automated.
- **Backup bucket in the same project.** A compromised project owner could delete both clusters and the backups. The fix is a copy in a separate project with a retention lock.
