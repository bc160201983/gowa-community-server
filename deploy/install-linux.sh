#!/bin/sh
# GoW Ascension community server: one-command install on a fresh Ubuntu 22.04/24.04 VPS (DigitalOcean, Hetzner, ...).
#   sudo sh deploy/install-linux.sh --name "My Server" [--ip <public IP>] [--https]
# What it does: user "gowa", code in /opt/gowa/server, data in /opt/gowa/data (accounts, keys, leaderboards),
# systemd service "gowa", firewall (ufw) ports 22/tcp, 36101/tcp, 36102/tcp, 36103/udp, daily log rotation and nightly
# account backups. --https also installs Caddy with a free <ip-with-dashes>.sslip.io certificate for the admin page and
# the game's sign-in (Let's Encrypt; ports 80/443). Run it again to update the code; data and keys are kept.
set -e
NAME="GoW Ascension community server"; IP=""; HTTPS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --name) NAME="$2"; shift 2;;
    --ip) IP="$2"; shift 2;;
    --https) HTTPS=1; shift;;
    *) echo "unknown option $1"; exit 2;;
  esac
done
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
HERE=$(cd "$(dirname "$0")/.." && pwd)
if [ -z "$IP" ]; then
  # the address of the interface that holds the default route (a VPS has its public IP there)
  IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="src") print $(i+1)}')
fi
case "$IP" in ""|10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*)
  echo "No public IPv4 found on this machine ($IP). Pass it: --ip <public IP>"; exit 1;; esac
echo "== installing for $IP ($NAME)"
export DEBIAN_FRONTEND=noninteractive
apt-get -qq update
apt-get -qq install -y python3 ufw logrotate >/dev/null
id gowa >/dev/null 2>&1 || useradd -r -m -d /opt/gowa -s /usr/sbin/nologin gowa
mkdir -p /opt/gowa/server /opt/gowa/data /opt/gowa/backups
cp "$HERE"/server/*.py "$HERE"/server/admin_page.html /opt/gowa/server/
chown -R gowa:gowa /opt/gowa; chmod 750 /opt/gowa
sed -e "s|@IP@|$IP|g" -e "s|@NAME@|$NAME|g" "$HERE/deploy/gowa.service.template" > /etc/systemd/system/gowa.service
cp "$HERE/deploy/gowa.logrotate" /etc/logrotate.d/gowa
cp "$HERE/deploy/gowa-backup.sh" /usr/local/sbin/gowa-backup && chmod 755 /usr/local/sbin/gowa-backup
echo "17 3 * * * root /usr/local/sbin/gowa-backup" > /etc/cron.d/gowa-backup
ufw allow 22/tcp >/dev/null; ufw allow 36101/tcp >/dev/null; ufw allow 36102/tcp >/dev/null; ufw allow 36103/udp >/dev/null
if [ "$HTTPS" = 1 ]; then
  apt-get -qq install -y caddy >/dev/null
  DOMAIN="$(echo "$IP" | tr . -).sslip.io"
  sed -e "s|@DOMAIN@|$DOMAIN|g" "$HERE/deploy/Caddyfile.template" > /etc/caddy/Caddyfile
  ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
  systemctl restart caddy
fi
ufw --force enable >/dev/null
systemctl daemon-reload
systemctl enable gowa >/dev/null 2>&1
systemctl restart gowa
sleep 2
systemctl is-active gowa
echo
echo "== done. Players type this in the launcher's Online page, Server box:  $IP"
[ "$HTTPS" = 1 ] && echo "   Admin page: https://$DOMAIN/admin"
[ "$HTTPS" = 1 ] || echo "   Admin page: ssh -L 36101:127.0.0.1:36101 root@$IP  then  http://127.0.0.1:36101/admin"
echo "   Admin key:  $(cat /opt/gowa/data/admin.key 2>/dev/null || echo '(made on first start: /opt/gowa/data/admin.key)')"
echo "   Status:     curl http://$IP:36101/api/status"
