const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.join(__dirname, '..');
const code = fs.readFileSync(path.join(root, 'static/phone-access.js'), 'utf8');

function harness(data = {}) {
  const calls = [], elements = new Map();
  const el = id => {
    if (!elements.has(id)) {
      const classes = new Set();
      elements.set(id, {classes, textContent: '', value: '', disabled: false,
        classList: {add: cls => classes.add(cls), toggle: (cls, set) => set ? classes.add(cls) : classes.delete(cls)}});
    }
    return elements.get(id);
  };
  const context = vm.createContext({$: el, state: {data},
    navigator: {serviceWorker: {register: async p => calls.push(['register', p])}},
    document: {createElement: () => ({}), head: {appendChild: link => calls.push(['manifest', link.href])}},
    request: async (url, opts) => {calls.push(['request', url, opts]); return {installed: false, enabled: false};},
    toast: message => calls.push(['toast', message])});
  vm.runInContext(code, context);
  return {context, calls, elements, el};
}

test('phone uses Mac storage wording and installs assets without push permission', () => {
  const h = harness({phone_access: {remote: true, url: 'https://mac.example.ts.net:8443'}});
  h.context.renderPhoneAccess(); h.context.renderPhoneAccess();
  assert.equal(h.el('private-phone-controls').classes.has('hidden'), true);
  assert.equal(h.el('private-phone-address').value, 'https://mac.example.ts.net:8443');
  assert.match(h.el('private-phone-status').textContent, /saved on your Mac/);
  assert.match(h.el('notification-delivery-note').textContent, /closed are not enabled/);
  assert.equal(h.el('local-reminder-card').classes.has('hidden'), true);
  assert.deepEqual(h.calls.filter(c => c[0] === 'register'), [['register', '/sw.js']]);
  assert.equal(h.calls.some(c => c[0] === 'request'), false);
});

test('hosted workspace hides local phone setup and never calls its API', () => {
  const h = harness({hosted: true});
  h.context.renderPhoneAccess();
  assert.equal(h.el('private-phone-settings').classes.has('hidden'), true);
  assert.equal(h.calls.length, 0);
});

test('missing Tailscale shows steps and prevents premature enable', async () => {
  const h = harness();
  await h.context.loadPhoneAccess();
  assert.equal(h.el('private-phone-enable').disabled, true);
  assert.equal(h.el('private-phone-setup').classes.has('hidden'), false);
  assert.equal(h.el('private-phone-address-row').classes.has('hidden'), true);
  assert.match(h.el('private-phone-status').textContent, /Install and connect Tailscale/);
});

test('failed enable retains readable error and permits retry', async () => {
  const h = harness();
  vm.runInContext('phoneAccessStarted = true; phoneAccessStatus = {installed: true}', h.context);
  h.context.request = async (url, options) => {
    if (options) throw Error('Enable HTTPS certificates first.');
    return {installed: true, enabled: false};
  };
  await h.context.changePhoneAccess('enable');
  assert.equal(h.el('private-phone-enable').disabled, false);
  assert.match(h.el('private-phone-status').textContent, /Enable HTTPS certificates first/);
});

test('all phone bindings exist and the script loads before startup', () => {
  const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');
  for (const match of code.matchAll(/\$\('([^']+)'\)/g)) assert.ok(html.includes(`id="${match[1]}"`), match[1]);
  assert.ok(html.indexOf('/static/phone-access.js') < html.indexOf('/static/app.js'));
});

test('offline navigation handles unavailable Mac but preserves access denial', async () => {
  const listeners = {}, fallback = {status: 200, offline: true};
  const context = {URL, self: {location: {origin: 'https://mac.example.ts.net:8443'}, addEventListener: (type, fn) => listeners[type] = fn},
    caches: {match: async () => fallback}, fetch: async () => ({status: 502})};
  vm.runInNewContext(fs.readFileSync(path.join(root, 'static/sw.js'), 'utf8'), context);
  let response;
  const event = {request: {mode: 'navigate', url: 'https://mac.example.ts.net:8443/'}, respondWith: promise => response = promise};
  listeners.fetch(event);
  assert.equal(await response, fallback);
  context.fetch = async () => ({status: 403});
  listeners.fetch(event);
  assert.equal((await response).status, 403);
  context.fetch = async () => {throw Error('Mac asleep');};
  listeners.fetch(event);
  assert.equal(await response, fallback);
  response = null;
  listeners.fetch({request: {mode: 'cors', url: 'https://mac.example.ts.net:8443/api/dashboard'}, respondWith: promise => response = promise});
  assert.equal(response, null);
});
