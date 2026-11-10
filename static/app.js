'use strict';

// Per-browser appearance preference. No identity or personal details are committed.
const AVAILABLE_THEMES = new Set(['system', 'github-light', 'github-dark', 'linear-dark', 'catppuccin-mocha', 'nord', 'dracula']);
let notificationData = null;
let notificationRequestRunning = false;
let selectedTheme = 'system';
try { selectedTheme = localStorage.getItem('github-workdesk-theme') || 'system'; } catch (_) {}
if (!AVAILABLE_THEMES.has(selectedTheme)) selectedTheme = 'system';
function applyTheme(theme, persist = true) {
  if (!AVAILABLE_THEMES.has(theme)) return;
  selectedTheme = theme;
  const dark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  document.documentElement.dataset.theme = theme === 'system' ? (dark ? 'github-dark' : 'github-light') : theme;
  if (persist) try { localStorage.setItem('github-workdesk-theme', theme); } catch (_) {}
  if (document.getElementById('theme-select')) document.getElementById('theme-select').value = theme;
}
applyTheme(selectedTheme, false);
if (window.matchMedia) {
  const preference = window.matchMedia('(prefers-color-scheme: dark)');
  if (preference.addEventListener) preference.addEventListener('change', () => {
    if (selectedTheme === 'system') applyTheme('system', false);
  });
}
const state = {data:null, view:'overview', limit:30, filter:{period:'all',dateField:'updated_at',repo:'all',status:'any',query:'',from:'',to:''}, editing:null, poll:null};
const $ = id => document.getElementById(id);
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const short = (s,n=90) => String(s||'').length>n ? String(s).slice(0,n-1)+'…' : String(s||'');
const localDate = dt => {const d=new Date(dt);return isNaN(+d)?'':`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`};
const dateToday = () => localDate(new Date());
const relativeDate = input => {if(!input)return '—';const time=new Date(input).getTime();if(!time)return '—';const days=Math.floor((Date.now()-time)/86400000);if(days<0)return 'Future';if(days===0)return 'Today';if(days===1)return 'Yesterday';if(days<14)return `${days}d ago`;return new Date(time).toLocaleDateString('en-AU',{day:'numeric',month:'short',year:days>365? 'numeric':undefined})};
const formatTime = d => new Date(d).toLocaleString('en-AU',{day:'numeric',month:'short',year:'numeric',hour:'numeric',minute:'2-digit'});
const isFinished = t => t.source==='manual' ? t.local_status==='done' : ['merged','closed','closed_unknown'].includes(t.github_state) || (!t.role_author && !t.role_review && !t.role_assigned && (t.was_review_requested || t.local_status==='unassigned'));
const isSnoozed = t => !!t.snoozed_until && t.snoozed_until>=dateToday();
const priorityText = v => ({1:'High',2:'Medium',3:'Low'})[v] || 'Medium';
const inScope = t => t.source==='manual' || state.filter.repo==='all' || (state.filter.repo==='focus' ? (!(state.data.settings.focus_orgs||[]).length || (state.data.settings.focus_orgs||[]).some(o=>t.repo.split('/')[0].toLowerCase()===o.toLowerCase())) : t.repo===state.filter.repo);
const mainItems = () => state.data.tasks.filter(inScope);
function counts(){const ts=mainItems();return {reviews:ts.filter(t=>t.role_review&&!isFinished(t)).length,prs:ts.filter(t=>t.kind==='pr'&&t.role_author&&!isFinished(t)).length,issues:ts.filter(t=>t.kind==='issue'&&t.role_assigned&&!isFinished(t)).length,manual:ts.filter(t=>t.source==='manual'&&!isFinished(t)).length,completed:ts.filter(isFinished).length}}
const navNames = {notifications:'Notifications',overview:'Overview',reviews:'My reviews',prs:'My pull requests',issues:'GitHub issues',tasks:'Personal tasks',completed:'Completed',history:'Activity history',settings:'Settings & backup'};
const pageInfo={notifications:['STAY UP TO DATE','Your notifications','New activity in the conversations and work that involve you.'],overview:['YOUR DAILY SNAPSHOT','Your work at a glance','Track what’s moving, what needs your review, and what’s next.'],reviews:['CODE REVIEW QUEUE','Reviews awaiting you','The pull requests where your feedback is currently requested.'],prs:['PULL REQUEST TRACKER','Your pull requests','Monitor outstanding, merged, closed, and draft PRs.'],issues:['ISSUE TRACKER','GitHub issues','Your assigned issues and issues you opened.'],tasks:['PERSONAL TO-DOS','Your personal tasks','Add, prioritise, and complete work that does not live on GitHub.'],completed:['WORK HISTORY','Completed and closed','A lasting record of merged, closed, and finished work.'],history:['AUDIT TRAIL','Activity history','See what changed and when it happened.'],settings:['YOUR WORKSPACE','Settings & backups','Manage synchronization, reminders, and local data.']};

async function request(path,opts={}){const r=await fetch(path,{...opts,headers:{'Content-Type':'application/json',...(opts.headers||{})}});let v;try{v=await r.json()}catch{v={error:`HTTP ${r.status}`}}if(!r.ok)throw Error(v.error||`HTTP ${r.status}`);return v}
function toast(message,error=false){const node=document.createElement('div');node.className='toast'+(error?' error':'');node.textContent=message;$('toast-stack').appendChild(node);setTimeout(()=>node.remove(),4700)}
function banner(html){$('banner').innerHTML=html;$('banner').classList.remove('hidden')}
function clearBanner(){$('banner').classList.add('hidden')}
async function refresh(silent=false){try{state.data=await request('/api/dashboard');render();if(!silent&&!state.data.sync.running)toast('Local dashboard refreshed');}catch(e){banner(`<strong>Cannot load the local database.</strong> ${esc(e.message)}`)}}

function render(){if(!state.data)return;const data=state.data, c=counts();
  $('stat-reviews').textContent=c.reviews;$('stat-prs').textContent=c.prs;$('stat-issues').textContent=c.issues;$('stat-manual').textContent=c.manual;$('stat-completed').textContent=c.completed;
  for(const [k,v] of Object.entries({reviews:c.reviews,prs:c.prs,issues:c.issues,tasks:c.manual,completed:c.completed}))$('nav-'+k).textContent=v;
  $('today-label').textContent=new Date().toLocaleDateString('en-AU',{weekday:'short',day:'numeric',month:'short',year:'numeric'});
  $('footer-date').textContent=formatTime(new Date());
  $('nav').querySelectorAll('[data-view]').forEach(e=>e.classList.toggle('active',e.dataset.view===state.view));
  document.querySelectorAll('.sidebar .nav-item').forEach(e=>e.classList.toggle('active',e.dataset.view===state.view));
  const [eyebrow,title,subtitle]=pageInfo[state.view];$('page-eyebrow').textContent=eyebrow;$('breadcrumb-view').textContent=navNames[state.view];
  if(state.view==='overview') {const hour=new Date().getHours();$('page-title').innerHTML=(hour<12?'Good morning':hour<18?'Good afternoon':'Good evening')+' <span class="title-accent">✦</span>';}else $('page-title').textContent=title;
  $('page-subtitle').textContent=subtitle;
  $('normal-panel').classList.toggle('hidden',['history','settings','notifications'].includes(state.view));
  $('notifications-panel').classList.toggle('hidden',state.view!=='notifications');
  $('history-panel').classList.toggle('hidden',state.view!=='history');
  $('settings-panel').classList.toggle('hidden',state.view!=='settings');
  $('overview-panels').classList.toggle('hidden',state.view!=='overview');
  const last=data.runs.find(r=>r.status!=='running');$('signal-dot').className='signal '+(last?.status==='success'?'online':last?'error':'');
  $('connection-text').textContent=last?.status==='success'?'GitHub connected':last?.status==='partial'?'Sync partially complete':last?.status==='failed'?'Sync needs attention':'Local workspace';
  $('sync-small').textContent=last?'Synced '+relativeDate(last.finished_at):'Not synced yet';
  $('last-updated').textContent=last?`Last sync: ${formatTime(last.finished_at||last.started_at)}`:'No GitHub sync recorded';
  const syncing=!!data.sync.running;$('sync-btn').disabled=syncing;$('sync-button-label').textContent=syncing?'Syncing…':'Sync GitHub';$('sync-icon').textContent=syncing?'◌':'↻';
  if(last?.status==='failed')banner(`<strong>Last GitHub sync failed.</strong> ${esc(last.errors.join('; '))} <button data-go="settings">Open settings</button>`);
  else if(last?.status==='partial')banner(`<strong>Partial GitHub sync.</strong> Some searches failed or were incomplete; previous assignments were preserved. ${esc(last.errors.slice(0,2).join('; '))}`);
  else if(!last)banner('<strong>Welcome to your local workdesk.</strong> Sign in with <code>gh auth login</code> on your computer, then select “Sync GitHub” to import your PRs, review requests, and issues. You can add personal tasks immediately.');
  else clearBanner();
  $('banner').querySelectorAll('[data-go]').forEach(b=>b.onclick=()=>switchView(b.dataset.go));
  renderRepositories();renderTable();renderAttention();renderChart();renderHistory();renderSettings();renderNotifications();
}

function renderRepositories(){const sel=$('repo-select'),current=state.filter.repo;const fixed=[['all','All repositories'],['focus','Focus organizations']];sel.innerHTML=fixed.map(([v,t])=>`<option value="${esc(v)}">${esc(t)}</option>`).join('')+state.data.repositories.map(v=>`<option value="${esc(v)}">${esc(v)}</option>`).join('');sel.value=current;if(sel.value!==current){state.filter.repo='all';sel.value='all'}}
function afterDays(days){const d=new Date();d.setHours(0,0,0,0);d.setDate(d.getDate()-Number(days)+1);return d.getTime()}
function filteredTasks(){const f=state.filter;return mainItems().filter(t=>{
  const finished=isFinished(t);
  if(state.view==='reviews' && !(t.role_review&&!finished))return false;
  if(state.view==='prs' && !(t.kind==='pr'&&t.role_author))return false;
  if(state.view==='issues' && !(t.kind==='issue'&&(t.role_assigned||t.role_author)))return false;
  if(state.view==='tasks' && t.source!=='manual')return false;
  if(state.view==='completed' && !finished)return false;
  if(state.view==='overview' && (finished||isSnoozed(t)))return false;
  if(f.status==='open'&&finished)return false;
  if(f.status==='done'&&!finished)return false;
  if(f.status==='snoozed'&&!isSnoozed(t))return false;
  if(f.query&&!`${t.title} ${t.repo} ${t.notes} ${t.description} ${t.labels.join(' ')}`.toLowerCase().includes(f.query.toLowerCase()))return false;
  const dt=t[f.dateField];
  if(f.period!=='all'){
    if(!dt)return false;
    const ms=new Date(dt).getTime();
    if(f.period==='custom'){
      const d=localDate(dt);if(f.from&&d<f.from)return false;if(f.to&&d>f.to)return false;
    }else if(ms<afterDays(f.period))return false;
  }
  return true;
}).sort((a,b)=>{
  if(state.view==='completed')return new Date(b.closed_at||b.updated_at)-new Date(a.closed_at||a.updated_at);
  const rank=t=>{if(isSnoozed(t))return 7;if(t.role_review)return 0;if(t.due_date&&t.due_date<dateToday())return 1;if(t.local_status==='blocked')return 2;if(t.priority===1)return 3;if(t.role_author)return 4;return 5};
  return rank(a)-rank(b)||(a.due_date||'9999').localeCompare(b.due_date||'9999')||new Date(b.updated_at)-new Date(a.updated_at);
});}
function taskBadge(t){if(t.source==='manual')return `<span class="small-badge ${t.local_status==='done'?'done':t.local_status==='blocked'?'blocked':'local'}">${esc({todo:'To do',in_progress:'In progress',blocked:'Blocked',done:'Done'}[t.local_status]||t.local_status)}</span>`;
  if(t.github_state==='merged')return '<span class="small-badge done">Merged</span>';
  if(t.github_state==='closed')return '<span class="small-badge done">Closed</span>';
  if(t.github_state==='closed_unknown')return '<span class="small-badge draft">Closed · unverified</span>';
  if(t.role_review)return '<span class="small-badge review">Review needed</span>';
  if(t.local_status==='unassigned'&&!t.role_review&&!t.role_author&&!t.role_assigned)return '<span class="small-badge local">No longer assigned</span>';
  if(t.was_review_requested&&!t.role_review&&!t.role_author&&!t.role_assigned)return '<span class="small-badge done">Review no longer requested</span>';
  if(t.is_draft)return '<span class="small-badge draft">Draft</span>';
  return '<span class="small-badge">Open</span>';
}
function taskRole(t){if(t.source==='manual')return '<span class="small-badge local">Personal</span>';let roles=[];if(t.role_review)roles.push('Reviewer');if(t.role_author)roles.push('Author');if(t.role_assigned)roles.push('Assignee');if(!roles.length&&t.was_review_requested)roles.push('Past reviewer');return `<span class="small-badge ${t.role_review?'review':''}">${esc(roles.join(' / ')||'GitHub')}</span>`}
function taskNumber(t){const m=t.url.match(/\/(?:pull|issues)\/(\d+)$/);return m?'#'+m[1]:''}
function getTask(id){return state.data.tasks.find(t=>t.id===id)}
function taskRow(t){const main=esc(short(t.title,135));const url=t.url&&/^https?:\/\//.test(t.url)?esc(t.url):'';const title=url?`<a class="task-title task-link" href="${url}" target="_blank" rel="noopener noreferrer" title="Open GitHub">${main}</a>`:`<span class="task-title">${main}</span>`;
  const due=t.due_date?`<div class="due-inline ${t.due_date<dateToday()&&!isFinished(t)?'overdue':''}">${t.due_date<dateToday()&&!isFinished(t)?'Overdue':'Due'} ${esc(t.due_date)}</div>`:'';
  const note=t.notes?' <span title="Has private notes">✎</span>':'';
  return `<tr data-id="${esc(t.id)}"><td>${title}<div class="task-repo"><span class="repo-box">${t.source==='manual'?'TASK':t.kind==='pr'?'PR':'ISSUE'}</span><span>${esc(t.repo||'Personal workspace')} ${esc(taskNumber(t))}</span>${note}</div>${due}</td><td>${taskRole(t)}</td><td>${taskBadge(t)}${isSnoozed(t)?' <span class="small-badge snoozed">Snoozed</span>':''}</td><td><span class="priority"><span class="priority-dot ${t.priority===1?'high':t.priority===3?'low':''}"></span>${priorityText(t.priority)}</span></td><td title="${esc(formatTime(t.updated_at))}">${relativeDate(t.updated_at)}</td><td><div class="row-actions"><button class="row-action" data-edit="${esc(t.id)}">Details</button>${t.source==='manual'&&!isFinished(t)?`<button class="row-action" data-complete="${esc(t.id)}">Done ✓</button>`:''}${t.role_review&&!isFinished(t)?`<button class="row-action" data-snooze="${esc(t.id)}">${isSnoozed(t)?'Unsnooze':'Snooze'}</button>`:''}</div></td></tr>`}
function renderTable(){if(['history','settings','notifications'].includes(state.view))return;const t=filteredTasks(),count=t.length;const titles={overview:['YOUR WORK QUEUE','Outstanding work'],reviews:['CODE REVIEW','Requested reviews'],prs:['PR TRACKER','Your pull requests'],issues:['ISSUE TRACKER','Issues'],tasks:['YOUR TO-DOS','Personal tasks'],completed:['ARCHIVE','Completed / closed work']};$('table-eyebrow').textContent=titles[state.view][0];$('table-heading').firstChild.textContent=titles[state.view][1]+' ';$('result-count').textContent=count;
  $('tasks-body').innerHTML=t.slice(0,state.limit).map(taskRow).join('');$('empty-state').classList.toggle('hidden',count!==0);$('table-footer').textContent=`Showing ${Math.min(count,state.limit)} of ${count} matching items`;
  $('show-more-btn').classList.toggle('hidden',count<=state.limit);
  $('empty-title').textContent=state.data.tasks.length?'No tasks match your filters':'Nothing here yet';$('empty-message').textContent=state.data.tasks.length?'Try another date, repository or status filter.':'Sync GitHub or create a personal task to get started.';
  document.querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>openTask(getTask(b.dataset.edit)));
  document.querySelectorAll('[data-complete]').forEach(b=>b.onclick=()=>patchTask(b.dataset.complete,{local_status:'done'},'Task completed'));
  document.querySelectorAll('[data-snooze]').forEach(b=>b.onclick=()=>{const t=getTask(b.dataset.snooze);const tomorrow=new Date();tomorrow.setDate(tomorrow.getDate()+1);patchTask(t.id,{snoozed_until:isSnoozed(t)?null:localDate(tomorrow)},isSnoozed(t)?'Review unsnoozed':'Review snoozed until tomorrow')});
}
function renderAttention(){const tasks=mainItems().filter(t=>!isFinished(t)&&!isSnoozed(t)).sort((a,b)=> (b.role_review-a.role_review)||((a.due_date||'9999').localeCompare(b.due_date||'9999'))||(a.priority-b.priority));const list=tasks.slice(0,3);$('attention-list').innerHTML=list.length?list.map(t=>`<div class="attention-item" data-attention="${esc(t.id)}"><div class="attention-icon ${t.due_date?'due':''}">${t.role_review?'◎':t.due_date?'◷':'⑂'}</div><div class="attention-main"><div class="attention-name">${esc(short(t.title,90))}</div><div class="attention-meta">${esc(t.repo||'Personal task')} · ${t.role_review?'Review requested':t.due_date?'Due '+t.due_date:priorityText(t.priority)+' priority'}</div></div><div class="attention-time">${relativeDate(t.updated_at)}</div></div>`).join(''):'<div class="no-attention">✓ No urgent work in your current scope.</div>';
  document.querySelectorAll('[data-attention]').forEach(el=>el.onclick=()=>openTask(getTask(el.dataset.attention)));
}
function renderChart(){const snapshots=state.data.snapshots.slice(-26);const totals=snapshots.map(x=>x.open_prs+x.reviews+x.issues+x.manual_open);const latest=totals.length?totals[totals.length-1]:0;$('trend-latest').textContent=totals.length?latest:'—';const max=Math.max(...totals,1);$('trends-chart').innerHTML=snapshots.length?snapshots.map((row,i)=>`<div class="bar-slot" style="height:${Math.max(4,totals[i]/max*98)}%" data-title="${esc(row.created_at.slice(0,10))}: ${totals[i]} active" role="img" aria-label="${totals[i]} active at ${row.created_at.slice(0,10)}"></div>`).join(''):'<div class="no-attention" style="align-self:center;width:100%">A progress chart appears after your first GitHub sync.</div>'}
function renderHistory(){const ev=state.data.events;if(!ev.length){$('events-list').innerHTML='<div class="no-attention">No activity recorded yet.</div>';return}const lookup=new Map(state.data.tasks.map(t=>[t.id,t]));$('events-list').innerHTML=ev.map(e=>{const t=lookup.get(e.task_id);return `<div class="event-list-item"><div class="event-dot">${e.type.includes('review')?'◎':e.type.includes('status')?'↗':e.type.includes('manual')?'☑':'◌'}</div><div class="event-body">${esc(e.message)}${t?.url?` <a href="${esc(t.url)}" target="_blank" rel="noopener noreferrer">↗ Open</a>`:''}<div class="event-meta">${esc(e.type.replaceAll('_',' '))} · ${formatTime(e.created_at)}</div></div></div>`}).join('')}
function renderSettings(){const data=state.data;
  if (!$('notification-settings-form').contains(document.activeElement)) {
    $('notifications-enabled').checked=data.settings.notifications_enabled;
    $('notifications-desktop').checked=data.settings.desktop_notifications;
    $('notification-interval').value=String(data.settings.notification_interval);
  }
  const mac=data.platform==='darwin';
  $('notification-background-install').disabled=!mac||data.notification_watcher;
  $('notification-background-remove').disabled=!mac||!data.notification_watcher;
  $('notification-browser-permission').classList.toggle('hidden',mac);
  $('notification-delivery-note').textContent=mac ? (data.notification_watcher?'Background checks enabled: alerts continue when Workdesk is closed, while you are logged in and awake.':'Desktop alerts work while the Workdesk server runs. Enable background checks to continue after closing it.') : 'Browser alerts require this page to stay open and browser permission. Automatic inbox checks run while the Workdesk server is running.';
$('settings-username').value=data.settings.github_username;$('settings-orgs').value=(data.settings.focus_orgs||[]).join(', ');$('settings-lookback').value=data.settings.lookback_days;
  $('reminder-time').value=data.reminder.time;$('reminder-status').textContent=data.reminder.installed?'Enabled: every day at '+data.reminder.time+' (local time)':'Not enabled. The webpage still supports manual sync.';
  $('reminder-install').disabled=data.platform!=='darwin';$('reminder-remove').disabled=data.platform!=='darwin'||!data.reminder.installed;
  $('database-path').textContent=data.data_dir+'/workdesk.sqlite3';$('theme-select').value=selectedTheme;$('auth-hint').textContent='GitHub account: '+(data.settings.github_username || (data.settings.last_synced_user ? data.settings.last_synced_user+' (auto-detected)' : 'auto-detect on sync'))+' · If authentication fails, run “gh auth login” in Terminal.';
}
function switchView(view){state.view=view;state.limit=30;render();window.scrollTo({top:0,behavior:'smooth'})}

function openTask(task=null){state.editing=task;const isGh=!!task&&task.source==='github';$('task-id').value=task?.id||'';$('modal-title').textContent=!task?'Create a new task':isGh?'Edit local tracking details':'Edit personal task';$('modal-eyebrow').textContent=isGh?'GITHUB ITEM · PRIVATE ANNOTATIONS':'PERSONAL TASK';
  $('task-title').value=task?.title||'';$('task-description').value=task?.description||'';$('task-status').value=task?.local_status||'todo';$('task-priority').value=task?.priority||2;$('task-due').value=task?.due_date||'';$('task-repo').value=task?.repo||'';$('task-url').value=task?.url||'';$('task-notes').value=task?.notes||'';
  for(const id of ['title-wrapper','description-wrapper','status-wrapper','repo-wrapper','url-wrapper'])$(id).classList.toggle('hidden',isGh);
  $('task-title').required=!isGh;$('task-delete').classList.toggle('hidden',!task||isGh);
  $('task-submit').textContent='Save details';$('modal-backdrop').classList.remove('hidden');
  (!isGh?$('task-title'):$('task-notes')).focus();
}
function closeTask(){$('modal-backdrop').classList.add('hidden');state.editing=null}
async function patchTask(id,payload,message){try{await request('/api/tasks/'+encodeURIComponent(id),{method:'PATCH',body:JSON.stringify(payload)});await refresh(true);toast(message)}catch(e){toast(e.message,true)}}
async function submitTask(e){e.preventDefault();const task=state.editing;const payload={priority:Number($('task-priority').value),due_date:$('task-due').value||null,notes:$('task-notes').value};if(!task||task.source==='manual')Object.assign(payload,{title:$('task-title').value.trim(),description:$('task-description').value,local_status:$('task-status').value,repo:$('task-repo').value.trim(),url:$('task-url').value.trim()});
  try{if(task)await request('/api/tasks/'+encodeURIComponent(task.id),{method:'PATCH',body:JSON.stringify(payload)});else await request('/api/tasks',{method:'POST',body:JSON.stringify(payload)});closeTask();await refresh(true);toast(task?'Task updated':'Task created and saved locally')}catch(err){toast(err.message,true)}}
async function startSync(){try{await request('/api/sync',{method:'POST',body:'{}'});state.data.sync.running=true;render();toast('Sync started. GitHub history is saved locally.');if(state.poll)clearInterval(state.poll);state.poll=setInterval(async()=>{await refresh(true);if(!state.data.sync.running){clearInterval(state.poll);state.poll=null;if(state.data.sync.error)toast(state.data.sync.error,true);else toast('GitHub sync complete')}} ,2500)}catch(e){toast(e.message,true)}}
async function saveSettings(e){e.preventDefault();const payload={github_username:$('settings-username').value.trim(),focus_orgs:$('settings-orgs').value.split(',').map(x=>x.trim()).filter(Boolean),lookback_days:Number($('settings-lookback').value)};try{await request('/api/settings',{method:'POST',body:JSON.stringify(payload)});await refresh(true);toast('Settings saved. Sync again to apply GitHub history settings.')}catch(err){toast(err.message,true)}}
async function reminder(action){try{await request('/api/reminder',{method:'POST',body:JSON.stringify({action,time:$('reminder-time').value})});await refresh(true);toast(action==='install'?'Daily GitHub sync and notification enabled':'Daily reminder disabled')}catch(e){toast(e.message,true)}}
const notificationReasons = {mention:'You were mentioned',team_mention:'Your team was mentioned',assign:'Assigned to you',review_requested:'Review requested',author:'Update on your work',comment:'Reply in a conversation',manual:'Subscribed conversation',state_change:'Status changed'};
function renderNotifications(){
  if(!notificationData)return;
  const data=notificationData;
  $('nav-notifications').textContent=data.unread;
  $('notification-count').textContent=data.unread;
  $('notification-bell').setAttribute('aria-label',`${data.unread} unread notifications${data.status.error ? ', checks need attention' : ''}`);
  $('notification-bell').classList.toggle('notification-error',!!data.status.error);
  document.title=(data.unread ? `(${data.unread}) ` : '')+'GitHub Workdesk';
  const status=!data.enabled?'Automatic checks are paused.':data.status.error?`Checks need attention: ${data.status.error}`:data.status.checked_at?`Watching @${data.username} · Last checked ${formatTime(data.status.checked_at)} · Checks about every ${data.interval/60} minute(s).`:'Waiting for the first GitHub check…';
  $('notification-status').textContent=status;
  $('notification-settings-status').textContent=status;
  $('notifications-read-all').disabled=!data.items.some(n=>n.unread);
  $('notifications-list').innerHTML=data.items.length?data.items.map(n=>`<article class="notification-item ${n.unread?'unread':''}"><div class="notification-content"><div class="notification-reason">${esc(notificationReasons[n.reason]||n.reason.replaceAll('_',' '))} · ${esc(n.kind)}</div><a href="${esc(n.url)}" target="_blank" rel="noopener noreferrer">${esc(n.title)}</a><div class="event-meta">${esc(n.repo)} · ${esc(formatTime(n.updated_at))}</div></div>${n.unread?`<button class="button button-quiet" data-notification-read="${esc(n.thread_id)}">Mark read</button>`:'<span class="muted-small">Read</span>'}</article>`).join(''):'<div class="no-attention">No notifications yet. New activity involving you will appear here.</div>';
  $('notifications-list').querySelectorAll('[data-notification-read]').forEach(button=>button.onclick=()=>markNotificationsRead(data.items.filter(n=>n.thread_id===button.dataset.notificationRead)));
}
async function refreshNotifications(){
  if(notificationRequestRunning)return;
  notificationRequestRunning=true;
  try{
    const next=await request('/api/notifications');
    const fresh=notificationData&&next.username===notificationData.username?next.items.filter(n=>n.alert_seq>notificationData.latest):[];
    notificationData=next;renderNotifications();
    if(next.enabled&&fresh.length){
      toast(`${fresh.length} new GitHub update${fresh.length===1?'':'s'}: ${short(fresh[0].title,100)}`);
      if(next.desktop&&state.data?.platform!=='darwin'&&'Notification' in window&&Notification.permission==='granted'){
        try {
          const alert=new Notification('GitHub Workdesk',{body:fresh.length===1?`${fresh[0].repo}: ${fresh[0].title}`:`${fresh.length} new updates in your notification inbox.`,tag:'workdesk-'+next.latest});
          alert.onclick=()=>{window.focus();switchView('notifications');alert.close()};
        } catch (_) { /* Some browsers require a service worker; the inbox and toast still work. */ }
      }
    }
  }catch(e){$('notification-status').textContent='Cannot refresh notifications: '+e.message;$('notification-settings-status').textContent='Cannot refresh notifications: '+e.message}
  finally{notificationRequestRunning=false}
}
async function markNotificationsRead(items){
  try{await request('/api/notifications/read',{method:'POST',body:JSON.stringify({username:notificationData.username,items:items.map(n=>({thread_id:n.thread_id,revision:n.revision}))})});await refreshNotifications()}
  catch(e){toast(e.message,true)}
}
async function saveNotificationSettings(event){
  event.preventDefault();
  try{await request('/api/settings',{method:'POST',body:JSON.stringify({notifications_enabled:$('notifications-enabled').checked,desktop_notifications:$('notifications-desktop').checked,notification_interval:Number($('notification-interval').value)})});await refresh(true);await refreshNotifications();toast('Notification settings saved')}
  catch(e){toast(e.message,true)}
}
async function allowBrowserNotifications(){
  if(!('Notification' in window)){toast('This browser does not support desktop notifications. Your inbox still works.',true);return}
  try{const permission=await Notification.requestPermission();toast(permission==='granted'?'Browser alerts enabled. Keep this page open.':'Allow notifications for this site in your browser settings to receive desktop alerts.',permission!=='granted')}
  catch(e){toast(e.message,true)}
}
async function notificationWatcher(action){
  try{await request('/api/notification-watcher',{method:'POST',body:JSON.stringify({action})});await refresh(true);await refreshNotifications();toast(action==='install'?'Background notification checks enabled':'Background checks removed; checks continue while Workdesk runs')}
  catch(e){toast(e.message,true)}
}

function bind(){document.querySelectorAll('.nav-item[data-view]').forEach(e=>e.onclick=()=>switchView(e.dataset.view));document.querySelectorAll('[data-jump]').forEach(e=>e.onclick=()=>switchView(e.dataset.jump));$('sync-btn').onclick=startSync;$('refresh-btn').onclick=()=>refresh();$('new-task-btn').onclick=()=>openTask();$('empty-new').onclick=()=>openTask();$('show-reviews').onclick=()=>switchView('reviews');$('show-more-btn').onclick=()=>{state.limit+=30;renderTable()};$('export-csv').onclick=()=>{window.location.href='/api/export.csv'};
  $('modal-close').onclick=closeTask;$('task-cancel').onclick=closeTask;$('modal-backdrop').onclick=e=>{if(e.target===$('modal-backdrop'))closeTask()};document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!$('modal-backdrop').classList.contains('hidden'))closeTask()});$('task-form').onsubmit=submitTask;
  $('task-delete').onclick=async()=>{const task=state.editing;if(!task||task.source!=='manual')return;if(!confirm('Delete this manual task? This cannot be undone.'))return;try{await request('/api/tasks/'+encodeURIComponent(task.id),{method:'DELETE',body:'{}'});closeTask();await refresh(true);toast('Task deleted')}catch(err){toast(err.message,true)}};
  $('search-input').oninput=e=>{state.filter.query=e.target.value;state.limit=30;renderTable()};
  $('period-select').onchange=e=>{state.filter.period=e.target.value;$('custom-dates').classList.toggle('hidden',e.target.value!=='custom');state.limit=30;renderTable()};
  $('date-field').onchange=e=>{state.filter.dateField=e.target.value;renderTable()};
  $('repo-select').onchange=e=>{state.filter.repo=e.target.value;state.limit=30;render()};
  $('status-select').onchange=e=>{state.filter.status=e.target.value;state.limit=30;renderTable()};
  $('date-from').onchange=e=>{state.filter.from=e.target.value;renderTable()};
  $('date-to').onchange=e=>{state.filter.to=e.target.value;renderTable()};
  $('notification-bell').onclick=()=>switchView('notifications');
  $('notifications-read-all').onclick=()=>markNotificationsRead(notificationData?.items.filter(n=>n.unread)||[]);
  $('notification-settings-form').onsubmit=saveNotificationSettings;
  $('notification-browser-permission').onclick=allowBrowserNotifications;
  $('notification-background-install').onclick=()=>notificationWatcher('install');
  $('notification-background-remove').onclick=()=>notificationWatcher('remove');
  $('theme-select').onchange=e=>applyTheme(e.target.value);$('settings-form').onsubmit=saveSettings;$('reminder-install').onclick=()=>reminder('install');$('reminder-remove').onclick=()=>reminder('remove');
}

bind();refresh(true);refreshNotifications();
setInterval(refreshNotifications,5000);
// Periodic refresh sees changes made by the independent macOS daily sync job.
setInterval(()=>{if($('modal-backdrop').classList.contains('hidden')&&!state.data?.sync.running)refresh(true)},60000);
