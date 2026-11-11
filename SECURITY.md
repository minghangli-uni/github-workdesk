# Security and privacy

GitHub Workdesk supports **local-only mode** (`app.py`) and **single-owner hosted mode** (`cloud.py`). Source files must never contain your database, credentials, backups, or real work records.

- Local `app.py` binds to `127.0.0.1`. Never port-forward or publicly expose this unauthenticated development server. Use the separate authenticated cloud entry point for hosting.
- GitHub API access requires a `gh` authentication session or a token supplied at runtime. Use the minimum permissions necessary and authorize access to private organization repositories only if required. No token is bundled with this repository.
- The app's GitHub integration is read-only. Local task editing does not write to GitHub.
- Keep your SQLite database, exports, tokens, screenshots and log files outside Git. `.gitignore` blocks common local-data names, and the publisher stages only an explicit list of source files. Still review `git status` and commit history before publishing.
- Demo screenshots should only contain fictional repository names and synthetic data. Do not post private GitHub issue/PR titles in public bug reports.
- A public source repository does not make your dashboard public. Local data resides on your machine; hosted data resides on your chosen server and requires owner sign-in.
- To report a security vulnerability, use a private contact channel or GitHub's private vulnerability reporting feature if enabled. Avoid posting exploit details or tokens in public issues.

Hosted mode requires an HTTPS origin, GitHub OAuth credentials, a random encryption
secret, and the owner's numeric GitHub ID. OAuth uses state, PKCE, an expiring
one-time flow and a browser-bound cookie. Only a verified matching account receives
a session. Sessions use random opaque cookies with Secure/HttpOnly/SameSite=Lax;
the server stores their hashes and checks expiry. Mutations require the configured
origin and a per-session CSRF token. All work APIs and exports require authentication.

OAuth/refresh tokens and push subscriptions are encrypted in a separate identity
database, excluded from JSON/SQLite work exports. Session and identity endpoints
return no bearer tokens. Keep the persistent disk and matching secret protected.
GitHub OAuth's `repo` scope includes write privileges; Workdesk's repository code
only performs reads. OAuth token exchange/refresh and delivery to push services
necessarily use POST; these do not modify GitHub issues or repositories.

Push subscriptions accept only supported Apple, Google and Mozilla HTTPS push
hosts, reject credentials/custom ports, validate encryption keys and disable
redirects. Lock-screen messages omit private titles and repository names. Alerts
are opt-in per device and signing out removes that session's subscriptions.

Run one container/process behind an HTTPS proxy, with persistent storage. This
implementation is for one owner's devices, not shared multi-user SaaS. No offline
work cache is maintained. See [HOSTING.md](HOSTING.md) for operation and recovery.
