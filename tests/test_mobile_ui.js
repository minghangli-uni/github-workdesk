const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.join(__dirname, '..');
const code = fs.readFileSync(path.join(root, 'static/mobile.js'), 'utf8');

function harness({installed = false, apple = false, permission = 'granted', withPush = true} = {}) {
  const calls = [], elements = new Map();
  const el = id => {
    if (!elements.has(id)) elements.set(id, {textContent: '', disabled: false, classList: {toggle(){}, add(){}}});
    return elements.get(id);
  };
  let subscription = null;
  const newSubscription = {endpoint: 'https://web.push.apple.com/test', unsubscribe: async () => { calls.push('unsubscribe'); subscription = null; }};
  const registration = withPush ? {pushManager: {
    getSubscription: async () => subscription,
    subscribe: async options => {calls.push(['subscribe', options]); subscription = newSubscription; return subscription;}
  }} : {};
  const window = {matchMedia: () => ({matches: installed}), location: {replace: url => calls.push(['navigate', url])}};
  if (withPush) {window.Notification = {}; window.PushManager = {};}
  const context = vm.createContext({window, navigator: {userAgent: apple ? 'iPhone' : 'Desktop', platform: '', maxTouchPoints: 0},
    Notification: {requestPermission: async () => {calls.push('permission'); return permission;}},
    atob: value => Buffer.from(value, 'base64').toString('binary'), Uint8Array,
    $: el, state: {data: {cloud: {push: {public_key: 'YWJjZA'}}}},
    request: async (url, opts) => {calls.push(['request', url, opts]); return {};},
    toast: (message, error) => calls.push(['toast', message, error])
  });
  vm.runInContext(code, context);
  context.registration = registration;
  vm.runInContext('workdeskRegistration = Promise.resolve(registration)', context);
  return {context, calls, elements, setSubscription: () => { subscription = newSubscription; }};
}

test('iPhone explains installation before requesting notification permission', async () => {
  const h = harness({apple: true});
  await h.context.enableDevicePush();
  assert.equal(h.calls.some(c => c === 'permission'), false);
  assert.match(h.calls[0][1], /Add to Home Screen/);
});

test('installed phone requests permission from the tap and saves the subscription', async () => {
  const h = harness({apple: true, installed: true});
  const pending = h.context.enableDevicePush();
  assert.equal(h.calls[0], 'permission');
  await pending;
  const send = h.calls.find(c => c[0] === 'request');
  assert.equal(send[1], '/api/push/subscribe');
  assert.equal(JSON.parse(send[2].body).endpoint, 'https://web.push.apple.com/test');
  assert.equal(h.elements.get('push-enable').disabled, true);
});

test('denied permission never creates or stores a subscription', async () => {
  const h = harness({permission: 'denied'});
  await h.context.enableDevicePush();
  assert.equal(h.calls.some(c => c[0] === 'subscribe' || c[0] === 'request'), false);
  assert.equal(h.calls.at(-1)[2], true);
});

test('turning off push removes server delivery before unsubscribing the device', async () => {
  const h = harness(); h.setSubscription();
  await h.context.disableDevicePush();
  assert.equal(h.calls[0][1], '/api/push/unsubscribe');
  assert.equal(h.calls[1], 'unsubscribe');
  assert.equal(h.elements.get('push-disable').disabled, true);
});

test('sign out still works when the browser has no PushManager', async () => {
  const h = harness({withPush: false});
  await h.context.signOutWorkdesk();
  assert.equal(h.calls[0][1], '/auth/logout');
  assert.deepEqual(h.calls.at(-1), ['navigate', '/login']);
});

test('all mobile DOM bindings exist and scripts load in dependency order', () => {
  const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');
  const ids = [...code.matchAll(/\$\('([^']+)'\)/g)].map(m => m[1]);
  for (const id of ids) assert.ok(html.includes(`id="${id}"`), id);
  assert.ok(html.indexOf('/static/mobile.js') < html.indexOf('/static/app.js'));
  const manifest = JSON.parse(fs.readFileSync(path.join(root, 'static/manifest.webmanifest'), 'utf8'));
  assert.equal(manifest.start_url, '/');
  for (const icon of manifest.icons) assert.ok(fs.existsSync(path.join(root, icon.src)));
});

test('service worker notification clicks open the inbox without trusting payload URLs', async () => {
  const listeners = {}, calls = [];
  const self = {location: {origin: 'https://workdesk.example'}, addEventListener: (type, fn) => listeners[type] = fn,
    clients: {matchAll: async () => [], openWindow: async url => calls.push(url)}};
  vm.runInNewContext(fs.readFileSync(path.join(root, 'static/sw.js'), 'utf8'), {self, URL});
  let pending;
  listeners.notificationclick({notification: {data: {url: 'https://evil.example'}, close(){}}, waitUntil: p => pending = p});
  await pending;
  assert.deepEqual(calls, ['/?view=notifications']);
});
