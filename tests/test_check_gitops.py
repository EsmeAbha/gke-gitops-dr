"""The invariant checker must pass on the repo and catch each dangerous change."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def run_on(tmp_path, mutate=None):
    for d in ("argocd", "database", "deploy", "platform", "scripts"):
        shutil.copytree(ROOT / d, tmp_path / d)
    if mutate:
        mutate(tmp_path)
    p = subprocess.run([sys.executable, str(tmp_path / "scripts" / "check_gitops.py")], capture_output=True, text=True)
    return p.returncode, p.stdout


def edit(path, old, new):
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_repo_passes(tmp_path):
    code, out = run_on(tmp_path)
    assert code == 0, out


@pytest.mark.parametrize("name,mutate,expect", [
    ("half-done failover: only one region flipped",
     lambda r: edit(r / "database/regions/tw/cluster.yaml", "    primary: app-db-sg", "    primary: app-db-tw"),
     "split brain"),
    ("Argo CD may prune the database",
     lambda r: edit(r / "database/regions/sg/cluster.yaml", "Prune=false,Delete=false", "Validate=false"),
     "Prune=false"),
    ("both regions archive to one path",
     lambda r: edit(r / "database/regions/tw/cluster.yaml", "        serverName: app-db-tw\n  replica:", "        serverName: app-db-sg\n  replica:"),
     "its own name"),
    ("replica source is not defined",
     lambda r: edit(r / "database/regions/tw/cluster.yaml", "    source: app-db-sg\n  externalClusters", "    source: app-db-xx\n  externalClusters"),
     "not an externalCluster"),
    ("no archive_timeout",
     lambda r: edit(r / "database/regions/sg/cluster.yaml", 'archive_timeout: "60s"', 'work_mem: "8MB"'),
     "archive_timeout"),
    ("database project would deploy a forbidden kind",
     lambda r: (r / "database/regions/sg/cluster.yaml").write_text(
         (r / "database/regions/sg/cluster.yaml").read_text() + "---\napiVersion: v1\nkind: Secret\nmetadata: {name: x}\n"),
     "forbids"),
    ("both regions initialise a fresh database",
     lambda r: edit(r / "database/regions/tw/cluster.yaml", "    recovery:\n      source: app-db-sg", "    initdb:\n      database: app"),
     "independent databases"),
    ("Application points at a missing path",
     lambda r: edit(r / "argocd/clusters/gke-sg/kustomization.yaml", "deploy/gke-sg", "deploy/gke-sgp"),
     "no kustomization.yaml"),
])
def test_dangerous_changes_are_caught(tmp_path, name, mutate, expect):
    code, out = run_on(tmp_path, mutate)
    assert code == 1 and expect in out, f"{name}:\n{out}"
