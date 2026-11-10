import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { startGeminiLiveRuntime } from '../../../src/row_bot/voice/gemini_live_runtime.js';

type Listener = (event: { data?: unknown }) => void;

class FakeSocket {
  static OPEN = 1;
  static instances: FakeSocket[] = [];
  url: string;
  readyState = 0;
  sent: string[] = [];
  private listeners = new Map<string, Listener[]>();

  constructor(url: string) {
    this.url = url;
    FakeSocket.instances.push(this);
  }

  addEventListener(type: string, listener: Listener) {
    this.listeners.set(type, [...(this.listeners.get(type) || []), listener]);
  }

  dispatch(type: string, event: { data?: unknown } = {}) {
    for (const listener of this.listeners.get(type) || []) listener(event);
  }

  open() {
    this.readyState = FakeSocket.OPEN;
    this.dispatch('open');
  }

  receive(payload: unknown) {
    this.dispatch('message', { data: JSON.stringify(payload) });
  }

  send(data: string) {
    this.sent.push(data);
  }

  close() {
    this.readyState = 3;
    this.dispatch('close');
  }
}

type Source = {
  stop: ReturnType<typeof vi.fn>;
  start: ReturnType<typeof vi.fn>;
  connect: ReturnType<typeof vi.fn>;
};

class FakeContext {
  static last: FakeContext | null = null;
  sampleRate = 16000;
  currentTime = 0;
  state = 'running';
  destination = {};
  sources: Source[] = [];
  constructor() {
    FakeContext.last = this;
  }
  resume() {
    return Promise.resolve();
  }
  close() {
    return Promise.resolve();
  }
  createBuffer(_channels: number, length: number, rate: number) {
    return { duration: length / rate, copyToChannel: vi.fn() };
  }
  createBufferSource() {
    const source: Source = {
      stop: vi.fn(),
      connect: vi.fn(),
      start: vi.fn(() => {
        this.currentTime += 0.1;
      }),
    };
    this.sources.push(source);
    return source;
  }
  createMediaStreamSource() {
    return { connect: vi.fn() };
  }
  createScriptProcessor() {
    return { connect: vi.fn(), disconnect: vi.fn(), onaudioprocess: null };
  }
  createGain() {
    return { gain: { value: 0 }, connect: vi.fn(), disconnect: vi.fn() };
  }
}

function pcm() {
  return Buffer.from([0, 0, 1, 0]).toString('base64');
}

function options(overrides: Record<string, unknown> = {}) {
  return {
    token: 'auth_tokens/one',
    connectionSeconds: 0,
    sessionId: 1,
    threadId: 'A',
    generationId: '',
    current: () => true,
    emit: vi.fn(),
    onStatus: vi.fn(),
    onTranscript: vi.fn(),
    requestToken: vi.fn().mockResolvedValue({
      token: 'auth_tokens/two',
      connectionSeconds: 1,
    }),
    onRuntime: vi.fn(),
    ...overrides,
  };
}

async function connected(overrides: Record<string, unknown> = {}) {
  const value = options(overrides);
  const runtime = await startGeminiLiveRuntime(value);
  const socket = FakeSocket.instances[0];
  socket.open();
  socket.receive({ setupComplete: {} });
  return { runtime, socket, value };
}

beforeEach(() => {
  FakeSocket.instances = [];
  FakeContext.last = null;
  vi.stubGlobal('WebSocket', FakeSocket);
  vi.stubGlobal('AudioContext', FakeContext);
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    value: {
      getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [{ stop: vi.fn() }] }),
    },
  });
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('Gemini Live browser runtime', () => {
  it('connects with the minted token and answers a hand-off with WHEN_IDLE', async () => {
    const { runtime, socket, value } = await connected();
    expect(socket.url).toContain('BidiGenerateContentConstrained');
    expect(socket.url).toContain('access_token=auth_tokens%2Fone');
    expect(socket.url).not.toContain('key=');
    expect(JSON.parse(socket.sent[0]).setup.model).toBe('models/gemini-3.8-live');
    expect(JSON.parse(socket.sent[0]).setup.sessionResumption).toEqual({});
    expect(value.onStatus).toHaveBeenLastCalledWith({
      state: 'listening',
      queuedMs: 0,
      connections: 1,
    });

    socket.receive({
      toolCall: {
        functionCalls: [
          { id: 'call-1', name: 'row_bot_agent_consult', args: { request: 'work' } },
        ],
      },
    });
    expect(value.emit).toHaveBeenCalledWith(
      expect.objectContaining({
        type: 'function_call_ready',
        call_id: 'call-1',
        name: 'row_bot_agent_consult',
      }),
    );
    expect(value.onStatus).toHaveBeenLastCalledWith({
      state: 'thinking',
      queuedMs: 0,
      connections: 1,
    });

    expect(runtime.sendRunEvent('Three things are waiting.', { generation_id: 'run-1' })).toBe(true);
    const response = JSON.parse(socket.sent[socket.sent.length - 1]);
    const answer = response.toolResponse.functionResponses[0];
    expect(answer.id).toBe('call-1');
    expect(answer.response.result).toBe('Three things are waiting.');
    expect(answer.scheduling).toBe('WHEN_IDLE');
  });

  it('keeps the newest call when a cancel clears the stale consult', async () => {
    const { runtime, socket } = await connected();
    socket.receive({
      toolCall: { functionCalls: [{ id: 'call-1', name: 'row_bot_agent_consult', args: {} }] },
    });
    runtime.sendFunctionOutput(
      'call-1',
      JSON.stringify({ control: 'cancel', status: 'cancelled', speakable: 'Stopped.' }),
    );
    const stopped = JSON.parse(socket.sent[socket.sent.length - 1]);
    expect(stopped.toolResponse.functionResponses[0].response.result).toBe('Stopped.');

    socket.receive({
      toolCall: { functionCalls: [{ id: 'call-2', name: 'row_bot_agent_consult', args: {} }] },
    });
    runtime.sendRunEvent('It is in My work for your OK.');
    const answer = JSON.parse(socket.sent[socket.sent.length - 1]);
    expect(answer.toolResponse.functionResponses[0].id).toBe('call-2');
    expect(answer.toolResponse.functionResponses[0].scheduling).toBe('WHEN_IDLE');
  });

  it('stops playback and clears the queue the moment the voice is interrupted', async () => {
    const { socket, value } = await connected();
    socket.receive({
      serverContent: { modelTurn: { parts: [{ inlineData: { data: pcm() } }] } },
    });
    expect(value.onStatus).toHaveBeenLastCalledWith(
      expect.objectContaining({ state: 'speaking' }),
    );
    const source = FakeContext.last!.sources[0];
    socket.receive({ serverContent: { interrupted: true } });
    expect(source.stop).toHaveBeenCalled();
    expect(value.onStatus).toHaveBeenLastCalledWith({
      state: 'listening',
      queuedMs: 0,
      connections: 1,
    });
    expect(value.emit).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'barge_in_cancelled' }),
    );
  });

  it('reports the speech window and one user transcript per turn', async () => {
    vi.useFakeTimers();
    const { socket, value } = await connected();
    socket.receive({ voiceActivity: { type: 'ACTIVITY_START' } });
    socket.receive({
      serverContent: { inputTranscription: { text: 'What is waiting on me?' } },
    });
    socket.receive({ voiceActivity: { type: 'ACTIVITY_END' } });
    await vi.advanceTimersByTimeAsync(500);
    expect(value.emit).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'speech_started' }),
    );
    expect(value.emit).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'speech_stopped' }),
    );
    expect(value.emit).toHaveBeenCalledWith(
      expect.objectContaining({
        type: 'transcript_final',
        text: 'What is waiting on me?',
      }),
    );
    expect(value.onTranscript).toHaveBeenCalledWith(
      'user',
      'What is waiting on me?',
    );
  });

  it('reports one final assistant caption for the whole answer', async () => {
    const { socket, value } = await connected();
    socket.receive({
      serverContent: { outputTranscription: { text: 'Three things are ' } },
    });
    socket.receive({
      serverContent: { outputTranscription: { text: 'waiting on you.' } },
    });
    socket.receive({ serverContent: { turnComplete: true } });
    expect(value.onTranscript).toHaveBeenCalledTimes(1);
    expect(value.onTranscript).toHaveBeenCalledWith(
      'assistant',
      'Three things are waiting on you.',
    );
  });

  it('reconnects on its timer with the latest session handle and a fresh token', async () => {
    vi.useFakeTimers();
    const { socket, value } = await connected({ connectionSeconds: 1 });
    socket.receive({ sessionResumptionUpdate: { newHandle: 'handle-1', resumable: true } });
    await vi.advanceTimersByTimeAsync(1000);
    expect(value.requestToken).toHaveBeenCalledTimes(1);
    expect(FakeSocket.instances).toHaveLength(2);
    const next = FakeSocket.instances[1];
    expect(next.url).toContain('auth_tokens%2Ftwo');
    next.open();
    expect(JSON.parse(next.sent[0]).setup.sessionResumption.handle).toBe('handle-1');
    next.receive({ setupComplete: {} });
    expect(value.onStatus).toHaveBeenLastCalledWith({
      state: 'listening',
      queuedMs: 0,
      connections: 2,
    });
  });
});

