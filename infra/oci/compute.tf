data "oci_identity_availability_domains" "this" {
  compartment_id = var.tenancy_ocid
}

data "oci_core_images" "ubuntu" {
  compartment_id           = var.tenancy_ocid
  operating_system         = "Canonical Ubuntu"
  operating_system_version = "22.04"
  shape                    = var.instance_shape
  sort_by                  = "TIMECREATED"
  sort_order               = "DESC"
}

resource "oci_core_instance" "this" {
  compartment_id      = var.tenancy_ocid
  availability_domain = data.oci_identity_availability_domains.this.availability_domains[0].name
  display_name        = "${var.project_name}-server"
  shape               = var.instance_shape

  create_vnic_details {
    subnet_id        = oci_core_subnet.this.id
    assign_public_ip = true
  }

  source_details {
    source_type = "image"
    source_id   = data.oci_core_images.ubuntu.images[0].id
  }

  # source_details: data.oci_core_images.ubuntuは「最新のイメージ」を都度
  # 取得するため、新しいイメージが公開されるとForceNew属性が変わったと
  # 判定され、稼働中のインスタンスが削除→再作成されてしまう
  # metadata（user_data）: cloud-initは初回起動時にしか実行されないため、
  # 更新してもインスタンスには反映されない。同じくForceNew属性のため、
  # 放置すると不要な再作成を引き起こす
  # 一度作成したインスタンスはここでは追従させない
  lifecycle {
    ignore_changes = [source_details, metadata]
  }

  metadata = {
    ssh_authorized_keys = file(var.ssh_public_key_path)
    user_data = base64encode(<<-EOF
      #!/bin/bash
      apt-get update
      apt-get install -y python3-venv python3-pip git
      # OCIのUbuntuイメージはOSレベルのiptablesでSSH以外を拒否する既定設定のため、
      # OCIセキュリティリストで許可していてもStreamlitのポートは別途開ける必要がある
      iptables -I INPUT 5 -p tcp --dport 8501 -j ACCEPT -m state --state NEW
      netfilter-persistent save
      EOF
    )
  }
}
