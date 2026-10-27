output "clusters" {
  value = { for k, c in google_container_cluster.region : k => { name = c.name, location = c.location } }
}

output "backup_bucket" {
  value = google_storage_bucket.backups.url
}

output "backup_service_account" {
  value = google_service_account.cnpg_backup.email
}

output "get_credentials" {
  description = "Commands to add both clusters to kubeconfig."
  value = [for k, c in google_container_cluster.region :
  "gcloud container clusters get-credentials ${c.name} --region ${c.location} --project ${var.project_id}"]
}
