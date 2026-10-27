terraform {
  required_version = ">= 1.6"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 6.0" }
  }
}

provider "google" {
  project = var.project_id
}

locals {
  # region key -> namespace/cluster name of the Postgres cluster that runs there (see database/regions)
  db_clusters = { for k, _ in var.regions : k => "app-db-${k}" }
}

resource "google_project_service" "apis" {
  for_each           = toset(["container.googleapis.com", "compute.googleapis.com", "storage.googleapis.com", "iam.googleapis.com"])
  service            = each.value
  disable_on_destroy = false
}

# ---------- network: one VPC, a subnet per region, NAT for egress from private nodes ----------

resource "google_compute_network" "vpc" {
  name                    = "gitops-dr"
  auto_create_subnetworks = false
  depends_on              = [google_project_service.apis]
}

resource "google_compute_subnetwork" "region" {
  for_each                 = var.regions
  name                     = "gitops-dr-${each.key}"
  region                   = each.value.region
  network                  = google_compute_network.vpc.id
  ip_cidr_range            = each.value.nodes_cidr
  private_ip_google_access = true
  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = each.value.pods_cidr
  }
  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = each.value.services_cidr
  }
}

resource "google_compute_router" "region" {
  for_each = var.regions
  name     = "gitops-dr-${each.key}"
  region   = each.value.region
  network  = google_compute_network.vpc.id
}

resource "google_compute_router_nat" "region" {
  for_each                           = var.regions
  name                               = "gitops-dr-${each.key}"
  region                             = each.value.region
  router                             = google_compute_router.region[each.key].name
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"
  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

# ---------- one Autopilot cluster per region, each running its own Argo CD ----------

resource "google_container_cluster" "region" {
  for_each         = var.regions
  name             = "gitops-dr-${each.key}"
  location         = each.value.region
  enable_autopilot = true
  network          = google_compute_network.vpc.id
  subnetwork       = google_compute_subnetwork.region[each.key].id

  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }

  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = false
    master_ipv4_cidr_block  = each.value.master_cidr
  }

  master_authorized_networks_config {
    dynamic "cidr_blocks" {
      for_each = var.admin_cidrs
      content {
        cidr_block   = cidr_blocks.value
        display_name = "admin"
      }
    }
  }

  release_channel {
    channel = "REGULAR"
  }

  # Upgrades never land on both regions in the same window.
  maintenance_policy {
    recurring_window {
      start_time = each.value.maintenance_start
      end_time   = each.value.maintenance_end
      recurrence = "FREQ=WEEKLY;BYDAY=SA,SU"
    }
  }

  deletion_protection = var.deletion_protection
}

# ---------- backups: one multi-region bucket that outlives either region ----------

resource "google_storage_bucket" "backups" {
  name                        = "${var.project_id}-pg-backups"
  location                    = var.backup_location # multi-region, e.g. ASIA
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  # Barman deletes expired backups itself (retentionPolicy). Versioning plus soft delete means a
  # compromised credential or a bad retention setting can't destroy the backups outright.
  versioning {
    enabled = true
  }
  soft_delete_policy {
    retention_duration_seconds = 7 * 24 * 3600
  }
  lifecycle_rule {
    condition {
      days_since_noncurrent_time = 7
      with_state                 = "ARCHIVED"
    }
    action {
      type = "Delete"
    }
  }
  depends_on = [google_project_service.apis]
}

resource "google_service_account" "cnpg_backup" {
  account_id   = "cnpg-backup"
  display_name = "CloudNativePG backups (Workload Identity)"
}

# Bucket-scoped, not project-wide: this identity can touch the backups and nothing else.
resource "google_storage_bucket_iam_member" "cnpg_backup" {
  bucket = google_storage_bucket.backups.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.cnpg_backup.email}"
}

# Only the Postgres pods' Kubernetes service accounts (named after their Cluster) may act as it.
resource "google_service_account_iam_member" "workload_identity" {
  for_each           = local.db_clusters
  service_account_id = google_service_account.cnpg_backup.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.db_namespace}/${each.value}]"
  depends_on         = [google_container_cluster.region] # the identity pool exists once a cluster does
}
