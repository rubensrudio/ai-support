import { HttpErrorResponse } from '@angular/common/http';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { of, throwError } from 'rxjs';

import { AuthService, Me } from '../../core/auth/auth.service';
import { I18nService } from '../../core/i18n/i18n.service';
import { MESSAGES } from '../../core/i18n/messages';
import { LoginPage } from './login.page';

const ALEX: Me = { display_name: 'Alex Morgan', language: 'en' };

describe('LoginPage', () => {
  let fixture: ComponentFixture<LoginPage>;
  let host: HTMLElement;
  let i18n: I18nService;
  let login: ReturnType<typeof vi.fn>;
  let router: Router;

  async function render(): Promise<void> {
    fixture.detectChanges();
    await fixture.whenStable();
  }

  function type(selector: string, value: string): void {
    const input = host.querySelector<HTMLInputElement>(selector);
    if (!input) throw new Error(`missing ${selector}`);
    input.value = value;
    input.dispatchEvent(new Event('input'));
  }

  function submit(): void {
    host.querySelector('form')?.dispatchEvent(new Event('submit', { cancelable: true }));
  }

  beforeEach(async () => {
    localStorage.clear();
    login = vi.fn(() => of(ALEX));
    await TestBed.configureTestingModule({
      imports: [LoginPage],
      providers: [provideRouter([]), { provide: AuthService, useValue: { login } }],
    }).compileComponents();

    i18n = TestBed.inject(I18nService);
    i18n.setLang('pt');
    router = TestBed.inject(Router);
    vi.spyOn(router, 'navigateByUrl').mockResolvedValue(true);

    fixture = TestBed.createComponent(LoginPage);
    host = fixture.nativeElement as HTMLElement;
    await render();
  });

  afterEach(() => localStorage.clear());

  it('shows the title and persona instructions in Portuguese, then in English after setLang', async () => {
    expect(host.querySelector('h1')?.textContent).toContain(MESSAGES.pt['login.title']);
    expect(host.textContent).toContain(MESSAGES.pt['login.personas.intro']);
    expect(host.textContent).toContain(MESSAGES.pt['login.personas.password']);
    expect(host.textContent).toContain('alex');
    expect(host.textContent).toContain('bia');

    i18n.setLang('en');
    await render();

    expect(host.querySelector('h1')?.textContent).toContain(MESSAGES.en['login.title']);
    expect(host.textContent).toContain(MESSAGES.en['login.personas.intro']);
    expect(host.textContent).not.toContain(MESSAGES.pt['login.personas.intro']);
  });

  it('switches language from the PT/EN selector', async () => {
    const en = host.querySelector<HTMLButtonElement>('button[data-lang="en"]');
    en?.click();
    await render();

    expect(i18n.lang()).toBe('en');
    expect(en?.getAttribute('aria-pressed')).toBe('true');
    expect(host.querySelector('h1')?.textContent).toContain(MESSAGES.en['login.title']);
  });

  it('submit calls AuthService.login and navigates to /chat on success', async () => {
    type('#login-username', 'alex');
    type('#login-password', 'pw');
    submit();
    await render();

    expect(login).toHaveBeenCalledWith('alex', 'pw');
    expect(router.navigateByUrl).toHaveBeenCalledWith('/chat');
  });

  it('shows the translated INVALID_CREDENTIALS message on 401', async () => {
    login.mockReturnValue(
      throwError(
        () =>
          new HttpErrorResponse({
            status: 401,
            error: { error: { code: 'INVALID_CREDENTIALS', message: 'x', trace_id: 't' } },
          }),
      ),
    );
    type('#login-username', 'alex');
    type('#login-password', 'bad');
    submit();
    await render();

    const alert = host.querySelector('[role="alert"]');
    expect(alert?.textContent).toContain(MESSAGES.pt['errors.INVALID_CREDENTIALS']);
    expect(router.navigateByUrl).not.toHaveBeenCalled();

    i18n.setLang('en');
    await render();
    expect(host.querySelector('[role="alert"]')?.textContent).toContain(
      MESSAGES.en['errors.INVALID_CREDENTIALS'],
    );
  });

  it('fills the username from a persona card, even after manual typing', async () => {
    type('#login-username', 'bia');
    const cards = host.querySelectorAll<HTMLButtonElement>('.persona');
    cards[1].click();
    cards[0].click();
    await render();

    expect(host.querySelector<HTMLInputElement>('#login-username')?.value).toBe('alex');
    expect(document.activeElement?.id).toBe('login-password');
  });

  it('does not call login with an empty username or password', async () => {
    type('#login-username', '   ');
    submit();
    await render();

    expect(login).not.toHaveBeenCalled();
    expect(host.querySelector('[role="alert"]')?.textContent).toContain(
      MESSAGES.pt['login.validation.required'],
    );
  });
});
