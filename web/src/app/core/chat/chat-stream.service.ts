import { HttpErrorResponse, HttpHeaders } from '@angular/common/http';
import { Injectable } from '@angular/core';
import { Observable, Subscriber } from 'rxjs';

import {
  ChatRequest,
  ConfirmationDecision,
  ConfirmationRequest,
  StreamEvent,
} from './chat.models';
import { parseSse } from './sse-parser';

const API_BASE = '/api';

/**
 * Streaming chat client (CT-51) for `POST /chat/stream` and
 * `POST /chat/confirmations/{id}/stream` (CT-76).
 *
 * Uses `fetch` + `ReadableStream` because `EventSource` cannot POST a body. Each subscription
 * opens one request; unsubscribing before the stream ends calls `AbortController.abort()`
 * (STR-04). SSE `error` events are emitted like any other event (the page decides what to show)
 * and the Observable completes right after `done` or `error`. Failures before the stream opens
 * (non-2xx, network) error the Observable with an `HttpErrorResponse`, so `toApiError` from
 * `core/http` handles them exactly as for `ChatApiService` (e.g. 401 → `AUTH_REQUIRED`).
 */
@Injectable({ providedIn: 'root' })
export class ChatStreamService {
  stream(req: ChatRequest): Observable<StreamEvent> {
    return this.open(`${API_BASE}/chat/stream`, req);
  }

  confirmStream(id: string, decision: ConfirmationDecision): Observable<StreamEvent> {
    const body: ConfirmationRequest = { decision };
    return this.open(`${API_BASE}/chat/confirmations/${encodeURIComponent(id)}/stream`, body);
  }

  private open(url: string, body: unknown): Observable<StreamEvent> {
    return new Observable<StreamEvent>((subscriber) => {
      const controller = new AbortController();
      const state = { settled: false };

      void consume(url, body, controller.signal, subscriber, state);

      return () => {
        if (!state.settled) controller.abort();
      };
    });
  }
}

interface StreamState {
  /** True once the Observable ended on its own (terminal event or error). */
  settled: boolean;
}

async function consume(
  url: string,
  body: unknown,
  signal: AbortSignal,
  subscriber: Subscriber<StreamEvent>,
  state: StreamState,
): Promise<void> {
  const fail = (err: unknown): void => {
    if (subscriber.closed) return;
    state.settled = true;
    subscriber.error(err);
  };

  let response: Response;
  try {
    response = await fetch(url, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify(body),
      signal,
    });
  } catch (err) {
    fail(networkError(url, err));
    return;
  }

  try {
    if (!response.ok) {
      fail(await httpError(url, response));
      return;
    }
    if (!response.body) {
      fail(networkError(url, new Error('Response has no body')));
      return;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
      const { done, value } = await reader.read();
      const parsed = done
        ? parseSse(decoder.decode(), buffer)
        : parseSse(decoder.decode(value, { stream: true }), buffer);
      buffer = parsed.rest;

      for (const event of parsed.events) {
        if (subscriber.closed) return;
        subscriber.next(event);
        if (event.type === 'done' || event.type === 'error') {
          state.settled = true;
          subscriber.complete();
          // Let the server close the connection on its own: aborting here would look like a
          // client disconnect (STR-91) while it may still be finishing the turn.
          await drain(reader);
          return;
        }
      }

      if (done) {
        fail(networkError(url, new Error('Stream ended before a terminal event')));
        return;
      }
    }
  } catch (err) {
    // An abort triggered by unsubscribe lands here with the subscriber already closed.
    fail(networkError(url, err));
  }
}

async function drain(reader: ReadableStreamDefaultReader<Uint8Array>): Promise<void> {
  try {
    for (;;) {
      const { done } = await reader.read();
      if (done) return;
    }
  } catch {
    // Nothing is listening anymore.
  }
}

function networkError(url: string, cause: unknown): HttpErrorResponse {
  return new HttpErrorResponse({ status: 0, statusText: 'Unknown Error', url, error: cause });
}

async function httpError(url: string, response: Response): Promise<HttpErrorResponse> {
  const text = await response.text();
  let error: unknown = text;
  if (text) {
    try {
      error = JSON.parse(text);
    } catch {
      // Not JSON: keep the raw text, `toApiError` maps it to UNKNOWN.
    }
  }
  const headers: Record<string, string> = {};
  response.headers.forEach((value, key) => {
    headers[key] = value;
  });
  return new HttpErrorResponse({
    status: response.status,
    statusText: response.statusText,
    url,
    headers: new HttpHeaders(headers),
    error,
  });
}
