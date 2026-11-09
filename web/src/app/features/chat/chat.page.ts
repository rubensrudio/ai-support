import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  Injector,
  afterNextRender,
  afterRenderEffect,
  computed,
  inject,
  signal,
  viewChild,
} from '@angular/core';
import { Router } from '@angular/router';

import { AuthService } from '../../core/auth/auth.service';
import { ChatApiService } from '../../core/chat/chat-api.service';
import {
  ChatResponse,
  ChatStatus,
  ConfirmationDecision,
  ConfirmationPrompt,
  MAX_MESSAGE_CHARS,
} from '../../core/chat/chat.models';
import { ErrorMessage, apiErrorMessage, toApiError } from '../../core/http/api-error';
import { I18nService } from '../../core/i18n/i18n.service';
import { LangSwitchComponent } from '../../core/i18n/lang-switch.component';
import { MessageKey } from '../../core/i18n/messages';
import { ConfirmationCardComponent } from './confirmation-card.component';

interface ChatTurn {
  id: number;
  role: 'user' | 'assistant';
  content: string;
  status: ChatStatus | null;
}

type Validation = 'empty' | 'tooLong' | null;

/** Confirmation errors after which the action can no longer be decided (CT-46). */
const TERMINAL_CONFIRMATION_ERRORS: ReadonlySet<string> = new Set([
  'CONFIRMATION_NOT_FOUND',
  'CONFIRMATION_NOT_PENDING',
]);

/** A destructive tool waiting for the customer's decision, anchored to the turn that asked. */
interface PendingConfirmation {
  turnId: number;
  prompt: ConfirmationPrompt;
}

const STATUS_LABELS: Partial<Record<ChatStatus, MessageKey>> = {
  blocked: 'chat.status.blocked',
  degraded: 'chat.status.degraded',
  limit_reached: 'chat.status.limit_reached',
  awaiting_confirmation: 'chat.status.awaiting_confirmation',
};

/**
 * Conversation screen (CHAT-01): thread, validated input (CHAT-90/91), sign-out and the
 * confirmation of destructive tools (TOOL-05/13), which locks the composer until decided.
 */
@Component({
  selector: 'app-chat-page',
  imports: [LangSwitchComponent, ConfirmationCardComponent],
  templateUrl: './chat.page.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ChatPage {
  protected readonly i18n = inject(I18nService);
  private readonly api = inject(ChatApiService);
  private readonly auth = inject(AuthService);
  private readonly router = inject(Router);
  private readonly injector = inject(Injector);

  private readonly threadEl = viewChild<ElementRef<HTMLElement>>('thread');
  private readonly inputEl = viewChild<ElementRef<HTMLTextAreaElement>>('input');

  protected readonly maxChars = MAX_MESSAGE_CHARS;
  protected readonly turns = signal<readonly ChatTurn[]>([]);
  protected readonly draft = signal('');
  protected readonly sending = signal(false);
  protected readonly deciding = signal(false);
  protected readonly pending = signal<PendingConfirmation | null>(null);
  private readonly conversationId = signal<string | null>(null);
  private readonly submitted = signal(false);
  private readonly error = signal<ErrorMessage | null>(null);
  private nextId = 1;

  /** No new message while an answer or a confirmation decision is outstanding. */
  protected readonly locked = computed(() => this.sending() || this.pending() !== null);
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
      this.pending();
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
    if (this.locked()) return;
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

  protected onDecision(decision: ConfirmationDecision): void {
    const pending = this.pending();
    if (!pending || this.sending()) return;
    this.error.set(null);
    this.sending.set(true);
    this.deciding.set(true);

    this.api.confirm(pending.prompt.id, decision).subscribe({
      next: (res) => this.onReply(res),
      error: (err: unknown) => this.onDecisionError(err),
    });
  }

  private onReply(res: ChatResponse): void {
    if (res.conversation_id) this.conversationId.set(res.conversation_id);
    const turn = this.push('assistant', res.content, res.status);
    const prompt = res.status === 'awaiting_confirmation' ? res.confirmation : null;
    this.pending.set(prompt ? { turnId: turn.id, prompt } : null);
    this.sending.set(false);
    this.deciding.set(false);
    // The confirmation card takes the focus itself; otherwise go back to the composer.
    if (!prompt) this.focusInput();
  }

  /**
   * Only a confirmation the server no longer holds (404/409) or an expired session drops the
   * card. On 5xx or network failures the server rolled back and the confirmation is still
   * pending, so the card stays (buttons enabled again) and the composer stays locked.
   */
  private onDecisionError(err: unknown): void {
    this.sending.set(false);
    this.deciding.set(false);
    const apiError = toApiError(err);
    if (apiError.code === 'AUTH_REQUIRED') {
      this.pending.set(null);
      this.auth.clear();
      void this.router.navigateByUrl('/login');
      return;
    }
    this.error.set(apiErrorMessage(apiError, this.i18n.lang()));
    if (TERMINAL_CONFIRMATION_ERRORS.has(apiError.code)) {
      this.pending.set(null);
      this.focusInput();
    }
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

  /** Runs after the next render, once the textarea is no longer `disabled`. */
  private focusInput(): void {
    afterNextRender(() => this.inputEl()?.nativeElement.focus(), { injector: this.injector });
  }
}
