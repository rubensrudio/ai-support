import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';

import {
  ChatRequest,
  ChatResponse,
  ConfirmationDecision,
  ConfirmationRequest,
} from './chat.models';

const API_BASE = '/api';

/** Non-streaming chat client (CT-26). Streaming lives in `chat-stream.service.ts` (phase 9). */
@Injectable({ providedIn: 'root' })
export class ChatApiService {
  private readonly http = inject(HttpClient);

  send(req: ChatRequest): Observable<ChatResponse> {
    return this.http.post<ChatResponse>(`${API_BASE}/chat`, req, { withCredentials: true });
  }

  confirm(id: string, decision: ConfirmationDecision): Observable<ChatResponse> {
    const body: ConfirmationRequest = { decision };
    return this.http.post<ChatResponse>(
      `${API_BASE}/chat/confirmations/${encodeURIComponent(id)}`,
      body,
      { withCredentials: true },
    );
  }
}
