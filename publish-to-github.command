#!/usr/bin/env bash
# Publish this source code to a repository belonging to the authenticated GitHub user.
# Usage: bash publish-to-github.command --public [optional-repository-name]
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
cd "$(dirname "$0")"

if [[ "${1:-}" != "--public" && "${1:-}" != "--private" ]]; then
  echo "Usage: bash publish-to-github.command --public [repository-name]"
  echo "       bash publish-to-github.command --private [repository-name]"
  echo "Visibility must be explicitly selected. This publishes SOURCE CODE, not your local data."
  exit 2
fi
VISIBILITY="$1"
REPO="${2:-github-workdesk}"
if [[ $# -gt 2 || ! "$REPO" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,99}$ || "$REPO" == .* ]]; then
  echo "Invalid repository name."
  exit 2
fi
for exe in git gh; do
  if ! command -v "$exe" >/dev/null 2>&1; then
    echo "Missing '$exe'. Install Git and GitHub CLI: https://cli.github.com/"
    exit 1
  fi
done
if ! gh auth status --hostname github.com >/dev/null 2>&1; then
  echo "Run 'gh auth login' first."
  exit 1
fi
OWNER="$(gh api user --jq '.login')"
FULL_REPO="$OWNER/$REPO"
if [[ -z "$OWNER" ]]; then echo 'Cannot detect GitHub login.'; exit 1; fi

# Refuse reusing an existing folder with unrelated Git state.
if [[ -d .git ]] && git remote get-url origin >/dev/null 2>&1; then
  echo "A Git origin already exists. Review the destination and push manually."
  exit 1
fi
if gh repo view "$FULL_REPO" --json nameWithOwner >/dev/null 2>&1; then
  echo "Repository https://github.com/$FULL_REPO already exists. Nothing pushed."
  exit 1
fi

echo ""
echo "Publishing source code to: https://github.com/$FULL_REPO"
echo "Repository visibility: ${VISIBILITY#--}"
echo "Only explicitly listed source files will be staged."
echo "No local database, tokens, exports, backups, or activity logs will be included."
echo ""
if [[ "$VISIBILITY" == '--public' ]]; then
  printf 'Type PUBLIC to confirm that this source code may be seen by everyone: '
  read -r confirmation
  if [[ "$confirmation" != 'PUBLIC' ]]; then echo 'Cancelled.'; exit 1; fi
fi

if [[ ! -d .git ]]; then git init -b main; fi
if ! git config user.name >/dev/null; then git config user.name "$OWNER"; fi
if ! git config user.email >/dev/null; then
  USER_ID="$(gh api user --jq '.id')"
  git config user.email "${USER_ID}+${OWNER}@users.noreply.github.com"
fi

SOURCE_FILES=(
  .gitignore
  .dockerignore
  .env.example
  .github/workflows/tests.yml
  LICENSE
  README.md
  ROADMAP.md
  SECURITY.md
  HOSTING.md
  CONTRIBUTING.md
  publish-to-github.command
  start.command
  app.py
  notifications.py
  contributions.py
  sync_diagnostics.py
  local_backups.py
  cloud.py
  cloud_identity.py
  cloud_push.py
  container_start.py
  Dockerfile
  render.yaml
  requirements-cloud.txt
  static/index.html
  static/styles.css
  static/app.js
  static/contributions.js
  static/sync-status.js
  static/mobile.js
  static/local-data.js
  static/mobile.css
  static/login.html
  static/offline.html
  static/sw.js
  static/manifest.webmanifest
  static/icon-192.png
  static/icon-512.png
  scripts/generate-app-icons.py
  tests/test_app.py
  tests/test_notifications.py
  tests/test_contributions.py
  tests/test_sync_diagnostics.py
  tests/test_cloud.py
  tests/test_contributions_ui.js
  tests/test_sync_status_ui.js
  tests/test_mobile_ui.js
  tests/test_local_backups.py
  tests/test_local_data_ui.js
  docs/dashboard-demo.png
)
git add -- "${SOURCE_FILES[@]}"

# Confirm unexpected files have not been staged by a pre-existing Git repo.
EXPECTED=$(mktemp)
ACTUAL=$(mktemp)
trap 'rm -f "$EXPECTED" "$ACTUAL"' EXIT
printf '%s\n' "${SOURCE_FILES[@]}" | sort > "$EXPECTED"
git diff --cached --name-only | sort > "$ACTUAL"
if grep -q '^$' "$ACTUAL"; then echo 'Unexpected empty file path.'; exit 1; fi
if [[ -n "$(comm -13 "$EXPECTED" "$ACTUAL")" ]]; then
  echo 'STOP: Unexpected staged files. Review with: git diff --cached --name-only'
  exit 1
fi
if ! git diff --cached --quiet; then git commit -m 'Initial public release: GitHub Workdesk'; fi

gh repo create "$FULL_REPO" "$VISIBILITY" \
  --description "Local-first GitHub PR, review, issue and task dashboard" \
  --source . --remote origin --push
printf '\nPublished: https://github.com/%s\n' "$FULL_REPO"
