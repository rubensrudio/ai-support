import { ChangeDetectionStrategy, Component, inject } from '@angular/core';

import { I18nService } from './i18n.service';
import { SUPPORTED_LANGS } from './messages';

/** PT/EN toggle shared by the login and chat pages (STR-08). */
@Component({
  selector: 'app-lang-switch',
  template: `
    <div class="lang-switch" role="group" [attr.aria-label]="i18n.t('lang.label')">
      @for (lang of langs; track lang) {
        <button
          type="button"
          class="lang-switch__option"
          [attr.data-lang]="lang"
          [attr.aria-pressed]="i18n.lang() === lang"
          [attr.lang]="lang"
          [attr.title]="i18n.t(lang === 'pt' ? 'lang.pt' : 'lang.en')"
          (click)="i18n.setLang(lang)"
        >
          {{ lang.toUpperCase() }}
        </button>
      }
    </div>
  `,
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class LangSwitchComponent {
  protected readonly i18n = inject(I18nService);
  protected readonly langs = SUPPORTED_LANGS;
}
