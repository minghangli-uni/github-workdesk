'use strict';
let appearanceLoaded = false;
let appearanceSaving = false;
let localBackupSaving = false;

async function restoreAppearance(data) {
  if (appearanceLoaded) return;
  appearanceLoaded = true;
  if (data.settings.theme_configured && AVAILABLE_THEMES.has(data.settings.theme)) {
    applyTheme(data.settings.theme);
    return;
  }
  // Preserve an existing browser choice when upgrading to database-backed preferences.
  if (selectedTheme !== 'system') {
    try {
      await request('/api/settings', {method: 'POST', body: JSON.stringify({theme: selectedTheme})});
      data.settings.theme = selectedTheme;
      data.settings.theme_configured = true;
    } catch (_) {
      appearanceLoaded = false;
      toast('Your theme is still in this browser. It could not yet be saved with your Workdesk data.', true);
    }
  }
}

async function saveAppearance(theme) {
  if (!state.data || appearanceSaving || !AVAILABLE_THEMES.has(theme)) return;
  appearanceSaving = true;
  const previous = selectedTheme;
  applyTheme(theme);
  $('theme-select').disabled = true;
  try {
    await request('/api/settings', {method: 'POST', body: JSON.stringify({theme})});
    state.data.settings.theme = theme;
    state.data.settings.theme_configured = true;
    appearanceLoaded = true;
    toast(state.data.hosted ? 'Appearance saved in Workdesk.' : 'Appearance saved on this computer.');
  } catch (error) {
    applyTheme(previous);
    toast('Appearance could not be saved: ' + error.message, true);
  } finally {
    appearanceSaving = false;
    $('theme-select').disabled = false;
  }
}

function renderLocalData(data) {
  const local = !data.hosted;
  $('local-save-controls').classList.toggle('hidden', !local);
  if (!local) return;
  $('backup-heading').textContent = data.platform === 'darwin' ? 'Saved on this Mac' : 'Saved on this computer';
  $('backup-description').textContent = 'Tasks, notes, synced GitHub records, contribution evidence, notification history and settings are saved here. Each successful Save or sync writes to your database. You can close and reopen Workdesk without losing saved work.';
  $('sidebar-storage-note').textContent = 'Saved on this computer. No hosting subscription is needed. GitHub is read-only.';
  $('local-backup-button').textContent = data.platform === 'darwin' ? 'Save backup on this Mac' : 'Save backup on this computer';
  $('local-backup-button').disabled = localBackupSaving;
  $('theme-select').disabled = appearanceSaving;
  const storage = data.storage;
  if (!storage) return;
  $('database-path').textContent = storage.database_path;
  $('local-backup-directory').textContent = storage.backup_directory;
  const last = storage.latest_backup;
  $('local-backup-status').textContent = storage.error
    ? 'Backup needs attention: ' + storage.error + ' Your main database remains in the folder above.'
    : last ? 'Latest backup: ' + formatTime(last.created_at) + ' · ' + (last.kind === 'auto' ? 'Automatic' : 'Manual')
    : 'The first automatic backup will be created while Workdesk runs.';
  $('local-backup-status').classList.toggle('backup-error', !!storage.error);
}

async function saveLocalBackup() {
  if (localBackupSaving) return;
  localBackupSaving = true;
  $('local-backup-button').disabled = true;
  try {
    const result = await request('/api/local-backup', {method: 'POST', body: '{}'});
    state.data.storage = result.storage;
    renderLocalData(state.data);
    toast('Complete backup saved on this computer.');
  } catch (error) {
    toast('Backup could not be saved: ' + error.message, true);
  } finally {
    localBackupSaving = false;
    $('local-backup-button').disabled = false;
  }
}

async function openLocalData() {
  try {
    await request('/api/open-data-folder', {method: 'POST', body: '{}'});
  } catch (error) { toast(error.message, true); }
}

function bindLocalData() {
  $('local-backup-button').onclick = saveLocalBackup;
  $('local-folder-button').onclick = openLocalData;
}
