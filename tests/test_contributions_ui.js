'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const root=path.resolve(__dirname,'..');
const source=fs.readFileSync(path.join(root,'static/contributions.js'),'utf8');
function context(items){
  const sandbox={state:{data:{contributions:{items}}},esc:v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),relativeDate:v=>v,formatTime:v=>v,afterDays:days=>Date.parse('2026-11-10T00:00:00Z')-days*86400000};
  vm.createContext(sandbox);
  vm.runInContext(source+'\nthis.api={contributionView,filteredContributions,contributionRow};',sandbox);
  return sandbox.api;
}
function item(id,changes={}){return {id,kind:'pr',repo:'example/shared',number:1,title:'Shared work',url:'https://github.com/example/shared/pull/1',state:'open',status:'Awaiting review',roles:['code'],updated_at:'2026-11-09T00:00:00Z',last_contributed_at:'2026-10-01T00:00:00Z',evidence:[],notes:'',details_complete:true,...changes}}
test('active and history split by PR state, including standalone commits',()=>{
  const api=context([item('active'),item('merged',{state:'merged'}),item('commit',{kind:'commit',state:'committed'}),item('discussion',{roles:['commenter']})]);
  assert.equal(api.filteredContributions().map(i=>i.id).join(','),'active');
  api.contributionView.discussion=true;
  assert.equal(api.filteredContributions().length,2);
  api.contributionView.tab='history';
  assert.equal(api.filteredContributions().length,2);
});
test('repository, role and personal contribution date filter independently of latest activity',()=>{
  const api=context([item('old'),item('recent',{roles:['coauthor'],last_contributed_at:'2026-11-08T00:00:00Z'})]);
  api.contributionView.period='7';assert.equal(api.filteredContributions()[0].id,'recent');
  api.contributionView.role='reviewer';assert.equal(api.filteredContributions().length,0);
  api.contributionView.role='all';api.contributionView.repo='other/repo';assert.equal(api.filteredContributions().length,0);
});
test('search includes evidence and notes; sorting supports your own contribution date',()=>{
  const api=context([item('first',{notes:'Release context'}),item('second',{last_contributed_at:'2026-11-01T00:00:00Z',updated_at:'2026-11-02T00:00:00Z'})]);
  assert.equal(api.filteredContributions()[0].id,'first');
  api.contributionView.sort='mine';assert.equal(api.filteredContributions()[0].id,'second');
  api.contributionView.query='release';assert.equal(api.filteredContributions()[0].id,'first');
});
test('evidence, titles and draft notes are escaped and retained in rendering',()=>{
  const api=context([]);const value=item('safe',{title:'<img onerror=alert(1)>',notes:'Old notes',evidence:[{id:'review:1',type:'review',roles:['reviewer'],title:'<script>bad</script>',url:'https://github.com/example/shared/pull/1#pullrequestreview-1',date:'2026-11-01',historical:true}]});
  api.contributionView.drafts.set('safe','</textarea><script>draft</script>');
  api.contributionView.expanded.add('safe');
  const html=api.contributionRow(value);
  assert.ok(!html.includes('<script>'));
  assert.ok(html.includes('&lt;img'));
  assert.ok(html.includes('&lt;/textarea&gt;'));
  assert.ok(html.includes('Previously recorded'));
  assert.ok(html.includes(' open>'));
});
test('all fixed DOM bindings exist and contribution helpers load before app startup',()=>{
  const html=fs.readFileSync(path.join(root,'static/index.html'),'utf8');
  const main=fs.readFileSync(path.join(root,'static/app.js'),'utf8');
  for(const match of (source+'\n'+main).matchAll(/\$\('([^']+)'\)/g)){
    if(match[1].endsWith('-'))continue;
    assert.ok(html.includes(`id="${match[1]}"`),`Missing DOM element: ${match[1]}`);
  }
  assert.ok(html.indexOf('/static/contributions.js')<html.indexOf('/static/app.js'));
  assert.ok(main.includes('bindContributions();'));
  assert.ok(main.includes('renderContributions();'));
});
