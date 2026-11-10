import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable, map } from 'rxjs';

const API_BASE = '/api';

/** Rating of an assistant answer (CT-65). */
export type FeedbackRating = 'up' | 'down';

/** Body of `POST /messages/{message_id}/feedback` (CT-65). */
export interface FeedbackRequest {
  rating: FeedbackRating;
}

/**
 * Client of `POST /messages/{message_id}/feedback` (CT-65, OBS-02). The server answers 204 and
 * upserts one rating per message, so sending another rating replaces the previous one.
 */
@Injectable({ providedIn: 'root' })
export class FeedbackApiService {
  private readonly http = inject(HttpClient);

  send(messageId: number, rating: FeedbackRating): Observable<void> {
    const body: FeedbackRequest = { rating };
    return this.http
      .post<unknown>(`${API_BASE}/messages/${encodeURIComponent(messageId)}/feedback`, body, {
        withCredentials: true,
      })
      .pipe(map(() => undefined));
  }
}
