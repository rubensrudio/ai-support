import { DOCUMENT, Injectable, Signal, inject, signal } from '@angular/core';

import { Lang, MESSAGES, MessageKey, SUPPORTED_LANGS } from './messages';

export type { Lang, MessageKey } from './messages';

/** localStorage key holding the language the user picked by hand (CT-24). */
export const LANG_STORAGE_KEY = 'atlas.lang';

export type MessageParams = Readonly<Record<string, string | number>>;

function isLang(value: unknown): value is Lang {
  return typeof value === 'string' && (SUPPORTED_LANGS as readonly string[]).includes(value);
}

/**
 * STR-09: a stored manual choice wins; otherwise the browser language if it is Portuguese or
 * English; otherwise English.
 */
export function detectInitialLang(
  navigatorLanguage: string | null | undefined,
  stored: string | null | undefined,
): Lang {
  if (isLang(stored)) return stored;
  const browser = (navigatorLanguage ?? '').toLowerCase();
  if (browser.startsWith('pt')) return 'pt';
  return 'en';
}

function readStored(): string | null {
  try {
    return globalThis.localStorage?.getItem(LANG_STORAGE_KEY) ?? null;
  } catch {
    return null;
  }
}

/** Runtime-switchable i18n built on signals (DA-7); no i18n library. */
@Injectable({ providedIn: 'root' })
export class I18nService {
  private readonly document = inject(DOCUMENT);
  private readonly current = signal<Lang>(
    detectInitialLang(globalThis.navigator?.language, readStored()),
  );

  readonly lang: Signal<Lang> = this.current.asReadonly();

  constructor() {
    this.document.documentElement.lang = this.current();
  }

  setLang(lang: Lang): void {
    this.current.set(lang);
    this.document.documentElement.lang = lang;
    try {
      globalThis.localStorage?.setItem(LANG_STORAGE_KEY, lang);
    } catch {
      // Storage may be blocked (private mode); the choice still applies to this tab.
    }
  }

  /** Text of `key` in the current language, with `{name}` placeholders filled from `params`. */
  t(key: MessageKey, params?: MessageParams): string {
    const template = MESSAGES[this.current()][key];
    if (!params) return template;
    return template.replace(/\{(\w+)\}/g, (match, name: string) =>
      name in params ? String(params[name]) : match,
    );
  }
}
