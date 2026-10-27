variable "project_id" {
  type = string
}

variable "regions" {
  description = "Region key (matches database/regions/<key>) -> settings. Exactly one is primary at a time; that is decided in Git, not here."
  type = map(object({
    region            = string
    nodes_cidr        = string
    pods_cidr         = string
    services_cidr     = string
    master_cidr       = string
    maintenance_start = string
    maintenance_end   = string
  }))
  default = {
    sg = {
      region            = "asia-southeast1"
      nodes_cidr        = "10.10.0.0/20"
      pods_cidr         = "10.100.0.0/16"
      services_cidr     = "10.110.0.0/20"
      master_cidr       = "172.16.0.0/28"
      maintenance_start = "2026-01-03T18:00:00Z"
      maintenance_end   = "2026-01-03T22:00:00Z"
    }
    tw = {
      region            = "asia-east1"
      nodes_cidr        = "10.20.0.0/20"
      pods_cidr         = "10.101.0.0/16"
      services_cidr     = "10.111.0.0/20"
      master_cidr       = "172.16.0.16/28"
      maintenance_start = "2026-01-04T18:00:00Z"
      maintenance_end   = "2026-01-04T22:00:00Z"
    }
  }
  validation {
    condition     = length(var.regions) == 2
    error_message = "This layout is one primary region and one DR region."
  }
}

variable "backup_location" {
  description = "Multi-region location for the backup bucket; must contain both regions."
  type        = string
  default     = "ASIA"
}

variable "db_namespace" {
  type    = string
  default = "db"
}

variable "admin_cidrs" {
  description = "Networks allowed to reach the cluster API servers (e.g. your office, a CI runner range)."
  type        = list(string)
}

variable "deletion_protection" {
  type    = bool
  default = true
}
