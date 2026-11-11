# Workdesk on your Mac and iPhone

Hosted mode runs the existing Python sync engine on an always-on server. Open the
same HTTPS address on your Mac and iPhone to share tasks, private notes,
contributions and notification read status. Your Mac can be asleep or off.
This is an installable web app, not an App Store or native Swift app.

The existing `python3 app.py` localhost mode still works without pip dependencies.
It remains a separate local workspace; it does not continuously mirror the hosted
database. Use the hosted address on both devices after importing your saved work.

## Prepared hosting option: Render

`render.yaml` defines one Docker web service in Singapore with a 1 GB persistent
disk and the 0.5 CPU / 512 MB plan. At the time of preparation (November 2026),
[Render lists this compute plan at US$7/month](https://render.com/pricing), plus
disk storage and any applicable usage/taxes. Confirm the current total in Render
before creating it. A free service that sleeps is unsuitable for background alerts.
No paid service is created by checking this configuration into Git.

1. Push these source changes to your own GitHub repository. Do not include your
   local database or credentials.
2. In Render, create a **Blueprint** from the repository and review `render.yaml`.
   Note the service's assigned HTTPS address. If the address has not been assigned
   yet, register the OAuth app with a placeholder HTTPS address and update both
   URLs below as soon as Render assigns it, before the first sign-in.
3. In [GitHub → Settings → Developer settings → OAuth Apps](https://github.com/settings/developers),
   register a new OAuth app:
   - Application name: GitHub Workdesk
   - Homepage URL: your assigned HTTPS address
   - Authorization callback URL: that address followed by `/auth/callback`
4. Set `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET` in Render's secret settings.
   Set `WORKDESK_OWNER_ID` to your numeric account ID. On your Mac, run
   `gh api user --jq .id` to look it up. Never paste secrets into a commit or chat.
5. Render generates `WORKDESK_SECRET_KEY`. Keep it stable across redeployments and
   retain a secure recovery copy. It encrypts credentials on the persistent disk;
   replacing it prevents the old identity store from being opened.
6. Deploy, open the HTTPS address, and sign in. Only the configured GitHub account
   ID is accepted. Other users cannot open your dashboard, exports or APIs.
7. Before the first hosted sync, use **Settings & backup → JSON export** in your
   local Workdesk. In hosted Workdesk, use **Settings & backup → Workdesk everywhere
   → Import a Workdesk JSON export**. Import copies tasks, annotations, history,
   contributions and discovery preferences into an empty hosted workspace. It is
   transactional and refuses to overwrite existing hosted work. It does not copy
   credentials, device subscriptions or the old notification polling checkpoint;
   the hosted notification inbox establishes its own quiet baseline.
8. Choose **Sync GitHub**. Thereafter, the hosted service refreshes work about every
   30 minutes and checks notifications at the interval in Settings. GitHub polling
   limits and retry backoff still apply.

After switching to the hosted workspace, you can pause notifications in the old
local copy to avoid duplicate alerts. The hosted service works independently.

GitHub OAuth scopes include `repo`, `notifications`, `read:user`, `read:org`, and
`user:email`. GitHub's classic OAuth `repo` scope includes write permissions even
though Workdesk only reads repository data. Review GitHub's consent screen;
organization SSO/approval requirements still apply. GitHub tokens and optional
refresh tokens stay encrypted on the host and are never sent to the browser.

## Install on your devices

- **iPhone:** open the hosted address in Safari → Share → Add to Home Screen →
  Open as Web App. Launch the new icon, sign in if needed, then choose
  **Settings → Workdesk everywhere → Enable device notifications**. Web Push
  requires iOS 16.4 or later and Home Screen installation.
- **Mac:** open the same hosted address in Safari → File → Add to Dock on supported
  macOS versions. Alternatively, use the browser directly.
- Enable notifications separately on each device. Signing out turns off push
  delivery for that session's subscriptions; it does not stop other devices.

Alerts use generic lock-screen text to avoid displaying private repository names.
The first notification poll is quiet. Subsequent new or updated threads can trigger
push even when Workdesk is closed. This is polling plus push delivery, not instant
GitHub webhooks; Focus settings, network availability and the platform can delay it.
There is no offline editing: the app shows an offline screen and only confirms
saves after the server accepts them.

## Operations and recovery

- Keep exactly **one process and one service instance**. SQLite, synchronization
  locks, push checkpoints and the background worker share the persistent disk.
  Adding replicas requires a different database/job architecture.
- Run `cloud.py` through the provided container behind the host's HTTPS proxy.
  It uses Waitress; the local `app.py` development HTTP server must stay private.
  The container launcher initializes the mounted data directory and drops to
  UID/GID 10001 before the server starts.
- Set `WORKDESK_PUBLIC_URL` to the exact HTTPS origin if using your own domain,
  and update GitHub's callback URL. Forward the original `Host` header. The app
  does not trust arbitrary forwarded host or scheme headers.
- Data exports contain work records, not the separate encrypted identity database.
  Keep regular JSON/SQLite exports plus provider disk backups. To recover OAuth
  sessions/subscriptions as well, retain the disk and its matching secret key.
- Do not change `WORKDESK_OWNER_ID` on an existing data directory. The stored
  owner binding rejects a different account. Account transfer is not supported.
- Redeployments with a persistent disk can briefly interrupt service. Background
  checks resume when the process restarts; unsent push updates retain checkpoints.
- Missing/deleted push subscriptions are removed. Temporary delivery failures
  retry with backoff and appear in device settings. Inbox records remain available.
- Source publication and cloud setup do not upload your local records. Import is
  an explicit authenticated action in your own hosted workspace.

## Validation

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-cloud.txt
.venv/bin/python -m unittest discover -s tests -v
node --test tests/test_*_ui.js
docker build -t github-workdesk .
```

Automated tests use fictional accounts and mocked OAuth/push services. Real GitHub
sign-in and iPhone delivery need verification after hosting credentials are set.
To run outside Render, provide the environment variables in `.env.example`, mount
persistent storage writable by container UID 10001, and route HTTPS to port 10000.
