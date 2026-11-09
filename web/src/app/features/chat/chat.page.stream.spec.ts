import { HttpErrorResponse } from '@angular/common/http';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { Subject, of, throwError } from 'rxjs';

import { AuthService } from '../../core/auth/auth.service';
import { ChatStreamService } from '../../core/chat/chat-stream.service';
import { DoneStreamEvent, StreamEvent } from '../../core/chat/chat.models';
import { I18nService } from '../../core/i18n/i18n.service';
import { Lang, MESSAGES } from '../../core/i18n/messages';
import { ChatPage } from './chat.page';

const CONVERSATION = '55555555-5555-5555-5555-555555555555';
const CONFIRMATION_ID = '66666666-6666-6666-6666-666666666666';

function done(content: string, extra: Partial<DoneStreamEvent> = {}): DoneStreamEvent {
  return {
    type: 'done',
    status: 'completed',
    message_id: 1,
    conversation_id: CONVERSATION,
    trace_id: 'trace',
    language: 'en',
    content,
    confirmation: null,
    ...extra,
  };
}

describe('ChatPage streaming', () => {
  let fixture: ComponentFixture<ChatPage>;
  let host: HTMLElement;
  let events: Subject<StreamEvent>;
  let stream: ReturnType<typeof vi.fn>;
  let confirmStream: ReturnType<typeof vi.fn>;
  let clear: ReturnType<typeof vi.fn>;
  let router: Router;

  async function render(): Promise<void> {
    fixture.detectChanges();
    await fixture.whenStable();
  }

  async function emit(event: StreamEvent): Promise<void> {
    events.next(event);
    await render();
  }

  async function sendMessage(text: string): Promise<void> {
    const input = host.querySelector<HTMLTextAreaElement>('#chat-input');
    if (!input) throw new Error('missing #chat-input');
    input.value = text;
    input.dispatchEvent(new Event('input'));
    host.querySelector('form')?.dispatchEvent(new Event('submit', { cancelable: true }));
    await render();
  }

  function cancelButton(): HTMLButtonElement | null {
    return host.querySelector<HTMLButtonElement>('button[data-action="cancel"]');
  }

  function lastAssistant(): HTMLElement | null {
    const all = host.querySelectorAll<HTMLElement>('.chat-message--assistant');
    return all.length ? all[all.length - 1] : null;
  }

  async function setLang(lang: Lang): Promise<void> {
    TestBed.inject(I18nService).setLang(lang);
    await render();
  }

  beforeEach(async () => {
    localStorage.clear();
    events = new Subject<StreamEvent>();
    stream = vi.fn(() => events);
    confirmStream = vi.fn(() => of(done('Order #123 was cancelled.')));
    clear = vi.fn();
    await TestBed.configureTestingModule({
      imports: [ChatPage],
      providers: [
        provideRouter([]),
        { provide: ChatStreamService, useValue: { stream, confirmStream } },
        { provide: AuthService, useValue: { logout: vi.fn(() => of(undefined)), clear } },
      ],
    }).compileComponents();

    TestBed.inject(I18nService).setLang('en');
    router = TestBed.inject(Router);
    vi.spyOn(router, 'navigateByUrl').mockResolvedValue(true);

    fixture = TestBed.createComponent(ChatPage);
    host = fixture.nativeElement as HTMLElement;
    await render();
  });

  afterEach(() => localStorage.clear());

  it('sends through the stream and renders tokens incrementally', async () => {
    await sendMessage('Hello');

    expect(stream).toHaveBeenCalledWith({
      message: 'Hello',
      conversation_id: null,
      ui_language: 'en',
    });
    await emit({ type: 'token', text: 'Hi, ' });
    expect(lastAssistant()?.textContent).toContain('Hi,');
    await emit({ type: 'token', text: 'I am Atlas.' });
    expect(lastAssistant()?.textContent).toContain('Hi, I am Atlas.');

    await emit(done('Hi, I am Atlas.'));
    expect(host.querySelectorAll('.chat-message--assistant').length).toBe(1);
  });

  it('shows the translated step label on tool_start{get_order} and removes it on tool_end', async () => {
    await sendMessage('Where is order 123?');
    await emit({ type: 'tool_start', call_id: 'c1', tool: 'get_order', label_key: 'STEP_GET_ORDER' });

    const status = () => host.querySelector('.chat__typing')?.textContent ?? '';
    expect(status()).toContain(MESSAGES.en['step.get_order']);
    expect(MESSAGES.en['step.get_order']).toBe('looking up order…');

    await setLang('pt');
    expect(status()).toContain('consultando pedido…');

    await emit({ type: 'tool_end', call_id: 'c1', tool: 'get_order', ok: true });
    expect(host.textContent).not.toContain('consultando pedido…');
    expect(host.textContent).not.toContain('looking up order…');
  });

  it('falls back to the generic step label for an unknown tool', async () => {
    await sendMessage('Hello');
    await emit({ type: 'tool_start', call_id: 'c9', tool: 'mystery', label_key: 'STEP_GENERIC' });
    expect(host.querySelector('.chat__typing')?.textContent).toContain(MESSAGES.en['step.generic']);
  });

  it('lists the citations under the answer with document, section and page', async () => {
    await sendMessage('What is the refund policy?');
    await emit({ type: 'token', text: 'You have 30 days [1].' });
    await emit({
      type: 'citation',
      index: 1,
      document: 'Refund policy',
      doc_key: 'refund-policy',
      section: 'Deadlines',
      page: 3,
    });
    await emit({
      type: 'citation',
      index: 2,
      document: 'Terms of service',
      doc_key: null,
      section: null,
      page: null,
    });
    await emit(done('You have 30 days [1].'));

    const items = lastAssistant()?.querySelectorAll('.chat-sources li') ?? [];
    expect(items.length).toBe(2);
    expect(items[0].textContent).toContain('Refund policy');
    expect(items[0].textContent).toContain('Deadlines');
    expect(items[0].textContent).toContain('p. 3');
    expect(items[1].textContent).toContain('Terms of service');
  });

  it('shows cancel only while answering; cancelling unsubscribes and keeps the partial', async () => {
    expect(cancelButton()).toBeNull();
    await sendMessage('Tell me a long story');
    await emit({ type: 'token', text: 'Once upon' });
    expect(cancelButton()).not.toBeNull();
    expect(cancelButton()?.textContent).toContain(MESSAGES.en['chat.cancel']);

    cancelButton()?.click();
    await render();

    expect(events.observed).toBe(false);
    events.next({ type: 'token', text: ' a time' });
    await render();

    const message = lastAssistant();
    expect(message?.textContent).toContain('Once upon');
    expect(message?.textContent).not.toContain('a time');
    expect(message?.classList).toContain('chat-message--cancelled');
    expect(message?.textContent).toContain(MESSAGES.en['chat.cancelled']);
    expect(cancelButton()).toBeNull();
    expect(host.querySelector<HTMLButtonElement>('button[type="submit"]')?.disabled).toBe(false);
  });

  it('has no cancel button after done (STR-92)', async () => {
    await sendMessage('Hello');
    await emit({ type: 'token', text: 'Hi' });
    expect(cancelButton()).not.toBeNull();

    await emit(done('Hi there.'));
    expect(cancelButton()).toBeNull();
    expect(lastAssistant()?.textContent).toContain('Hi there.');
    expect(lastAssistant()?.classList).not.toContain('chat-message--cancelled');
  });

  it('keeps the conversation_id from done for the next message', async () => {
    await sendMessage('Hello');
    await emit(done('Hi.'));

    events = new Subject<StreamEvent>();
    stream.mockReturnValue(events);
    await sendMessage('Again');
    expect(stream).toHaveBeenLastCalledWith({
      message: 'Again',
      conversation_id: CONVERSATION,
      ui_language: 'en',
    });
  });

  it('shows the translated error on an SSE error and retries the last question', async () => {
    await sendMessage('Hello');
    await emit({ type: 'token', text: 'Partial' });
    await emit({ type: 'error', code: 'STREAM_ERROR', message: 'boom', trace_id: 't' });

    const message = lastAssistant();
    expect(message?.querySelector('[role="alert"]')?.textContent).toContain(
      MESSAGES.en['chat.error'],
    );
    expect(cancelButton()).toBeNull();

    await setLang('pt');
    expect(lastAssistant()?.textContent).toContain(MESSAGES.pt['chat.error']);
    await setLang('en');

    events = new Subject<StreamEvent>();
    stream.mockReturnValue(events);
    const retry = host.querySelector<HTMLButtonElement>('button[data-action="retry"]');
    expect(retry?.textContent).toContain(MESSAGES.en['chat.retry']);
    retry?.click();
    await render();

    expect(stream).toHaveBeenCalledTimes(2);
    expect(stream).toHaveBeenLastCalledWith({
      message: 'Hello',
      conversation_id: null,
      ui_language: 'en',
    });
    expect(host.querySelector('button[data-action="retry"]')).toBeNull();
  });

  it('shows the translated error when the stream fails after it opened', async () => {
    await sendMessage('Hello');
    await emit({ type: 'token', text: 'Partial' });
    events.error(new HttpErrorResponse({ status: 0, error: new Error('reset') }));
    await render();

    expect(lastAssistant()?.querySelector('[role="alert"]')?.textContent).toContain(
      MESSAGES.en['chat.error'],
    );
    expect(lastAssistant()?.textContent).toContain('Partial');
    expect(host.querySelector('button[data-action="retry"]')).not.toBeNull();
  });

  it('keeps the conversation from start when the first answer is cancelled', async () => {
    await sendMessage('Hello');
    await emit({ type: 'start', conversation_id: CONVERSATION, trace_id: 'trace' });
    await emit({ type: 'token', text: 'Hi' });
    cancelButton()?.click();
    await render();

    events = new Subject<StreamEvent>();
    stream.mockReturnValue(events);
    await sendMessage('Again');
    expect(stream).toHaveBeenLastCalledWith({
      message: 'Again',
      conversation_id: CONVERSATION,
      ui_language: 'en',
    });
  });

  it('keeps the conversation from start when the first answer fails', async () => {
    await sendMessage('Hello');
    await emit({ type: 'start', conversation_id: CONVERSATION, trace_id: 'trace' });
    await emit({ type: 'error', code: 'STREAM_ERROR', message: 'boom', trace_id: 'trace' });

    events = new Subject<StreamEvent>();
    stream.mockReturnValue(events);
    host.querySelector<HTMLButtonElement>('button[data-action="retry"]')?.click();
    await render();
    expect(stream).toHaveBeenLastCalledWith({
      message: 'Hello',
      conversation_id: CONVERSATION,
      ui_language: 'en',
    });
  });

  it('drops the text streamed before a tool call from the final answer', async () => {
    await sendMessage('Where is order 123?');
    await emit({ type: 'token', text: 'Let me check that order.' });
    expect(lastAssistant()?.textContent).toContain('Let me check that order.');

    await emit({ type: 'tool_start', call_id: 'c1', tool: 'get_order', label_key: 'STEP_GET_ORDER' });
    expect(host.textContent).not.toContain('Let me check that order.');

    await emit({ type: 'tool_end', call_id: 'c1', tool: 'get_order', ok: true });
    await emit({ type: 'token', text: 'Order #123 shipped.' });
    expect(lastAssistant()?.textContent).toContain('Order #123 shipped.');
    expect(lastAssistant()?.textContent).not.toContain('Let me check');
  });

  it('follows the expired-session flow on 401', async () => {
    stream.mockReturnValue(
      throwError(
        () =>
          new HttpErrorResponse({
            status: 401,
            error: { error: { code: 'AUTH_REQUIRED', message: 'x', trace_id: 't' } },
          }),
      ),
    );
    await sendMessage('Hello');

    expect(clear).toHaveBeenCalled();
    expect(router.navigateByUrl).toHaveBeenCalledWith('/login');
  });

  it('opens the confirmation card from done and decides through confirmStream', async () => {
    await sendMessage('Cancel order 123');
    await emit({ type: 'token', text: 'I can cancel order #123 for you.' });
    await emit(
      done('I can cancel order #123 for you.', {
        status: 'awaiting_confirmation',
        message_id: null,
        confirmation: {
          id: CONFIRMATION_ID,
          tool: 'cancel_order',
          target: '#123',
          amount_cents: null,
          currency: null,
        },
      }),
    );

    expect(host.querySelector('.confirm-card')).not.toBeNull();
    host.querySelector<HTMLButtonElement>('button[data-decision="confirm"]')?.click();
    await render();

    expect(confirmStream).toHaveBeenCalledWith(CONFIRMATION_ID, 'confirm');
    expect(host.querySelector('.confirm-card')).toBeNull();
    expect(lastAssistant()?.textContent).toContain('Order #123 was cancelled.');
  });
});
