# Roadmap

This document tracks ideas rather than promises. Proposals and pull requests are welcome.

## Appearance and accessibility

- [x] Add a theme selector (Follow System, GitHub Light/Dark, Linear-inspired Dark, Catppuccin Mocha, Nord, Dracula)
- [x] Persist theme preference locally without relying on GitHub
- [ ] Audit contrast, reduced-motion support, focus indicators, and keyboard navigation
- [ ] Add configurable density and table column preferences

## Work prioritization

- [ ] **Today:** focus on reviews, overdue tasks, changes from collaborators, and pinned items
- [ ] **Waiting on others:** track blocked work separately from items requiring your action
- [ ] Saved filter views and more flexible organization/repository groups
- [ ] Link manual tasks to PRs and issues

## GitHub signals

- [x] Personal notification inbox, automatic polling, desktop alerts, and optional macOS background checks

- [ ] Flag Changes Requested, failing CI checks, unresolved review threads, and stale PRs
- [ ] Distinguish submitted reviews from withdrawn review requests
- [ ] Improve rate-limit handling and incremental synchronization
- [ ] Optional repository-level filters in GitHub search queries

## Portability

- [ ] Cross-platform background scheduling and native desktop notifications (browser alerts available; background scheduler is macOS only)
- [ ] Guided database backup restoration and retention controls
- [ ] Optional authenticated, encrypted multi-device synchronization
- [ ] Reproducible release artifacts and installers

## Security principles

GitHub integration stays read-only by default. The dashboard remains localhost-only unless a dedicated, authenticated hosted mode is designed and independently reviewed. Personal activity metadata, local notes and credentials must never be bundled with release artifacts or source-control commits.
