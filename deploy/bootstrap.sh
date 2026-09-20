#!/usr/bin/env bash
# Fresh Ubuntu 24.04 VPS -> ready for Nightjar. Run as root, once.
set -euo pipefail

USERNAME="${1:?usage: bootstrap.sh <username> <ssh-public-key-file>}"
KEYFILE="${2:?usage: bootstrap.sh <username> <ssh-public-key-file>}"

apt-get update && apt-get -y upgrade
apt-get install -y ca-certificates curl gnupg ufw fail2ban unattended-upgrades

# --- non-root user with sudo ---------------------------------------------
id -u "$USERNAME" &>/dev/null || adduser --disabled-password --gecos "" "$USERNAME"
usermod -aG sudo "$USERNAME"
install -d -m 700 -o "$USERNAME" -g "$USERNAME" "/home/$USERNAME/.ssh"
install -m 600 -o "$USERNAME" -g "$USERNAME" "$KEYFILE" "/home/$USERNAME/.ssh/authorized_keys"

# --- docker ---------------------------------------------------------------
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
usermod -aG docker "$USERNAME"

# --- firewall -------------------------------------------------------------
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

# --- ssh hardening --------------------------------------------------------
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart ssh

# --- unattended security updates -----------------------------------------
dpkg-reconfigure -f noninteractive unattended-upgrades

echo
echo "Done. Reconnect as: ssh $USERNAME@<ip>"
echo "Verify you can log in BEFORE closing this session."
