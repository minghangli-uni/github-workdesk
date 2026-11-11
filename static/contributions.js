'use strict';
const contributionView = {tab:'active',repo:'all',role:'all',period:'all',query:'',discussion:false,sort:'activity',limit:30,expanded:new Set(),drafts:new Map(),account:null};
const contributionRoles = {author:'PR author',coauthor:'Co-author',code:'Code contributor',committer:'Committer',reviewer:'Reviewer',commenter:'Discussion'};
function contributionItems(){return state.data?.contributions?.items||[]}
function contributionActive(item){return item.kind==='pr'&&item.state==='open'}
function contributionVisible(item){return contributionView.discussion||item.roles.some(role=>role!=='commenter')}
function filteredContributions(){
  const f=contributionView;
  return contributionItems().filter(item=>contributionVisible(item)&&
    (f.tab==='active'?contributionActive(item):!contributionActive(item))&&
    (f.repo==='all'||item.repo===f.repo)&&(f.role==='all'||item.roles.includes(f.role))&&
    (f.period==='all'||new Date(item.last_contributed_at).getTime()>=afterDays(f.period))&&
    (!f.query||`${item.title} ${item.repo} ${item.notes} ${item.evidence.map(e=>e.title).join(' ')}`.toLowerCase().includes(f.query.toLowerCase()))
  ).sort((a,b)=>new Date(f.sort==='mine'?b.last_contributed_at:b.updated_at)-new Date(f.sort==='mine'?a.last_contributed_at:a.updated_at));
}
function contributionRow(item){
  const commits=item.evidence.filter(e=>e.type==='commit').length;
  const reviews=item.evidence.filter(e=>e.type==='review').length;
  const evidence=[...item.evidence].sort((a,b)=>new Date(b.date)-new Date(a.date));
  const notes=contributionView.drafts.has(item.id)?contributionView.drafts.get(item.id):item.notes;
  return `<article class="contribution-card"><div class="contribution-heading"><div><div class="task-repo">${esc(item.repo)} · ${item.kind==='pr'?'PR #'+item.number:'Commit'}</div><a class="contribution-title" href="${esc(item.url)}" target="_blank" rel="noopener noreferrer">${esc(item.title)}</a></div><span class="small-badge ${contributionActive(item)?'review':'done'}">${esc(item.status)}</span></div>
    <div class="contribution-badges">${item.roles.filter(role=>role!=='commenter'||contributionView.discussion).map(role=>`<span class="small-badge">${esc(contributionRoles[role]||role)}</span>`).join('')}<span class="muted-small">${[commits?`${commits} commit${commits===1?'':'s'}`:'',reviews?`${reviews} review${reviews===1?'':'s'}`:''].filter(Boolean).join(' · ')}</span></div>
    <div class="contribution-dates"><span title="${esc(formatTime(item.last_contributed_at))}">Your last contribution: <strong>${relativeDate(item.last_contributed_at)}</strong></span><span title="${esc(formatTime(item.updated_at))}">Latest activity: <strong>${relativeDate(item.updated_at)}</strong></span></div>
    <div class="button-row"><a class="button button-quiet" href="${esc(item.url)}" target="_blank" rel="noopener noreferrer">Open ${item.kind==='pr'?'PR':'commit'} ↗</a>${item.kind==='pr'?`<button class="button button-outline" data-contribution-follow="${esc(item.id)}" aria-pressed="${item.followed}">${item.followed?'Following updates ✓':'Follow updates'}</button>`:''}</div>
    <details data-contribution-details="${esc(item.id)}" ${contributionView.expanded.has(item.id)?'open':''}><summary>Your contribution · ${evidence.length} record${evidence.length===1?'':'s'}${item.notes?' · Private notes':''}</summary>${!item.details_complete?'<p class="settings-note">Some details could not be checked. Previously verified evidence is retained until a complete sync.</p>':''}<ul class="contribution-evidence">${evidence.map(e=>`<li><a href="${esc(e.url)}" target="_blank" rel="noopener noreferrer">${esc(e.type==='commit'?e.sha.slice(0,7)+' · '+e.title:e.title)}</a><span>${e.historical?'Previously recorded · ':''}${esc(e.roles.map(role=>contributionRoles[role]||role).join(' / '))} · ${esc(formatTime(e.date))}</span></li>`).join('')}</ul><label class="contribution-notes">Private notes<textarea data-contribution-notes="${esc(item.id)}" maxlength="10000" rows="3" placeholder="Your context and next steps; saved only in Workdesk">${esc(notes)}</textarea></label><button class="button button-primary" data-contribution-save="${esc(item.id)}">Save notes</button></details></article>`;
}
function renderContributions(){
  const data=state.data?.contributions;
  if(!data)return;
  if(contributionView.account!==data.username){contributionView.account=data.username;contributionView.drafts.clear();contributionView.expanded.clear()}
  const visible=data.items.filter(contributionVisible);
  const active=visible.filter(contributionActive).length;
  $('nav-contributions').textContent=active;
  $('contribution-active-count').textContent=active;
  $('contribution-history-count').textContent=visible.length-active;
  $('contribution-active').setAttribute('aria-pressed',String(contributionView.tab==='active'));
  $('contribution-history').setAttribute('aria-pressed',String(contributionView.tab==='history'));
  if(state.view!=='contributions')return;
  const run=data.sync;
  $('contribution-sync-status').textContent=state.data.sync.running?'Syncing contribution evidence…':run?`Last checked ${formatTime(run.checked_at)} · ${run.repositories} repositories discovered`:'Select Sync GitHub to discover your contributions.';
  const errors=run?.errors||[], warnings=run?.warnings||[], summary=diagnosticSummary(errors,warnings);
  $('contribution-coverage-summary').textContent='Discovery coverage'+(summary?' · '+summary:'');
  $('contribution-coverage').innerHTML=summary?diagnosticsHTML(errors,warnings):'Search includes your PRs, submitted reviews, commit authorship and co-author credits. Open PRs are also scanned in your discovered and extra repositories.';
  $('contribution-coverage').classList.toggle('contribution-warning',!!errors.length);
  const select=$('contribution-repo');
  select.innerHTML='<option value="all">All repositories</option>'+[...new Set(data.items.map(i=>i.repo))].sort().map(repo=>`<option value="${esc(repo)}">${esc(repo)}</option>`).join('');
  select.value=contributionView.repo;
  if(select.value!==contributionView.repo){contributionView.repo='all';select.value='all'}
  const items=filteredContributions();
  $('contribution-result-count').textContent=`Showing ${Math.min(items.length,contributionView.limit)} of ${items.length} matching contributions`;
  $('contribution-more').classList.toggle('hidden',items.length<=contributionView.limit);
  // Background refresh must not replace an editor while the user is typing.
  if(document.activeElement?.matches('[data-contribution-notes]'))return;
  $('contributions-list').innerHTML=items.length?items.slice(0,contributionView.limit).map(contributionRow).join(''):`<div class="empty-state"><div class="empty-glyph">⑂</div><h3>${data.items.length?'No contributions match these filters':'Your contributions will appear here'}</h3><p>${data.items.length?'Try History, another repository, or a different contribution type.':'Sync GitHub to find your authored PRs, commits, co-author credits and reviews.'}</p><p>For work in another repository, add it under Settings → Contribution discovery.</p></div>`;
  document.querySelectorAll('[data-contribution-details]').forEach(details=>details.ontoggle=()=>{if(details.open)contributionView.expanded.add(details.dataset.contributionDetails);else contributionView.expanded.delete(details.dataset.contributionDetails)});
}
async function updateContribution(id,changes){
  try{await request('/api/contributions',{method:'POST',body:JSON.stringify({username:state.data.contributions.username,id,...changes})});if('notes' in changes)contributionView.drafts.delete(id);await refresh(true);toast('notes' in changes?'Private notes saved':changes.followed?'Following PR updates through your notification inbox':'Stopped following PR updates')}
  catch(e){toast(e.message,true)}
}
function bindContributions(){
  for(const tab of ['active','history'])$('contribution-'+tab).onclick=()=>{contributionView.tab=tab;contributionView.limit=30;renderContributions()};
  for(const [id,field] of [['contribution-repo','repo'],['contribution-role','role'],['contribution-period','period'],['contribution-sort','sort']])$(id).onchange=event=>{contributionView[field]=event.target.value;if(field==='role'&&event.target.value==='commenter'){contributionView.discussion=true;$('contribution-discussion').checked=true}contributionView.limit=30;renderContributions()};
  $('contribution-search').oninput=event=>{contributionView.query=event.target.value;contributionView.limit=30;renderContributions()};
  $('contribution-discussion').onchange=event=>{contributionView.discussion=event.target.checked;contributionView.limit=30;renderContributions()};
  $('contribution-more').onclick=()=>{contributionView.limit+=30;renderContributions()};
  $('contribution-settings').onclick=()=>switchView('settings');
  $('contributions-list').oninput=event=>{const id=event.target.dataset.contributionNotes;if(id)contributionView.drafts.set(id,event.target.value)};
  $('contributions-list').onclick=event=>{
    const follow=event.target.closest('[data-contribution-follow]');
    const save=event.target.closest('[data-contribution-save]');
    if(follow){const item=contributionItems().find(i=>i.id===follow.dataset.contributionFollow);updateContribution(item.id,{followed:!item.followed})}
    if(save){const item=contributionItems().find(i=>i.id===save.dataset.contributionSave);updateContribution(item.id,{notes:contributionView.drafts.get(item.id)??item.notes})}
  };
  $('contribution-settings-form').onsubmit=async event=>{
    event.preventDefault();
    const split=id=>$(id).value.split(/[\n,]+/).map(v=>v.trim()).filter(Boolean);
    try{await request('/api/settings',{method:'POST',body:JSON.stringify({contribution_repos:split('settings-contribution-repos'),contribution_emails:split('settings-contribution-emails')})});await refresh(true);toast('Contribution settings saved. Sync GitHub to apply them.')}
    catch(e){toast(e.message,true)}
  };
}
