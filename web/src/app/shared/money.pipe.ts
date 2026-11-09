import { Pipe, PipeTransform } from '@angular/core';

import { Lang } from '../core/i18n/messages';

/** The only currency of the demo (TOOL-19, LAC-30). */
export const CURRENCY = 'USD';

const LOCALES: Readonly<Record<Lang, string>> = { en: 'en-US', pt: 'pt-BR' };

const formatters = new Map<Lang, Intl.NumberFormat>();

function formatter(lang: Lang): Intl.NumberFormat {
  let fmt = formatters.get(lang);
  if (!fmt) {
    fmt = new Intl.NumberFormat(LOCALES[lang], { style: 'currency', currency: CURRENCY });
    formatters.set(lang, fmt);
  }
  return fmt;
}

/**
 * Formats an amount in USD cents with the convention of the conversation language (TOOL-19):
 * `en` → `$12.50`, `pt` → `US$ 12,50`. Missing amounts render as an empty string.
 */
@Pipe({ name: 'money' })
export class MoneyPipe implements PipeTransform {
  transform(cents: number | null | undefined, lang: Lang): string {
    if (cents === null || cents === undefined || !Number.isFinite(cents)) return '';
    return formatter(lang).format(cents / 100);
  }
}
