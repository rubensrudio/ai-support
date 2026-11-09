import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Injectable, Signal, computed, inject, signal } from '@angular/core';
import { Observable, catchError, finalize, map, of, tap, throwError } from 'rxjs';

import { Lang } from '../i18n/messages';

/** Signed-in persona as returned by `/auth/login` and `/auth/me` (CT-17). */
export interface Me {
  display_name: string;
  language: Lang;
}

interface LoginResponse {
  customer: Me;
}

const AUTH_BASE = '/api/auth';

/**
 * Session client (CT-25). The session itself is an HttpOnly cookie set by the API (DA-10); this
 * service only remembers who is signed in, never a token.
 */
@Injectable({ providedIn: 'root' })
export class AuthService {
  private readonly http = inject(HttpClient);
  private readonly current = signal<Me | null>(null);

  readonly user: Signal<Me | null> = this.current.asReadonly();
  readonly isAuthenticated: Signal<boolean> = computed(() => this.current() !== null);

  login(username: string, password: string): Observable<Me> {
    return this.http
      .post<LoginResponse>(
        `${AUTH_BASE}/login`,
        { username, password },
        { withCredentials: true },
      )
      .pipe(
        map((res) => res.customer),
        tap((me) => this.current.set(me)),
      );
  }

  /** Ends the session on the server; the local state is cleared even if the call fails. */
  logout(): Observable<void> {
    return this.http
      .post<void>(`${AUTH_BASE}/logout`, null, { withCredentials: true })
      .pipe(finalize(() => this.clear()));
  }

  /** Current persona, or `null` when the API answers 401. Other failures are re-thrown. */
  me(): Observable<Me | null> {
    return this.http.get<Me>(`${AUTH_BASE}/me`, { withCredentials: true }).pipe(
      tap((me) => this.current.set(me)),
      catchError((err: unknown) => {
        if (err instanceof HttpErrorResponse && err.status === 401) {
          this.clear();
          return of(null);
        }
        return throwError(() => err);
      }),
    );
  }

  /** Forgets the signed-in persona (e.g. after a 401 from another endpoint). */
  clear(): void {
    this.current.set(null);
  }
}
