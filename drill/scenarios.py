"""DR drills that measure what matters: how long until writes work again (RTO) and how much
acknowledged data was lost (RPO). Both are measured, never assumed: a heartbeat writer records
every write the primary acknowledged, and after recovery the drill counts which ones survived.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .region import DrillError, Region

SCHEMA = ("create table if not exists dr_heartbeat ("
          "id bigserial primary key, written_at timestamptz not null default clock_timestamp())")


class Clock:
    def now(self) -> float:
        return time.monotonic()

    def sleep(self, s: float) -> None:
        time.sleep(s)


@dataclass
class Targets:
    rpo_seconds: float
    rto_seconds: float


@dataclass
class Result:
    scenario: str
    targets: Targets
    status: str = "running"            # passed | failed | aborted
    rto_seconds: float | None = None
    rpo_seconds: float | None = None
    acked_writes: int = 0
    lost_writes: int | None = None
    timeline: list[tuple[float, str]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def note(self, t: float, text: str) -> None:
        self.timeline.append((round(t, 1), text))

    def judge(self) -> "Result":
        if self.status == "aborted":
            return self
        if self.rto_seconds is None:
            self.problems.append("service was not recovered within the time limit")
        elif self.rto_seconds > self.targets.rto_seconds:
            self.problems.append(f"RTO {self.rto_seconds:.0f}s is over the {self.targets.rto_seconds:.0f}s target")
        if self.rpo_seconds is not None and self.rpo_seconds > self.targets.rpo_seconds:
            self.problems.append(f"RPO {self.rpo_seconds:.0f}s is over the {self.targets.rpo_seconds:.0f}s target")
        self.status = "failed" if self.problems else "passed"
        return self


class Aborted(Exception):
    """A precondition failed. Nothing destructive has happened yet."""


def wait_for(cond: Callable[[], bool], timeout: float, clock: Clock, every: float = 2.0) -> bool:
    deadline = clock.now() + timeout
    while clock.now() < deadline:
        if cond():
            return True
        clock.sleep(every)
    return cond()


class Heartbeat:
    """Writes one row at a time and remembers each write the database acknowledged."""

    def __init__(self, region: Region):
        self.region = region
        self.acked: list[tuple[int, float]] = []   # (id, written_at as epoch seconds, database clock)
        self.failures = 0

    def beat(self) -> bool:
        try:
            rows = self.region.sql("insert into dr_heartbeat default values "
                                   "returning id, extract(epoch from written_at)")
        except DrillError:
            self.failures += 1
            return False
        self.acked.append((int(rows[0][0]), float(rows[0][1])))
        return True


def survivors(region: Region, acked: list[tuple[int, float]]) -> set[int]:
    """Ids of acknowledged writes present in `region`. Matched on id AND timestamp: Postgres logs
    sequences in batches of 32, so a promoted standby can hand out ids the lost primary already
    used, and an id match alone would count those new rows as survivors."""
    if not acked:
        return set()
    rows = region.sql("select id, extract(epoch from written_at) from dr_heartbeat "
                      f"where id between {acked[0][0]} and {acked[-1][0]}")
    present = {int(i): float(ts) for i, ts in rows}
    return {i for i, ts in acked if i in present and abs(present[i] - ts) < 1e-6}


def region_loss(primary: Region, standby: Region, targets: Targets, *, confirm: str, clock: Clock | None = None,
                warmup: float = 60, max_standby_lag: float = 120, interval: float = 1.0,
                promote: str = "patch", recover_timeout: float | None = None,
                log: Callable[[str], None] = print) -> Result:
    """Destroy the primary region's database, fail over to the standby region, measure RTO and RPO.

    promote="patch" promotes with kubectl (CI, kind). promote="gitops" waits for a person to merge
    the failover PR, so the measured RTO includes the human and Argo CD part of the runbook.
    """
    clock = clock or Clock()
    r = Result("region-loss", targets)
    t0 = clock.now()

    def note(text: str) -> None:
        r.note(clock.now() - t0, text)
        log(f"[{clock.now() - t0:7.1f}s] {text}")

    if confirm != primary.name:
        raise Aborted(f"refusing to destroy {primary.name}: pass confirm={primary.name!r} to proceed")

    # --- preflight: everything here is read-only; any failure stops the drill before damage ---
    try:
        for reg in (primary, standby):
            if not reg.healthy():
                raise Aborted(f"{reg.name} is not healthy")
        ok, msg = primary.archiving_ok()
        if not ok:
            raise Aborted(f"{primary.name} is not archiving WAL ({msg}); a failover now would lose data")
        if standby.writable():
            raise Aborted(f"{standby.name} is already accepting writes: two primaries")
        primary.sql(SCHEMA)
        note("preflight passed")

        hb = Heartbeat(primary)
        end = clock.now() + warmup
        while clock.now() < end:
            hb.beat()
            clock.sleep(interval)
        lag = standby.sql("select coalesce(extract(epoch from now() - pg_last_xact_replay_timestamp()), -1)")
        lag_s = float(lag[0][0])
        if lag_s < 0 or lag_s > max_standby_lag:
            raise Aborted(f"{standby.name} replay lag is {lag_s:.0f}s (limit {max_standby_lag:.0f}s); not a fair drill")
        note(f"{len(hb.acked)} heartbeats written; standby replay lag {lag_s:.0f}s")
    except (Aborted, DrillError) as e:
        r.status = "aborted"
        r.problems.append(str(e))
        note(f"ABORTED before any damage: {e}")
        return r

    # --- disaster ---
    hb.beat()
    primary.destroy()
    t_disaster = clock.now()
    note(f"DISASTER: {primary.name} destroyed")
    r.acked_writes = len(hb.acked)
    if hb.beat():   # the primary may answer for a moment while it shuts down; that write counts too
        r.acked_writes = len(hb.acked)

    # --- failover ---
    if promote == "patch":
        standby.promote()
        note(f"promotion requested: {standby.name} is now the designated primary")
    else:
        log(f">>> merge the failover PR now (replica.primary: {standby.name} in both regions)")
    recovered = wait_for(standby.writable, recover_timeout or targets.rto_seconds * 3, clock)
    if not recovered:
        note("standby did not accept writes in time")
        return r.judge()
    probe = Heartbeat(standby)
    if not wait_for(probe.beat, 60, clock, every=1):
        note("standby reports writable but writes fail")
        return r.judge()
    r.rto_seconds = clock.now() - t_disaster
    note(f"RECOVERED: {standby.name} accepts writes")

    # --- what survived ---
    kept = survivors(standby, hb.acked)
    lost = [(i, ts) for i, ts in hb.acked if i not in kept]
    r.lost_writes = len(lost)
    r.rpo_seconds = (hb.acked[-1][1] - max(ts for i, ts in hb.acked if i in kept)) if lost and kept else (
        0.0 if not lost else hb.acked[-1][1] - hb.acked[0][1])
    note(f"{len(kept)}/{len(hb.acked)} acknowledged writes survived")
    return r.judge()


def pitr(region: Region, targets: Targets, *, restore_name: str, clock: Clock | None = None,
         write_for: float = 30, interval: float = 1.0, restore_timeout: float | None = None,
         keep: bool = False, log: Callable[[str], None] = print) -> Result:
    """A bad change destroys data; restore a copy to just before it and check it's all there.

    RTO here is "bad change noticed" to "restored copy accepts queries". RPO is how far before the
    target time the restored data ends (0 when every write up to the target survived).
    """
    clock = clock or Clock()
    r = Result("point-in-time-restore", targets)
    t0 = clock.now()

    def note(text: str) -> None:
        r.note(clock.now() - t0, text)
        log(f"[{clock.now() - t0:7.1f}s] {text}")

    try:
        if not region.healthy():
            raise Aborted(f"{region.name} is not healthy")
        ok, msg = region.archiving_ok()
        if not ok:
            raise Aborted(f"{region.name} is not archiving WAL ({msg})")
        region.sql(SCHEMA)
        backup = f"{restore_name}-base"
        region.backup_now(backup)
        if not wait_for(lambda: region.backup_done(backup), 900, clock, every=5):
            raise Aborted("base backup did not complete")
        note("preflight passed; base backup completed")
    except (Aborted, DrillError) as e:
        r.status = "aborted"
        r.problems.append(str(e))
        note(f"ABORTED: {e}")
        return r

    hb = Heartbeat(region)
    end = clock.now() + write_for
    while clock.now() < end:
        hb.beat()
        clock.sleep(interval)
    target = float(region.sql("select extract(epoch from clock_timestamp())")[0][0])
    for _ in range(3):   # writes after the target: they must NOT be in the restore
        hb.beat()
        clock.sleep(interval)
    region.sql("delete from dr_heartbeat")
    note(f"BAD CHANGE: all rows deleted ({len(hb.acked)} heartbeats written)")
    region.sql("select pg_switch_wal()")   # close the WAL segment so it's archived now, not at archive_timeout
    t_bad = clock.now()

    target_iso = datetime.fromtimestamp(target, timezone.utc).isoformat()
    region.create_restore(restore_name, target_iso)
    note(f"restore of {region.name} to {target_iso} started as {restore_name}")
    restored = region.restore(restore_name)
    try:
        if not wait_for(restored.healthy, restore_timeout or targets.rto_seconds * 3, clock, every=5):
            note("restore did not become healthy in time")
            return r.judge()
        r.rto_seconds = clock.now() - t_bad
        note("RESTORED: copy is healthy")

        before = [(i, ts) for i, ts in hb.acked if ts <= target]
        after = [(i, ts) for i, ts in hb.acked if ts > target]
        kept = survivors(restored, hb.acked)
        missing = [x for x in before if x[0] not in kept]
        leaked = [x for x in after if x[0] in kept]
        r.acked_writes, r.lost_writes = len(before), len(missing)
        r.rpo_seconds = (target - max((ts for i, ts in before if i in kept), default=target - 1e9)) if missing else 0.0
        if leaked:
            r.problems.append(f"{len(leaked)} rows written after the target time are in the restore")
        note(f"{len(before) - len(missing)}/{len(before)} rows up to the target restored; {len(leaked)} later rows present")
    finally:
        if not keep:
            restored.delete()
    return r.judge()
