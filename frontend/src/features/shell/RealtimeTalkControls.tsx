import { useEffect, useRef, useState } from 'react';
import { Button } from '../../ui/primitives';
import type { DictationHandle } from '../../api/types';
import type { DictationScope } from './VoiceControls';
import {
  openRealtimeSession,
  type RealtimeEvent,
  type RealtimeEventResult,
  type RealtimeOutput,
  type RealtimeSession,
  type RealtimeSnapshot,
  type RealtimeStart,
  type RealtimeStatus,
  type RealtimeTranscriptRole,
} from '../../api/voice_realtime';

export type RealtimeTalkControlsProps = {
  scope: DictationScope;
  /** The saved Talk voice, shown on the controls for the proof and for people. */
  voice?: string;
  available: boolean;
  disabled?: boolean;
  start(requestId: string, signal: AbortSignal): Promise<RealtimeStart>;
  /** A fresh one-use token for each later Gemini connection. */
  token?(handle: DictationHandle, signal: AbortSignal): Promise<RealtimeStart>;
  exchange?(
    handle: DictationHandle,
    sdp: string,
    signal: AbortSignal,
  ): Promise<string>;
  stop(handle: DictationHandle): Promise<RealtimeSnapshot>;
  heartbeat(
    handle: DictationHandle,
    signal: AbortSignal,
  ): Promise<RealtimeSnapshot>;
  event(
    handle: DictationHandle,
    event: RealtimeEvent,
    signal: AbortSignal,
  ): Promise<RealtimeEventResult>;
  output?: RealtimeOutput;
  onBusy?(busy: boolean): void;
  onRun?(runId: string): void;
};
type Attempt = {
  scope: DictationScope;
  stopped: boolean;
  abort: AbortController;
  stop: RealtimeTalkControlsProps['stop'];
  handle?: DictationHandle;
  session?: RealtimeSession;
  heartbeat?: ReturnType<typeof setInterval>;
  expiry?: ReturnType<typeof setTimeout>;
  polling: boolean;
  runId?: string;
  gemini: boolean;
};
type Caption = { role: RealtimeTranscriptRole; text: string };

function dispose(a: Attempt) {
  a.stopped = true;
  a.abort.abort();
  clearInterval(a.heartbeat);
  clearTimeout(a.expiry);
  if (a.session) void a.session.stop();
}

function failureMessage(code: unknown) {
  if (code === 'realtime_auth_unavailable')
    return 'Talk stopped: the voice provider refused the key. Check the key in Providers.';
  if (code === 'realtime_quota_or_rate_limit')
    return 'Talk stopped: the voice provider hit its quota or rate limit.';
  return 'Realtime Talk stopped. Check voice readiness and the conversation before retrying.';
}

const STATES = ['idle', 'connecting', 'listening', 'thinking', 'speaking', 'stopping'];

function shownState(value: string) {
  if (STATES.includes(value)) return value;
  if (value === 'interrupted' || value === 'connected') return 'listening';
  if (value === 'stopped' || value === 'stopping') return 'stopping';
  return 'connecting';
}

export default function RealtimeTalkControls(props: RealtimeTalkControlsProps) {
  const [state, setState] = useState('idle');
  const [message, setMessage] = useState('');
  const [caption, setCaption] = useState('');
  const [captions, setCaptions] = useState(true);
  const [speech, setSpeech] = useState(true);
  const [history, setHistory] = useState<Caption[]>([]);
  const [expiryMs, setExpiryMs] = useState<number>();
  const [sessionId, setSessionId] = useState('');
  const [connections, setConnections] = useState(0);
  const [queuedMs, setQueuedMs] = useState(0);
  const current = useRef<Attempt | null>(null);
  const latest = useRef(props);
  latest.current = props;
  const muted = useRef(false);
  const gemini = useRef(false);

  useEffect(() => {
    setState('idle');
    setMessage('');
    setCaption('');
    setHistory([]);
    setConnections(0);
    setQueuedMs(0);
    setSessionId('');
    gemini.current = false;
    return () => {
      const a = current.current;
      current.current = null;
      if (a) {
        dispose(a);
        latest.current.onBusy?.(false);
        if (a.handle) void a.stop(a.handle).catch(() => undefined);
      }
    };
  }, [
    props.scope.conversationId,
    props.scope.clientSessionId,
    props.scope.serverEpoch,
    props.scope.selectionKey,
    props.available,
  ]);

  const live = (a: Attempt) => current.current === a && !a.stopped;
  function identity(a: Attempt, handle: DictationHandle) {
    return (
      handle.conversation_id === a.scope.conversationId &&
      handle.server_epoch === a.scope.serverEpoch &&
      (!a.handle ||
        (handle.lease_id === a.handle.lease_id &&
          handle.voice_session_id === a.handle.voice_session_id))
    );
  }

  async function stop(a: Attempt, reason = '') {
    dispose(a);
    if (current.current === a) {
      setState('stopping');
      if (reason) setMessage(reason);
    }
    if (!a.handle) return;
    try {
      const result = await a.stop(a.handle);
      if (current.current !== a) return;
      if (!identity(a, result.handle))
        throw new Error('voice_identity_changed');
      if (result.quiesced) {
        current.current = null;
        setState('idle');
        setQueuedMs(0);
        latest.current.onBusy?.(false);
      }
    } catch {
      if (current.current === a)
        setMessage(
          'Voice is stopping. Check its status before starting again.',
        );
    }
  }

  function apply(a: Attempt, snapshot: RealtimeSnapshot) {
    if (!live(a)) return;
    if (['stopped', 'stopping', 'error'].includes(snapshot.state)) {
      void stop(a, 'Realtime session ended.');
      return;
    }
    if (
      !identity(a, snapshot.handle) ||
      !Number.isFinite(snapshot.expires_in_ms) ||
      snapshot.expires_in_ms <= 0
    ) {
      void stop(a, 'Realtime session expired.');
      return;
    }
    clearTimeout(a.expiry);
    setExpiryMs(snapshot.expires_in_ms);
    a.expiry = setTimeout(
      () => void stop(a, 'Realtime session expired.'),
      Math.min(snapshot.expires_in_ms, 120_000),
    );
    if (!a.gemini) setState(shownState(snapshot.state));
    if (snapshot.run_id && snapshot.run_id !== a.runId) {
      a.runId = snapshot.run_id;
      latest.current.onRun?.(snapshot.run_id);
    }
  }

  async function start() {
    if (current.current || !props.available || props.disabled) return;
    if (!globalThis.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      setMessage(
        'Realtime Talk needs a secure browser context with microphone support.',
      );
      return;
    }
    const a: Attempt = {
      scope: { ...props.scope },
      stopped: false,
      abort: new AbortController(),
      stop: props.stop,
      polling: false,
      gemini: false,
    };
    current.current = a;
    setState('connecting');
    setMessage('');
    setCaption('');
    setHistory([]);
    setConnections(0);
    setQueuedMs(0);
    props.onBusy?.(true);
    try {
      const result = await props.start(crypto.randomUUID(), a.abort.signal);
      if (!identity(a, result.snapshot.handle))
        throw new Error('voice_identity_changed');
      a.handle = result.snapshot.handle;
      a.gemini = result.snapshot.transport === 'gemini_live';
      gemini.current = a.gemini;
      setSessionId(result.snapshot.handle.lease_id);
      setConnections(1);
      if (!live(a)) {
        await stop(a);
        return;
      }
      apply(a, result.snapshot);
      if (!live(a)) return;
      a.heartbeat = setInterval(() => {
        if (!live(a) || a.polling) return;
        a.polling = true;
        void latest.current
          .heartbeat(a.handle!, a.abort.signal)
          .then((snapshot) => apply(a, snapshot))
          .catch(() => {
            if (live(a))
              void stop(a, 'Voice connection lost. Realtime Talk stopped.');
          })
          .finally(() => {
            a.polling = false;
          });
      }, 30_000);
      const onStatus = (status: RealtimeStatus) => {
        if (!live(a)) return;
        setState(shownState(status.state));
        setConnections(status.connections || 0);
        setQueuedMs(Math.max(0, Math.round(status.queuedMs || 0)));
      };
      await openRealtimeSession({
        start: result,
        token: a.gemini
          ? () => latest.current.token!(a.handle!, a.abort.signal)
          : undefined,
        exchange: props.exchange
          ? (sdp, signal) => props.exchange!(a.handle!, sdp, signal)
          : undefined,
        signal: a.abort.signal,
        current: () => live(a),
        event: (event, signal) =>
          latest.current.event(a.handle!, event, signal),
        onSession: (session) => {
          a.session = session;
          session.mute(muted.current);
          if (!live(a)) void session.stop();
        },
        onSnapshot: (snapshot) => apply(a, snapshot),
        onStatus: a.gemini ? onStatus : undefined,
        onCaption: (text, role) => {
          if (!live(a)) return;
          const safe = role === 'user' ? 'user' : 'assistant';
          setCaption(text);
          setHistory((items) => [...items, { role: safe, text }]);
        },
        onFailure: (code) => {
          if (live(a)) void stop(a, failureMessage(code));
        },
      });
    } catch (error) {
      const code =
        error && typeof error === 'object' && 'code' in error ? error.code : '';
      const reason = failureMessage(code);
      if (current.current === a) {
        if (a.handle) await stop(a, reason);
        else {
          dispose(a);
          current.current = null;
          setState('idle');
          props.onBusy?.(false);
          setMessage(reason);
        }
      }
    }
  }

  useEffect(() => {
    const a = current.current;
    if (a && !a.stopped && props.output) a.session?.sendOutput(props.output);
  }, [props.output, state]);

  const label =
    state === 'idle'
      ? ''
      : state === 'stopping'
        ? 'Stopping…'
        : state === 'speaking'
          ? 'Speaking…'
          : state === 'thinking'
            ? 'Thinking…'
            : state === 'listening'
              ? 'Listening…'
              : 'Connecting…';
  return (
    <div
      className="voice-controls realtime-talk-controls"
      role="group"
      aria-label="Realtime Talk controls"
      aria-busy={state !== 'idle'}
      data-testid="talk-controls"
      data-voice={props.voice || ''}
      data-state={state}
      data-session={sessionId}
      data-connections={connections}
      data-queued-ms={queuedMs}
      data-expires-ms={expiryMs ?? ''}
    >
      {state === 'idle' ? (
        <Button
          data-testid="talk-start"
          disabled={!props.available || props.disabled}
          onClick={() => void start()}
        >
          Talk
        </Button>
      ) : (
        <Button
          data-testid="talk-stop"
          onClick={() => {
            const a = current.current;
            if (a) void stop(a);
          }}
        >
          {state === 'stopping' ? 'Check voice status' : 'Stop Talk'}
        </Button>
      )}
      <Button
        aria-pressed={speech}
        onClick={() => {
          muted.current = speech;
          setSpeech(!speech);
          current.current?.session?.mute(speech);
        }}
      >
        Speech
      </Button>
      <Button aria-pressed={captions} onClick={() => setCaptions(!captions)}>
        Captions
      </Button>
      {label && (
        <span role="status" className="voice-state-reason">
          {label}
        </span>
      )}
      {state === 'idle' && !props.available && (
        <small className="voice-state-reason">
          Realtime Talk is unavailable in this session.
        </small>
      )}
      {captions && history.length > 0 && (
        <ol className="voice-transcript" data-testid="talk-transcript">
          {history.map((entry, index) => (
            <li key={`${entry.role}-${index}`} data-role={entry.role}>
              {entry.text}
            </li>
          ))}
        </ol>
      )}
      {captions && caption && (
        <p className="voice-caption" role="status" aria-label="Voice caption">
          {caption}
        </p>
      )}
      {message && (
        <p role="alert" className="voice-error" data-testid="talk-error">
          {message}
        </p>
      )}
    </div>
  );
}
