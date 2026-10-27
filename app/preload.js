// The only bridge between the page and the main process.
const { contextBridge, ipcRenderer, webUtils } = require('electron');

const call = (channel, ...args) => ipcRenderer.invoke(channel, ...args);
const on = channel => fn => {
  const listener = (_e, payload) => fn(payload);
  ipcRenderer.on(channel, listener);
  return () => ipcRenderer.removeListener(channel, listener);
};

contextBridge.exposeInMainWorld('api', {
  getSettings: () => call('get-settings'),
  setSettings: next => call('set-settings', next),
  getState: () => call('get-state'),
  runChecks: () => call('run-checks'),
  restartEngine: () => call('restart-engine'),

  oneDriveStatus: () => call('onedrive-status'),
  connectOneDrive: () => call('onedrive-connect'),
  cancelOneDrive: () => call('onedrive-cancel'),
  disconnectOneDrive: () => call('onedrive-disconnect'),
  uploadNow: () => call('upload-now'),

  setupCheck: step => call('setup-check', step),
  setupAction: (action, values) => call('setup-action', { action, values }),
  setupSecrets: () => call('setup-secrets'),

  addLinks: (links, section, priority, instructions) => call('add-links', { links, section, priority, instructions }),
  addFiles: (files, priority, instructions) => call('add-files', { files, priority, instructions }),
  pickFiles: () => call('pick-files'),
  pickFolder: () => call('pick-folder'),
  pathForFile: file => webUtils.getPathForFile(file),
  removePending: name => call('remove-pending', name),

  retry: id => call('job-action', { op: 'retry', id }),
  cancel: id => call('job-action', { op: 'cancel', id }),
  remove: id => call('job-action', { op: 'remove', id }),
  setPriority: (id, priority) => call('job-action', { op: 'priority', id, priority }),
  jobLog: id => call('job-log', id),

  listShorts: () => call('list-shorts'),
  savePost: (id, patch) => call('save-post', { id, patch }),
  resolveCheck: (id, action) => call('resolve-check', { id, action }),
  openExternal: url => call('open-external', url),
  showFolder: () => call('show-folder'),

  getStyle: () => call('style:get'),
  setStyle: next => call('style:set', next),
  styleFonts: () => call('style:fonts'),
  styleShorts: () => call('style:shorts'),
  previewStyle: (id, style) => call('style:preview', { id, style }),
  openMusic: () => call('music:open'),
  musicStatus: () => call('music:status'),

  onEvent: on('engine-event'),
  onEngineStatus: on('engine-status'),
  onEngineLog: on('engine-log'),
  onPending: on('pending'),
  onUploadStatus: on('upload-status'),
  onOneDriveStatus: on('onedrive-status'),
  onShortsChanged: on('shorts-changed'),
  onSetupLog: on('setup-log'),
  onStyleLog: on('style-log'),
});
