import { HttpErrorResponse } from '@angular/common/http';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { Subject, of, throwError } from 'rxjs';

import { AuthService } from '../../core/auth/auth.service';
import { ChatApiService } from '../../core/chat/chat-api.service';
import { ChatResponse } from '../../core/chat/chat.models';
import { I18nService } from '../../core/i18n/i18n.service';
import { MESSAGES } from '../../core/i18n/messages';
import { ChatPage } from './chat.page';

const CONVERSATION = '33333333-3333-3333-3333-333333333333';
const CONFIRMATION_ID = '44444444-4444-4444-4444-444444444444';

function awaiting(): ChatResponse {
  return {
    conversation_id: CONVERSATION,
    message_id: null,
    status: 'awaiting_confirmation',
    content: 'I can cancel order #123 for you.',
    language: 'en',
    citations: [],
    confirmation: {
      id: CONFIRMATION_ID,
      tool: 'cancel_order',
      target: '#123',
      amount_cents: null,
      currency: null,
    },
    trace_id: 'trace-1',
  };
}

function completed(content: string): ChatResponse {
  return {
    conversation_id: CONVERSATION,
    message_id: 7,
    status: 'completed',
    content,
    language: 'en',
    citations: [],
    confirmation: null,
    trace_id: 'trace-2',
  };
}

function apiError(status: number, code: string): HttpErrorResponse {
  return new HttpErrorResponse({
    status,
    error: { error: { code, message: 'x', trace_id: 't' } },
  });
}

describe('ChatPage confirmation flow', () => {
  let fixture: ComponentFixture<ChatPage>;
  let host: HTMLElement;
  let send: ReturnType<typeof vi.fn>;
  let confirm: ReturnType<typeof vi.fn>;

  async function render(): Promise<void> {
    fixture.detectChanges();
    await fixture.whenStable();
  }

  function input(): HTMLTextAreaElement {
    const el = host.querySelector<HTMLTextAreaElement>('#chat-input');
    if (!el) throw new Error('missing #chat-input');
    return el;
  }

  function submitButton(): HTMLButtonElement {
    const el = host.querySelector<HTMLButtonElement>('button[type="submit"]');
    if (!el) throw new Error('missing submit button');
    return el;
  }

  function decisionButton(decision: 'confirm' | 'refuse'): HTMLButtonElement {
    const el = host.querySelector<HTMLButtonElement>(`button[data-decision="${decision}"]`);
    if (!el) throw new Error(`missing ${decision} button`);
    return el;
  }

  async function sendMessage(text: string): Promise<void> {
    const el = input();
    el.value = text;
    el.dispatchEvent(new Event('input'));
    host.querySelector('form')?.dispatchEvent(new Event('submit', { cancelable: true }));
    await render();
  }

  beforeEach(async () => {
    localStorage.clear();
    send = vi.fn(() => of(awaiting()));
    confirm = vi.fn(() => of(completed('Order #123 was cancelled.')));
    await TestBed.configureTestingModule({
      imports: [ChatPage],
      providers: [
        provideRouter([]),
        { provide: ChatApiService, useValue: { send, confirm } },
        { provide: AuthService, useValue: { logout: vi.fn(() => of(undefined)), clear: vi.fn() } },
      ],
    }).compileComponents();

    TestBed.inject(I18nService).setLang('en');
    vi.spyOn(TestBed.inject(Router), 'navigateByUrl').mockResolvedValue(true);

    fixture = TestBed.createComponent(ChatPage);
    host = fixture.nativeElement as HTMLElement;
    await render();
  });

  afterEach(() => localStorage.clear());

  it('shows the confirmation card and locks the composer while awaiting a decision', async () => {
    await sendMessage('Cancel order 123');

    const card = host.querySelector('.confirm-card');
    expect(card).not.toBeNull();
    expect(card?.textContent).toContain('Confirm: cancel order #123?');
    expect(host.querySelector('[role="log"]')?.textContent).toContain(
      'I can cancel order #123 for you.',
    );
    expect(input().disabled).toBe(true);
    expect(submitButton().disabled).toBe(true);
    expect(document.activeElement).toBe(card);

    // Enter in the (disabled) field must not send anything either.
    input().dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', cancelable: true }));
    await render();
    expect(send).toHaveBeenCalledTimes(1);
  });

  it('confirms through the API, renders the next reply and removes the card', async () => {
    await sendMessage('Cancel order 123');
    decisionButton('confirm').click();
    await render();

    expect(confirm).toHaveBeenCalledWith(CONFIRMATION_ID, 'confirm');
    expect(host.querySelector('.confirm-card')).toBeNull();
    const messages = host.querySelectorAll('.chat-message--assistant');
    expect(messages[messages.length - 1]?.textContent).toContain('Order #123 was cancelled.');
    expect(input().disabled).toBe(false);
    expect(submitButton().disabled).toBe(false);
    expect(document.activeElement).toBe(input());
  });

  it('refuses through the API', async () => {
    confirm.mockReturnValue(of(completed('Okay, I did not cancel it.')));
    await sendMessage('Cancel order 123');
    decisionButton('refuse').click();
    await render();

    expect(confirm).toHaveBeenCalledWith(CONFIRMATION_ID, 'refuse');
    expect(host.querySelector('[role="log"]')?.textContent).toContain('Okay, I did not cancel it.');
    expect(host.querySelector('.confirm-card')).toBeNull();
  });

  it('shows the sending state and disables the card buttons while the decision is in flight', async () => {
    const pending = new Subject<ChatResponse>();
    confirm.mockReturnValue(pending);
    await sendMessage('Cancel order 123');
    decisionButton('confirm').click();
    await render();

    expect(host.textContent).toContain(MESSAGES.en['confirm.sending']);
    expect(decisionButton('confirm').disabled).toBe(true);
    expect(decisionButton('refuse').disabled).toBe(true);
    decisionButton('confirm').click();
    expect(confirm).toHaveBeenCalledTimes(1);

    pending.next(completed('Done.'));
    pending.complete();
    await render();
    expect(host.querySelector('.confirm-card')).toBeNull();
  });

  it('carries the conversation forward after the decision', async () => {
    await sendMessage('Cancel order 123');
    decisionButton('confirm').click();
    await render();

    send.mockReturnValue(of(completed('Anything else?')));
    await sendMessage('Thanks');
    expect(send).toHaveBeenLastCalledWith({
      message: 'Thanks',
      conversation_id: CONVERSATION,
      ui_language: 'en',
    });
  });

  it.each([
    [404, 'CONFIRMATION_NOT_FOUND'],
    [409, 'CONFIRMATION_NOT_PENDING'],
  ])('shows the error banner and frees the composer on %s %s', async (status, code) => {
    confirm.mockReturnValue(throwError(() => apiError(status, code)));
    await sendMessage('Cancel order 123');
    decisionButton('confirm').click();
    await render();

    expect(host.querySelector('[role="alert"]')?.textContent).toContain(
      MESSAGES.en[`errors.${code}` as keyof typeof MESSAGES.en],
    );
    expect(host.querySelector('.confirm-card')).toBeNull();
    expect(input().disabled).toBe(false);
    expect(submitButton().disabled).toBe(false);
  });

  it('keeps the card on 502 LLM_UNAVAILABLE so the customer can retry', async () => {
    confirm.mockReturnValueOnce(throwError(() => apiError(502, 'LLM_UNAVAILABLE')));
    await sendMessage('Cancel order 123');
    decisionButton('confirm').click();
    await render();

    expect(host.querySelector('[role="alert"]')?.textContent).toContain(
      MESSAGES.en['errors.LLM_UNAVAILABLE'],
    );
    expect(host.querySelector('.confirm-card')).not.toBeNull();
    expect(decisionButton('confirm').disabled).toBe(false);
    expect(decisionButton('refuse').disabled).toBe(false);
    expect(input().disabled).toBe(true);
    expect(submitButton().disabled).toBe(true);

    decisionButton('confirm').click();
    await render();

    expect(confirm).toHaveBeenCalledTimes(2);
    expect(confirm).toHaveBeenLastCalledWith(CONFIRMATION_ID, 'confirm');
    expect(host.querySelector('.confirm-card')).toBeNull();
    expect(host.querySelector('[role="alert"]')).toBeNull();
    expect(host.querySelector('[role="log"]')?.textContent).toContain('Order #123 was cancelled.');
    expect(input().disabled).toBe(false);
  });

  it('keeps the card when the server cannot be reached', async () => {
    confirm.mockReturnValueOnce(
      throwError(() => new HttpErrorResponse({ status: 0, error: new ProgressEvent('error') })),
    );
    await sendMessage('Cancel order 123');
    decisionButton('refuse').click();
    await render();

    expect(host.querySelector('[role="alert"]')?.textContent).toContain(
      MESSAGES.en['errors.NETWORK'],
    );
    expect(host.querySelector('.confirm-card')).not.toBeNull();
    expect(decisionButton('refuse').disabled).toBe(false);
    expect(input().disabled).toBe(true);
  });

  it('drops the card and goes to /login when the session expired', async () => {
    confirm.mockReturnValue(throwError(() => apiError(401, 'AUTH_REQUIRED')));
    await sendMessage('Cancel order 123');
    decisionButton('confirm').click();
    await render();

    expect(host.querySelector('.confirm-card')).toBeNull();
    expect(TestBed.inject(Router).navigateByUrl).toHaveBeenCalledWith('/login');
  });
});
