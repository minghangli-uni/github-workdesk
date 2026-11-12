const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.join(__dirname, '..');
const read = name => fs.readFileSync(path.join(root, 'static', name), 'utf8');

function task(id, extra = {}) {
  return {id, source:'github', kind:'pr', repo:'example/project', url:`https://github.com/example/project/pull/${id}`,
    title:'Example work', github_state:'open', role_author:true, role_review:false, role_assigned:false,
    was_review_requested:false, is_draft:false, local_status:'todo', priority:2, labels:[], notes:'', description:'', updated_at:'2026-11-01', ...extra};
}
function harness(tasks = [], items = [], settings = {}) {
  const elements = new Map(), timers = new Map(); let timerId = 0;
  const el = id => {
    if (!elements.has(id)) {
      const classes = new Set();
      elements.set(id, {value:'', textContent:'', innerHTML:'', checked:false, firstChild:{textContent:''},
        classList:{toggle:(key, on)=>on?classes.add(key):classes.delete(key),add:key=>classes.add(key),contains:key=>classes.has(key)},
        setAttribute(){},querySelectorAll:()=>[],contains:()=>false});
    }
    return elements.get(id);
  };
  const context = vm.createContext({document:{getElementById:el,documentElement:{dataset:{}},querySelectorAll:()=>[]},
    window:{matchMedia:()=>({matches:false,addEventListener(){}}),scrollTo(){}},
    localStorage:{getItem:()=>null,setItem(){}}, setTimeout:fn=>{timers.set(++timerId,fn);return timerId;}, clearTimeout:id=>timers.delete(id),
    input:{tasks,contributions:{items,username:'owner'},settings:{focus_orgs:[],...settings},repositories:[...new Set(tasks.map(t=>t.repo))]}});
  for (const name of ['work-items.js','view-filters.js','contributions.js']) vm.runInContext(read(name), context);
  vm.runInContext(read('app.js').split('\nconst initialView=')[0], context);
  vm.runInContext('state.data=input; this.state=state; this.contributionView=contributionView; restoreViewFilters(input); indexWork(input);', context);
  context.render = () => {};
  context.renderContributions = () => {};
  context.request = async () => ({});
  return {context, el, timers};
}

test('roles join verified contributions by repository and PR without inferring a submitted review', () => {
  const a = task('1', {role_author:false,role_assigned:true,was_review_requested:true});
  const b = task('2', {role_author:false,was_review_requested:true});
  const h = harness([a,b], [{kind:'pr',repo:'EXAMPLE/PROJECT',number:1,roles:['coauthor','code','reviewer']}]);
  assert.equal(h.context.taskRoles(a).join(','), 'assigned,reviewer,coauthor,code');
  assert.equal(h.context.taskRoles(b).join(','), 'review_previous');
  assert.doesNotMatch(h.context.taskRole(b), />Reviewed</);
  assert.equal(h.context.taskRoles({...a,repo:'different/project'}).join(','), 'assigned,review_previous');
  const mismatch = harness([a], [{kind:'pr',repo:a.repo,number:1,roles:['reviewer']}], {last_synced_user:'another-user'});
  assert.equal(mismatch.context.taskRoles(a).join(','), 'assigned,review_previous');
});

test('a requested review and a previously submitted review can both be shown', () => {
  const h = harness();
  const roles = h.context.workRoles(task('1',{role_review:true}), ['reviewer']);
  assert.equal(roles.join(','), 'author,review_requested,reviewer');
  const html = h.context.roleBadges(roles);
  assert.match(html, />Review requested</); assert.match(html, />Reviewed</);
  assert.equal(h.context.workRoles(task('1',{source:'manual'}), ['coauthor']).join(','), 'personal');
});

test('PR outcomes, issues and unverifiable closure remain distinct, even with stale draft flags', () => {
  const h = harness();
  const html = state => h.context.taskBadge(task('1',{github_state:state,is_draft:true}));
  assert.match(html('merged'), /status-merged/);
  assert.match(html('closed'), /status-closed/);
  assert.match(html('closed'), /Closed without merge/);
  assert.match(html('closed_unknown'), /Closed · unverified/);
  assert.doesNotMatch(html('closed_unknown'), /status-merged|status-closed/);
  const issue = h.context.taskBadge(task('1',{kind:'issue',github_state:'closed'}));
  assert.match(issue, />Closed</); assert.doesNotMatch(issue, /without merge/);
  const draft = h.context.taskBadge(task('1',{is_draft:true,role_review:true}));
  assert.match(draft, /Draft/); assert.match(draft, /Review needed/);
});

test('table and contributions use the same lifecycle badge while preserving review context', () => {
  const h = harness();
  for (const status of ['merged','closed','closed_unknown']) {
    assert.equal(h.context.taskBadge(task('1',{github_state:status})), h.context.contributionStatusBadges({kind:'pr',state:status}));
  }
  const html = h.context.contributionStatusBadges({kind:'pr',state:'open',status:'Changes requested'});
  assert.match(html, />Open</); assert.match(html, /Changes requested/);
  assert.match(h.context.contributionStatusBadges({kind:'commit',state:'committed'}), /Committed/);
});

test('status and role filters match independent facts and can show historical PRs from Overview', () => {
  const tasks = [task('1',{github_state:'merged'}), task('2',{github_state:'closed'}), task('3',{kind:'issue',github_state:'closed'}), task('4',{is_draft:true}), task('5'), task('6',{github_state:'closed_unknown'})];
  const h = harness(tasks, [{kind:'pr',repo:'example/project',number:1,roles:['coauthor']}]);
  for (const [status, expected] of [['merged','1'],['closed_unmerged','2'],['closed_issue','3'],['draft','4'],['open','5'],['closed_unknown','6']]) {
    h.context.state.filter.status = status;
    assert.equal(h.context.filteredTasks().map(t=>t.id).join(','), expected);
  }
  h.context.state.filter.status='any'; h.context.state.filter.role='coauthor';
  assert.equal(h.context.filteredTasks().map(t=>t.id).join(','), '1');
});

test('saved filters restore separately by view and survive refresh without overwriting typing', () => {
  const saved = {tables:{prs:{status:'merged',role:'coauthor'},overview:{status:'outstanding'}},contributions:{tab:'history',status:'closed_unmerged',role:'code'}};
  const h = harness([],[],{view_filters:saved});
  h.context.switchView('prs');
  assert.equal(h.context.state.filter.status, 'merged');
  assert.equal(h.context.state.filter.role, 'coauthor');
  h.context.state.filter.query = 'Unfinished search';
  h.context.restoreViewFilters({settings:{view_filters:{}}});
  assert.equal(h.context.state.filter.query, 'Unfinished search');
  h.context.switchView('overview');
  assert.equal(h.context.state.filter.status, 'outstanding');
  h.context.switchView('prs');
  assert.equal(h.context.state.filter.query, 'Unfinished search');
  assert.equal(h.context.contributionView.status, 'closed_unmerged');
});

test('rapid changes during a filter save are queued and the newest selection persists', async () => {
  const h = harness(); const requests=[]; let finish;
  h.context.request = (url, options) => {requests.push(JSON.parse(options.body));return new Promise(resolve=>finish=resolve);};
  h.context.state.filter.status='merged';h.context.queueFilterSave();
  const first = h.context.flushFilterSave();
  h.context.state.filter.status='closed_unmerged';h.context.queueFilterSave();
  finish({});await first;
  const next = h.context.flushFilterSave();finish({});await next;
  assert.equal(requests.length, 2);
  assert.equal(requests[0].view_filters.tables.overview.status, 'merged');
  assert.equal(requests[1].view_filters.tables.overview.status, 'closed_unmerged');
  assert.equal(h.context.state.data.settings.view_filters.tables.overview.status, 'closed_unmerged');
});

test('failed saving reports failure without losing filters and retry succeeds', async () => {
  const h = harness();h.context.state.filter.role='reviewer';
  h.context.request = async()=>{throw Error('Offline');};
  h.context.queueFilterSave();await h.context.flushFilterSave();
  assert.equal(h.context.state.filter.role, 'reviewer');
  assert.match(h.el('filters-save-status').textContent, /could not be saved/);
  assert.equal(h.el('filters-save-retry').classList.contains('hidden'), false);
  h.context.request = async()=>({});h.context.queueFilterSave();await h.context.flushFilterSave();
  assert.equal(h.context.state.data.settings.view_filters.tables.overview.role, 'reviewer');
});

test('choosing contribution status switches to the matching tab and resets do not clear notes', () => {
  const h = harness();h.context.bindContributions();h.context.bindViewFilters();
  h.context.contributionView.drafts.set('pr:1','Private unsaved note');
  h.el('contribution-status').onchange({target:{value:'merged'}});
  assert.equal(h.context.contributionView.tab,'history');
  h.el('contribution-active').onclick();
  assert.equal(h.context.contributionView.status,'any');
  h.el('contribution-filters-reset').onclick();
  assert.equal(h.context.contributionView.drafts.get('pr:1'),'Private unsaved note');
});

test('all status colour pairs meet 4.5:1 text contrast in light and dark modes', () => {
  const css = read('styles.css');
  const matches = [...css.matchAll(/--status-(\w+)-(bg|fg):(#\w{6})/g)];
  assert.equal(matches.length,24);
  function luminance(hex) {
    const rgb=[1,3,5].map(i=>parseInt(hex.slice(i,i+2),16)/255).map(v=>v<=0.04045?v/12.92:((v+0.055)/1.055)**2.4);
    return rgb[0]*.2126+rgb[1]*.7152+rgb[2]*.0722;
  }
  for (let i=0;i<matches.length;i+=2) {
    const a=luminance(matches[i][3]),b=luminance(matches[i+1][3]);
    const ratio=(Math.max(a,b)+.05)/(Math.min(a,b)+.05);
    assert.ok(ratio>=4.5, `${matches[i][1]} has ${ratio.toFixed(2)}:1 contrast`);
  }
});

test('new controls exist and shared presentation loads before its callers', () => {
  const html=read('index.html');
  for (const name of ['view-filters.js','work-items.js']) {
    for (const match of read(name).matchAll(/\$\('([^']+)'\)/g)) assert.ok(html.includes(`id="${match[1]}"`));
    assert.ok(html.indexOf(`/static/${name}`)<html.indexOf('/static/contributions.js'));
  }
});
