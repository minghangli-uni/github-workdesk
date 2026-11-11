'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const root=path.resolve(__dirname,'..');
const source=fs.readFileSync(path.join(root,'static/sync-status.js'),'utf8');
const sandbox={esc:v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))};
vm.createContext(sandbox);
vm.runInContext(source+'\nthis.api={syncPresentation,diagnosticSummary,diagnosticsHTML,syncBannerHTML,renderSyncNotice};',sandbox);
const {syncPresentation,diagnosticsHTML,syncBannerHTML}=sandbox.api;
const warning='Open-PR scans limited to 30 repositories.';
test('coverage notes use an informational connected state without a retry or error headline',()=>{
  const run={id:1,status:'limited',errors:[],warnings:[warning]};
  const view=syncPresentation(run), html=syncBannerHTML(run);
  assert.equal(view.title,'Synced with limited contribution coverage');
  assert.equal(view.tone,'info');assert.equal(view.signal,'online');
  assert.ok(html.includes('Contribution coverage notes (1)'));
  assert.ok(!html.includes('Requests to retry'));
  assert.ok(!html.includes('data-sync-retry'));
});
test('mixed outcomes show every failure and coverage note in separate groups',()=>{
  const errors=['Review lookup timed out','Authentication failed','Third failure'];
  const warnings=[warning,'Commit links limited to 100','PR evidence limited to 200','Optional email access unavailable'];
  const run={id:2,status:'partial',errors,warnings};
  const html=syncBannerHTML(run),view=syncPresentation(run);
  assert.equal(view.title,'Sync completed with 3 request failures');
  assert.equal(view.tone,'warning');assert.equal(view.signal,'warning');
  for(const message of [...errors,...warnings])assert.ok(html.includes(message));
  assert.ok(html.indexOf('Requests to retry (3)')<html.indexOf('Contribution coverage notes (4)'));
  assert.ok(html.includes('data-sync-retry'));
  assert.ok(html.includes('data-run-id="2" open'));
});
test('failed sync never becomes a successful coverage notice',()=>{
  assert.equal(syncPresentation({status:'failed',errors:['Bad credentials'],warnings:[warning]}).title,'GitHub sync failed');
  assert.equal(syncPresentation({status:'failed',errors:[],warnings:[warning]}).tone,'error');
});
test('clean successful sync clears the notice and unknown partial results remain visible',()=>{
  assert.equal(syncBannerHTML({status:'success',errors:[],warnings:[]}),'');
  assert.equal(syncPresentation({status:'partial',errors:[],warnings:[]}).title,'Some data could not be refreshed');
});
test('diagnostics escape remote messages and support singular labels',()=>{
  const run={id:'"><img>',status:'partial',errors:['<script>alert(1)</script>'],warnings:['<img onerror=bad()>']};
  const html=syncBannerHTML(run);
  assert.equal(syncPresentation(run).title,'Sync completed with 1 request failure');
  assert.ok(!html.includes('<script>'));assert.ok(!html.includes('<img'));
  assert.ok(html.includes('&lt;script&gt;'));
  assert.equal(sandbox.api.diagnosticSummary(['failure'],['note']),'1 request failure · 1 coverage note');
});
test('report expansion survives refresh; retry action is disabled during an active sync',()=>{
  let details={dataset:{runId:'7'},open:false};
  const retry={};let retries=0;
  sandbox.document={getElementById:()=>details,querySelector:()=>retry};
  sandbox.banner=()=>{details={dataset:{runId:'7'},open:true}};
  sandbox.state={data:{sync:{running:true}}};sandbox.startSync=()=>retries++;
  sandbox.api.renderSyncNotice({id:7,status:'partial',errors:['Timeout'],warnings:[]});
  assert.equal(details.open,false);assert.equal(retry.disabled,true);
  sandbox.state.data.sync.running=false;
  sandbox.api.renderSyncNotice({id:7,status:'partial',errors:['Timeout'],warnings:[]});
  assert.equal(retry.disabled,false);retry.onclick();assert.equal(retries,1);
});
test('the main page and contribution view share the complete report renderer',()=>{
  const html=fs.readFileSync(path.join(root,'static/index.html'),'utf8');
  const main=fs.readFileSync(path.join(root,'static/app.js'),'utf8');
  const contributions=fs.readFileSync(path.join(root,'static/contributions.js'),'utf8');
  assert.ok(html.indexOf('/static/sync-status.js')<html.indexOf('/static/contributions.js'));
  assert.ok(main.includes('renderSyncNotice(last)'));
  assert.ok(!main.includes('errors.slice(0,2)'));
  assert.ok(contributions.includes('diagnosticsHTML(errors,warnings)'));
});
