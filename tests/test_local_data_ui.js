const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '..');
const code = fs.readFileSync(path.join(root, 'static/local-data.js'), 'utf8');

function harness(theme = 'system') {
  const calls = [], elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {textContent: '', disabled: false, classList: {toggle(){}}});
    return elements.get(id);
  };
  const data = {platform: 'darwin', settings: {theme: 'system', theme_configured: false}};
  const context = vm.createContext({selectedTheme: theme, AVAILABLE_THEMES: new Set(['system', 'nord', 'dracula']),
    state: {data}, $: element, formatTime: x => x,
    request: async (url, options) => {calls.push(['request', url, JSON.parse(options.body)]);return {storage: {latest_backup: {kind: 'manual', created_at: 'today'}}};},
    toast: (message, error) => calls.push(['toast', message, error])});
  context.applyTheme = theme => {context.selectedTheme = theme;calls.push(['theme', theme]);};
  vm.runInContext(code, context);
  return {context, calls, elements, data};
}

test('database theme returns after browser data is cleared', async () => {
  const h = harness();
  await h.context.restoreAppearance({settings: {theme: 'nord', theme_configured: true}});
  assert.equal(h.context.selectedTheme, 'nord');
  assert.equal(h.calls.some(call => call[0] === 'request'), false);
});

test('existing browser theme migrates into saved settings once', async () => {
  const h = harness('dracula');
  await h.context.restoreAppearance(h.data);
  await h.context.restoreAppearance(h.data);
  assert.equal(h.calls.filter(call => call[0] === 'request').length, 1);
  assert.deepEqual(h.calls[0], ['request', '/api/settings', {theme: 'dracula'}]);
  assert.equal(h.data.settings.theme_configured, true);
});

test('choosing a theme waits for the server before confirming persistence', async () => {
  const h = harness();let finish;
  h.context.request = () => new Promise(resolve => finish = resolve);
  const pending = h.context.saveAppearance('nord');
  assert.equal(h.elements.get('theme-select').disabled, true);
  assert.equal(h.data.settings.theme_configured, false);
  assert.equal(h.calls.some(call => call[0] === 'toast'), false);
  finish({});await pending;
  assert.equal(h.data.settings.theme, 'nord');
  assert.equal(h.elements.get('theme-select').disabled, false);
  assert.match(h.calls.at(-1)[1], /saved on this computer/);
});

test('failed appearance save restores the previous theme and reports failure', async () => {
  const h = harness('dracula');
  h.context.request = async () => {throw Error('disk unavailable');};
  await h.context.saveAppearance('nord');
  assert.equal(h.context.selectedTheme, 'dracula');
  assert.equal(h.data.settings.theme_configured, false);
  assert.equal(h.calls.at(-1)[2], true);
});

test('manual backup prevents duplicate clicks and only confirms a completed save', async () => {
  const h = harness();let finish, requests = 0;
  h.context.request = () => {requests++;return new Promise(resolve => finish = resolve);};
  const first = h.context.saveLocalBackup();
  await h.context.saveLocalBackup();
  assert.equal(requests, 1);
  assert.equal(h.elements.get('local-backup-button').disabled, true);
  finish({storage: {latest_backup: {kind: 'manual', created_at: 'today'}}});
  await first;
  assert.match(h.elements.get('local-backup-status').textContent, /Latest backup: today/);
  assert.equal(h.elements.get('local-backup-button').disabled, false);
  assert.match(h.calls.at(-1)[1], /Complete backup saved/);
});

test('all local storage controls exist and load before the main app', () => {
  const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');
  for (const match of code.matchAll(/\$\('([^']+)'\)/g)) assert.ok(html.includes(`id="${match[1]}"`), match[1]);
  assert.ok(html.indexOf('/static/local-data.js') < html.indexOf('/static/app.js'));
});
