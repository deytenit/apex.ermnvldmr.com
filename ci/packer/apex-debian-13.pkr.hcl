packer {
  required_plugins {
    qemu = {
      version = ">= 1.1.0"
      source  = "github.com/hashicorp/qemu"
    }
  }
}

variable "debian_cloud_image_url" {
  type    = string
  default = "https://cloud.debian.org/images/cloud/trixie/daily/latest/debian-13-genericcloud-amd64-daily.qcow2"
}

variable "debian_cloud_image_checksum" {
  type    = string
  default = "none"
}

variable "vm_name" {
  type    = string
  default = "com-ermnvldmr-apex-debian-13-amd64.qcow2"
}

variable "accelerator" {
  type    = string
  default = "kvm"
}

source "qemu" "apex_debian" {
  iso_url          = var.debian_cloud_image_url
  iso_checksum     = var.debian_cloud_image_checksum
  disk_image       = true
  disk_size        = "10G"
  format           = "qcow2"
  output_directory = "output-qemu"
  shutdown_command = "echo 'packer' | sudo -S shutdown -P now"
  accelerator      = var.accelerator
  cd_files         = ["ci/packer/cidata/user-data", "ci/packer/cidata/meta-data"]
  cd_label         = "cidata"
  ssh_username     = "debian"
  ssh_password     = "packer"
  ssh_timeout      = "10m"
  vm_name          = var.vm_name
  net_device       = "virtio-net"
  disk_interface   = "virtio"
  headless         = true

  qemuargs = [
    ["-m", "2048M"],
    ["-smp", "2"]
  ]
}

build {
  sources = ["source.qemu.apex_debian"]

  provisioner "shell" {
    execute_command = "echo 'packer' | sudo -S sh -c '{{ .Vars }} {{ .Path }}'"
    scripts = [
      "ci/packer/scripts/01-base-system.sh",
      "ci/packer/scripts/02-storage-docker.sh",
      "ci/packer/scripts/03-bootstrap-helper.sh",
      "ci/packer/scripts/04-cleanup.sh"
    ]
  }

  post-processor "shell-local" {
    inline = [
      "echo 'Compressing and optimizing qcow2 image with 2M cluster size...'",
      "qemu-img convert -f qcow2 -O qcow2 -o cluster_size=2M -c output-qemu/${var.vm_name} output-qemu/com-ermnvldmr-apex-debian-13-optimized.qcow2",
      "mv output-qemu/com-ermnvldmr-apex-debian-13-optimized.qcow2 output-qemu/${var.vm_name}"
    ]
  }
}
