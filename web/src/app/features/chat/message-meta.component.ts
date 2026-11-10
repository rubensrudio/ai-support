import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  inject,
  input,
  signal,
} from '@angular/core';
import { Subscription } from 'rxjs';

import { FeedbackApiService, FeedbackRating } from '../../core/chat/feedback-api.service';
import { I18nService } from '../../core/i18n/i18n.service';

/** How long the "copied" notice stays before it is cleared. */
const COPIED_NOTICE_MS = 2000;

/**
 * Footer of an assistant answer: the `trace_id` in small, copyable text (OBS-06) and the
 * 👍/👎 buttons that rate the answer (OBS-02, CT-65). Without a `messageId` (the answer was never
 * stored, e.g. it failed first) only the trace id is shown. A new click on the other option
 * replaces the rating; a failure keeps the previous choice and shows a translated error.
 */
@Component({
  selector: 'app-message-meta',
  template: `
    <div class="message-meta">
      <span class="message-meta__trace">
        <span class="message-meta__trace-label">{{ i18n.t('feedback.traceLabel') }}</span>
        <code class="message-meta__trace-id">{{ traceId() }}</code>
        <button
          type="button"
          class="message-meta__button"
          data-action="copy-trace"
          [attr.aria-label]="i18n.t('feedback.copy')"
          [attr.title]="i18n.t('feedback.copy')"
          (click)="copy()"
        >
          <span aria-hidden="true">⧉</span>
        </button>
        <span class="message-meta__copied" aria-live="polite">{{
          copied() ? i18n.t('feedback.copied') : ''
        }}</span>
      </span>
      @if (messageId() !== null) {
        <span
          class="message-meta__rating"
          role="group"
          [attr.aria-label]="i18n.t('feedback.groupLabel')"
        >
          <button
            type="button"
            class="message-meta__button"
            data-rating="up"
            [attr.aria-label]="i18n.t('feedback.up')"
            [attr.aria-pressed]="rating() === 'up'"
            [attr.title]="i18n.t('feedback.up')"
            [disabled]="sending()"
            (click)="rate('up')"
          >
            <span aria-hidden="true">👍</span>
          </button>
          <button
            type="button"
            class="message-meta__button"
            data-rating="down"
            [attr.aria-label]="i18n.t('feedback.down')"
            [attr.aria-pressed]="rating() === 'down'"
            [attr.title]="i18n.t('feedback.down')"
            [disabled]="sending()"
            (click)="rate('down')"
          >
            <span aria-hidden="true">👎</span>
          </button>
        </span>
        <span class="message-meta__alert" aria-live="polite">
          @if (errorText()) {
            <span class="message-meta__error">{{ errorText() }}</span>
          }
        </span>
      }
    </div>
  `,
  styles: `
    .message-meta {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 4px 12px;
      margin-top: 4px;
      font-size: 0.75rem;
      color: var(--text-muted);
    }
    .message-meta__trace,
    .message-meta__rating {
      display: inline-flex;
      align-items: center;
      gap: 4px;
    }
    .message-meta__trace-id {
      font-family: var(--mono);
      user-select: all;
      word-break: break-all;
    }
    .message-meta__button {
      padding: 2px 6px;
      border: 1px solid transparent;
      border-radius: var(--radius-sm);
      background: transparent;
      color: inherit;
      font: inherit;
      line-height: 1.2;
      cursor: pointer;
    }
    .message-meta__button:hover:not(:disabled) {
      border-color: var(--border);
    }
    .message-meta__button:focus-visible {
      outline: 3px solid var(--focus);
      outline-offset: 2px;
    }
    .message-meta__button[aria-pressed='true'] {
      border-color: var(--accent);
      background: var(--surface-muted);
    }
    .message-meta__button:disabled {
      cursor: progress;
      opacity: 0.6;
    }
    .message-meta__error {
      color: var(--danger);
    }
  `,
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class MessageMetaComponent {
  protected readonly i18n = inject(I18nService);
  private readonly feedback = inject(FeedbackApiService);

  readonly traceId = input.required<string>();
  readonly messageId = input<number | null>(null);

  /** The rating the server accepted last; `null` until one is sent successfully. */
  protected readonly rating = signal<FeedbackRating | null>(null);
  protected readonly sending = signal(false);
  protected readonly copied = signal(false);
  private readonly failed = signal(false);
  protected readonly errorText = computed(() =>
    this.failed() ? this.i18n.t('feedback.error') : '',
  );

  private request: Subscription | null = null;
  private copiedTimer: ReturnType<typeof setTimeout> | null = null;

  constructor() {
    inject(DestroyRef).onDestroy(() => {
      this.request?.unsubscribe();
      if (this.copiedTimer !== null) clearTimeout(this.copiedTimer);
    });
  }

  protected rate(rating: FeedbackRating): void {
    const messageId = this.messageId();
    if (messageId === null || this.sending()) return;
    this.sending.set(true);
    this.failed.set(false);
    this.request = this.feedback.send(messageId, rating).subscribe({
      complete: () => {
        this.rating.set(rating);
        this.sending.set(false);
      },
      error: () => {
        this.failed.set(true);
        this.sending.set(false);
      },
    });
  }

  /** Copies the trace id; without a usable clipboard it does nothing (the id stays selectable). */
  protected copy(): void {
    let pending: Promise<void>;
    try {
      const clipboard = globalThis.navigator?.clipboard;
      if (!clipboard) return;
      pending = clipboard.writeText(this.traceId());
    } catch {
      return;
    }
    pending.then(
      () => this.showCopied(),
      () => undefined,
    );
  }

  private showCopied(): void {
    this.copied.set(true);
    if (this.copiedTimer !== null) clearTimeout(this.copiedTimer);
    this.copiedTimer = setTimeout(() => {
      this.copied.set(false);
      this.copiedTimer = null;
    }, COPIED_NOTICE_MS);
  }
}
