'use strict';
let workdeskRegistration = null;
let workdeskMobileStarted = false;

function installedWorkdesk() {
  return window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
}
function appleMobile() {
  return /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
}
function applicationKey(encoded) {
  const raw = atob(encoded.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - encoded.length % 4) % 4));
  return Uint8Array.from(raw, c => c.charCodeAt(0));
}
function renderMobile() {
  const hosted = !!state.data?.hosted;
  document.body.classList.toggle('hosted', hosted);
  $('cloud-account').classList.toggle('hidden', !hosted);
  $('phone-settings').classList.toggle('hidden', !hosted);
  if (!hosted) return;
  $('cloud-user').textContent = '@' + state.data.cloud.login;
  $('settings-username').disabled = true;
  $('auth-description').textContent = 'Connected with GitHub sign-in. Workdesk reads your GitHub activity; notes and personal tasks are shared across your devices.';
  $('auth-hint').textContent = 'Signed in as @' + state.data.cloud.login + '. Reconnect GitHub here if authorization expires.';
  $('sidebar-storage-note').textContent = 'Your private workspace is shared across your devices. GitHub is read-only.';
  $('backup-description').textContent = 'Your work is saved in hosted storage. Download a backup anytime. Exports exclude sign-in credentials and device subscriptions.';
  $('database-path').textContent = 'Shared hosted workspace';
  $('backup-eyebrow').textContent = 'YOUR DATA';
  $('contribution-storage-note').textContent = 'Following a PR adds its updates to your inbox. Private notes and follow preferences are shared across your devices.';
  $('notification-browser-permission').classList.add('hidden');
  $('notification-background-install').classList.add('hidden');
  $('notification-background-remove').classList.add('hidden');
  $('notification-local-note').classList.add('hidden');
  $('local-reminder-card').classList.add('hidden');
  $('notification-alert-label').textContent = 'Send alerts to enabled devices';
  $('notification-delivery-note').textContent = 'Checks run on the hosted server, including while your Mac is off. Enable notifications on each device below.';
  $('cloud-sync-note').textContent = `After your first sync, your work refreshes about every ${state.data.cloud.sync_interval / 60} minutes. Notification checks use your frequency above.`;
  $('push-server-status').textContent = state.data.cloud.push.errors.join(' ');
  $('install-help').classList.toggle('hidden', installedWorkdesk());
  $('backup-import').disabled = !!state.data.tasks.length || !!state.data.runs.length || !!state.data.contributions?.items?.length;
  if (!workdeskMobileStarted) {
    workdeskMobileStarted = true;
    const manifest = document.createElement('link');
    manifest.rel = 'manifest'; manifest.href = '/manifest.webmanifest'; document.head.appendChild(manifest);
    if ('serviceWorker' in navigator) {
      workdeskRegistration = navigator.serviceWorker.register('/sw.js').then(async registration => {
        await navigator.serviceWorker.ready;
        if ('PushManager' in window) {
          const subscription = await registration.pushManager.getSubscription();
          if (subscription) await request('/api/push/subscribe', {method: 'POST', body: JSON.stringify(subscription)});
        }
        await updateDeviceStatus(registration);
        return registration;
      }).catch(() => { $('push-device-status').textContent = 'Notifications could not be set up. Reload Workdesk to retry.'; return null; });
    } else $('push-device-status').textContent = 'Use Safari, Chrome, or Firefox over HTTPS for device notifications.';
  }
}
async function updateDeviceStatus(registration) {
  const subscription = registration?.pushManager ? await registration.pushManager.getSubscription() : null;
  $('push-device-status').textContent = subscription ? 'Notifications are enabled on this device.' : 'Notifications are off on this device.';
  $('push-enable').disabled = !!subscription;
  $('push-disable').disabled = !subscription;
}
async function enableDevicePush() {
  if (appleMobile() && !installedWorkdesk()) {
    toast('In Safari, use Share → Add to Home Screen, then open Workdesk from its icon to enable notifications.', true); return;
  }
  if (!('Notification' in window) || !('PushManager' in window)) {
    toast('This browser does not support push notifications. Your inbox still works.', true); return;
  }
  try {
    const permission = await Notification.requestPermission();
    if (permission !== 'granted') throw Error('Allow Workdesk notifications in your device settings, then try again.');
    const registration = await workdeskRegistration;
    if (!registration) throw Error('Reload Workdesk to finish setting up notifications.');
    const subscription = await registration.pushManager.getSubscription() || await registration.pushManager.subscribe({
      userVisibleOnly: true, applicationServerKey: applicationKey(state.data.cloud.push.public_key)
    });
    await request('/api/push/subscribe', {method: 'POST', body: JSON.stringify(subscription)});
    await updateDeviceStatus(registration);
    toast('Notifications enabled on this device. Your Mac can be off.');
  } catch (error) { toast(error.message, true); }
}
async function disableDevicePush() {
  try {
    const registration = await workdeskRegistration;
    const subscription = await registration?.pushManager?.getSubscription();
    if (subscription) {
      await request('/api/push/unsubscribe', {method: 'POST', body: JSON.stringify({endpoint: subscription.endpoint})});
      await subscription.unsubscribe();
    }
    await updateDeviceStatus(registration);
    toast('Notifications turned off on this device.');
  } catch (error) { toast(error.message, true); }
}
async function signOutWorkdesk() {
  try {
    // The server revokes this session and its devices even if browser cleanup fails.
    await request('/auth/logout', {method: 'POST', body: '{}'});
    if (workdeskRegistration) workdeskRegistration.then(async registration => {
      const subscription = await registration?.pushManager?.getSubscription();
      if (subscription) await subscription.unsubscribe();
    }).catch(() => {});
    window.location.replace('/login');
  } catch (error) { toast(error.message, true); }
}
async function importWorkdeskBackup(event) {
  const file = event.target.files[0];
  if (!file) return;
  try {
    if (file.size > 20 * 1024 * 1024) throw Error('Choose a JSON export smaller than 20 MB.');
    const payload = JSON.parse(await file.text());
    const result = await request('/api/import', {method: 'POST', body: JSON.stringify(payload)});
    await refresh(true);
    toast(`Imported ${result.imported} saved records. Your original Mac copy is unchanged.`);
  } catch (error) { toast(error.message, true); }
  finally { event.target.value = ''; }
}
function bindMobile() {
  $('push-enable').onclick = enableDevicePush;
  $('push-disable').onclick = disableDevicePush;
  $('cloud-signout').onclick = signOutWorkdesk;
  $('backup-import').onchange = importWorkdeskBackup;
  if ('serviceWorker' in navigator) navigator.serviceWorker.addEventListener('message', event => {
    if (event.data?.type === 'OPEN_NOTIFICATIONS') switchView('notifications');
  });
}
