import { TestBed } from '@angular/core/testing';

import { I18nService, LANG_STORAGE_KEY, detectInitialLang } from './i18n.service';
import { MESSAGES } from './messages';

function freshService(navigatorLanguage: string): I18nService {
  vi.spyOn(navigator, 'language', 'get').mockReturnValue(navigatorLanguage);
  TestBed.resetTestingModule();
  return TestBed.inject(I18nService);
}

describe('I18nService', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it('starts in Portuguese for a pt-BR browser', () => {
    expect(freshService('pt-BR').lang()).toBe('pt');
  });

  it('starts in English for an en-US browser', () => {
    expect(freshService('en-US').lang()).toBe('en');
  });

  it('falls back to English for any other browser language', () => {
    expect(freshService('de-DE').lang()).toBe('en');
  });

  it('prefers the stored choice over the browser language', () => {
    localStorage.setItem(LANG_STORAGE_KEY, 'pt');
    expect(freshService('en-US').lang()).toBe('pt');
  });

  it('ignores an invalid stored value', () => {
    expect(detectInitialLang('pt-PT', 'fr')).toBe('pt');
  });

  it('setLang switches the translated text and persists the choice', () => {
    const i18n = freshService('en-US');
    const english = i18n.t('login.title');

    i18n.setLang('pt');

    expect(i18n.lang()).toBe('pt');
    expect(i18n.t('login.title')).not.toBe(english);
    expect(i18n.t('login.title')).toBe(MESSAGES.pt['login.title']);
    expect(localStorage.getItem(LANG_STORAGE_KEY)).toBe('pt');
    expect(document.documentElement.lang).toBe('pt');
  });

  it('interpolates named parameters', () => {
    const i18n = freshService('en-US');
    expect(i18n.t('chat.counter', { count: 12, max: 4000 })).toBe('12 / 4000');
  });

  it('has exactly the same keys in the pt and en dictionaries', () => {
    const pt = Object.keys(MESSAGES.pt).sort();
    const en = Object.keys(MESSAGES.en).sort();
    expect(pt).toEqual(en);
    for (const key of en) {
      expect(MESSAGES.en[key as keyof typeof MESSAGES.en].length).toBeGreaterThan(0);
      expect(MESSAGES.pt[key as keyof typeof MESSAGES.pt].length).toBeGreaterThan(0);
    }
  });
});
