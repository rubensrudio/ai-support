/**
 * TypeScript mirror of `POST /chat` (CT-14, `api/chat/schemas.py`) and of the confirmation
 * endpoint (CT-46). Field names follow the API (snake_case) on purpose.
 */

import { Lang } from '../i18n/messages';

export type { ApiErrorBody, ApiErrorCode, ApiErrorEnvelope } from '../http/api-error';
export { MAX_MESSAGE_CHARS } from '../http/api-error';

export type ChatStatus =
  | 'completed'
  | 'awaiting_confirmation'
  | 'blocked'
  | 'degraded'
  | 'limit_reached';

export interface ChatRequest {
  message: string;
  conversation_id: string | null;
  ui_language: Lang | null;
}

export interface Citation {
  index: number;
  document: string;
  section: string | null;
  page: number | null;
}

export interface ConfirmationPrompt {
  id: string;
  tool: string;
  target: string;
  amount_cents: number | null;
  currency: 'USD' | null;
}

export interface ChatResponse {
  conversation_id: string | null;
  message_id: number | null;
  status: ChatStatus;
  content: string;
  language: Lang;
  citations: Citation[];
  confirmation: ConfirmationPrompt | null;
  trace_id: string;
}

export type ConfirmationDecision = 'confirm' | 'refuse';

export interface ConfirmationRequest {
  decision: ConfirmationDecision;
}

/**
 * Server-sent events of `POST /chat/stream` and `POST /chat/confirmations/{id}/stream` (CT-76),
 * mirroring `api/agent/events.py` (CT-45). The SSE `event:` name equals `type`.
 */
/** First event of a stream: conversation and trace of the turn, before any token. */
export interface StartStreamEvent {
  type: 'start';
  conversation_id: string | null;
  trace_id: string;
}

export interface TokenStreamEvent {
  type: 'token';
  text: string;
}

export interface ToolStartStreamEvent {
  type: 'tool_start';
  call_id: string;
  tool: string;
  label_key: string;
}

export interface ToolEndStreamEvent {
  type: 'tool_end';
  call_id: string;
  tool: string;
  ok: boolean;
}

export interface CitationStreamEvent {
  type: 'citation';
  index: number;
  document: string;
  doc_key: string | null;
  section: string | null;
  page: number | null;
}

export interface DoneStreamEvent {
  type: 'done';
  status: ChatStatus;
  message_id: number | null;
  conversation_id: string | null;
  trace_id: string;
  language: Lang;
  content: string;
  confirmation: ConfirmationPrompt | null;
}

export interface ErrorStreamEvent {
  type: 'error';
  code: string;
  message: string;
  trace_id: string;
}

export type StreamEvent =
  | StartStreamEvent
  | TokenStreamEvent
  | ToolStartStreamEvent
  | ToolEndStreamEvent
  | CitationStreamEvent
  | DoneStreamEvent
  | ErrorStreamEvent;
