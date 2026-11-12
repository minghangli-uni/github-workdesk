'use strict';
const WORK_ROLES = {
  author: ['Author', 'You opened this item.'],
  assigned: ['Assignee', 'This item is assigned to you.'],
  review_requested: ['Review requested', 'Your review is currently requested.'],
  reviewer: ['Reviewed', 'A submitted review from you was verified.'],
  coauthor: ['Co-author', 'Your co-author credit was verified in a commit.'],
  code: ['Code contributor', 'Your commit authorship was verified.'],
  committer: ['Committer', 'You are recorded as the committer; this is separate from authorship.'],
  commenter: ['Participant', 'You commented in this discussion.'],
  personal: ['Personal', 'A personal task stored in Workdesk.'],
  review_previous: ['Previously requested', 'Your review was previously requested. This does not confirm that you reviewed it.'],
  unassigned: ['Previously assigned', 'This item is no longer assigned to you.']
};
const workIndex = {tasks: new Map(), contributions: new Map()};
function workKey(item) {
  if (item.kind !== 'pr' || !item.repo) return '';
  const number = item.number || String(item.url || '').match(/\/pull\/(\d+)\/?$/)?.[1];
  return number ? item.repo.toLowerCase() + '#' + number : '';
}
function indexWork(data) {
  workIndex.tasks.clear(); workIndex.contributions.clear();
  const owner = data.settings?.last_synced_user || data.settings?.github_username;
  if (owner && data.contributions?.username && owner.toLowerCase() !== data.contributions.username.toLowerCase()) return;
  for (const item of data.tasks || []) if (workKey(item)) workIndex.tasks.set(workKey(item), item);
  for (const item of data.contributions?.items || []) if (workKey(item)) workIndex.contributions.set(workKey(item), item);
}
function workRoles(task, evidenceRoles = []) {
  if (task?.source === 'manual') return ['personal'];
  const roles = new Set(evidenceRoles.filter(role => ['author','reviewer','coauthor','code','committer','commenter'].includes(role)));
  if (task?.role_author) roles.add('author');
  if (task?.role_assigned) roles.add('assigned');
  if (task?.role_review && task.github_state === 'open') roles.add('review_requested');
  else if ((task?.was_review_requested || task?.role_review) && !roles.has('reviewer')) roles.add('review_previous');
  if (task?.local_status === 'unassigned' && !task.role_assigned) roles.add('unassigned');
  return Object.keys(WORK_ROLES).filter(role => roles.has(role));
}
function taskRoles(task) { return workRoles(task, workIndex.contributions.get(workKey(task))?.roles || []); }
function contributionRoleIds(item) { return workRoles(workIndex.tasks.get(workKey(item)), item.roles || []); }
function roleBadges(roles) {
  if (!roles.length) return '<span class="small-badge role-badge">GitHub</span>';
  return roles.map(role => {
    const info = WORK_ROLES[role];
    return info ? `<span class="small-badge role-badge${role === 'review_requested' ? ' role-requested' : ''}" title="${esc(info[1])}">${esc(info[0])}</span>` : '';
  }).join('');
}
function workStatus(item) {
  if (item.source === 'manual') {
    const statuses = {todo: ['todo','To do','○'], in_progress: ['progress','In progress','◐'], blocked: ['blocked','Blocked','!'], done: ['done','Done','✓']};
    return statuses[item.local_status] || ['unknown','Status unavailable','?'];
  }
  const status = item.github_state ?? item.state;
  if (status === 'merged') return ['merged','Merged','⑂'];
  if (status === 'closed') return item.kind === 'pr' ? ['closed','Closed without merge','×'] : ['issue-closed','Closed','✓'];
  if (status === 'closed_unknown') return ['unknown','Closed · unverified','?'];
  if (item.kind === 'commit') return ['committed','Committed','●'];
  if (status === 'open') return (item.is_draft || item.status === 'Draft') ? ['draft','Draft','◌'] : ['open','Open','○'];
  return ['unknown','Status unavailable','?'];
}
function statusBadge(item) {
  const [type, label, icon] = workStatus(item);
  return `<span class="small-badge status-badge status-${type}"><span aria-hidden="true">${icon}</span>${esc(label)}</span>`;
}
function attentionBadge(label, tone = 'neutral') {
  return `<span class="small-badge attention-badge attention-${tone}">${esc(label)}</span>`;
}
function taskStatusBadges(task) {
  let html = statusBadge(task);
  if (task.github_state === 'open') {
    if (task.role_review) html += attentionBadge('Review needed', 'review');
    else if (task.was_review_requested && !task.role_author && !task.role_assigned) html += attentionBadge('Review no longer requested');
    else if (task.local_status === 'unassigned') html += attentionBadge('No longer assigned');
  }
  return html;
}
function contributionStatusBadges(item) {
  let html = statusBadge(item);
  if (item.state === 'open' && item.status !== 'Draft') {
    if (item.status === 'Changes requested') html += attentionBadge('Changes requested', 'changes');
    else if (item.status === 'Reviewed') html += attentionBadge('Approved review', 'approved');
    else if (item.status === 'Awaiting review') html += attentionBadge('Awaiting review', 'review');
    else if (item.details_complete === false) html += attentionBadge('Details incomplete');
  }
  return html;
}
function matchesWorkStatus(item, filter, finished = false, snoozed = false) {
  const type = workStatus(item)[0];
  switch (filter) {
    case 'outstanding': return !finished && !snoozed;
    case 'finished': return finished;
    case 'snoozed': return snoozed;
    case 'closed_unmerged': return type === 'closed';
    case 'closed_issue': return type === 'issue-closed';
    case 'closed_unknown': return (item.github_state ?? item.state) === 'closed_unknown';
    case 'in_progress': return type === 'progress';
    case 'any': case undefined: return true;
    default: return type === filter;
  }
}
