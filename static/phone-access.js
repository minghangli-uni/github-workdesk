'use strict';
let phoneAccessStatus = null;
let phoneAccessLoading = false;
let phoneAccessStarted = false;
let privateMobileStarted = false;

function renderPhoneAccess() {
  const data = state.data;
  const remote = !!data?.phone_access?.remote;
  $('private-phone-settings').classList.toggle('hidden', !!data?.hosted);
  if (!data || data.hosted) return;
  $('private-phone-controls').classList.toggle('hidden', remote);
  $('private-phone-setup').classList.toggle('hidden', remote || !!phoneAccessStatus?.enabled);
  const url = remote ? data.phone_access.url : phoneAccessStatus?.url;
  $('private-phone-address-row').classList.toggle('hidden', !url);
  $('private-phone-address').value = url || '';
  if (remote) {
    $('private-phone-status').textContent = 'Connected to your Mac through Tailscale. Changes are saved on your Mac.';
    $('private-phone-description').textContent = 'You are using the same tasks, contributions, notes and inbox as your Mac. Keep your Mac awake, connected and running Workdesk.';
    $('auth-description').textContent = 'GitHub is connected through your Mac. Workdesk reads GitHub activity and saves your tracking changes on your Mac.';
    $('auth-hint').textContent = 'If GitHub needs reconnecting, run “gh auth login” on your Mac.';
    for (const id of ['notification-browser-permission', 'notification-background-install', 'notification-background-remove', 'notification-local-note', 'local-reminder-card']) $(id).classList.add('hidden');
    $('notification-alert-label').textContent = 'Show alerts on your Mac';
    $('notification-delivery-note').textContent = 'The inbox updates while this app is open. Alerts while the iPhone app is closed are not enabled. Manage Mac background checks from your Mac.';
    if (!privateMobileStarted) {
      privateMobileStarted = true;
      const manifest = document.createElement('link');
      manifest.rel = 'manifest'; manifest.href = '/manifest.webmanifest'; document.head.appendChild(manifest);
      if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
    }
    return;
  }
  const status = phoneAccessStatus;
  $('private-phone-status').textContent = phoneAccessLoading ? 'Checking your private connection…' : status?.message || (status?.installed ? 'Tailscale is installed. Connect it, then enable iPhone access.' : 'Install and connect Tailscale on both devices to get started.');
  $('private-phone-enable').disabled = phoneAccessLoading || !status?.installed || !!status?.enabled;
  $('private-phone-enable').classList.toggle('hidden', !!status?.enabled);
  $('private-phone-check').disabled = phoneAccessLoading;
  $('private-phone-disable').disabled = phoneAccessLoading;
  $('private-phone-disable').classList.toggle('hidden', !status?.enabled);
  if (!phoneAccessStarted) { phoneAccessStarted = true; loadPhoneAccess(); }
}

async function loadPhoneAccess() {
  try { phoneAccessStatus = await request('/api/phone-access'); }
  catch (error) { phoneAccessStatus = {...phoneAccessStatus, message: 'Could not check phone access: ' + error.message}; }
  renderPhoneAccess();
}

async function changePhoneAccess(action) {
  if (phoneAccessLoading) return;
  phoneAccessLoading = true; renderPhoneAccess();
  try {
    phoneAccessStatus = await request('/api/phone-access', {method: 'POST', body: JSON.stringify({action})});
    toast(phoneAccessStatus.message);
  } catch (error) {
    await loadPhoneAccess();
    phoneAccessStatus = {...phoneAccessStatus, message: error.message};
    toast(error.message, true);
  } finally { phoneAccessLoading = false; renderPhoneAccess(); }
}

function bindPhoneAccess() {
  $('private-phone-enable').onclick = () => changePhoneAccess('enable');
  $('private-phone-check').onclick = () => changePhoneAccess('check');
  $('private-phone-disable').onclick = () => changePhoneAccess('disable');
  $('private-phone-copy').onclick = async () => {
    try { await navigator.clipboard.writeText($('private-phone-address').value); toast('Private address copied. Open it on your iPhone with Tailscale connected.'); }
    catch (_) { $('private-phone-address').select(); toast('Select and copy the private address above.'); }
  };
}
