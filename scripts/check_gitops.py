"""Checks the invariants this repo's DR design depends on, before anything reaches a cluster.

Schema validation (kubeconform, in CI) says each manifest is well-formed. This says the set of
manifests is safe:
  - both regions agree on which one is primary (two primaries = split brain, diverging data);
  - every replica/bootstrap source and object store a Cluster names actually exists;
  - each region archives WAL to its own path (two writers on one path corrupt the archive);
  - Clusters are protected from Argo CD prune/delete;
  - every Argo CD Application points at a real path, in a project that allows what it deploys.

It renders the local kustomizations itself (resources, components, JSON patches, namespace),
so it runs anywhere with Python and PyYAML. Remote resources (pinned operator releases) are skipped.

    python scripts/check_gitops.py          # exit 1 on any violation
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = "barman-cloud.cloudnative-pg.io"


def load_docs(path: Path) -> list[dict]:
    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]


def json_patch(doc: dict, ops: list[dict]) -> None:
    for op in ops:
        parts = [p.replace("~1", "/").replace("~0", "~") for p in op["path"].strip("/").split("/")]
        parent = doc
        for p in parts[:-1]:
            parent = parent[int(p)] if isinstance(parent, list) else parent.setdefault(p, {})
        last = parts[-1]
        if op["op"] == "replace" and last not in parent:
            raise ValueError(f"replace of missing path {op['path']}")
        if op["op"] in ("add", "replace"):
            parent[last] = copy.deepcopy(op["value"])
        else:
            raise ValueError(f"unsupported op {op['op']}")


def matches(doc: dict, target: dict) -> bool:
    return all(doc.get("kind") == v if k == "kind" else doc["metadata"].get("name") == v
               for k, v in target.items() if k in ("kind", "name"))


def render(directory: Path) -> list[dict]:
    """A small subset of kustomize: enough for this repo's local overlays."""
    k = yaml.safe_load((directory / "kustomization.yaml").read_text(encoding="utf-8"))
    docs: list[dict] = []
    for r in k.get("resources", []):
        if r.startswith("http"):
            continue
        p = directory / r
        docs += render(p) if p.is_dir() else load_docs(p)
    for c in k.get("components", []):
        comp_dir = directory / c
        comp = yaml.safe_load((comp_dir / "kustomization.yaml").read_text(encoding="utf-8"))
        for r in comp.get("resources", []):
            docs += load_docs(comp_dir / r)
        apply_patches(docs, comp.get("patches", []))
    apply_patches(docs, k.get("patches", []))
    if ns := k.get("namespace"):
        for d in docs:
            if d["kind"] not in ("Namespace", "AppProject", "Application"):
                d["metadata"]["namespace"] = ns
    return docs


def apply_patches(docs: list[dict], patches: list[dict]) -> None:
    for p in patches:
        hits = [d for d in docs if matches(d, p.get("target", {}))]
        if not hits:
            raise ValueError(f"patch target {p.get('target')} matched nothing")
        for d in hits:
            json_patch(d, yaml.safe_load(p["patch"]))


def check_region(name: str, docs: list[dict]) -> tuple[list[str], dict | None]:
    errors = []
    clusters = [d for d in docs if d["kind"] == "Cluster"]
    stores = {d["metadata"]["name"] for d in docs if d["kind"] == "ObjectStore"}
    if len(clusters) != 1:
        return [f"{name}: expected exactly one Cluster, found {len(clusters)}"], None
    c = clusters[0]
    cname, spec = c["metadata"]["name"], c["spec"]
    where = f"{name}/{cname}"

    opts = c["metadata"].get("annotations", {}).get("argocd.argoproj.io/sync-options", "")
    if "Prune=false" not in opts or "Delete=false" not in opts:
        errors.append(f"{where}: must carry argocd.argoproj.io/sync-options: Prune=false,Delete=false")

    archivers = [p for p in spec.get("plugins", []) if p.get("name") == PLUGIN and p.get("isWALArchiver")]
    if len(archivers) != 1:
        errors.append(f"{where}: needs exactly one WAL-archiving {PLUGIN} plugin")
        archive_path = None
    else:
        params = archivers[0].get("parameters", {})
        archive_path = params.get("serverName", cname)
        if params.get("barmanObjectName") not in stores:
            errors.append(f"{where}: archives to ObjectStore {params.get('barmanObjectName')!r}, which isn't in this overlay")
        if archive_path != cname:
            errors.append(f"{where}: archives under serverName {archive_path!r}; it must be its own name")

    external = {e["name"]: e for e in spec.get("externalClusters", [])}
    for e in external.values():
        if e.get("plugin", {}).get("parameters", {}).get("barmanObjectName") not in stores:
            errors.append(f"{where}: externalCluster {e['name']} reads from an ObjectStore not in this overlay")
    replica = spec.get("replica", {})
    for field in ("primary", "source"):
        if not replica.get(field):
            errors.append(f"{where}: spec.replica.{field} is required in the distributed topology")
    if replica.get("source") and replica["source"] not in external:
        errors.append(f"{where}: replica.source {replica['source']!r} is not an externalCluster")
    if replica.get("source") == cname:
        errors.append(f"{where}: replica.source must be the other region, not itself")
    if src := spec.get("bootstrap", {}).get("recovery", {}).get("source"):
        if src not in external:
            errors.append(f"{where}: bootstrap.recovery.source {src!r} is not an externalCluster")
    if spec.get("postgresql", {}).get("parameters", {}).get("archive_timeout") is None:
        errors.append(f"{where}: set archive_timeout; without it RPO to the other region is unbounded on a quiet database")

    for b in (d for d in docs if d["kind"] == "ScheduledBackup"):
        if b["spec"]["cluster"]["name"] != cname:
            errors.append(f"{name}: ScheduledBackup {b['metadata']['name']} targets unknown cluster")
    return errors, {"name": cname, "primary": replica.get("primary"), "archive": archive_path,
                    "bootstrap": spec.get("bootstrap", {}).get("recovery", {}).get("source", "initdb")}


def check_pair(env: str, regions: dict[str, dict]) -> list[str]:
    errors = []
    primaries = {r["primary"] for r in regions.values()}
    names = {r["name"] for r in regions.values()}
    if len(primaries) != 1:
        errors.append(f"{env}: regions disagree on the primary {sorted(primaries)}: that is two primaries (split brain)")
    elif (p := primaries.pop()) not in names:
        errors.append(f"{env}: replica.primary {p!r} is not one of the clusters {sorted(names)}")
    archives = [r["archive"] for r in regions.values()]
    if len(set(archives)) != len(archives):
        errors.append(f"{env}: two clusters archive WAL to the same path: {archives}")
    if sum(r["bootstrap"] == "initdb" for r in regions.values()) > 1:
        errors.append(f"{env}: more than one region runs initdb: those are independent databases, not replicas")
    for r in regions.values():
        if r["bootstrap"] not in ("initdb", r["name"]) and r["bootstrap"] not in names:
            errors.append(f"{env}: {r['name']} restores from {r['bootstrap']!r}, which is not the other region")
        if r["bootstrap"] == r["name"]:
            errors.append(f"{env}: {r['name']} restores from its own archive")
    return errors


def check_argocd() -> list[str]:
    errors = []
    for cluster_dir in sorted((ROOT / "argocd" / "clusters").iterdir()):
        if cluster_dir.name == "base":
            continue
        docs = render(cluster_dir)
        projects = {d["metadata"]["name"]: d["spec"] for d in docs if d["kind"] == "AppProject"}
        for app in (d for d in docs if d["kind"] == "Application"):
            where = f"argocd/clusters/{cluster_dir.name}: {app['metadata']['name']}"
            src, proj = app["spec"]["source"], projects.get(app["spec"]["project"])
            if not (ROOT / src["path"] / "kustomization.yaml").exists():
                errors.append(f"{where}: source path {src['path']} has no kustomization.yaml")
                continue
            if proj is None:
                errors.append(f"{where}: project {app['spec']['project']} is not defined")
                continue
            if src["repoURL"] not in proj["sourceRepos"]:
                errors.append(f"{where}: repo not allowed by its project")
            dest_ns = app["spec"]["destination"]["namespace"]
            if dest_ns not in {d["namespace"] for d in proj["destinations"]}:
                errors.append(f"{where}: destination namespace {dest_ns} not allowed by its project")
            allowed = proj.get("namespaceResourceWhitelist")
            if allowed is not None:
                kinds = {(w["group"], w["kind"]) for w in allowed}
                for d in render(ROOT / src["path"]):
                    key = (d["apiVersion"].split("/")[0] if "/" in d["apiVersion"] else "", d["kind"])
                    if key not in kinds:
                        errors.append(f"{where}: deploys {d['kind']}, which project {app['spec']['project']} forbids")
    return errors


def main() -> int:
    errors = []
    for env in ("gke", "kind"):
        regions = {}
        for overlay in sorted((ROOT / "deploy").glob(f"{env}-*")):
            try:
                docs = render(overlay)
            except (ValueError, KeyError, OSError) as e:
                errors.append(f"{overlay.name}: does not render: {e}")
                continue
            errs, summary = check_region(overlay.name, docs)
            errors += errs
            if summary:
                regions[overlay.name] = summary
        errors += check_pair(env, regions)
    errors += check_argocd()
    for e in errors:
        print("FAIL", e)
    print(f"{len(errors)} problem(s)" if errors else "gitops invariants: OK")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
