import type { RealtimeMetadata } from './realtime_runtime';

export type GeminiStatus = {
  state: string;
  queuedMs: number;
  connections: number;
};

export type GeminiLiveRuntime = {
  stop(): Promise<void>;
  mute(value: boolean): void;
  sendFunctionOutput(callId: string, output: string, metadata?: RealtimeMetadata): boolean;
  sendRunEvent(text: string, metadata?: RealtimeMetadata): boolean;
  setGeneration(sessionId: number, threadId: string, previous: string, next: string): boolean;
  session: { audio: { muted: boolean } } | null;
};

export type GeminiLiveOptions = {
  token: string | null;
  connectionSeconds: number;
  sessionId: number;
  threadId?: string;
  generationId?: string;
  current?(): boolean;
  emit(event: Record<string, unknown>): void;
  onStatus?(status: GeminiStatus): void;
  onTranscript?(role: 'user' | 'assistant', text: string): void;
  requestToken(): Promise<{ token: string; connectionSeconds: number }>;
  onRuntime?(runtime: GeminiLiveRuntime): void;
};

export function startGeminiLiveRuntime(options: GeminiLiveOptions): Promise<GeminiLiveRuntime>;
