#!/bin/sh
# Provision (or update) the evaluation gateway in an unprivileged LXC on a Proxmox host.
#
#   eval/gateway/deploy-lxc.sh            # as your normal user, not sudo
#   (MERCURY, VMID, KEY_SRC below can be overridden with environment variables)
#
# Run from the runner host. It needs root ssh to the Proxmox host (once, by whoever runs it);
# the runner itself never gets Proxmox access. Safe to re-run: it creates the container only
# if missing, always re-installs gateway.py and the unit, and rewrites the key from KEY_SRC.
# The provider key is streamed over ssh into a 0600 file inside the container and is never
# printed or stored on the Proxmox host.
#
# Result:
#   container  envelope-gateway (Debian 13, unprivileged, onboot)
#   gateway    systemd unit, listens on 127.0.0.1:8787 inside the container only
#   runner     ssh key restricted to (a) forwarding 127.0.0.1:8787 and (b) the `relay`
#              forced command, which only pipes to the admin socket; it cannot read the key
#   runner host: ~/.config/envelope-eval/{gateway.toml,gateway_ed25519,known_hosts} and a user
#              unit `envelope-gateway-tunnel` publishing the container's gateway on
#              127.0.0.1:8787 here
set -eu

MERCURY=${MERCURY:-root@192.168.94.24}
VMID=${VMID:-114}
NAME=envelope-gateway
TEMPLATE=${TEMPLATE:-local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst}
KEY_SRC=${KEY_SRC:-$HOME/.qwen/settings.json}   # JSON with env.OPENROUTER_API_KEY
PORT=${PORT:-8787}
HERE=$(cd "$(dirname "$0")" && pwd)
CFG=$HOME/.config/envelope-eval
RUNNER_KEY=$CFG/gateway_ed25519

[ "$(id -u)" -ne 0 ] || { echo "run as your normal user, not with sudo: the key, config and tunnel unit belong to you" >&2; exit 1; }

m() { ssh -o BatchMode=yes "$MERCURY" "$@"; }
ct() { m "pct exec $VMID -- $*"; }

m true || { echo "cannot ssh to $MERCURY (BatchMode: needs an agent key and a known host key)" >&2; exit 1; }
mkdir -p "$CFG" && chmod 700 "$CFG"
[ -f "$RUNNER_KEY" ] || ssh-keygen -q -t ed25519 -N '' -C envelope-eval-runner -f "$RUNNER_KEY"

# --- container ------------------------------------------------------------------------
if ! m "pct status $VMID" >/dev/null 2>&1; then
  echo "creating container $VMID"
  m "pct create $VMID $TEMPLATE --hostname $NAME --unprivileged 1 --features nesting=1 --cores 1 --memory 256 \
     --swap 0 --rootfs local-zfs:2 --net0 name=eth0,bridge=br0,ip=dhcp,firewall=0 --onboot 1 \
     --tags envelope-eval --description 'Evaluation model gateway: holds the OpenRouter key'"
fi
# systemd 257 in an unprivileged container needs nesting (as on the other containers here)
if ! m "pct config $VMID" | grep -q 'nesting=1'; then
  m "pct set $VMID --features nesting=1"
  m "pct status $VMID" | grep -q running && m "pct reboot $VMID"
fi
m "pct status $VMID" | grep -q running || m "pct start $VMID"

IP=
for _ in $(seq 1 30); do
  IP=$(ct "ip -4 -o addr show dev eth0" 2>/dev/null | sed -E 's/.* inet ([0-9.]+)\/.*/\1/' | head -1)
  [ -n "$IP" ] && break
  sleep 2
done
[ -n "$IP" ] || { echo "container has no IPv4 address" >&2; exit 1; }
for tool in python3 sshd systemctl; do
  ct "sh -c 'command -v $tool >/dev/null || [ -x /usr/sbin/$tool ]'" ||
    { echo "container lacks $tool: installing packages needs your approval" >&2; exit 1; }
done

# --- files and accounts ---------------------------------------------------------------
scp -q "$HERE/gateway.py" "$MERCURY:/tmp/envelope-gateway.py"
m "pct push $VMID /tmp/envelope-gateway.py /tmp/gateway.py && rm -f /tmp/envelope-gateway.py"
PUB=$(cat "$RUNNER_KEY.pub")
ct "sh -s" <<EOF
set -eu
getent group envelope-eval >/dev/null || groupadd --system envelope-eval
getent passwd envelope-gateway >/dev/null || useradd --system --no-create-home \
  --home-dir /var/lib/envelope-gateway --shell /usr/sbin/nologin --gid envelope-eval envelope-gateway
getent passwd gw-runner >/dev/null || useradd --create-home --shell /bin/sh --gid envelope-eval gw-runner
passwd -l gw-runner >/dev/null
install -d -m 0755 /opt/envelope-gateway
install -m 0644 /tmp/gateway.py /opt/envelope-gateway/gateway.py && rm -f /tmp/gateway.py
install -d -o envelope-gateway -g envelope-eval -m 0700 /etc/envelope-gateway

# The runner's key: forwarding to the gateway port only, and the relay as its one command.
install -d -o gw-runner -g envelope-eval -m 0700 /home/gw-runner/.ssh
printf '%s\n' 'command="/usr/bin/python3 /opt/envelope-gateway/gateway.py relay --socket /run/envelope-gateway/admin.sock",restrict,port-forwarding,permitopen="127.0.0.1:$PORT" $PUB' \
  > /home/gw-runner/.ssh/authorized_keys
chown gw-runner:envelope-eval /home/gw-runner/.ssh/authorized_keys
chmod 0600 /home/gw-runner/.ssh/authorized_keys
cat > /etc/ssh/sshd_config.d/envelope-gateway.conf <<'CONF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
AllowUsers gw-runner
AllowStreamLocalForwarding no
X11Forwarding no
CONF

cat > /etc/systemd/system/envelope-gateway.service <<'UNIT'
[Unit]
Description=Envelope evaluation model gateway
After=network-online.target
Wants=network-online.target

[Service]
User=envelope-gateway
Group=envelope-eval
UMask=0027
ExecStart=/usr/bin/python3 /opt/envelope-gateway/gateway.py --listen 127.0.0.1:$PORT --admin-socket /run/envelope-gateway/admin.sock --key-file /etc/envelope-gateway/openrouter.key --ledger /var/lib/envelope-gateway/ledger.jsonl --app-url https://envelope.local/eval --app-title 'Envelope evaluation harness' --app-categories cli-agent
RuntimeDirectory=envelope-gateway
RuntimeDirectoryMode=0770
StateDirectory=envelope-gateway
StateDirectoryMode=0750
Restart=always
RestartSec=2
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable ssh >/dev/null 2>&1 || true
systemctl restart ssh 2>/dev/null || systemctl restart sshd
EOF

# --- the provider key: straight into a 0600 file, never echoed -------------------------
python3 - "$KEY_SRC" <<'PY' | ct "sh -c 'umask 077; cat > /etc/envelope-gateway/openrouter.key'"
import json, sys
key = json.load(open(sys.argv[1]))["env"]["OPENROUTER_API_KEY"].strip()
if not key:
    raise SystemExit("empty OPENROUTER_API_KEY")
sys.stdout.write(key)
PY
ct "sh -c 'chown envelope-gateway:envelope-eval /etc/envelope-gateway/openrouter.key; chmod 0600 /etc/envelope-gateway/openrouter.key; systemctl enable envelope-gateway >/dev/null 2>&1; systemctl restart envelope-gateway'"
sleep 2
ct "systemctl is-active envelope-gateway" | grep -qx active ||
  { ct "journalctl -u envelope-gateway -n 20 --no-pager" >&2; exit 1; }

# --- the runner side ------------------------------------------------------------------
HOSTKEY=$(ct "cat /etc/ssh/ssh_host_ed25519_key.pub" | cut -d' ' -f1,2)
HOST=$IP
if getent hosts "$NAME.lab.lan" >/dev/null 2>&1; then HOST=$NAME.lab.lan; fi
{ echo "$HOST $HOSTKEY"; [ "$HOST" = "$IP" ] || echo "$IP $HOSTKEY"; } > "$CFG/known_hosts"
cat > "$CFG/gateway.toml" <<EOF
host = "$HOST"
user = "gw-runner"
key = "$RUNNER_KEY"
known_hosts = "$CFG/known_hosts"
port = $PORT
EOF

mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/envelope-gateway-tunnel.service" <<EOF
[Unit]
Description=Tunnel to the envelope evaluation gateway (LXC)
After=network-online.target

[Service]
ExecStart=/usr/bin/ssh -N -F /dev/null -i $RUNNER_KEY -o IdentitiesOnly=yes \
  -o UserKnownHostsFile=$CFG/known_hosts -o StrictHostKeyChecking=yes -o BatchMode=yes \
  -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
  -L 127.0.0.1:$PORT:127.0.0.1:$PORT gw-runner@$HOST
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now envelope-gateway-tunnel >/dev/null 2>&1
echo "gateway ready: container $VMID at $HOST, tunnel on 127.0.0.1:$PORT"
