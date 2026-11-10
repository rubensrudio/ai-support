import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ComponentFixture, TestBed } from '@angular/core/testing';

import { I18nService } from '../../core/i18n/i18n.service';
import { Lang } from '../../core/i18n/messages';
import { MessageMetaComponent } from './message-meta.component';

const TRACE = '0123456789abcdef0123456789abcdef';

describe('MessageMetaComponent', () => {
  let fixture: ComponentFixture<MessageMetaComponent>;
  let host: HTMLElement;
  let http: HttpTestingController;

  async function render(messageId: number | null, lang: Lang = 'en'): Promise<void> {
    TestBed.inject(I18nService).setLang(lang);
    fixture = TestBed.createComponent(MessageMetaComponent);
    fixture.componentRef.setInput('traceId', TRACE);
    fixture.componentRef.setInput('messageId', messageId);
    host = fixture.nativeElement as HTMLElement;
    await settle();
  }

  async function settle(): Promise<void> {
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  function rating(value: 'up' | 'down'): HTMLButtonElement {
    const el = host.querySelector<HTMLButtonElement>(`button[data-rating="${value}"]`);
    if (!el) throw new Error(`missing ${value} button`);
    return el;
  }

  beforeEach(async () => {
    localStorage.clear();
    await TestBed.configureTestingModule({
      imports: [MessageMetaComponent],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    http.verify();
    localStorage.clear();
    vi.restoreAllMocks();
  });

  it('renders the received trace id with a translated label', async () => {
    await render(5);
    expect(host.querySelector('.message-meta__trace-id')?.textContent?.trim()).toBe(TRACE);
    expect(host.querySelector('.message-meta__trace-label')?.textContent?.trim()).toBe('Trace');
  });

  it('labels the rating buttons in the current language', async () => {
    await render(5, 'pt');
    expect(rating('up').getAttribute('aria-label')).toBe('Resposta útil');
    expect(rating('down').getAttribute('aria-label')).toBe('Resposta não útil');
    expect(rating('up').getAttribute('aria-pressed')).toBe('false');
    expect(rating('down').getAttribute('aria-pressed')).toBe('false');
  });

  it("clicking 'down' POSTs {rating:'down'} to /api/messages/{id}/feedback and keeps the choice", async () => {
    await render(5);
    rating('down').click();
    await settle();

    expect(rating('down').disabled).toBe(true);
    expect(rating('up').disabled).toBe(true);
    const req = http.expectOne('/api/messages/5/feedback');
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ rating: 'down' });
    req.flush(null, { status: 204, statusText: 'No Content' });
    await settle();

    expect(rating('down').getAttribute('aria-pressed')).toBe('true');
    expect(rating('up').getAttribute('aria-pressed')).toBe('false');
    expect(rating('down').disabled).toBe(false);
    expect(host.querySelector('.message-meta__error')?.textContent?.trim() ?? '').toBe('');
  });

  it('clicking the other option afterwards switches the rating', async () => {
    await render(5);
    rating('down').click();
    await settle();
    http
      .expectOne('/api/messages/5/feedback')
      .flush(null, { status: 204, statusText: 'No Content' });
    await settle();

    rating('up').click();
    await settle();
    const req = http.expectOne('/api/messages/5/feedback');
    expect(req.request.body).toEqual({ rating: 'up' });
    req.flush(null, { status: 204, statusText: 'No Content' });
    await settle();

    expect(rating('up').getAttribute('aria-pressed')).toBe('true');
    expect(rating('down').getAttribute('aria-pressed')).toBe('false');
  });

  it('shows a translated error and keeps the previous choice when sending fails', async () => {
    await render(5);
    rating('up').click();
    await settle();
    http
      .expectOne('/api/messages/5/feedback')
      .flush(
        { error: { code: 'INTERNAL_ERROR', message: 'x', trace_id: 't' } },
        { status: 500, statusText: 'Server Error' },
      );
    await settle();

    const error = host.querySelector('.message-meta__error');
    expect(error?.textContent?.trim()).toBe("Couldn't send your feedback. Please try again.");
    expect(error?.closest('[aria-live]')).not.toBeNull();
    expect(rating('up').getAttribute('aria-pressed')).toBe('false');
    expect(rating('up').disabled).toBe(false);
  });

  it('without a message id shows only the trace id, with no rating buttons', async () => {
    await render(null);
    expect(host.querySelector('.message-meta__trace-id')?.textContent?.trim()).toBe(TRACE);
    expect(host.querySelectorAll('button[data-rating]').length).toBe(0);
  });

  it('copies the trace id and announces it', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    await render(5);

    host.querySelector<HTMLButtonElement>('button[data-action="copy-trace"]')?.click();
    await settle();

    expect(writeText).toHaveBeenCalledWith(TRACE);
    expect(host.querySelector('.message-meta__copied')?.textContent?.trim()).toBe('Copied');
  });

  it('fails silently when the clipboard is unavailable', async () => {
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true });
    await render(5);

    host.querySelector<HTMLButtonElement>('button[data-action="copy-trace"]')?.click();
    await settle();

    expect(host.querySelector('.message-meta__copied')?.textContent?.trim() ?? '').toBe('');
  });
});
