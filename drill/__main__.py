"""Run DR drills against real clusters.

    python -m drill region-loss --primary app-db-sg@db-sg --standby app-db-tw@db-tw \\
        --confirm-destroy app-db-sg --rpo 30 --rto 300
    python -m drill pitr --cluster app-db-sg@db-sg --rpo 0 --rto 600

A region is CLUSTER@NAMESPACE[@KUBECONTEXT]. Exit code: 0 passed, 1 target missed, 2 aborted.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import report
from .region import KubeRegion
from .scenarios import Aborted, Targets, pitr, region_loss


def region(spec: str) -> KubeRegion:
    name, ns, *ctx = spec.split("@")
    return KubeRegion(name, ns, ctx[0] if ctx else None)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="drill", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="scenario", required=True)
    rl = sub.add_parser("region-loss", help="destroy the primary region's database and fail over")
    rl.add_argument("--primary", required=True)
    rl.add_argument("--standby", required=True)
    rl.add_argument("--confirm-destroy", required=True, metavar="CLUSTER",
                    help="must equal the primary's cluster name: this drill deletes it")
    rl.add_argument("--promote", choices=["patch", "gitops"], default="patch")
    rl.add_argument("--warmup", type=float, default=60)
    pt = sub.add_parser("pitr", help="destroy data with a bad change and restore to just before it")
    pt.add_argument("--cluster", required=True)
    pt.add_argument("--keep", action="store_true", help="keep the restored copy for inspection")
    for s in (rl, pt):
        s.add_argument("--rpo", type=float, required=True, help="target, seconds")
        s.add_argument("--rto", type=float, required=True, help="target, seconds")
        s.add_argument("--environment", default="unnamed")
        s.add_argument("--out", type=Path, default=Path("drill-results"))
    a = p.parse_args(argv)
    targets = Targets(a.rpo, a.rto)

    try:
        if a.scenario == "region-loss":
            result = region_loss(region(a.primary), region(a.standby), targets, confirm=a.confirm_destroy,
                                 promote=a.promote, warmup=a.warmup)
        else:
            result = pitr(region(a.cluster), targets, restore_name=f"{region(a.cluster).name}-pitr-{int(time.time())}",
                          keep=a.keep)
    except Aborted as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2

    a.out.mkdir(parents=True, exist_ok=True)
    stem = a.out / f"{a.scenario}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    stem.with_suffix(".md").write_text(report.markdown([result], a.environment), encoding="utf-8")
    stem.with_suffix(".json").write_text(report.to_json([result], a.environment), encoding="utf-8")
    print(report.markdown([result], a.environment))
    return report.exit_code([result])


if __name__ == "__main__":
    sys.exit(main())
