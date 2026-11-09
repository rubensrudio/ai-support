import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  afterNextRender,
  computed,
  inject,
  input,
  output,
  viewChild,
} from '@angular/core';

import { ConfirmationDecision, ConfirmationPrompt } from '../../core/chat/chat.models';
import { I18nService } from '../../core/i18n/i18n.service';
import { MessageKey } from '../../core/i18n/messages';
import { MoneyPipe } from '../../shared/money.pipe';

/** Destructive tools that need the customer's explicit confirmation (TOOL-12). */
const TOOL_LABELS: Readonly<Record<string, MessageKey>> = {
  cancel_order: 'tools.cancel_order',
  issue_refund: 'tools.issue_refund',
};

let nextCardId = 0;

/**
 * "Confirm: <action> <target>?" card for a suspended destructive tool (TOOL-05). It only emits
 * the customer's `decision`; the chat page calls the API.
 */
@Component({
  selector: 'app-confirmation-card',
  imports: [MoneyPipe],
  template: `
    <section
      #card
      class="confirm-card"
      role="group"
      tabindex="-1"
      [attr.aria-labelledby]="questionId"
      [attr.aria-busy]="busy()"
      [attr.data-tool]="prompt().tool"
    >
      <p [id]="questionId" class="confirm-card__question">{{ question() }}</p>
      @if (prompt().amount_cents !== null) {
        <p class="confirm-card__amount">
          {{ i18n.t('confirm.amount', { amount: prompt().amount_cents | money: i18n.lang() }) }}
        </p>
      }
      <div class="confirm-card__actions">
        <button
          type="button"
          class="button button--primary"
          data-decision="confirm"
          [disabled]="busy()"
          (click)="decide('confirm')"
        >
          {{ i18n.t('confirm.accept') }}
        </button>
        <button
          type="button"
          class="button button--ghost"
          data-decision="refuse"
          [disabled]="busy()"
          (click)="decide('refuse')"
        >
          {{ i18n.t('confirm.refuse') }}
        </button>
      </div>
    </section>
  `,
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ConfirmationCardComponent {
  protected readonly i18n = inject(I18nService);
  private readonly card = viewChild.required<ElementRef<HTMLElement>>('card');

  readonly prompt = input.required<ConfirmationPrompt>();
  readonly busy = input(false);
  readonly decision = output<ConfirmationDecision>();

  protected readonly questionId = `confirm-card-question-${nextCardId++}`;
  protected readonly question = computed(() =>
    this.i18n.t('confirm.question', {
      action: this.i18n.t(TOOL_LABELS[this.prompt().tool] ?? 'tools.unknown'),
      target: this.prompt().target,
    }),
  );

  constructor() {
    afterNextRender(() => this.card().nativeElement.focus());
  }

  protected decide(decision: ConfirmationDecision): void {
    if (this.busy()) return;
    this.decision.emit(decision);
  }
}
