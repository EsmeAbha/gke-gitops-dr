"""The drill logic against a simulated pair of regions.

The simulation keeps what makes DR hard: WAL reaches the other region only after an archive
delay (so the newest writes are lost with the primary), promotion takes time, and the promoted
standby's sequence hands out ids the lost primary already used.
"""
import json
from datetime import datetime

import pytest

from drill import report
from drill.region import DrillError, KubeRegion, restore_manifest
from drill.scenarios import Aborted, Targets, pitr, region_loss

T0 = 1_800_000_000.0


class FakeClock:
    def __init__(self):
        self.t = T0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


class World:
    def __init__(self, archive_delay=10.0, promote_delay=20.0):
        self.clock = FakeClock()
        self.archive_delay = archive_delay
        self.promote_delay = promote_delay
        self.rows = []                  # (id, ts) written on the primary
        self.archived_until = None      # frozen when the primary dies
        self.switched_at = None

    def archived(self):
        cutoff = self.archived_until if self.archived_until is not None else self.clock.t - self.archive_delay
        if self.switched_at is not None:
            cutoff = max(cutoff, self.switched_at)
        return [r for r in self.rows if r[1] <= cutoff]


class FakeRegion:
    def __init__(self, world, name, role, archiving=True, lag=5.0):
        self.w, self.name, self.role = world, name, role
        self.archiving, self.lag = archiving, lag
        self.destroyed = self.deleted = False
        self.promoted_at = None
        self.own_rows = []              # written here after promotion
        self.next_id = None
        self.deleted_all = False
        self.restores = {}
        self.restore_rows = None        # set on a restored copy
        self.healthy_at = T0

    # --- state ---
    def is_primary(self):
        if self.role == "primary":
            return not self.destroyed
        return self.promoted_at is not None and self.w.clock.t >= self.promoted_at + self.w.promote_delay

    def visible(self):
        if self.restore_rows is not None:
            return self.restore_rows
        if self.role == "primary":
            return [] if self.deleted_all else self.w.rows
        return self.w.archived() + self.own_rows

    # --- Region protocol ---
    def healthy(self):
        return not self.destroyed and self.w.clock.t >= self.healthy_at

    def archiving_ok(self):
        return self.archiving, "" if self.archiving else "barman-cloud: access denied"

    def writable(self):
        return self.is_primary()

    def sql(self, q):
        if self.destroyed:
            raise DrillError("connection refused")
        t = self.w.clock.t
        if q.startswith("create table"):
            return []
        if q.startswith("insert"):
            if not self.is_primary():
                raise DrillError("cannot execute INSERT in a read-only transaction")
            if self.role == "primary":
                row = (len(self.w.rows) + 1, t)
                self.w.rows.append(row)
            else:
                if self.next_id is None:   # sequence resumes from the last value it replayed
                    self.next_id = max((i for i, _ in self.w.archived()), default=0) + 1
                row = (self.next_id, t)
                self.next_id += 1
                self.own_rows.append(row)
            return [[str(row[0]), repr(row[1])]]
        if q.startswith("select pg_is_in_recovery"):
            return [["f" if self.is_primary() else "t"]]
        if "pg_last_xact_replay_timestamp" in q:
            return [[str(self.lag)]]
        if q.startswith("select id, extract"):
            lo, hi = (int(x) for x in q.split("between ")[1].split(" and "))
            return [[str(i), repr(ts)] for i, ts in self.visible() if lo <= i <= hi]
        if q.startswith("select extract(epoch from clock_timestamp())"):
            return [[repr(t)]]
        if q.startswith("delete"):
            self.deleted_all = True
            return []
        if q.startswith("select pg_switch_wal"):
            self.w.switched_at = t
            return [["0/5000000"]]
        raise AssertionError(f"unexpected SQL {q}")

    def destroy(self):
        self.destroyed = True
        self.w.archived_until = self.w.clock.t - self.w.archive_delay

    def promote(self):
        self.promoted_at = self.w.clock.t

    def backup_now(self, name):
        pass

    def backup_done(self, name):
        return True

    def create_restore(self, name, target_time, ignore_target=False):
        target = datetime.fromisoformat(target_time).timestamp()
        copy = FakeRegion(self.w, name, "restore")
        rows = self.w.archived()
        copy.restore_rows = rows if self.ignore_target else [r for r in rows if r[1] <= target]
        copy.healthy_at = self.w.clock.t + 40
        self.restores[name] = copy

    ignore_target = False

    def restore(self, name):
        return self.restores[name]

    def delete(self):
        self.deleted = True


def setup(**kw):
    w = World(**{k: v for k, v in kw.items() if k in ("archive_delay", "promote_delay")})
    p = FakeRegion(w, "app-db-sg", "primary", archiving=kw.get("archiving", True))
    s = FakeRegion(w, "app-db-tw", "standby", lag=kw.get("lag", 5.0))
    return w, p, s


def run_loss(w, p, s, targets=Targets(30, 120), **kw):
    return region_loss(p, s, targets, confirm=kw.pop("confirm", "app-db-sg"), clock=w.clock,
                       log=lambda _: None, **kw)


def test_region_loss_measures_data_lost_and_time_to_recover():
    w, p, s = setup(archive_delay=10, promote_delay=20)
    r = run_loss(w, p, s)
    assert r.status == "passed", r.problems
    # writes from the last 10s before the disaster weren't archived yet: ~10 of them are gone
    assert 9 <= r.lost_writes <= 12
    assert 9 <= r.rpo_seconds <= 12
    assert 20 <= r.rto_seconds <= 25
    assert p.destroyed and s.promoted_at is not None


def test_ids_reused_by_the_promoted_standby_are_not_counted_as_survivors():
    w, p, s = setup(archive_delay=10)
    r = run_loss(w, p, s)
    reused = {i for i, _ in s.own_rows} & {i for i, _ in w.rows}
    assert reused, "the simulation should reproduce sequence reuse"
    assert r.lost_writes == len(w.rows) - len(w.archived())


def test_missed_targets_fail_the_drill():
    w, p, s = setup(archive_delay=45, promote_delay=200)
    r = run_loss(w, p, s, targets=Targets(30, 120), recover_timeout=600)
    assert r.status == "failed"
    assert any("RPO" in x for x in r.problems) and any("RTO" in x for x in r.problems)
    assert report.exit_code([r]) == 1


def test_refuses_to_destroy_without_naming_the_cluster():
    w, p, s = setup()
    with pytest.raises(Aborted):
        run_loss(w, p, s, confirm="yes")
    assert not p.destroyed


@pytest.mark.parametrize("kw,reason", [
    ({"archiving": False}, "not archiving"),
    ({"lag": 900}, "replay lag"),
])
def test_unsafe_preconditions_abort_before_anything_is_destroyed(kw, reason):
    w, p, s = setup(**kw)
    r = run_loss(w, p, s)
    assert r.status == "aborted" and reason in r.problems[0]
    assert not p.destroyed and s.promoted_at is None
    assert report.exit_code([r]) == 2


def test_aborts_if_standby_already_accepts_writes():
    w, p, s = setup()
    s.promoted_at = T0 - 100   # someone already promoted it: two primaries
    r = run_loss(w, p, s)
    assert r.status == "aborted" and "two primaries" in r.problems[0] and not p.destroyed


def test_standby_that_never_recovers_fails_with_no_rto():
    w, p, s = setup(promote_delay=10_000)
    r = run_loss(w, p, s, recover_timeout=100)
    assert r.status == "failed" and r.rto_seconds is None
    assert "not recovered" in report.markdown([r], "test")


def test_pitr_restores_everything_up_to_the_target_and_nothing_after():
    w, p, _ = setup(archive_delay=10)
    r = pitr(p, Targets(0, 120), restore_name="restore-1", clock=w.clock, log=lambda _: None)
    assert r.status == "passed", r.problems
    assert r.rpo_seconds == 0 and r.lost_writes == 0 and r.acked_writes >= 29
    assert p.restores["restore-1"].deleted, "the restored copy is cleaned up"


def test_pitr_catches_a_restore_that_ignores_the_target_time():
    w, p, _ = setup()
    p.ignore_target = True
    r = pitr(p, Targets(0, 120), restore_name="restore-1", clock=w.clock, log=lambda _: None)
    assert r.status == "failed" and "after the target time" in r.problems[0]


def test_pitr_aborts_when_archiving_is_broken():
    w, p, _ = setup(archiving=False)
    r = pitr(p, Targets(0, 120), restore_name="restore-1", clock=w.clock, log=lambda _: None)
    assert r.status == "aborted" and not p.restores


# --- the kubectl side ---

SOURCE = {
    "metadata": {"name": "app-db-sg", "namespace": "db"},
    "spec": {
        "storage": {"size": "20Gi"},
        "serviceAccountTemplate": {"metadata": {"annotations": {"iam.gke.io/gcp-service-account": "x@p.iam.gserviceaccount.com"}}},
        "plugins": [{"name": "barman-cloud.cloudnative-pg.io", "isWALArchiver": True,
                     "parameters": {"barmanObjectName": "backups", "serverName": "app-db-sg"}}],
    },
}


def test_restore_manifest_reads_the_source_archive_and_never_writes_to_it():
    m = restore_manifest(SOURCE, "app-db-sg-pitr", "2026-10-27T10:00:00+00:00")
    spec = m["spec"]
    assert spec["bootstrap"]["recovery"]["recoveryTarget"]["targetTime"] == "2026-10-27T10:00:00+00:00"
    assert spec["externalClusters"][0]["plugin"]["parameters"] == {"barmanObjectName": "backups", "serverName": "app-db-sg"}
    assert "plugins" not in spec, "a restored copy must not archive (it would write into the source's backups)"
    assert spec["serviceAccountTemplate"] == SOURCE["spec"]["serviceAccountTemplate"], "needs the identity to read GCS"
    assert m["metadata"]["namespace"] == "db"


def test_kube_region_commands():
    calls = []

    def run(args, stdin):
        calls.append((args, stdin))
        if args[-3:] == ["app-db-tw", "-o", "json"]:
            return json.dumps({"status": {"phase": "Cluster in healthy state", "readyInstances": 3,
                                          "currentPrimary": "app-db-tw-1"}})
        return "f\n"

    r = KubeRegion("app-db-tw", "db", "gke-tw", run)
    assert r.healthy() and r.writable()
    exec_args = calls[-1][0]
    assert exec_args[4:8] == ["exec", "app-db-tw-1", "-c", "postgres"]
    assert exec_args[-2:] == ["-c", "select pg_is_in_recovery()"] and "ON_ERROR_STOP=1" in exec_args
    r.promote()
    args = calls[-1][0]
    assert args[:4] == ["--context", "gke-tw", "-n", "db"]
    assert json.loads(args[-1]) == {"spec": {"replica": {"primary": "app-db-tw"}}}


def test_markdown_report():
    w, p, s = setup()
    md = report.markdown([run_loss(w, p, s)], "kind")
    assert "| region-loss | **PASS** |" in md and "DISASTER" in md
