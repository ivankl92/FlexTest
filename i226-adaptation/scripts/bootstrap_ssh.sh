#!/usr/bin/env bash
# bootstrap_ssh.sh - one-time passwordless-ssh and passwordless-sudo setup
# between PC1 (this machine) and PC2.
#
# Usage: sudo ./bootstrap_ssh.sh ivank@172.16.28.17
#
# The password is read interactively (or from the SSHPASS environment variable)
# and is never written to disk or to a log.
set -euo pipefail

TARGET="${1:?usage: bootstrap_ssh.sh user@host}"
USER_NAME="${TARGET%@*}"

command -v sshpass >/dev/null || { apt-get update -qq && apt-get install -y sshpass >/dev/null; }

if [[ -z "${SSHPASS:-}" ]]; then
  read -rsp "password for ${TARGET}: " SSHPASS; echo
  export SSHPASS
fi

# root's key is what run_measurement.sh uses, since it runs under sudo.
[[ -f /root/.ssh/id_ed25519 ]] || ssh-keygen -t ed25519 -N '' -f /root/.ssh/id_ed25519 -C "tsn-flextest-pc1"

sshpass -e ssh-copy-id -o StrictHostKeyChecking=accept-new -i /root/.ssh/id_ed25519.pub "$TARGET"

# The measurement scripts call sudo on PC2 non-interactively.
sshpass -e ssh -o StrictHostKeyChecking=accept-new "$TARGET" \
  "echo '$SSHPASS' | sudo -S bash -c \"echo '${USER_NAME} ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/99-tsn-flextest && chmod 440 /etc/sudoers.d/99-tsn-flextest\"" \
  >/dev/null

unset SSHPASS

echo -n "verifying: "
ssh -o BatchMode=yes "$TARGET" "sudo -n true && echo 'passwordless ssh + sudo OK'"
