const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('kin', {
  getState: () => ipcRenderer.invoke('app:state'),
  listModels: () => ipcRenderer.invoke('model:list'),
  setModel: (model) => ipcRenderer.invoke('model:set', model),
  chat: (message) => ipcRenderer.invoke('chat:send', message),
  addDocuments: () => ipcRenderer.invoke('documents:add'),
  removeDocument: (id) => ipcRenderer.invoke('documents:remove', id),
  updateProfile: (profile) => ipcRenderer.invoke('profile:update', profile),
  exportKnowledge: () => ipcRenderer.invoke('knowledge:export'),
  importKnowledge: () => ipcRenderer.invoke('knowledge:import'),
  clearChat: () => ipcRenderer.invoke('chat:clear'),
  evaluateCandidate: (url, values) => ipcRenderer.invoke('optimization:evaluate', url, values),
  getFederationStatus: () => ipcRenderer.invoke('federation:status'),
  saveFederation: (config) => ipcRenderer.invoke('federation:save', config),
  startFederation: () => ipcRenderer.invoke('federation:start'),
  stopFederation: () => ipcRenderer.invoke('federation:stop')
});
