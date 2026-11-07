import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import ConversationVoice from './ConversationVoice';
import type { ClientController } from '../../api/controller';
import type { TalkControlsProps } from './TalkControls';
import type { RealtimeTalkControlsProps } from './RealtimeTalkControls';

let talk: TalkControlsProps;
let realtime: RealtimeTalkControlsProps;
vi.mock('./TalkControls', () => ({
  default: (props: TalkControlsProps) => {
    talk = props;
    return <span>Local voice controls</span>;
  },
}));
vi.mock('./RealtimeTalkControls', () => ({
  default: (props: RealtimeTalkControlsProps) => {
    realtime = props;
    return <span>Realtime voice controls</span>;
  },
}));
const handle = {
  conversation_id: 'conversation-a',
  server_epoch: 'epoch',
  voice_session_id: 1,
  lease_id: '11111111-1111-4111-8111-111111111111',
};
const scope = {
  conversationId: 'conversation-a',
  clientSessionId: 'session',
  serverEpoch: 'epoch',
  selectionKey: '1',
};

function setup(provider = 'gemini_live') {
  const controller = {
    startTalk: vi.fn().mockResolvedValue({ handle, run_id: null }),
    startRealtime: vi
      .fn()
      .mockResolvedValue({ snapshot: { handle, run_id: null } }),
    voiceRun: vi.fn(),
    voiceControl: vi.fn(),
    transcribeTalk: vi.fn(),
    talkOutput: vi.fn(),
    realtimeExchange: vi.fn(),
    realtimeEvent: vi.fn(),
    realtimeToken: vi.fn().mockResolvedValue({ snapshot: { handle } }),
    settingsSnapshot: vi.fn().mockResolvedValue({
      voice: { runtime: { talk_provider: provider } },
    }),
  };
  const props = {
    controller: controller as unknown as ClientController,
    scope,
    available: true,
    disabled: false,
    running: false,
    context: {
      conversation_revision: '2',
      model_selection: {
        provider_id: 'fixture',
        model_ref: 'model:fixture:chat',
      },
      write_targets: [
        {
          kind: 'artifact' as const,
          binding_id: 'binding',
          resource_id: 'artifact',
          binding_revision: '3',
          resource_revision: '4',
        },
      ],
    },
    targets: ['Project sketch'],
    onBusy: vi.fn(),
  };
  const view = render(<ConversationVoice {...props} />);
  return { controller, props, view };
}
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

it('shows one live Talk control for a cloud voice, with no mode select', async () => {
  setup('gemini_live');
  await waitFor(() => expect(realtime).toBeTruthy());
  expect(realtime.voice).toBe('gemini_live');
  expect(realtime.available).toBe(true);
  expect(
    screen.queryByRole('combobox', { name: 'Talk mode' }),
  ).not.toBeInTheDocument();
  expect(screen.queryByText('Local voice controls')).not.toBeInTheDocument();
});

it('routes a Local pick to the unchanged local Talk, opened by the same button', async () => {
  setup('local');
  const button = await screen.findByRole('button', { name: 'Talk' });
  fireEvent.click(button);
  expect(await screen.findByText('Local voice controls')).toBeVisible();
  expect(talk.available).toBe(true);
});

it('starts a cloud Talk through normal admission with the captured targets', async () => {
  const { controller, props } = setup('openai_realtime');
  await waitFor(() => expect(realtime.voice).toBe('openai_realtime'));
  const signal = new AbortController().signal;
  await act(async () => {
    realtime.onBusy?.(true);
    await realtime.start('request', signal);
  });
  expect(controller.startRealtime).toHaveBeenCalledWith(
    scope,
    { ...props.context, request_id: 'request' },
    signal,
  );
});

it('asks the selected controller for each resumed Gemini token', async () => {
  const { controller } = setup('gemini_live');
  await waitFor(() => expect(realtime).toBeTruthy());
  const signal = new AbortController().signal;
  await realtime.token!(handle, signal);
  expect(controller.realtimeToken).toHaveBeenCalledWith(scope, handle, signal);
});

it('polls one exact realtime run at a time and stops after its saved output', async () => {
  vi.useFakeTimers();
  const { controller } = setup('gemini_live');
  await act(async () => {
    await Promise.resolve();
  });
  let resolve!: (value: unknown) => void;
  controller.voiceRun.mockImplementationOnce(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  await act(async () => {
    await realtime.start('request', new AbortController().signal);
  });
  await act(async () => {
    realtime.onRun?.('run');
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1500);
  });
  expect(controller.voiceRun).toHaveBeenCalledTimes(1);
  expect(controller.voiceRun.mock.calls[0][1]).toBe('realtime');
  await act(async () => {
    resolve({
      handle,
      run_id: 'run',
      state: 'completed',
      output_id: 'output',
      text: 'Saved result.',
    });
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5000);
  });
  expect(controller.voiceRun).toHaveBeenCalledTimes(1);
});

it('local Talk still runs on the local voice path', async () => {
  const { controller, props } = setup('local');
  fireEvent.click(await screen.findByRole('button', { name: 'Talk' }));
  const signal = new AbortController().signal;
  await act(async () => {
    talk.onBusy?.(true);
    await talk.start('request', signal);
  });
  expect(controller.startTalk).toHaveBeenCalledWith(
    scope,
    { ...props.context, request_id: 'request' },
    signal,
  );
});
