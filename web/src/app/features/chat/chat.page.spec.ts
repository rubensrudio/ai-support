import { HttpErrorResponse } from '@angular/common/http';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { Subject, of, throwError } from 'rxjs';

import { AuthService } from '../../core/auth/auth.service';
import { ChatApiService } from '../../core/chat/chat-api.service';
import { ChatResponse, MAX_MESSAGE_CHARS } from '../../core/chat/chat.models';
import { I18nService } from '../../core/i18n/i18n.service';
import { MESSAGES } from '../../core/i18n/messages';
import { ChatPage } from './chat.page';

const CONVERSATION = '22222222-2222-2222-2222-222222222222';

function reply(content: string): ChatResponse {
  return {
    conversation_id: CONVERSATION,
    message_id: 1,
    status: 'completed',
    content,
    language: 'en',
    citations: [],
    confirmation: null,
    trace_id: 'trace',
  };
}

describe('ChatPage', () => {
  let fixture: ComponentFixture<ChatPage>;
  let host: HTMLElement;
  let send: ReturnType<typeof vi.fn>;
  let logout: ReturnType<typeof vi.fn>;
  let clear: ReturnType<typeof vi.fn>;
  let router: Router;

  async function render(): Promise<void> {
    fixture.detectChanges();
    await fixture.whenStable();
  }

  function type(value: string): void {
    const input = host.querySelector<HTMLTextAreaElement>('#chat-input');
    if (!input) throw new Error('missing #chat-input');
    input.value = value;
    input.dispatchEvent(new Event('input'));
  }

  async function submit(): Promise<void> {
    host.querySelector('form')?.dispatchEvent(new Event('submit', { cancelable: true }));
    await render();
  }

  beforeEach(async () => {
    localStorage.clear();
    send = vi.fn(() => of(reply('Hi, I am Atlas.')));
    logout = vi.fn(() => of(undefined));
    clear = vi.fn();
    await TestBed.configureTestingModule({
      imports: [ChatPage],
      providers: [
        provideRouter([]),
        { provide: ChatApiService, useValue: { send } },
        { provide: AuthService, useValue: { logout, clear } },
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

  it('does not send a whitespace-only message and shows the validation text', async () => {
    type('   \n  ');
    await submit();

    expect(send).not.toHaveBeenCalled();
    expect(host.querySelector('#chat-input-error')?.textContent).toContain(
      MESSAGES.en['chat.validation.empty'],
    );
    expect(host.querySelector('#chat-input')?.getAttribute('aria-invalid')).toBe('true');
  });

  it('blocks messages above the max length and shows the counter', async () => {
    type('a'.repeat(MAX_MESSAGE_CHARS + 1));
    await render();

    expect(host.querySelector('#chat-input-counter')?.textContent).toContain(
      `${MAX_MESSAGE_CHARS + 1} / ${MAX_MESSAGE_CHARS}`,
    );
    expect(host.querySelector('#chat-input-error')?.textContent).toContain(
      MESSAGES.en['chat.validation.tooLong'].replace('{max}', String(MAX_MESSAGE_CHARS)),
    );
    expect(host.querySelector<HTMLButtonElement>('button[type="submit"]')?.disabled).toBe(true);

    await submit();
    expect(send).not.toHaveBeenCalled();
  });

  it('accepts a message of exactly the max length', async () => {
    type('a'.repeat(MAX_MESSAGE_CHARS));
    await submit();
    expect(send).toHaveBeenCalledTimes(1);
  });

  it('shows the assistant reply and sends the received conversation_id next time', async () => {
    type('Hello');
    await submit();

    expect(send).toHaveBeenNthCalledWith(1, {
      message: 'Hello',
      conversation_id: null,
      ui_language: 'en',
    });
    const thread = host.querySelector('[role="log"]');
    expect(thread?.textContent).toContain('Hello');
    expect(thread?.textContent).toContain('Hi, I am Atlas.');
    expect(host.querySelector<HTMLTextAreaElement>('#chat-input')?.value).toBe('');

    send.mockReturnValue(of(reply('Second answer')));
    type('Again');
    await submit();

    expect(send).toHaveBeenNthCalledWith(2, {
      message: 'Again',
      conversation_id: CONVERSATION,
      ui_language: 'en',
    });
    expect(host.querySelectorAll('.chat-message').length).toBe(4);
  });

  it('disables the form while a message is being sent', async () => {
    const pending = new Subject<ChatResponse>();
    send.mockReturnValue(pending);
    type('Hello');
    await submit();

    expect(host.querySelector<HTMLButtonElement>('button[type="submit"]')?.disabled).toBe(true);
    expect(host.textContent).toContain(MESSAGES.en['chat.sending']);

    pending.next(reply('Done'));
    pending.complete();
    await render();
    expect(host.querySelector<HTMLButtonElement>('button[type="submit"]')?.disabled).toBe(false);
  });

  it('shows a friendly error and restores the draft when the API fails', async () => {
    send.mockReturnValue(
      throwError(
        () =>
          new HttpErrorResponse({
            status: 502,
            error: { error: { code: 'LLM_UNAVAILABLE', message: 'x', trace_id: 't' } },
          }),
      ),
    );
    type('Hello');
    await submit();

    expect(host.querySelector('[role="alert"]')?.textContent).toContain(
      MESSAGES.en['errors.LLM_UNAVAILABLE'],
    );
    expect(host.querySelector<HTMLTextAreaElement>('#chat-input')?.value).toBe('Hello');
    expect(host.querySelectorAll('.chat-message').length).toBe(0);
  });

  it('goes back to /login when the session expired', async () => {
    send.mockReturnValue(
      throwError(
        () =>
          new HttpErrorResponse({
            status: 401,
            error: { error: { code: 'AUTH_REQUIRED', message: 'x', trace_id: 't' } },
          }),
      ),
    );
    type('Hello');
    await submit();

    expect(clear).toHaveBeenCalled();
    expect(router.navigateByUrl).toHaveBeenCalledWith('/login');
  });

  it('logs out and navigates to /login', async () => {
    host.querySelector<HTMLButtonElement>('button[data-action="logout"]')?.click();
    await render();

    expect(logout).toHaveBeenCalled();
    expect(router.navigateByUrl).toHaveBeenCalledWith('/login');
  });
});
