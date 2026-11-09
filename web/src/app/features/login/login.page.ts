import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  inject,
  signal,
  viewChild,
} from '@angular/core';
import { Router } from '@angular/router';

import { AuthService } from '../../core/auth/auth.service';
import { ErrorMessage, apiErrorMessage, toApiError } from '../../core/http/api-error';
import { I18nService } from '../../core/i18n/i18n.service';
import { LangSwitchComponent } from '../../core/i18n/lang-switch.component';
import { MessageKey } from '../../core/i18n/messages';

interface PersonaHint {
  username: string;
  name: MessageKey;
  language: MessageKey;
}

/** Bilingual sign-in for the fixed test personas (DEMO-01, DEMO-06, DEMO-11). */
@Component({
  selector: 'app-login-page',
  imports: [LangSwitchComponent],
  templateUrl: './login.page.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class LoginPage {
  protected readonly i18n = inject(I18nService);
  private readonly auth = inject(AuthService);
  private readonly router = inject(Router);

  protected readonly personas: readonly PersonaHint[] = [
    { username: 'alex', name: 'login.personas.alexName', language: 'login.personas.alexLang' },
    { username: 'bia', name: 'login.personas.biaName', language: 'login.personas.biaLang' },
  ];

  private readonly usernameEl = viewChild<ElementRef<HTMLInputElement>>('usernameInput');
  private readonly passwordEl = viewChild<ElementRef<HTMLInputElement>>('passwordInput');

  protected readonly username = signal('');
  protected readonly password = signal('');
  protected readonly submitting = signal(false);
  private readonly error = signal<ErrorMessage | null>(null);

  /** Re-evaluated on language change, so a shown error follows the selector. */
  protected readonly errorText = computed(() => {
    const err = this.error();
    return err ? this.i18n.t(err.key, err.params) : '';
  });

  protected onInput(field: 'username' | 'password', event: Event): void {
    const value = (event.target as HTMLInputElement).value;
    (field === 'username' ? this.username : this.password).set(value);
  }

  /** Fills the username from a persona card and moves focus to the password. */
  protected usePersona(username: string): void {
    this.username.set(username);
    const input = this.usernameEl()?.nativeElement;
    if (input) input.value = username;
    this.passwordEl()?.nativeElement.focus();
  }

  protected onSubmit(event: Event): void {
    event.preventDefault();
    if (this.submitting()) return;

    const username = this.username().trim();
    const password = this.password();
    if (!username || !password) {
      this.error.set({ key: 'login.validation.required', params: {} });
      return;
    }

    this.error.set(null);
    this.submitting.set(true);
    this.auth.login(username, password).subscribe({
      next: () => {
        this.submitting.set(false);
        void this.router.navigateByUrl('/chat');
      },
      error: (err: unknown) => {
        this.submitting.set(false);
        this.error.set(apiErrorMessage(toApiError(err), this.i18n.lang()));
      },
    });
  }
}
