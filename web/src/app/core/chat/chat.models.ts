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
