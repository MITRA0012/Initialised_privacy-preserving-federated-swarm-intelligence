/// <reference types="vite/client" />
interface Window {
  kin: {
    getState(): Promise<{ profile: Profile; documents: DocumentItem[]; messages: ChatMessage[]; dataDir: string }>;
    listModels(): Promise<{ online: boolean; models: Array<{ name: string; size: number }>; error?: string }>;
    setModel(model: string): Promise<Profile>;
    chat(message: string, mode?: 'local' | 'general'): Promise<{ answer: string; sources: string[] }>;
    addDocuments(): Promise<DocumentItem[]>;
    removeDocument(id: string): Promise<DocumentItem[]>;
    updateProfile(profile: Partial<Profile>): Promise<Profile>;
    exportKnowledge(): Promise<boolean>;
    importKnowledge(): Promise<boolean>;
    clearChat(): Promise<boolean>;
    evaluateCandidate(url: string, values: Record<string, number>): Promise<number>;
    getFederationStatus(): Promise<{ config: KinFederationConfig; processes: Record<string, string>; inference: { running: boolean; ready: boolean; round: number } }>;
    saveFederation(config: KinFederationConfig): Promise<KinFederationConfig>;
    startFederation(): Promise<{ started: boolean }>;
    stopFederation(): Promise<{ stopped: boolean }>;
  };
}
type Profile = { name: string; instructions: string; model: string; temperature: number };
type DocumentItem = { id: string; name: string; addedAt: string };
type ChatMessage = { role: 'user' | 'assistant'; content: string; at?: string; sources?: string[] };
type KinFederationConfig = { coordinator: string; caPath: string; privateKeyPath: string; pythonPath: string; projectPath: string; inferencePort: number };
