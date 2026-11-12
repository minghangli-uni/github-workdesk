# Workdesk on your Mac and iPhone

Your Mac runs Workdesk and keeps its SQLite database and automatic backups. Your
iPhone opens that same workspace through a private HTTPS connection provided by
Tailscale. Render is not involved. This is an installable web app, not an App Store
app. No database migration or second GitHub login is needed.

## One-time setup

1. Start Workdesk on your Mac by double-clicking `start.command`. Open
   <http://127.0.0.1:8765>.
2. Install [Tailscale for Mac](https://tailscale.com/download/mac) and
   [Tailscale for iPhone](https://tailscale.com/download/ios). Open each app, sign
   in to the **same Tailscale account**, approve its VPN setup, and connect it.
   Use your own account on both devices; tagged devices are not supported.
3. In [Tailscale DNS settings](https://login.tailscale.com/admin/dns), enable
   **MagicDNS** and **HTTPS Certificates**. Tailscale's HTTPS setup explains the
   certificate transparency implications for your Mac's DNS name.
4. In Workdesk on the Mac, open **Settings → Connect to your Mac**. Click
   **Check connection**, then **Enable iPhone access**.
5. Copy the displayed private address (it ends in `.ts.net:8443`). Open it in
   **Safari on your iPhone**, with Tailscale connected.
6. Use **Share → Add to Home Screen**. If Safari offers **Open as Web App**, leave
   it enabled. Launch Workdesk from the new icon.

On a Mac with macOS Sonoma or later, Safari's **File → Add to Dock** installs the
local Workdesk address as a web app. The icon opens Workdesk; it does not start
the Python server. Keep `start.command` running, or run it again when needed.

Tailscale's [Personal plan](https://tailscale.com/pricing) is free for eligible
non-commercial personal use. Check its terms for work use. Tailscale coordinates
your private connection; it does not host the Workdesk database. This setup does
not sign you up for a paid plan.

## What works and what must stay on

- Tasks, contributions, notes, sync, settings, and notification history use the
  same Mac database. A successful Save on the iPhone is saved on the Mac.
- **Save backup on your Mac** creates a Mac backup. Download/export buttons save
  an additional copy on the device where you tap them.
- Keep the Mac awake, connected to the internet and Tailscale, and running
  Workdesk. A Home Screen icon does not make the phone independent of the Mac.
- The inbox refreshes while Workdesk is open on the iPhone. **Closed-app iPhone
  push alerts are not part of this setup.** Mac notification checks keep their
  existing settings.
- With the Mac offline, Workdesk shows an offline message. Private work records
  are not cached for offline use; unfinished edits are not saved automatically.
- Turn off access from **Settings → Turn off iPhone access** on the Mac. The
  gateway stops accepting requests immediately. Saved work is not deleted.

## Troubleshooting

- **Tailscale not found:** open it after installing. With the Standalone Mac
  version, enable its **Settings → CLI integration** if needed. Click Check
  connection again.
- **HTTPS setup failed:** confirm both devices are connected, and enable HTTPS
  certificates and MagicDNS in Tailscale DNS settings. Retry Enable iPhone access.
- **Port 8443 already in use:** Workdesk leaves existing Serve/Funnel mappings
  alone. Review `tailscale serve status` and free that port if appropriate. Do not
  reset all routes just to install Workdesk.
- **Wrong account / access denied:** sign in to the same Tailscale account on
  both devices. The GitHub and Tailscale accounts may use different emails.
- **Mac account or DNS name changed:** turn phone access off and on again from
  the Mac, then use the new address on the phone.
- **Phone cannot connect:** check the Mac is awake and Workdesk is running, then
  use Check connection. Tailscale access rules must allow your iPhone to reach
  this Mac on TCP 8443. A successful configuration check is not an end-to-end
  iPhone connectivity test.

## Implementation and security

The existing Mac endpoint stays on `127.0.0.1:8765`. Phone access creates a second
loopback listener at `127.0.0.1:8766` and a **Tailscale Serve** HTTPS reverse proxy
on port 8443. It never enables public Funnel access. Only the setup account's
`Tailscale-User-Login` identity is accepted. Missing identities (including Funnel
and tagged-device traffic), other users, unexpected hosts and cross-origin
writes are rejected. OS controls and connection setup remain Mac-only.

The gateway trusts the local Tailscale proxy. As with the existing local app,
other programs running as your Mac user can access local Workdesk data. Keep
Tailscale updated. See [Serve identity headers](https://tailscale.com/docs/features/tailscale-serve#identity-headers).

The owner login and private hostname are stored in `phone-access.json` beside the
database with owner-only file permissions. They are excluded from database
exports/backups; set up phone access again after moving to another Mac. No GitHub
token or Tailscale password is copied into this file. The service worker caches
only a generic offline page, not API responses or saved work.
