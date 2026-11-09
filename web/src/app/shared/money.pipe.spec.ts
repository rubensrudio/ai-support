import { MoneyPipe } from './money.pipe';

describe('MoneyPipe', () => {
  const pipe = new MoneyPipe();

  it('formats USD cents as en-US for English', () => {
    expect(pipe.transform(1250, 'en')).toBe('$12.50');
  });

  it('formats USD cents with the pt-BR convention for Portuguese', () => {
    const text = pipe.transform(1250, 'pt');
    expect(text).toContain('12,50');
    expect(text).toContain('US$');
  });

  it('returns an empty string when there is no amount', () => {
    expect(pipe.transform(null, 'en')).toBe('');
    expect(pipe.transform(undefined, 'pt')).toBe('');
  });
});
