# Hosting guide: run your own GoW Ascension server and connect it to the launcher

There are three ways to host. Pick one:

| Way | Who can join | You need | Time |
|---|---|---|---|
| **A. LAN** (launcher button) | people on your own network (same router) | nothing extra | 1 minute |
| **B. VPS** (rented Linux server) | everyone, anywhere | a $4-6/month VPS | 10 minutes |
| **C. Your Windows PC** | everyone, anywhere | router port forwarding, a public IP | 20 minutes |

Every player, on any server, still needs **their own Online Pass** (the game checks it itself) and the
**same game region** (USA players with USA players, Europe with Europe).

---

## A. LAN party (easiest)

1. Host: open the launcher, page **Online**, turn on **Play online**.
2. Host: section **LAN play**, press **Host a LAN server**. When Windows Firewall asks about Python, tick
   **Private networks** and press **Allow**.
3. Everyone else (same Wi-Fi / router): page **Online**, press **Refresh**. The host's server appears at the top of
   **Pick a server** (region "LAN"). Pick it.
4. Everyone: same **Match code** (or none), press **Play**, then Multi-Player.

The LAN server stops when the host presses **Stop LAN server** or closes the launcher.

---

## B. A VPS (recommended for a public server)

### 1. Rent the server
Any provider works (DigitalOcean, Hetzner, Vultr, Linode...). Choose:
- **Ubuntu 24.04** (or 22.04), the smallest plan (1 CPU, 512 MB-1 GB RAM is enough),
- a **region near your players** (pings are everything in this game),
- **SSH key** login (the provider shows how to add yours).

Write down the server's **public IPv4** (for example `203.0.113.25`).

### 2. Log in
Windows: open **PowerShell** and type (your IP):
```
ssh root@203.0.113.25
```
Answer `yes` the first time.

### 3. Install
```
apt-get update && apt-get install -y git
git clone https://github.com/bc160201983/gowa-community-server.git
cd gowa-community-server
sudo sh deploy/install-linux.sh --name "My Server" --https
```
- `--name` is what players see in the launcher.
- `--https` gives you a free certificate (Caddy + sslip.io): the admin page at
  `https://203-0-113-25.sslip.io/admin` and HTTPS for the game's requests.

At the end it prints:
- **the address players use**: your IP,
- **the admin page link**,
- **the admin key** (keep it secret).

### 4. Check it
```
curl http://203.0.113.25:36101/api/status
```
You should see `{"name": "My Server", ... "players_online": 0 ...}`.

### 5. Update later
```
cd gowa-community-server && git pull && sudo sh deploy/install-linux.sh --name "My Server" --https
```
Accounts, keys and leaderboards are kept. Players online are disconnected for a few seconds, so update when the
server is empty (the admin page shows who is online).

---

## C. Your own Windows PC on the internet

Only if your internet has a **real public IP** (many mobile/fiber providers use carrier-grade NAT, then this does
not work: ask your provider for a "public IP", or use a VPS).

1. Install **Python 3.11 or newer** from python.org (tick "Add python.exe to PATH").
2. Download this repository (green **Code** button > **Download ZIP**) and unzip it, e.g. to `C:\gowa-server`.
3. Give your PC a fixed address on your network (router settings: "DHCP reservation").
4. On your router, forward to that address: **TCP 36101**, **TCP 36102**, **UDP 36103**.
5. Double-click `run-windows.bat internet` (or in a command window: `run-windows.bat internet`). Allow Python in the
   Windows Firewall (Private and Public).
6. It prints your public address: that is what players type.

The server runs while that window is open.

---

## Connect your server to the launcher

### Players type it (works at once)
Launcher, page **Online**:
1. Turn on **Play online**.
2. **Server**: type your server's address (for example `203.0.113.25`).
3. **Player name**: up to 16 letters/digits (the first sign-in keeps the name for that PC).
4. Press **Play**. In the game: Multi-Player > Quick Match.

### Share it in one paste
Host: **New code**, then **Copy code**. It copies `CODE@your-server` (for example `K7QW2P@203.0.113.25`).
Friends paste that into **Match code**: their launcher switches to your server and your private match by itself.

### Get it into everyone's server list
The launcher's **Pick a server** list is filled from the main community servers' list. To be listed for everyone:
send the project's admins (Discord) your server's **name, address and region**. An admin adds it on their admin
page (**Community servers > Add**); every launcher then shows it, with its ping and players, the next time it
loads the list (Refresh).

You can do the same on your own server's admin page: list other servers there, and players who use your server
see them too.

### HTTPS note
The launcher uses HTTPS automatically only for the built-in USA and Europe servers. With other servers the game's
sign-in uses plain HTTP; that is still safe for the key (sign-in is a challenge: the key file never leaves the
player's PC), but the player name and match traffic are not encrypted.

---

## Admin page

- With `--https`: `https://<ip-with-dashes>.sslip.io/admin`.
- Without: from your PC, `ssh -L 36101:127.0.0.1:36101 root@<server>`, then open `http://127.0.0.1:36101/admin`.
- Paste the admin key (`/opt/gowa/data/admin.key` on the server). Eight wrong keys lock that address for 15 minutes.
- Tiles: players online/searching/in a match, live matches, relayed packets; **Players**: kick, ban/unban, leaderboard
  reset/hide; **Community servers**: the list players see.

Command line (on the server): `sudo -u gowa python3 -I /opt/gowa/server/gowa_server.py --data /opt/gowa/data --admin list`
(also `ban NAME`, `unban NAME`).

## Discord posts (optional)

1. Discord: your channel > **Edit channel** > **Integrations** > **Webhooks** > **New Webhook** > **Copy Webhook URL**.
2. On the server:
   ```
   echo 'https://discord.com/api/webhooks/...' | sudo tee /opt/gowa/data/discord-webhook.txt
   sudo chown gowa:gowa /opt/gowa/data/discord-webhook.txt && sudo chmod 600 /opt/gowa/data/discord-webhook.txt
   ```
3. Edit `/etc/systemd/system/gowa.service`: add ` --discord-webhook-file /opt/gowa/data/discord-webhook.txt` at the end
   of the `ExecStart` line, then `sudo systemctl daemon-reload && sudo systemctl restart gowa`.

Posts: each match start, and the week's top 3 every day at 18:00 UTC (`--discord-daily-hour`).

## Troubleshooting

| Problem | Check |
|---|---|
| Launcher shows the server "offline" | `curl http://<ip>:36101/api/status` from another PC; firewall/ports (36101-36102 TCP, 36103 UDP) |
| Players sign in but never match | both use the same server and the same match code; same game region; Quick Match within ~5 minutes of each other |
| "name taken" | the name belongs to another PC's key; pick another name, or the admin unbans/renames |
| Lag | pick the server nearest to the middle of your players; players behind carrier-grade NAT always go through the relay |
| Logs | `journalctl -u gowa -n 100` and `/opt/gowa/server.log` |
| Service status | `systemctl status gowa` |
