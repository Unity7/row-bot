/* Gemini Live browser runtime for Row-Bot's Talk.
 *
 * The server mints the one-use auth token and owns Row-Bot's agent; this module
 * only carries the wire: 16 kHz microphone PCM in, 24 kHz model PCM out, the
 * three hand-off tool calls to the page, session resumption and one reconnect
 * per connection window.  It never sees the Google API key.
 */
const WS_BASE = 'wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContentConstrained';
const MODEL = 'models/gemini-3.8-live';
const INPUT_RATE = 16000;
const OUTPUT_RATE = 24000;
const HANDOVER = new Set(['row_bot_agent_consult', 'row_bot_agent_control', 'wait_for_user']);

function base64(bytes) {
  let text = '';
  for (let i = 0; i < bytes.length; i += 0x8000) {
    text += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return btoa(text);
}

function decodePcm16(text) {
  const binary = atob(String(text || ''));
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  const samples = new Float32Array(Math.floor(bytes.length / 2));
  const view = new DataView(bytes.buffer);
  for (let i = 0; i < samples.length; i += 1) samples[i] = view.getInt16(i * 2, true) / 32768;
  return samples;
}

function encodePcm16(samples, fromRate) {
  const ratio = fromRate / INPUT_RATE;
  const length = Math.max(1, Math.floor(samples.length / ratio));
  const bytes = new Uint8Array(length * 2);
  const view = new DataView(bytes.buffer);
  for (let i = 0; i < length; i += 1) {
    const at = i * ratio;
    const low = Math.floor(at);
    const high = Math.min(samples.length - 1, low + 1);
    const value = samples[low] + (samples[high] - samples[low]) * (at - low);
    view.setInt16(i * 2, Math.max(-1, Math.min(1, value)) * 32767, true);
  }
  return bytes;
}

function safeId(value, fallback) {
  const text = String(value || fallback || '');
  return /^[A-Za-z0-9:_-]{1,128}$/.test(text) ? text : fallback;
}

export async function startGeminiLiveRuntime(options) {
  const activation = {};
  window.RowBotGeminiActivation = activation;
  let generationId = options.generationId || '';
  let stopped = false;
  let muted = false;
  let connecting = false;
  let reconnecting = false;
  let reconnectDue = false;
  let connectionSeconds = Math.max(0, Number(options.connectionSeconds) || 0);
  let connections = 0;
  let latestHandle = '';
  let failures = 0;
  let userTurn = 0;
  let answerTurn = 0;
  let userText = '';
  let userEnded = false;
  let userFlush = null;
  let answerText = '';
  let answerActive = false;
  let outputSent = false;
  let socket = null;
  let ready = false;
  let socketTimer = null;
  let reconnectTimer = null;
  const pendingCalls = [];
  const sources = [];
  let ctx = null;
  let micStream = null;
  let micNode = null;
  let micSink = null;
  let nextStart = 0;
  let queueTimer = null;

  const isCurrent = () => !stopped && window.RowBotGeminiActivation === activation &&
    (!options.current || options.current());

  function emit(type, detail) {
    if (!isCurrent()) return;
    options.emit(Object.assign({type, generation_id: generationId}, detail || {}));
  }

  function status(state, queuedMs) {
    if (!isCurrent()) return;
    if (options.onStatus) {
      options.onStatus({state, queuedMs: Math.max(0, Math.round(queuedMs || 0)), connections});
    }
  }

  function pendingConsult() {
    return pendingCalls.some((call) => call.name === 'row_bot_agent_consult');
  }

  function queuedMs() {
    if (!ctx || !sources.length) return 0;
    return Math.max(0, (nextStart - ctx.currentTime) * 1000);
  }

  function playing() {
    return sources.length > 0 && ctx && nextStart > ctx.currentTime + 0.02;
  }

  function idleState() {
    return pendingConsult() ? 'thinking' : 'listening';
  }

  function stopPlayback() {
    for (const source of sources.splice(0)) {
      try { source.stop(); } catch (_) { /* already ended */ }
    }
    nextStart = 0;
    if (queueTimer) clearInterval(queueTimer);
    queueTimer = null;
  }

  function schedule(samples) {
    if (!ctx || !isCurrent()) return;
    try {
      ctx.resume().catch(() => undefined);
      const buffer = ctx.createBuffer(1, samples.length, OUTPUT_RATE);
      buffer.copyToChannel(samples, 0);
      const source = ctx.createBufferSource();
      source.buffer = buffer;
      source.connect(ctx.destination);
      const at = Math.max(ctx.currentTime + 0.02, nextStart || 0);
      source.start(at);
      nextStart = at + buffer.duration;
      sources.push(source);
      source.onended = () => {
        const index = sources.indexOf(source);
        if (index >= 0) sources.splice(index, 1);
        if (!sources.length) nextStart = 0;
      };
      if (!outputSent) {
        outputSent = true;
        emit('output_started', {response_id: `answer-${answerTurn}`, output_item_id: `answer-${answerTurn}`});
      }
      status('speaking', queuedMs());
      if (!queueTimer) {
        queueTimer = setInterval(() => {
          if (!isCurrent()) return;
          if (sources.length) {
            status('speaking', queuedMs());
            return;
          }
          clearInterval(queueTimer);
          queueTimer = null;
          status(idleState(), 0);
          if (reconnectDue) void maybeReconnect();
        }, 200);
      }
    } catch (_) {
      /* a closed audio context after Stop */
    }
  }

  function flushUser() {
    if (!userEnded) return;
    const text = userText.trim();
    userText = '';
    userEnded = false;
    if (userFlush) { clearTimeout(userFlush); userFlush = null; }
    if (!text) return;
    emit('transcript_final', {text, item_id: `user-${userTurn}`});
    if (options.onTranscript) options.onTranscript('user', text);
  }

  function flushUserSoon() {
    if (!userEnded) return;
    if (userFlush) clearTimeout(userFlush);
    userFlush = setTimeout(() => { userFlush = null; if (isCurrent()) flushUser(); }, 400);
  }

  function startAnswer() {
    if (answerActive) return;
    answerActive = true;
    answerTurn += 1;
    answerText = '';
    outputSent = false;
  }

  function flushAnswer() {
    const text = answerText.trim();
    answerText = '';
    answerActive = false;
    if (!text) return;
    emit('assistant_transcript_final', {
      text, response_id: `answer-${answerTurn}`, output_item_id: `answer-${answerTurn}`,
    });
    if (options.onTranscript) options.onTranscript('assistant', text);
  }

  function send(payload) {
    const current = socket;
    if (!current || current.readyState !== WebSocket.OPEN) return false;
    try {
      current.send(JSON.stringify(payload));
      return true;
    } catch (_) {
      return false;
    }
  }

  function sendResponse(callId, text, silent) {
    if (!callId) return false;
    const call = pendingCalls.find((entry) => entry.id === callId);
    let result = String(text || '');
    try {
      const parsed = JSON.parse(text);
      if (parsed && typeof parsed === 'object') {
        result = String(parsed.speakable || parsed.result || text || '');
        if (parsed.control === 'cancel' || ['cancelled', 'stopped'].includes(String(parsed.status))) {
          pendingCalls.length = 0;
        }
      }
    } catch (_) { /* the model speaks the plain answer */ }
    const index = pendingCalls.indexOf(call);
    if (index >= 0) pendingCalls.splice(index, 1);
    return send({
      toolResponse: {
        functionResponses: [{
          id: callId,
          name: call ? call.name : '',
          response: {result: silent ? '' : result},
          scheduling: 'WHEN_IDLE',
        }],
      },
    });
  }

  function sendRunEvent(text, meta) {
    if (!isCurrent()) return false;
    const call = pendingCalls.find((entry) => entry.name === 'row_bot_agent_consult');
    if (!call) return false;
    if (meta && typeof meta.generation_id === 'string') generationId = meta.generation_id;
    return sendResponse(call.id, text, Boolean(meta && meta.silent));
  }

  function sendFunctionOutput(callId, output, meta) {
    if (!isCurrent()) return false;
    if (meta && typeof meta.generation_id === 'string') generationId = meta.generation_id;
    return sendResponse(callId, typeof output === 'string' ? output : JSON.stringify(output || {}),
      Boolean(meta && meta.silent));
  }

  function armReconnect() {
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = null;
    if (!connectionSeconds || stopped) return;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      reconnectDue = true;
      void maybeReconnect();
    }, connectionSeconds * 1000);
  }

  function closeSocket() {
    const old = socket;
    socket = null;
    ready = false;
    if (socketTimer) { clearTimeout(socketTimer); socketTimer = null; }
    if (!old) return;
    try { old.close(); } catch (_) { /* already closed */ }
  }

  async function maybeReconnect() {
    if (stopped || connecting || reconnecting || !reconnectDue) return;
    if (playing() || pendingConsult()) return;
    reconnectDue = false;
    reconnecting = true;
    try {
      const next = await options.requestToken();
      const token = String(next && next.token || '');
      if (!token) throw new Error('realtime_credential_unavailable');
      if (next && Number(next.connectionSeconds) > 0) connectionSeconds = Number(next.connectionSeconds);
      closeSocket();
      connecting = false;
      connect(token);
    } catch (error) {
      reconnecting = false;
      connecting = false;
      emit('fatal_error', {message: String((error && (error.code || error.message)) || 'realtime_disconnected')});
    }
  }

  async function openMicrophone() {
    const AudioCtor = window.AudioContext || window.webkitAudioContext;
    ctx = new AudioCtor();
    await ctx.resume().catch(() => undefined);
    micStream = await navigator.mediaDevices.getUserMedia({audio: true});
    if (!isCurrent()) return;
    const source = ctx.createMediaStreamSource(micStream);
    micNode = ctx.createScriptProcessor(4096, 1, 1);
    micSink = ctx.createGain();
    micSink.gain.value = 0;
    micNode.onaudioprocess = (event) => {
      if (!isCurrent() || !ready || muted || !socket || socket.readyState !== WebSocket.OPEN) return;
      const pcm = encodePcm16(event.inputBuffer.getChannelData(0), ctx.sampleRate);
      send({realtimeInput: {audio: {data: base64(pcm), mimeType: `audio/pcm;rate=${INPUT_RATE}`}}});
    };
    source.connect(micNode);
    micNode.connect(micSink);
    micSink.connect(ctx.destination);
  }

  function handleVoiceActivity(activity) {
    if (!activity) return;
    const kind = String(activity.type || activity.voiceActivityType || '');
    if (kind === 'ACTIVITY_START' || kind === 'VOICE_ACTIVITY_START') {
      userTurn += 1;
      userText = '';
      userEnded = false;
      emit('speech_started');
      status('listening', 0);
    } else if (kind === 'ACTIVITY_END' || kind === 'VOICE_ACTIVITY_END') {
      userEnded = true;
      emit('speech_stopped');
      flushUserSoon();
    }
  }

  function inbound(payload) {
    if (!isCurrent() || !payload || typeof payload !== 'object') return;
    if (payload.setupComplete) {
      if (socketTimer) { clearTimeout(socketTimer); socketTimer = null; }
      connecting = false;
      reconnecting = false;
      ready = true;
      failures = 0;
      connections += 1;
      emit('connected');
      status('listening', 0);
      armReconnect();
      return;
    }
    if (payload.goAway) {
      reconnectDue = true;
      setTimeout(() => { void maybeReconnect(); }, 1500);
    }
    if (payload.sessionResumptionUpdate) {
      const handle = payload.sessionResumptionUpdate.newHandle;
      if (handle) latestHandle = String(handle);
    }
    handleVoiceActivity(payload.voiceActivity);
    if (payload.toolCall) {
      flushUser();
      if (answerActive) flushAnswer();
      const calls = payload.toolCall.functionCalls || [];
      for (const call of calls) {
        const callId = safeId(call.id, `call-${userTurn}-${pendingCalls.length}`);
        const name = String(call.name || '');
        if (name && !HANDOVER.has(name)) continue;
        pendingCalls.push({id: callId, name});
        emit('function_call_ready', {
          call_id: callId,
          name,
          arguments: JSON.stringify(call.args || {}),
          item_id: `call-${userTurn}`,
        });
      }
      status('thinking', 0);
      return;
    }
    if (payload.toolCallCancellation) {
      for (const callId of payload.toolCallCancellation.ids || []) {
        const index = pendingCalls.findIndex((entry) => entry.id === callId);
        if (index >= 0) pendingCalls.splice(index, 1);
      }
      return;
    }
    const content = payload.serverContent;
    if (!content) return;
    handleVoiceActivity(content.voiceActivity);
    if (content.interrupted) {
      stopPlayback();
      if (answerActive) flushAnswer();
      emit('barge_in_cancelled', {response_id: `answer-${answerTurn}`});
      status(idleState(), 0);
      if (reconnectDue) void maybeReconnect();
      return;
    }
    if (content.inputTranscription && content.inputTranscription.text) {
      userText += content.inputTranscription.text;
      flushUserSoon();
    }
    if (content.outputTranscription && content.outputTranscription.text) {
      startAnswer();
      answerText += content.outputTranscription.text;
    }
    const parts = (content.modelTurn && content.modelTurn.parts) || [];
    for (const part of parts) {
      if (part.inlineData && part.inlineData.data) {
        flushUser();
        startAnswer();
        schedule(decodePcm16(part.inlineData.data));
      }
    }
    if (content.turnComplete) {
      flushUser();
      if (answerActive) flushAnswer();
      if (outputSent) emit('output_audio_done', {response_id: `answer-${answerTurn}`});
      outputSent = false;
      // The model has finished generating, but the browser is still playing
      // queued audio: only call it listening once the queue is actually empty.
      status(playing() ? 'speaking' : idleState(), playing() ? queuedMs() : 0);
      if (!playing() && reconnectDue) void maybeReconnect();
    }
  }

  function connect(token) {
    connecting = true;
    ready = false;
    status('connecting', 0);
    const url = `${WS_BASE}?access_token=${encodeURIComponent(token)}`;
    let ws;
    try {
      ws = new WebSocket(url);
    } catch (error) {
      connecting = false;
      reconnecting = false;
      emit('fatal_error', {message: String((error && error.message) || 'realtime_connection_failed')});
      return;
    }
    socket = ws;
    socketTimer = setTimeout(() => {
      socketTimer = null;
      if (socket === ws && connecting) {
        connecting = false;
        reconnecting = false;
        emit('fatal_error', {message: 'realtime_connection_failed'});
      }
    }, 20000);
    ws.addEventListener('open', () => {
      if (!isCurrent() || socket !== ws) return;
      const setup = {model: MODEL, generationConfig: {responseModalities: ['AUDIO']}};
      setup.sessionResumption = latestHandle ? {handle: latestHandle} : {};
      try { ws.send(JSON.stringify({setup})); } catch (_) { /* the close below reports it */ }
    });
    ws.addEventListener('message', (event) => {
      if (!isCurrent() || socket !== ws) return;
      const data = event.data;
      if (typeof data === 'string') {
        try { inbound(JSON.parse(data)); } catch (_) { /* one malformed frame */ }
      } else if (data && typeof data.text === 'function') {
        data.text().then((text) => {
          if (!isCurrent() || socket !== ws) return;
          try { inbound(JSON.parse(text)); } catch (_) { /* one malformed frame */ }
        }).catch(() => undefined);
      } else if (data instanceof ArrayBuffer) {
        try { inbound(JSON.parse(new TextDecoder().decode(data))); } catch (_) { /* one malformed frame */ }
      }
    });
    ws.addEventListener('close', () => {
      if (socket !== ws) return;
      connecting = false;
      reconnecting = false;
      ready = false;
      socket = null;
      if (stopped || !isCurrent()) return;
      failures += 1;
      if (failures > 3) {
        emit('fatal_error', {message: 'realtime_disconnected'});
        return;
      }
      reconnectDue = true;
      setTimeout(() => { void maybeReconnect(); }, 600);
    });
    ws.addEventListener('error', () => {
      if (socket === ws && !stopped) emit('fatal_error', {message: 'realtime_connection_failed'});
    });
  }

  const runtime = {
    session: null,
    currentGeneration: () => generationId,
    setGeneration(expectedSession, expectedThread, expectedGeneration, nextGeneration) {
      if (!isCurrent() || typeof nextGeneration !== 'string' || nextGeneration.length > 128) return false;
      generationId = nextGeneration;
      return true;
    },
    mute(value) {
      muted = Boolean(value);
    },
    sendFunctionOutput,
    sendRunEvent,
    async stop() {
      stopped = true;
      window.RowBotGeminiActivation = null;
      reconnectDue = false;
      reconnecting = false;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (userFlush) clearTimeout(userFlush);
      stopPlayback();
      closeSocket();
      try { micNode && micNode.disconnect(); } catch (_) { /* closed */ }
      try { micSink && micSink.disconnect(); } catch (_) { /* closed */ }
      try { (micStream && micStream.getTracks() || []).forEach((track) => track.stop()); } catch (_) { /* closed */ }
      try { ctx && ctx.close(); } catch (_) { /* closed */ }
      ctx = null;
      micStream = null;
      micNode = null;
      micSink = null;
      pendingCalls.length = 0;
      status('idle', 0);
    },
  };
  runtime.session = {audio: {muted}};
  if (options.onRuntime) options.onRuntime(runtime);

  try {
    // The socket opens while the microphone is prepared: the token is already
    // minted, so both halves of a sub-3 s start run in parallel.
    connect(String(options.token || ''));
    await openMicrophone();
    if (!isCurrent()) { await runtime.stop(); return runtime; }
  } catch (error) {
    emit('fatal_error', {message: String((error && error.message) || 'realtime_connection_failed')});
    await runtime.stop();
  }
  return runtime;
}
