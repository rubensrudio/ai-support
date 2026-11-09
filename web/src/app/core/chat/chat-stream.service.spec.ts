import { HttpErrorResponse } from '@angular/common/http';
import { TestBed } from '@angular/core/testing';
import { Subscription, firstValueFrom, toArray } from 'rxjs';

import { apiErrorMessage, toApiError } from '../http/api-error';
import { ChatRequest, StreamEvent } from './chat.models';
import { ChatStreamService } from './chat-stream.service';

const REQUEST: ChatRequest = { message: 'Hi', conversation_id: null, ui_language: 'en' };

const TOKEN: StreamEvent = { type: 'token', text: 'Hel' };
const TOKEN_2: StreamEvent = { type: 'token', text: 'lo' };
const DONE: StreamEvent = {
  type: 'done',
  status: 'completed',
  message_id: 7,
  conversation_id: '11111111-1111-1111-1111-111111111111',
  trace_id: 'trace-1',
  language: 'en',
  content: 'Hello',
  confirmation: null,
};
const ERROR: StreamEvent = {
  type: 'error',
  code: 'STREAM_ERROR',
  message: 'boom',
  trace_id: 'trace-2',
};

function sse(event: StreamEvent): string {
  return `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`;
}

/** A body that sends `chunks` and then closes, unless `keepOpen` is set. */
function body(chunks: string[], keepOpen = false): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      if (!keepOpen) controller.close();
    },
  });
}

function streamResponse(chunks: string[], keepOpen = false): Response {
  return new Response(body(chunks, keepOpen), {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  });
}

describe('ChatStreamService', () => {
  let service: ChatStreamService;
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    TestBed.configureTestingModule({});
    service = TestBed.inject(ChatStreamService);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('POSTs the request to /api/chat/stream with credentials and SSE headers', async () => {
    fetchMock.mockResolvedValue(streamResponse([sse(DONE)]));

    await firstValueFrom(service.stream(REQUEST).pipe(toArray()));

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/chat/stream');
    expect(init.method).toBe('POST');
    expect(init.credentials).toBe('include');
    expect(init.headers).toEqual({
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
    });
    expect(JSON.parse(init.body as string)).toEqual(REQUEST);
    expect(init.signal).toBeInstanceOf(AbortSignal);
  });

  it('confirmStream POSTs the decision to /api/chat/confirmations/{id}/stream', async () => {
    fetchMock.mockResolvedValue(streamResponse([sse(DONE)]));

    await firstValueFrom(service.confirmStream('a/b', 'refuse').pipe(toArray()));

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/chat/confirmations/a%2Fb/stream');
    expect(JSON.parse(init.body as string)).toEqual({ decision: 'refuse' });
  });

  it('emits an event split across two chunks once and completes after done', async () => {
    const raw = sse(TOKEN) + sse(TOKEN_2);
    const cut = sse(TOKEN).length + 10;
    fetchMock.mockResolvedValue(
      streamResponse([raw.slice(0, 5), raw.slice(5, cut), raw.slice(cut), sse(DONE)]),
    );

    const events = await firstValueFrom(service.stream(REQUEST).pipe(toArray()));

    expect(events).toEqual([TOKEN, TOKEN_2, DONE]);
  });

  it('decodes a multi-byte character split between chunks', async () => {
    const bytes = new TextEncoder().encode(sse({ type: 'token', text: 'olá' }) + sse(DONE));
    const split = bytes.indexOf(0xc3) + 1;
    fetchMock.mockResolvedValue(
      new Response(
        new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(bytes.slice(0, split));
            controller.enqueue(bytes.slice(split));
            controller.close();
          },
        }),
        { status: 200 },
      ),
    );

    const events = await firstValueFrom(service.stream(REQUEST).pipe(toArray()));

    expect(events[0]).toEqual({ type: 'token', text: 'olá' });
  });

  it('completes right after done even if the connection stays open, and ignores later events', async () => {
    fetchMock.mockResolvedValue(streamResponse([sse(TOKEN) + sse(DONE) + sse(TOKEN_2)], true));

    const events = await firstValueFrom(service.stream(REQUEST).pipe(toArray()));

    expect(events).toEqual([TOKEN, DONE]);
  });

  it('emits an SSE error as an event and completes', async () => {
    fetchMock.mockResolvedValue(streamResponse([sse(TOKEN) + sse(ERROR) + sse(DONE)], true));

    const events = await firstValueFrom(service.stream(REQUEST).pipe(toArray()));

    expect(events).toEqual([TOKEN, ERROR]);
  });

  it('unsubscribe calls abort() on the request controller', async () => {
    const abort = vi.spyOn(AbortController.prototype, 'abort');
    fetchMock.mockResolvedValue(streamResponse([sse(TOKEN)], true));

    const received: StreamEvent[] = [];
    let sub: Subscription | undefined;
    await new Promise<void>((resolve) => {
      sub = service.stream(REQUEST).subscribe((event) => {
        received.push(event);
        resolve();
      });
    });
    sub?.unsubscribe();

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(received).toEqual([TOKEN]);
    expect(abort).toHaveBeenCalledTimes(1);
    expect(init.signal?.aborted).toBe(true);
  });

  it('unsubscribe before the response arrives aborts the request', () => {
    fetchMock.mockReturnValue(new Promise<Response>(() => undefined));

    const sub = service.stream(REQUEST).subscribe();
    sub.unsubscribe();

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.signal?.aborted).toBe(true);
  });

  it('does not abort after the stream completed with done', async () => {
    const abort = vi.spyOn(AbortController.prototype, 'abort');
    fetchMock.mockResolvedValue(streamResponse([sse(DONE)]));

    await firstValueFrom(service.stream(REQUEST).pipe(toArray()));

    expect(abort).not.toHaveBeenCalled();
  });

  it('turns a non-2xx response into an HttpErrorResponse carrying the API envelope', async () => {
    const envelope = { error: { code: 'AUTH_REQUIRED', message: 'Login', trace_id: 't-401' } };
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify(envelope), {
        status: 401,
        statusText: 'Unauthorized',
        headers: { 'Content-Type': 'application/json', 'X-Trace-Id': 't-401' },
      }),
    );

    const err: unknown = await firstValueFrom(service.stream(REQUEST)).catch((e: unknown) => e);

    expect(err).toBeInstanceOf(HttpErrorResponse);
    const httpErr = err as HttpErrorResponse;
    expect(httpErr.status).toBe(401);
    expect(httpErr.error).toEqual(envelope);
    expect(httpErr.headers.get('X-Trace-Id')).toBe('t-401');
    expect(toApiError(err)).toEqual({
      status: 401,
      code: 'AUTH_REQUIRED',
      traceId: 't-401',
      retryAt: null,
    });
  });

  it('keeps a non-JSON error body as text', async () => {
    fetchMock.mockResolvedValue(new Response('Bad gateway', { status: 502 }));

    const err: unknown = await firstValueFrom(service.stream(REQUEST)).catch((e: unknown) => e);

    expect((err as HttpErrorResponse).status).toBe(502);
    expect((err as HttpErrorResponse).error).toBe('Bad gateway');
    expect(toApiError(err).code).toBe('UNKNOWN');
  });

  it('turns a network failure into a NETWORK error', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'));

    const err: unknown = await firstValueFrom(service.stream(REQUEST)).catch((e: unknown) => e);

    expect(err).toBeInstanceOf(HttpErrorResponse);
    expect(apiErrorMessage(toApiError(err)).key).toBe('errors.NETWORK');
  });

  it('errors with NETWORK when the body ends before a terminal event', async () => {
    fetchMock.mockResolvedValue(streamResponse([sse(TOKEN)]));

    const seen: StreamEvent[] = [];
    const err = await new Promise<unknown>((resolve) => {
      service.stream(REQUEST).subscribe({
        next: (event) => seen.push(event),
        error: resolve,
        complete: () => resolve(null),
      });
    });

    expect(seen).toEqual([TOKEN]);
    expect(toApiError(err).code).toBe('NETWORK');
  });
});
