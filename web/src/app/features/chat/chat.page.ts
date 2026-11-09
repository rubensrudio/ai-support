import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  afterRenderEffect,
  computed,
  inject,
  signal,
  viewChild,
} from '@angular/core';
import { Router } from '@angular/router';

import { AuthService } from '../../core/auth/auth.service';
import { ChatApiService } from '../../core/chat/chat-api.service';
import { ChatResponse, ChatStatus, MAX_MESSAGE_CHARS } from '../../core/chat/chat.models';
import { ErrorMessage, apiErrorMessage, toApiError } from '../../core/http/api-error';
import { I18nService } from '../../core/i18n/i18n.service';
import { LangSwitchComponent } from '../../core/i18n/lang-switch.component';
import { MessageKey } from '../../core/i18n/messages';

interface ChatTurn {
  id: number;
  role: 'user' | 'assistant';
  content: string;
  status: ChatStatus | null;
}

type Validation = 'empty' | 'tooLong' | null;

const STATUS_LABELS: Partial<Record<ChatStatus, MessageKey>> = {
  blocked: 'chat.status.blocked',
  degraded: 'chat.status.degraded',
  limit_reached: 'chat.status.limit_reached',
  awaiting_confirmation: 'chat.status.awaiting_confirmation',
};

/** Conversation screen (CHAT-01): thread, validated input (CHAT-90/91) and sign-out. */
@Component({
  selector: 'app-chat-page',
  imports: [LangSwitchComponent],
  templateUrl: './chat.page.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ChatPage {
  protected readonly i18n = inject(I18nService);
  private readonly api = inject(ChatApiService);
  private readonly auth = inject(AuthService);
  private readonly router = inject(Router);

  private readonly threadEl = viewChild<ElementRef<HTMLElement>>('thread');
  private readonly inputEl = viewChild<ElementRef<HTMLTextAreaElement>>('input');

  protected readonly maxChars = MAX_MESSAGE_CHARS;
  protected readonly turns = signal<readonly ChatTurn[]>([]);
  protected readonly draft = signal('');
  protected readonly sending = signal(false);
  private readonly conversationId = signal<string | null>(null);
  private readonly submitted = signal(false);
  private readonly error = signal<ErrorMessage | null>(null);
  private nextId = 1;

  protected readonly length = computed(() => this.draft().length);
  protected readonly tooLong = computed(() => this.length() > MAX_MESSAGE_CHARS);
  protected readonly validation = computed<Validation>(() => {
    if (this.tooLong()) return 'tooLong';
    if (this.submitted() && this.draft().trim() === '') return 'empty';
    return null;
  });
  protected readonly validationText = computed(() => {
    switch (this.validation()) {
      case 'tooLong':
        return this.i18n.t('chat.validation.tooLong', { max: MAX_MESSAGE_CHARS });
      case 'empty':
        return this.i18n.t('chat.validation.empty');
      default:
        return '';
    }
  });
  protected readonly errorText = computed(() => {
    const err = this.error();
    return err ? this.i18n.t(err.key, err.params) : '';
  });

  constructor() {
    afterRenderEffect(() => {
      this.turns();
      this.sending();
      const el = this.threadEl()?.nativeElement;
      if (el) el.scrollTop = el.scrollHeight;
    });
  }

  protected statusLabel(status: ChatStatus | null): string {
    const key = status ? STATUS_LABELS[status] : undefined;
    return key ? this.i18n.t(key) : '';
  }

  protected onInput(event: Event): void {
    this.draft.set((event.target as HTMLTextAreaElement).value);
    this.submitted.set(false);
  }

  protected onKeydown(event: KeyboardEvent): void {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      this.send();
    }
  }

  protected onSubmit(event: Event): void {
    event.preventDefault();
    this.send();
  }

  private send(): void {
    if (this.sending()) return;
    this.submitted.set(true);
    if (this.validation() !== null) return;

    const text = this.draft().trim();
    this.error.set(null);
    this.sending.set(true);
    const userTurn = this.push('user', text, null);
    this.setDraft('');
    this.submitted.set(false);

    this.api
      .send({ message: text, conversation_id: this.conversationId(), ui_language: this.i18n.lang() })
      .subscribe({
        next: (res) => this.onReply(res),
        error: (err: unknown) => this.onError(err, userTurn),
      });
  }

  private onReply(res: ChatResponse): void {
    if (res.conversation_id) this.conversationId.set(res.conversation_id);
    this.push('assistant', res.content, res.status);
    this.sending.set(false);
    this.focusInput();
  }

  private onError(err: unknown, failed: ChatTurn): void {
    this.sending.set(false);
    const apiError = toApiError(err);
    if (apiError.code === 'AUTH_REQUIRED') {
      this.auth.clear();
      void this.router.navigateByUrl('/login');
      return;
    }
    if (apiError.code === 'CONVERSATION_NOT_FOUND') this.conversationId.set(null);
    // Take the unsent message off the thread and give it back to the input for a retry.
    this.turns.update((turns) => turns.filter((t) => t.id !== failed.id));
    this.setDraft(failed.content);
    this.error.set(apiErrorMessage(apiError, this.i18n.lang()));
    this.focusInput();
  }

  protected logout(): void {
    const leave = () => void this.router.navigateByUrl('/login');
    this.auth.logout().subscribe({ complete: leave, error: leave });
  }

  private push(role: ChatTurn['role'], content: string, status: ChatStatus | null): ChatTurn {
    const turn: ChatTurn = { id: this.nextId++, role, content, status };
    this.turns.update((turns) => [...turns, turn]);
    return turn;
  }

  /**
   * `[value]` only re-renders when the bound value changes, and typing does not go through it,
   * so programmatic changes are written to the element as well.
   */
  private setDraft(value: string): void {
    this.draft.set(value);
    const el = this.inputEl()?.nativeElement;
    if (el) el.value = value;
  }

  private focusInput(): void {
    this.inputEl()?.nativeElement.focus();
  }
}
