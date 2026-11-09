import { ComponentFixture, TestBed } from '@angular/core/testing';

import { ConfirmationPrompt } from '../../core/chat/chat.models';
import { I18nService } from '../../core/i18n/i18n.service';
import { Lang } from '../../core/i18n/messages';
import { ConfirmationCardComponent } from './confirmation-card.component';

const CANCEL: ConfirmationPrompt = {
  id: 'c-1',
  tool: 'cancel_order',
  target: '#123',
  amount_cents: null,
  currency: null,
};

const REFUND: ConfirmationPrompt = {
  id: 'c-2',
  tool: 'issue_refund',
  target: '#123',
  amount_cents: 1250,
  currency: 'USD',
};

describe('ConfirmationCardComponent', () => {
  let fixture: ComponentFixture<ConfirmationCardComponent>;
  let host: HTMLElement;

  async function render(
    lang: Lang,
    prompt: ConfirmationPrompt,
    busy = false,
  ): Promise<void> {
    TestBed.inject(I18nService).setLang(lang);
    fixture = TestBed.createComponent(ConfirmationCardComponent);
    fixture.componentRef.setInput('prompt', prompt);
    fixture.componentRef.setInput('busy', busy);
    host = fixture.nativeElement as HTMLElement;
    fixture.detectChanges();
    await fixture.whenStable();
  }

  function button(action: 'confirm' | 'refuse'): HTMLButtonElement {
    const el = host.querySelector<HTMLButtonElement>(`button[data-decision="${action}"]`);
    if (!el) throw new Error(`missing ${action} button`);
    return el;
  }

  beforeEach(async () => {
    localStorage.clear();
    await TestBed.configureTestingModule({ imports: [ConfirmationCardComponent] }).compileComponents();
  });

  afterEach(() => localStorage.clear());

  it('asks to confirm cancelling order #123 in English', async () => {
    await render('en', CANCEL);
    const question = host.querySelector('.confirm-card__question');
    expect(question?.textContent?.trim()).toBe('Confirm: cancel order #123?');
    expect(button('confirm').textContent?.trim()).toBe('Confirm');
    expect(button('refuse').textContent?.trim()).toBe('Refuse');
  });

  it('asks to confirm cancelling order #123 in Portuguese', async () => {
    await render('pt', CANCEL);
    const question = host.querySelector('.confirm-card__question');
    expect(question?.textContent?.trim()).toBe('Confirmar: cancelar pedido #123?');
    expect(button('confirm').textContent?.trim()).toBe('Confirmar');
    expect(button('refuse').textContent?.trim()).toBe('Recusar');
  });

  it('shows the refund amount formatted for the language', async () => {
    await render('en', REFUND);
    expect(host.querySelector('.confirm-card__question')?.textContent?.trim()).toBe(
      'Confirm: issue refund #123?',
    );
    expect(host.querySelector('.confirm-card__amount')?.textContent).toContain('$12.50');

    await render('pt', REFUND);
    const amount = host.querySelector('.confirm-card__amount')?.textContent ?? '';
    expect(amount).toContain('US$');
    expect(amount).toContain('12,50');
  });

  it('hides the amount when the action has none', async () => {
    await render('en', CANCEL);
    expect(host.querySelector('.confirm-card__amount')).toBeNull();
  });

  it('emits refuse when the refuse button is clicked', async () => {
    await render('en', CANCEL);
    const emitted: string[] = [];
    fixture.componentInstance.decision.subscribe((d) => emitted.push(d));
    button('refuse').click();
    expect(emitted).toEqual(['refuse']);
  });

  it('emits confirm when the confirm button is clicked', async () => {
    await render('en', CANCEL);
    const emitted: string[] = [];
    fixture.componentInstance.decision.subscribe((d) => emitted.push(d));
    button('confirm').click();
    expect(emitted).toEqual(['confirm']);
  });

  it('disables both buttons while busy', async () => {
    await render('en', CANCEL, true);
    expect(button('confirm').disabled).toBe(true);
    expect(button('refuse').disabled).toBe(true);
  });

  it('is an accessible group labelled by the question and takes focus', async () => {
    await render('en', CANCEL);
    const group = host.querySelector<HTMLElement>('[role="group"]');
    const labelId = group?.getAttribute('aria-labelledby');
    expect(labelId).toBeTruthy();
    expect(host.querySelector(`#${labelId}`)?.textContent?.trim()).toBe(
      'Confirm: cancel order #123?',
    );
    expect(document.activeElement).toBe(group);
  });
});
