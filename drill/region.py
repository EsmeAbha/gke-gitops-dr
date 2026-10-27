"""One region's CloudNativePG cluster, driven through kubectl.

kubectl (not a client library) on purpose: the drill uses exactly the access and contexts an
on-call engineer has, so a drill that passes proves the runbook works, not just the code.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Callable, Protocol

HEALTHY = "Cluster in healthy state"


class DrillError(Exception):
    pass


Runner = Callable[[list[str], "str | None"], str]


def run_kubectl(args: list[str], stdin: str | None = None) -> str:
    p = subprocess.run(["kubectl", *args], input=stdin, capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise DrillError(f"kubectl {' '.join(args[:4])}...: {p.stderr.strip()[:300]}")
    return p.stdout


class Region(Protocol):
    name: str

    def healthy(self) -> bool: ...
    def archiving_ok(self) -> tuple[bool, str]: ...
    def sql(self, query: str) -> list[list[str]]: ...
    def writable(self) -> bool: ...
    def destroy(self) -> None: ...
    def promote(self) -> None: ...
    def backup_now(self, name: str) -> None: ...
    def backup_done(self, name: str) -> bool: ...
    def create_restore(self, name: str, target_time: str) -> None: ...
    def restore(self, name: str) -> "Region": ...
    def delete(self) -> None: ...


@dataclass
class KubeRegion:
    """name is the CNPG Cluster name; context and namespace locate it."""
    name: str
    namespace: str
    context: str | None = None
    run: Runner = run_kubectl

    def _k(self, *args: str, stdin: str | None = None) -> str:
        ctx = ["--context", self.context] if self.context else []
        return self.run([*ctx, "-n", self.namespace, *args], stdin)

    def cluster(self) -> dict:
        return json.loads(self._k("get", "clusters.postgresql.cnpg.io", self.name, "-o", "json"))

    def healthy(self) -> bool:
        try:
            s = self.cluster().get("status", {})
        except DrillError:
            return False
        return s.get("phase") == HEALTHY and s.get("readyInstances", 0) >= 1

    def archiving_ok(self) -> tuple[bool, str]:
        for c in self.cluster().get("status", {}).get("conditions", []):
            if c.get("type") == "ContinuousArchiving":
                return c.get("status") == "True", c.get("message", "")
        return False, "no ContinuousArchiving condition yet"

    def sql(self, query: str) -> list[list[str]]:
        pod = self.cluster().get("status", {}).get("currentPrimary")
        if not pod:
            raise DrillError(f"{self.name}: no current primary")
        out = self._k("exec", pod, "-c", "postgres", "--", "psql", "-d", "app", "-v", "ON_ERROR_STOP=1",
                      "-X", "-q", "-tA", "-F", "\t", "-c", query)
        return [line.split("\t") for line in out.splitlines() if line]

    def writable(self) -> bool:
        try:
            return self.sql("select pg_is_in_recovery()") == [["f"]]
        except DrillError:
            return False

    def destroy(self) -> None:
        # Simulated region loss: the database and its volumes are gone. The object store is not.
        self._k("delete", "clusters.postgresql.cnpg.io", self.name, "--wait=false")

    def promote(self) -> None:
        # Failover without a promotion token: the old primary is presumed lost and must be rebuilt.
        # In production this is a Git change (Argo CD would revert a kubectl patch); see dr-plan.md.
        patch = json.dumps({"spec": {"replica": {"primary": self.name}}})
        self._k("patch", "clusters.postgresql.cnpg.io", self.name, "--type", "merge", "-p", patch)

    def backup_now(self, name: str) -> None:
        self._k("apply", "-f", "-", stdin=json.dumps({
            "apiVersion": "postgresql.cnpg.io/v1", "kind": "Backup",
            "metadata": {"name": name, "namespace": self.namespace},
            "spec": {"cluster": {"name": self.name}, "method": "plugin",
                     "pluginConfiguration": {"name": "barman-cloud.cloudnative-pg.io"}}}))

    def backup_done(self, name: str) -> bool:
        phase = json.loads(self._k("get", "backups.postgresql.cnpg.io", name, "-o", "json")).get("status", {}).get("phase")
        if phase == "failed":
            raise DrillError(f"backup {name} failed")
        return phase == "completed"

    def create_restore(self, name: str, target_time: str) -> None:
        self._k("apply", "-f", "-", stdin=json.dumps(restore_manifest(self.cluster(), name, target_time)))

    def restore(self, name: str) -> "KubeRegion":
        return KubeRegion(name, self.namespace, self.context, self.run)

    def delete(self) -> None:
        self._k("delete", "clusters.postgresql.cnpg.io", self.name, "--wait=false", "--ignore-not-found")


def restore_manifest(source: dict, name: str, target_time: str) -> dict:
    """A new Cluster restored from `source`'s archive to `target_time`, next to it. It reuses the
    source's object store, image and identity (for GKE Workload Identity), and doesn't archive, so
    it can never write into the source's backup path."""
    spec = source["spec"]
    archiver = next(p for p in spec["plugins"] if p.get("isWALArchiver"))
    src = source["metadata"]["name"]
    out = {
        "instances": 1,
        "storage": spec["storage"],
        "bootstrap": {"recovery": {"source": "origin", "recoveryTarget": {"targetTime": target_time}}},
        "externalClusters": [{"name": "origin", "plugin": {"name": archiver["name"], "parameters": {
            "barmanObjectName": archiver["parameters"]["barmanObjectName"],
            "serverName": archiver["parameters"].get("serverName", src)}}}],
    }
    for k in ("imageName", "serviceAccountTemplate", "resources"):
        if k in spec:
            out[k] = spec[k]
    return {"apiVersion": "postgresql.cnpg.io/v1", "kind": "Cluster",
            "metadata": {"name": name, "namespace": source["metadata"]["namespace"],
                         "labels": {"dr-drill": "pitr"}},
            "spec": out}
