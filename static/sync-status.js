'use strict';

function syncPresentation(run) {
  const errors=run?.errors||[], warnings=run?.warnings||[];
  if(!run)return {connection:'Local workspace',signal:'',tone:'info',title:'',description:''};
  if(run.status==='failed')return {connection:'Sync needs attention',signal:'error',tone:'error',title:'GitHub sync failed',description:'Saved work is preserved. Check the failure details before trying again.'};
  if(errors.length||run.status==='partial')return {connection:'Sync needs attention',signal:'warning',tone:'warning',title:errors.length?`Sync completed with ${errors.length} request failure${errors.length===1?'':'s'}`:'Some data could not be refreshed',description:'Saved work is preserved. Failed requests and contribution coverage notes are listed separately below.'};
  if(warnings.length||run.status==='limited')return {connection:'GitHub connected · limited coverage',signal:'online',tone:'info',title:'Synced with limited contribution coverage',description:'Your PRs, reviews and issues synced. Some contributions may be missing from this scan.'};
  return {connection:'GitHub connected',signal:'online',tone:'info',title:'',description:''};
}

function diagnosticSummary(errors=[],warnings=[]) {
  return [errors.length?`${errors.length} request failure${errors.length===1?'':'s'}`:'',warnings.length?`${warnings.length} coverage note${warnings.length===1?'':'s'}`:''].filter(Boolean).join(' · ');
}

function diagnosticsHTML(errors=[],warnings=[]) {
  const list=items=>`<ul>${items.map(message=>`<li>${esc(message)}</li>`).join('')}</ul>`;
  return (errors.length?`<section class="sync-diagnostic-errors"><h3>Requests to retry (${errors.length})</h3>${list(errors)}<p>Use Sync GitHub to retry. If a failure persists, check authentication or your connection.</p></section>`:'')+
    (warnings.length?`<section class="sync-diagnostic-notes"><h3>Contribution coverage notes (${warnings.length})</h3>${list(warnings)}<p>Contribution discovery settings let you prioritise repositories and add alternate commit emails. A retry may still report the same scan limits.</p></section>`:'');
}

function syncBannerHTML(run) {
  const view=syncPresentation(run), errors=run.errors||[], warnings=run.warnings||[];
  if(!view.title)return '';
  const count=diagnosticSummary(errors,warnings);
  return `<strong>${esc(view.title)}</strong><p class="sync-banner-description">${esc(view.description)}</p><details id="sync-diagnostics" data-run-id="${esc(run.id)}" ${errors.length?'open':''}><summary>View details${count?' · '+esc(count):''}</summary>${diagnosticsHTML(errors,warnings)}</details><div class="sync-banner-actions">${errors.length||run.status==='failed'?'<button type="button" data-sync-retry>Retry sync</button>':''}${warnings.length?'<button type="button" data-go="contributions">View contributions</button>':''}<button type="button" data-go="settings">Open settings</button></div>`;
}

function renderSyncNotice(run) {
  if(!run)return false;
  const html=syncBannerHTML(run);
  if(!html)return false;
  // Keep an expanded report open while the dashboard refreshes.
  const previous=document.getElementById('sync-diagnostics');
  const expanded=previous?.dataset.runId===String(run.id)?previous.open:null;
  banner(html,syncPresentation(run).tone);
  const details=document.getElementById('sync-diagnostics');
  if(expanded!==null&&details)details.open=expanded;
  const retry=document.querySelector('[data-sync-retry]');
  if(retry){retry.disabled=!!state.data.sync.running;retry.onclick=startSync}
  return true;
}
