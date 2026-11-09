import { HttpErrorResponse, provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { firstValueFrom } from 'rxjs';

import { apiErrorMessage, toApiError } from '../http/api-error';
import { ChatApiService } from './chat-api.service';
import { ChatResponse } from './chat.models';

const RESPONSE: ChatResponse = {
  conversation_id: '11111111-1111-1111-1111-111111111111',
  message_id: 7,
  status: 'completed',
  content: 'Hello!',
  language: 'en',
  citations: [],
  confirmation: null,
  trace_id: 'trace-1',
};

describe('ChatApiService', () => {
  let http: HttpTestingController;
  let api: ChatApiService;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    http = TestBed.inject(HttpTestingController);
    api = TestBed.inject(ChatApiService);
  });

  afterEach(() => http.verify());

  it('send POSTs the CT-14 body to /api/chat with credentials', async () => {
    const result = firstValueFrom(
      api.send({ message: 'Hi', conversation_id: null, ui_language: 'en' }),
    );

    const req = http.expectOne('/api/chat');
    expect(req.request.method).toBe('POST');
    expect(req.request.withCredentials).toBe(true);
    expect(req.request.body).toEqual({ message: 'Hi', conversation_id: null, ui_language: 'en' });
    req.flush(RESPONSE);

    expect(await result).toEqual(RESPONSE);
  });

  it("confirm('abc','refuse') POSTs the decision to /api/chat/confirmations/abc", async () => {
    const result = firstValueFrom(api.confirm('abc', 'refuse'));

    const req = http.expectOne('/api/chat/confirmations/abc');
    expect(req.request.method).toBe('POST');
    expect(req.request.withCredentials).toBe(true);
    expect(req.request.body).toEqual({ decision: 'refuse' });
    req.flush(RESPONSE);

    expect(await result).toEqual(RESPONSE);
  });

  it('confirm encodes the confirmation id in the path', () => {
    api.confirm('a/b', 'confirm').subscribe();
    http.expectOne('/api/chat/confirmations/a%2Fb').flush(RESPONSE);
  });
});

describe('api error mapping', () => {
  function httpError(status: number, body: unknown): HttpErrorResponse {
    return new HttpErrorResponse({ status, error: body });
  }

  it('reads the error envelope', () => {
    const err = toApiError(
      httpError(404, {
        error: { code: 'CONVERSATION_NOT_FOUND', message: 'Not found', trace_id: 't-9' },
      }),
    );
    expect(err).toEqual({
      status: 404,
      code: 'CONVERSATION_NOT_FOUND',
      traceId: 't-9',
      retryAt: null,
    });
    expect(apiErrorMessage(err)).toEqual({ key: 'errors.CONVERSATION_NOT_FOUND', params: {} });
  });

  it.each([
    'NOT_FOUND',
    'METHOD_NOT_ALLOWED',
    'AUTH_REQUIRED',
    'INVALID_CREDENTIALS',
    'CONVERSATION_NOT_FOUND',
    'VALIDATION_ERROR',
    'LLM_UNAVAILABLE',
    'DAILY_CAP_REACHED',
    'INTERNAL_ERROR',
  ])('maps %s to its own i18n key', (code) => {
    const err = toApiError(httpError(400, { error: { code, message: 'x', trace_id: 't' } }));
    expect(apiErrorMessage(err).key).toBe(`errors.${code}`);
  });

  it('passes the max length to MESSAGE_TOO_LONG', () => {
    const err = toApiError(
      httpError(422, { error: { code: 'MESSAGE_TOO_LONG', message: 'x', trace_id: 't' } }),
    );
    expect(apiErrorMessage(err)).toEqual({
      key: 'errors.MESSAGE_TOO_LONG',
      params: { max: 4000 },
    });
  });

  it('uses retry_at for RATE_LIMITED and a generic text without it', () => {
    const withTime = toApiError(
      httpError(429, {
        error: { code: 'RATE_LIMITED', message: 'x', trace_id: 't', retry_at: '2026-11-09T15:30:00Z' },
      }),
    );
    expect(withTime.retryAt).toBe('2026-11-09T15:30:00Z');
    expect(apiErrorMessage(withTime, 'en').key).toBe('errors.RATE_LIMITED');
    expect(apiErrorMessage(withTime, 'en').params['retry_at']).toMatch(/\d{1,2}:\d{2}/);

    const topLevel = toApiError(
      httpError(429, {
        error: { code: 'RATE_LIMITED', message: 'x', trace_id: 't' },
        retry_at: '2026-11-09T15:30:00Z',
      }),
    );
    expect(topLevel.retryAt).toBe('2026-11-09T15:30:00Z');

    const without = toApiError(
      httpError(429, { error: { code: 'RATE_LIMITED', message: 'x', trace_id: 't' } }),
    );
    expect(apiErrorMessage(without).key).toBe('errors.RATE_LIMITED_GENERIC');
  });

  it('treats status 0 as a network error and unknown codes as unknown', () => {
    expect(apiErrorMessage(toApiError(httpError(0, null))).key).toBe('errors.NETWORK');
    expect(apiErrorMessage(toApiError(httpError(500, '<html>'))).key).toBe('errors.UNKNOWN');
    expect(
      apiErrorMessage(toApiError(httpError(500, { error: { code: 'WHAT', message: '', trace_id: '' } })))
        .key,
    ).toBe('errors.UNKNOWN');
    expect(apiErrorMessage(toApiError(new Error('boom'))).key).toBe('errors.UNKNOWN');
  });
});
