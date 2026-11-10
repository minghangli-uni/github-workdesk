# Security and privacy

GitHub Workdesk is designed for **single-user localhost usage**. The publicly visible repository contains application source code and a fictional demo screenshot only; it should never contain your database, credentials, backups, or real work records.

- The web app binds to `127.0.0.1` and is **not** a production internet-facing service. Never port-forward it, publicly reverse-proxy it, or expose it as a shared hosted app without adding secure authentication, CSRF protections, and an independent security review.
- GitHub API access requires a `gh` authentication session or a token supplied at runtime. Use the minimum permissions necessary and authorize access to private organization repositories only if required. No token is bundled with this repository.
- The app's GitHub integration is read-only. Local task editing does not write to GitHub.
- Keep your SQLite database, exports, tokens, screenshots and log files outside Git. `.gitignore` blocks common local-data names, and the publisher stages only an explicit list of source files. Still review `git status` and commit history before publishing.
- Demo screenshots should only contain fictional repository names and synthetic data. Do not post private GitHub issue/PR titles in public bug reports.
- A public GitHub source repository **does not mean** a public hosted dashboard. Data continues to reside on each user's machine.
- To report a security vulnerability, use a private contact channel or GitHub's private vulnerability reporting feature if enabled. Avoid posting exploit details or tokens in public issues.
