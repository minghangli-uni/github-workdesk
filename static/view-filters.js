'use strict';
const TABLE_VIEWS = ['overview','reviews','prs','issues','tasks','completed'];
const TABLE_FILTER_RULES = {
  period: ['all','7','30','90','365','custom'], dateField: ['updated_at','created_at','closed_at'],
  repo: 200, role: ['all', ...Object.keys(WORK_ROLES)],
  status: ['any','outstanding','finished','open','draft','merged','closed_unmerged','closed_issue','closed_unknown','todo','in_progress','blocked','done','snoozed'],
  query: 300, from: 'date', to: 'date'
};
const CONTRIBUTION_FILTER_DEFAULTS = {tab:'active',repo:'all',role:'all',status:'any',period:'all',query:'',discussion:false,sort:'activity'};
const CONTRIBUTION_FILTER_RULES = {tab:['active','history'],repo:200,role:['all',...Object.keys(WORK_ROLES)],
  status:['any','open','draft','merged','closed_unmerged','closed_unknown','committed'],period:['all','7','30','90','365'],query:300,discussion:'boolean',sort:['activity','mine']};
let savedViewFilters = {tables:{},contributions:{}};
let viewFiltersLoaded = false;
let filterSaveTimer = null;
let filterSavePending = false;
let filterSaving = false;
let filterSaveMessage = '';
let filterSaveFailed = false;

function defaultTaskFilters(view) {
  return {period:'all',dateField:'updated_at',repo:'all',role:'all',status:view === 'overview' ? 'outstanding' : 'any',query:'',from:'',to:''};
}
function cleanFilters(value, defaults, rules) {
  const result = {...defaults};
  for (const [key, rule] of Object.entries(rules)) {
    const item = value?.[key];
    const valid = Array.isArray(rule) ? rule.includes(item) : typeof rule === 'number'
      ? typeof item === 'string' && item.length <= rule : rule === 'boolean'
      ? typeof item === 'boolean' : typeof item === 'string' && (item === '' || /^\d{4}-\d{2}-\d{2}$/.test(item));
    if (valid) result[key] = item;
  }
  return result;
}
function restoreViewFilters(data) {
  if (viewFiltersLoaded) return;
  viewFiltersLoaded = true;
  const saved = data.settings?.view_filters;
  for (const view of TABLE_VIEWS) savedViewFilters.tables[view] = cleanFilters(saved?.tables?.[view], defaultTaskFilters(view), TABLE_FILTER_RULES);
  savedViewFilters.contributions = cleanFilters(saved?.contributions, CONTRIBUTION_FILTER_DEFAULTS, CONTRIBUTION_FILTER_RULES);
  Object.assign(contributionView, savedViewFilters.contributions);
  if (TABLE_VIEWS.includes(state.view)) state.filter = {...savedViewFilters.tables[state.view]};
}
function rememberCurrentFilters() {
  if (TABLE_VIEWS.includes(state.view)) savedViewFilters.tables[state.view] = {...state.filter};
  savedViewFilters.contributions = Object.fromEntries(Object.keys(CONTRIBUTION_FILTER_DEFAULTS).map(key => [key, contributionView[key]]));
}
function selectViewFilters(view) {
  rememberCurrentFilters();
  if (TABLE_VIEWS.includes(view)) state.filter = {...(savedViewFilters.tables[view] || defaultTaskFilters(view))};
}
function showFilterSaveStatus() {
  for (const id of ['filters-save-status','contribution-filters-save-status']) $(id).textContent = filterSaveMessage;
  for (const id of ['filters-save-retry','contribution-filters-save-retry']) $(id).classList.toggle('hidden', !filterSaveFailed);
}
function queueFilterSave() {
  rememberCurrentFilters(); filterSavePending = true; filterSaveFailed = false;
  filterSaveMessage = 'Saving filters…'; showFilterSaveStatus();
  clearTimeout(filterSaveTimer); filterSaveTimer = setTimeout(flushFilterSave, 350);
}
async function flushFilterSave() {
  if (filterSaving || !filterSavePending) return;
  const snapshot = JSON.parse(JSON.stringify(savedViewFilters));
  filterSaving = true; filterSavePending = false;
  try {
    await request('/api/settings', {method:'POST', body:JSON.stringify({view_filters:snapshot})});
    state.data.settings.view_filters = snapshot;
    filterSaveMessage = 'Filters saved'; filterSaveFailed = false;
  } catch (_) {
    filterSaveMessage = 'Filters could not be saved.'; filterSaveFailed = true;
  } finally {
    filterSaving = false;
    if (filterSavePending) {filterSaveMessage = 'Saving filters…'; clearTimeout(filterSaveTimer); filterSaveTimer = setTimeout(flushFilterSave, 0);}
    showFilterSaveStatus();
  }
}
function renderTableFilters() {
  for (const [id, key] of [['search-input','query'],['period-select','period'],['date-field','dateField'],['status-select','status'],['role-select','role'],['date-from','from'],['date-to','to']]) {
    if ($(id).value !== state.filter[key]) $(id).value = state.filter[key];
  }
  $('custom-dates').classList.toggle('hidden', state.filter.period !== 'custom');
  showFilterSaveStatus();
}
function renderContributionFilters() {
  for (const [id, key] of [['contribution-search','query'],['contribution-period','period'],['contribution-role','role'],['contribution-status','status'],['contribution-sort','sort']]) {
    if ($(id).value !== contributionView[key]) $(id).value = contributionView[key];
  }
  $('contribution-discussion').checked = contributionView.discussion;
  showFilterSaveStatus();
}
function bindViewFilters() {
  $('filters-reset').onclick = () => {state.filter = defaultTaskFilters(state.view); state.limit = 30; queueFilterSave(); render();};
  $('contribution-filters-reset').onclick = () => {Object.assign(contributionView, CONTRIBUTION_FILTER_DEFAULTS); contributionView.limit = 30; queueFilterSave(); renderContributions();};
  for (const id of ['filters-save-retry','contribution-filters-save-retry']) $(id).onclick = queueFilterSave;
}
