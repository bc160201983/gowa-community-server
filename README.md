# GoW Ascension community server

<p align="left">
  <a href="https://www.youtube.com/@BilalReloaded"><img alt="YouTube: BilalReloaded" src="https://img.shields.io/badge/YouTube-BilalReloaded-FF0000?style=for-the-badge&logo=youtube&logoColor=white"></a>
  <a href="https://www.patreon.com/cw/bilalreloaded"><img alt="Support on Patreon" src="https://img.shields.io/badge/Patreon-Support-F96854?style=for-the-badge&logo=patreon&logoColor=white"></a>
</p>

A small, self-hosted matchmaking server for the **God of War: Ascension PC port** (PS3 recompilation), so the
game's multiplayer works again after the official servers closed.
Python 3 standard library only, no database server, no Sony keys. Unofficial fan project, not affiliated with Sony.

**Players still need their own Online Pass**, exactly like on the PS3: the game checks it itself, and this server
never touches that check.

## What it does

- **Matchmaking** for the game's own protocol: Quick Match, the Game Mode list (Favor of the Gods, Team Favor,
  Capture the Flag, Trial of the Gods solo/co-op), all 2 to 8 player modes.
- **Private matches**: a match code puts friends only with each other.
- **Accounts without passwords**: the first sign-in with a free name keeps that name for that player's PC
  (challenge-response with a key file, signed tickets). Bans and kicks.
- **Relay** for players whose router blocks a direct connection (carrier-grade NAT, strict NAT), inside one match only.
- **Admin web page**: players online, live matches, the search queue, bans, kicks, the community server list.
- **Leaderboards** from the stats the games save after each match, **all time and this week**
  (shown as "reported by players' games, not verified"); public page at `/leaderboard`.
- **LAN discovery**: on a home network the launcher finds the server by itself.
- **Discord posts** (optional): "match started" and the week's leaders, through a channel webhook.
- **Server list**: the launcher shows every server's ping, players online and players searching, with an *Auto*
  choice that goes where people are searching; match codes can carry their server (`CODE@server`).
- Rate limits, sign-in lockouts, connection caps, daily log rotation, nightly account backups.

## Host one

**Step-by-step guide (LAN, VPS, Windows PC, connecting to the launcher, admin, Discord, troubleshooting):
[docs/HOSTING-GUIDE.md](docs/HOSTING-GUIDE.md)**

### 1. On a VPS (players anywhere, about 5 minutes)

Any Ubuntu 22.04 / 24.04 server with a public IPv4 (a $4-6/month VPS is plenty). As root:

```sh
git clone https://github.com/<you>/gowa-community-server.git
cd gowa-community-server
sudo sh deploy/install-linux.sh --name "My Server" --https
```

It prints the address players type in the launcher, the admin page link and the admin key.
`--https` adds a free certificate (Caddy + sslip.io) so the admin page and the game's sign-in use HTTPS.
Run the same command again to update; accounts and keys are kept.

### 2. On your own Windows PC

- **Same network only (LAN):** easiest from the game's launcher: Online page, **Host a LAN server**. Or
  `run-windows.bat lan`. The others press **Refresh** in their launcher and pick it.
- **Internet:** `run-windows.bat internet`, then forward TCP 36101-36102 and UDP 36103 on your router to this PC.
  (It asks api.ipify.org once for your public address.)

Allow Python in the Windows Firewall when it asks.

### Ports

| Port | Use |
|---|---|
| TCP 36101 | HTTP: sign-in, match settings, peer addresses, status, admin page (local) |
| TCP 36102 | the game's matchmaker protocol (SAM) |
| UDP 36103 | address probe and relay |
| UDP 36104 | LAN discovery (LAN mode only) |
| UDP 3658 | (players' PCs) the game's own player-to-player traffic |

## Players

### What every player needs

1. **Your own copy of the game**, set up in the launcher as usual (the launcher shows *America disc* or *Europe disc*).
   Players in one match need the **same region**: USA (BCUS98232) with USA, Europe (BCES01741) with Europe, as on
   the PS3.
2. **Your own Online Pass** (the game calls it a *Network Pass*). It is the licence file **`INDEX.EDAT`** that your
   PS3 saved when you redeemed the Online Pass code from your game box (or bought it on the PlayStation Store).
   The game checks it itself, exactly like on the console: no pass, no online play. Nothing here skips that check,
   and nobody can give you theirs: use only your own.
   - **Where it is on your PS3:** `/dev_hdd0/game/BCUS98232/USRDIR/DLC/DLC0/INDEX.EDAT` (USA) or
     `/dev_hdd0/game/BCES01741/USRDIR/DLC/DLC0/INDEX.EDAT` (Europe). Copy it to your PC with a tool you already use
     for your console (for example an FTP server on your own PS3, or a backup of its hard drive).
   - **Where it goes on your PC** (inside the port folder; make the folders if they are missing):

     | Your game | Put `INDEX.EDAT` here |
     |---|---|
     | USA (BCUS98232) | `ascension-recomp\ascension\gamedata\dev_hdd0\game\BCUS98232\USRDIR\DLC\DLC0\INDEX.EDAT` |
     | Europe (BCES01741) | `ascension-recomp\ascension\gamedata\dev_hdd0\game\BCES01741\USRDIR\DLC\DLC0\INDEX.EDAT` |

   - The name must be exactly `INDEX.EDAT`. In File Explorer turn on *View > Show > File name extensions* so it is
     not secretly `INDEX.EDAT.txt`. The file is small (a few hundred bytes).
   - With *Play online* on, the launcher's **Play** tells you when the pass is not where the game reads it, with the
     exact path. If the game itself says **"A Network Pass is required"**, the pass is missing, in the wrong folder,
     or from the other region.
3. **A server**: type its address on the launcher's **Online** page (*Server*), paste a friend's invite code
   (`CODE@server`) into *Match code*, or pick a LAN server under *Pick a server*.
4. **A player name** (up to 16 letters or digits). The first sign-in keeps it for your PC: the key file
   `ascension-recomp\ascension\gamedata\mpnet-token.txt` proves it is you. Keep that file (copy it with your saves
   when you move to a new folder) and never share it.
5. **Windows Firewall:** when it asks about `AscensionRecomp.online.exe`, allow it (at least *Private networks*).
   The game talks to other players on **UDP 3658**.

### In the launcher

Launcher, **Online** page: turn on *Play online*, pick a server in *Pick a server* (or type its address in
*Server*), choose a player name, press Play. Everyone in one match must use the same server.

## Admin

- Page: `https://<ip-with-dashes>.sslip.io/admin` (with `--https`) or through an SSH tunnel
  (`ssh -L 36101:127.0.0.1:36101 root@<server>` then `http://127.0.0.1:36101/admin`).
- Key: `/opt/gowa/data/admin.key`. Eight wrong keys lock that address out for 15 minutes.
- Command line: `python3 -I server/gowa_server.py --data /opt/gowa/data --admin list | ban NAME | unban NAME`.
- Want your server in the launcher's list for everyone? Ask on the project's Discord.

## Discord posts (optional)

In Discord: channel settings > Integrations > Webhooks > New Webhook > Copy Webhook URL. Put the URL in a file on
the server (for example `/opt/gowa/data/discord-webhook.txt`, readable only by root and `gowa`) and add
`--discord-webhook-file /opt/gowa/data/discord-webhook.txt` to the `ExecStart` line of
`/etc/systemd/system/gowa.service`, then `systemctl daemon-reload && systemctl restart gowa`.
Posts: every match start (mode and players) and once a day the week's top 3 (`--discord-daily-hour`, UTC, default 18).
Mentions are switched off and posts are rate limited.

## Tests

```sh
python3 -I tests/test_sam.py
```

## Layout

```
server/   the server (gowa_server.py + its modules, admin_page.html)
deploy/   Linux installer, systemd unit, Caddy (HTTPS), log rotation, backups
tests/    unit and loopback tests
run-windows.bat
```
