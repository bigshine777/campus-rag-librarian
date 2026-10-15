variable "tenancy_ocid" {
  type = string
}

variable "region" {
  type    = string
  default = "ap-osaka-1"
}

variable "instance_shape" {
  type    = string
  default = "VM.Standard.E2.1.Micro"
}

variable "ssh_public_key_path" {
  type    = string
  default = "~/.ssh/id_ed25519.pub"
}

variable "project_name" {
  type    = string
  default = "ai-librarian"
}
