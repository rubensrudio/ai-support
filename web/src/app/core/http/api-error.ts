import { HttpErrorResponse } from '@angular/common/http';

import { Lang, MessageKey } from '../i18n/messages';

/** Every code of the API error catalog (plan 8.3) plus the framework-level ones. */
export const API_ERROR_CODES = [
  'VALIDATION_ERROR',
  'MESSAGE_TOO_LONG',
  'AUTH_REQUIRED',
  'INVALID_CREDENTIALS',
  'CONVERSATION_NOT_FOUND',
  'CONFIRMATION_NOT_FOUND',
  'CONFIRMATION_NOT_PENDING',
  'MESSAGE_NOT_FOUND',
  'RATE_LIMITED',
  'DAILY_CAP_REACHED',
  'LLM_UNAVAILABLE',
  'TRIAGE_UNAVAILABLE',
  'INTERNAL_ERROR',
  'STREAM_ERROR',
  'NOT_FOUND',
  'METHOD_NOT_ALLOWED',
] as const;

export type ApiErrorCode = (typeof API_ERROR_CODES)[number];

/** Body of the API error envelope (CT-3). */
export interface ApiErrorBody {
  code: string;
  message: string;
  trace_id: string;
  retry_at?: string | null;
}

export interface ApiErrorEnvelope {
  error: ApiErrorBody;
  retry_at?: string | null;
}

/** Normalised error the UI works with; never carries the server's raw text. */
export interface ApiError {
  status: number;
  code: ApiErrorCode | 'NETWORK' | 'UNKNOWN';
  traceId: string | null;
  retryAt: string | null;
}

export interface ErrorMessage {
  key: MessageKey;
  params: Record<string, string | number>;
}

/** Default `ATLAS_MAX_MESSAGE_CHARS` (CHAT-09/91). */
export const MAX_MESSAGE_CHARS = 4000;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function isApiErrorCode(value: unknown): value is ApiErrorCode {
  return typeof value === 'string' && (API_ERROR_CODES as readonly string[]).includes(value);
}

function asString(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null;
}

export function toApiError(err: unknown): ApiError {
  if (!(err instanceof HttpErrorResponse)) {
    return { status: -1, code: 'UNKNOWN', traceId: null, retryAt: null };
  }
  if (err.status === 0) {
    return { status: 0, code: 'NETWORK', traceId: null, retryAt: null };
  }
  const envelope: unknown = err.error;
  const body = isRecord(envelope) && isRecord(envelope['error']) ? envelope['error'] : null;
  const code = body?.['code'];
  return {
    status: err.status,
    code: isApiErrorCode(code) ? code : 'UNKNOWN',
    traceId: asString(body?.['trace_id']) ?? err.headers?.get('X-Trace-Id') ?? null,
    retryAt:
      asString(body?.['retry_at']) ??
      (isRecord(envelope) ? asString(envelope['retry_at']) : null),
  };
}

function formatTime(iso: string, lang: Lang): string | null {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleTimeString(lang === 'pt' ? 'pt-BR' : 'en-US', {
    hour: '2-digit',
    minute: '2-digit',
  });
}

/** i18n key and parameters for an API error, ready for `I18nService.t`. */
export function apiErrorMessage(err: ApiError, lang: Lang = 'en'): ErrorMessage {
  switch (err.code) {
    case 'NETWORK':
      return { key: 'errors.NETWORK', params: {} };
    case 'UNKNOWN':
      return { key: 'errors.UNKNOWN', params: {} };
    case 'MESSAGE_TOO_LONG':
      return { key: 'errors.MESSAGE_TOO_LONG', params: { max: MAX_MESSAGE_CHARS } };
    case 'RATE_LIMITED': {
      const time = err.retryAt ? formatTime(err.retryAt, lang) : null;
      return time
        ? { key: 'errors.RATE_LIMITED', params: { retry_at: time } }
        : { key: 'errors.RATE_LIMITED_GENERIC', params: {} };
    }
    default:
      return { key: `errors.${err.code}`, params: {} };
  }
}
