# GitHub Workdesk

**A local-first, open-source dashboard for managing your GitHub pull requests, requested reviews, issues, and personal to-dos.** Runs in your browser; your notes and synced metadata stay on your own machine. No hosted Workdesk account, Python dependencies, or server subscription required.

![GitHub Workdesk demo with fictional data](docs/dashboard-demo.png)

## What it does

- **PR tracking:** authored open and draft PRs, and recently closed or merged PRs.
- **Review queue:** requests for your review; removed requests are not automatically assumed to have been reviewed.
- **Issues:** assigned issues and issues you created.
- **Personal tasks:** title, priority, due date, notes, links, completion status and history, independent of GitHub.
- **Filters:** repository, text, time range, completion state and role.
- **Activity:** timestamped task updates and work-count snapshots.
- **Appearance:** Follow System, GitHub Light, GitHub Dark, Linear-inspired Dark, Catppuccin Mocha, Nord, and Dracula; saved in your browser.
- **Local backups:** export JSON, CSV, or a consistent SQLite backup.
- **Reminders:** optional macOS scheduled daily sync and desktop notification (Linux/Windows users can sync manually or schedule the CLI command themselves).

GitHub operations are **read-only**. You can edit your own tasks and local tracking notes; Workdesk will not post comments, edit issues, merge PRs, or change repositories.

## Requirements

- Python 3.10 or newer (standard library only)
- A GitHub account and network connection for GitHub synchronization
- [GitHub CLI (`gh`)](https://cli.github.com/) signed in with `gh auth login` **or** a suitable `GH_TOKEN` / `GITHUB_TOKEN` environment variable

## Install and start

```bash
git clone https://github.com/YOUR_USERNAME/github-workdesk.git
cd github-workdesk
python3 app.py
```

Or download the source ZIP and run `python3 app.py` from its folder. On macOS, you can also double-click `start.command` (after approving the file in macOS if prompted). The app opens **http://127.0.0.1:8765**.

1. Sign in to your own GitHub account with `gh auth login` (skip this if an authorized token is already in your environment).
2. Click **Sync GitHub**. Your GitHub username is detected automatically. No code edits are needed.
3. Use **Settings & backup** to optionally pin a GitHub username, set focus organizations, choose your preferred theme, and configure history lookback.
4. Create local tasks with **New task**. GitHub sync does not overwrite your private notes or local priorities.

When **Focus organizations** is empty, the app shows work from *all* accessible repositories. To narrow the list, enter an organization or repository owner in Settings, then select the **Focus organizations** filter. GitHub access is limited by your authenticated account permissions; private repositories require appropriate authorization and, when necessary, organization SSO approval.

## Command-line commands

```bash
python3 app.py                   # local web UI
python3 app.py --port 8799       # custom port
python3 app.py sync              # one-time GitHub sync
python3 app.py sync --notify     # sync and notify (macOS)
python3 app.py install-reminder --time 09:00  # macOS only
python3 app.py remove-reminder   # macOS only
python3 -m unittest discover -s tests -v
```

The macOS scheduled sync runs while you're logged in and the system executes the LaunchAgent. It does not require the browser page to stay open. Other operating systems can use the app and manual sync; automatic notifications and scheduling are currently implemented only for macOS.

## Data and privacy

GitHub Workdesk is not a hosted web service. Opening or publishing this **source repository** does not publish your GitHub work data. At runtime, the data lives in a per-user SQLite database:

| OS | Default database location |
| --- | --- |
| macOS | `~/Library/Application Support/GitHub Workdesk/workdesk.sqlite3` |
| Linux | `~/.local/share/github-workdesk/workdesk.sqlite3` |
| Windows | `%LOCALAPPDATA%/GitHub Workdesk/workdesk.sqlite3` |

Override with `WORKDESK_DATA_DIR=/custom/path`. The app listens on `127.0.0.1` and does **not** provide an authenticated multi-user server. Do not expose its port to the internet. GitHub requests go to the GitHub API; they do not flow through an application-operated backend. See [SECURITY.md](SECURITY.md) for more.

**Migration:** If you previously used an older, individually customized release, its SQLite database may have been stored in a differently named directory. This clean public release starts with its own database to avoid mixing installations. Back up the old database before considering any manual migration.

## Publishing your own fork or copy

To create a *new* repository from a downloaded source folder, install Git and GitHub CLI, authenticate with `gh auth login`, then run:

```bash
bash publish-to-github.command --public
```

This script detects your **own** GitHub username and asks you to explicitly confirm publication. It does not contain anyone else's GitHub identity. Use `--private` instead for a private repo. If a repository already exists at the target, the script refuses to overwrite it. Standard GitHub fork/clone flows are better for an existing repository.

## Contributing and license

Suggestions, issues and pull requests are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md). See [ROADMAP.md](ROADMAP.md) for proposed future enhancements. Licensed under the [MIT License](LICENSE). This is an independent community project and is not affiliated with or endorsed by GitHub.
