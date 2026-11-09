import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
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
import { Observable, Subscription } from 'rxjs';

import { AuthService } from '../../core/auth/auth.service';
import { ChatStreamService } from '../../core/chat/chat-stream.service';
import {
  ChatStatus,
  CitationStreamEvent,
  ConfirmationDecision,
  ConfirmationPrompt,
  DoneStreamEvent,
  ErrorStreamEvent,
  MAX_MESSAGE_CHARS,
  StreamEvent,
} from '../../core/chat/chat.models';
import {
  API_ERROR_CODES,
  ApiError,
  ErrorMessage,
  apiErrorMessage,
  toApiError,
} from '../../core/http/api-error';
import { I18nService } from '../../core/i18n/i18n.service';
import { LangSwitchComponent } from '../../core/i18n/lang-switch.component';
import { MESSAGES, MessageKey } from '../../core/i18n/messages';
import { ConfirmationCardComponent } from './confirmation-card.component';

/**
 * Lifecycle of a turn: an assistant turn is `streaming` from the moment the message is sent
 * until `done` (`complete`), the customer's cancel (`cancelled`, partial kept, STR-04/07) or a
 * failure after the stream opened (`failed`, STR-90). User turns are always `complete`.
 */
type TurnPhase = 'streaming' | 'complete' | 'cancelled' | 'failed';

/** A source shown under the answer (STR-03). */
type Source = Pick<CitationStreamEvent, 'index' | 'document' | 'section' | 'page'>;

interface ChatTurn {
  id: number;
  role: 'user' | 'assistant';
  content: string;
  status: ChatStatus | null;
  phase: TurnPhase;
  sources: readonly Source[];
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

/** The tool currently running between `tool_start` and its `tool_end` (STR-02). */
interface ActiveStep {
  callId: string;
  tool: string;
}

/** The answer being streamed right now; at most one at a time. */
interface ActiveStream {
  turnId: number;
  /** The question turn, taken back to the composer if the request is refused before any event. */
  question: { turnId: number; text: string } | null;
  /** Set when the stream answers a confirmation decision instead of a new message. */
  decision: boolean;
  /** True once the first event arrived, i.e. the server accepted the turn. */
  opened: boolean;
  subscription: Subscription | null;
}

const STATUS_LABELS: Partial<Record<ChatStatus, MessageKey>> = {
  blocked: 'chat.status.blocked',
  degraded: 'chat.status.degraded',
  limit_reached: 'chat.status.limit_reached',
  awaiting_confirmation: 'chat.status.awaiting_confirmation',
};

/** The error carried by an SSE `error` event, normalised like an HTTP one. */
function sseApiError(event: ErrorStreamEvent): ApiError {
  const known = (API_ERROR_CODES as readonly string[]).includes(event.code);
  return {
    status: -1,
    code: known ? (event.code as ApiError['code']) : 'UNKNOWN',
    traceId: event.trace_id,
    retryAt: null,
  };
}

/** `step.<tool>` when the dictionary knows the tool, else the generic label (mirrors STEP_*). */
function stepKey(tool: string): MessageKey {
  const key = `step.${tool}`;
  return key in MESSAGES.en ? (key as MessageKey) : 'step.generic';
}

/**
 * Conversation screen (CHAT-01): thread, validated input (CHAT-90/91), sign-out and the
 * confirmation of destructive tools (TOOL-05/13), which locks the composer until decided.
 *
 * Answers arrive through `ChatStreamService` (CT-51): tokens are appended as they come, the
 * running tool is shown as a translated step (STR-02), citations are listed under the answer
 * (STR-03) and the answer can be cancelled while it is in progress (STR-04/92).
 */
@Component({
  selector: 'app-chat-page',
  imports: [LangSwitchComponent, ConfirmationCardComponent],
  templateUrl: './chat.page.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ChatPage {
  protected readonly i18n = inject(I18nService);
  private readonly chat = inject(ChatStreamService);
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
  protected readonly step = signal<ActiveStep | null>(null);
  /** The failed assistant turn that offers "try again", and the question it resends. */
  protected readonly retry = signal<{ turnId: number; question: string } | null>(null);
  private readonly conversationId = signal<string | null>(null);
  private readonly submitted = signal(false);
  private readonly error = signal<ErrorMessage | null>(null);
  private active: ActiveStream | null = null;
  private lastQuestion = '';
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
  /** What the progress line says: the running step, else the generic "answering". */
  protected readonly progressText = computed(() => {
    const step = this.step();
    if (step) return this.i18n.t(stepKey(step.tool));
    return this.deciding() ? this.i18n.t('confirm.sending') : this.i18n.t('chat.sending');
  });

  constructor() {
    afterRenderEffect(() => {
      this.turns();
      this.sending();
      this.pending();
      this.step();
      const el = this.threadEl()?.nativeElement;
      if (el) el.scrollTop = el.scrollHeight;
    });
    // Leaving the page aborts the request like a cancel (STR-91 on the server side).
    inject(DestroyRef).onDestroy(() => this.active?.subscription?.unsubscribe());
  }

  protected statusLabel(status: ChatStatus | null): string {
    const key = status ? STATUS_LABELS[status] : undefined;
    return key ? this.i18n.t(key) : '';
  }

  /** An assistant turn with nothing to show yet stays hidden behind the progress line. */
  protected visible(turn: ChatTurn): boolean {
    return turn.role === 'user' || turn.phase !== 'streaming' || turn.content !== '';
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
    this.setDraft('');
    this.submitted.set(false);
    this.ask(text);
  }

  /** "Try again" after a failed answer: resend the last question as a new message (STR-90). */
  protected onRetry(): void {
    const retry = this.retry();
    if (this.locked() || !retry) return;
    this.ask(retry.question);
  }

  private ask(text: string): void {
    this.lastQuestion = text;
    const question = this.push('user', text, 'complete');
    const answer = this.push('assistant', '', 'streaming');
    this.start(
      this.chat.stream({
        message: text,
        conversation_id: this.conversationId(),
        ui_language: this.i18n.lang(),
      }),
      { turnId: answer.id, question: { turnId: question.id, text }, decision: false },
    );
  }

  protected onDecision(decision: ConfirmationDecision): void {
    const pending = this.pending();
    if (!pending || this.sending()) return;
    this.deciding.set(true);
    const answer = this.push('assistant', '', 'streaming');
    this.start(this.chat.confirmStream(pending.prompt.id, decision), {
      turnId: answer.id,
      question: null,
      decision: true,
    });
  }

  /** Stops the answer in progress: aborts the request and keeps the partial text (STR-04). */
  protected cancel(): void {
    const active = this.active;
    if (!active) return; // STR-92: after `done` there is nothing to cancel.
    active.subscription?.unsubscribe();
    this.finish();
    const turn = this.turns().find((t) => t.id === active.turnId);
    if (turn && turn.content === '') {
      this.turns.update((turns) => turns.filter((t) => t.id !== active.turnId));
    } else {
      this.updateTurn(active.turnId, { phase: 'cancelled' });
    }
    // The server may or may not have resolved the decision; drop the card and let the
    // customer carry on with a new message.
    if (active.decision) this.pending.set(null);
    this.focusInput();
  }

  private start(
    source: Observable<StreamEvent>,
    stream: Omit<ActiveStream, 'opened' | 'subscription'>,
  ): void {
    this.error.set(null);
    this.retry.set(null);
    this.sending.set(true);
    const active: ActiveStream = { ...stream, opened: false, subscription: null };
    this.active = active;
    const subscription = source.subscribe({
      next: (event) => this.onEvent(active, event),
      error: (err: unknown) => this.onStreamError(active, err),
      complete: () => {
        // Completing without `done`/`error` should not happen (CT-51); treat it as a failure.
        if (this.active === active) this.onStreamError(active, null);
      },
    });
    if (this.active === active) active.subscription = subscription;
  }

  private onEvent(active: ActiveStream, event: StreamEvent): void {
    if (this.active !== active) return; // Late event after cancel: never rendered.
    active.opened = true;
    switch (event.type) {
      case 'start':
        // Known from the first event, so a cancel or failure before `done` keeps the conversation.
        if (event.conversation_id) this.conversationId.set(event.conversation_id);
        break;
      case 'token':
        this.turns.update((turns) =>
          turns.map((t) => (t.id === active.turnId ? { ...t, content: t.content + event.text } : t)),
        );
        break;
      case 'tool_start':
        // Text streamed before a tool call is the model thinking aloud, not the final answer.
        this.updateTurn(active.turnId, { content: '' });
        this.step.set({ callId: event.call_id, tool: event.tool });
        break;
      case 'tool_end':
        if (this.step()?.callId === event.call_id) this.step.set(null);
        break;
      case 'citation':
        this.addSource(active.turnId, event);
        break;
      case 'done':
        this.onDone(active, event);
        break;
      case 'error':
        if (active.decision) this.failDecision(active, sseApiError(event));
        else this.fail(active);
        break;
    }
  }

  private addSource(turnId: number, event: CitationStreamEvent): void {
    const source: Source = {
      index: event.index,
      document: event.document,
      section: event.section,
      page: event.page,
    };
    this.turns.update((turns) =>
      turns.map((t) =>
        t.id === turnId && !t.sources.some((s) => s.index === source.index)
          ? { ...t, sources: [...t.sources, source] }
          : t,
      ),
    );
  }

  private onDone(active: ActiveStream, done: DoneStreamEvent): void {
    this.finish();
    if (done.conversation_id) this.conversationId.set(done.conversation_id);
    this.turns.update((turns) =>
      turns.map((t) =>
        t.id === active.turnId
          ? { ...t, content: done.content || t.content, status: done.status, phase: 'complete' }
          : t,
      ),
    );
    const prompt = done.status === 'awaiting_confirmation' ? done.confirmation : null;
    this.pending.set(prompt ? { turnId: active.turnId, prompt } : null);
    // The confirmation card takes the focus itself; otherwise go back to the composer.
    if (!prompt) this.focusInput();
  }

  /**
   * An expired session always goes back to /login. Decisions are handled by `failDecision`.
   * For a new message, a refusal before any event (the server never accepted the turn: rate
   * limit, too long, unknown conversation, unreachable server…) shows the specific message in the
   * banner and gives the question back to the composer; once the stream opened, the failure is
   * shown on the answer with "try again".
   */
  private onStreamError(active: ActiveStream, err: unknown): void {
    if (this.active !== active) return;
    const apiError = toApiError(err);
    if (apiError.code === 'AUTH_REQUIRED') {
      this.finish();
      this.removeTurn(active.turnId);
      this.pending.set(null);
      this.auth.clear();
      void this.router.navigateByUrl('/login');
      return;
    }
    if (active.decision) {
      this.failDecision(active, apiError);
      return;
    }
    if (active.opened || err === null) {
      this.fail(active);
      return;
    }

    this.finish();
    this.removeTurn(active.turnId);
    this.error.set(apiErrorMessage(apiError, this.i18n.lang()));
    if (apiError.code === 'CONVERSATION_NOT_FOUND') this.conversationId.set(null);
    // Take the unsent message off the thread and give it back to the input for a retry.
    if (active.question) {
      this.removeTurn(active.question.turnId);
      this.setDraft(active.question.text);
    }
    this.focusInput();
  }

  /** STR-90: keep whatever arrived, say the answer failed and offer to ask again. */
  private fail(active: ActiveStream): void {
    this.finish();
    this.updateTurn(active.turnId, { phase: 'failed' });
    this.retry.set({ turnId: active.turnId, question: this.lastQuestion });
  }

  /**
   * A decision that failed, before the stream or through an SSE `error`. Only a confirmation the
   * server no longer holds (not found / not pending) drops the card. Otherwise (LLM_UNAVAILABLE,
   * 5xx, network, broken stream) the server rolled back and the confirmation is still pending, so
   * the card stays with its buttons enabled and the composer stays locked. There is no "try
   * again" here: resending the question as a new message would refuse the action (TOOL-97).
   */
  private failDecision(active: ActiveStream, apiError: ApiError): void {
    this.finish();
    const turn = this.turns().find((t) => t.id === active.turnId);
    if (turn && turn.content === '') this.removeTurn(active.turnId);
    else this.updateTurn(active.turnId, { phase: 'failed' });
    this.error.set(apiErrorMessage(apiError, this.i18n.lang()));
    if (TERMINAL_CONFIRMATION_ERRORS.has(apiError.code)) {
      this.pending.set(null);
      this.focusInput();
    }
  }

  private finish(): void {
    this.active = null;
    this.step.set(null);
    this.sending.set(false);
    this.deciding.set(false);
  }

  protected logout(): void {
    const leave = () => void this.router.navigateByUrl('/login');
    this.auth.logout().subscribe({ complete: leave, error: leave });
  }

  private push(role: ChatTurn['role'], content: string, phase: TurnPhase): ChatTurn {
    const turn: ChatTurn = { id: this.nextId++, role, content, status: null, phase, sources: [] };
    this.turns.update((turns) => [...turns, turn]);
    return turn;
  }

  private updateTurn(id: number, patch: Partial<ChatTurn>): void {
    this.turns.update((turns) => turns.map((t) => (t.id === id ? { ...t, ...patch } : t)));
  }

  private removeTurn(id: number): void {
    this.turns.update((turns) => turns.filter((t) => t.id !== id));
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
