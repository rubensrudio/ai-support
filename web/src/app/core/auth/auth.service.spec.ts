import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { ActivatedRouteSnapshot, RouterStateSnapshot, UrlTree, provideRouter } from '@angular/router';
import { Observable, firstValueFrom } from 'rxjs';

import { authGuard } from './auth.guard';
import { AuthService, Me } from './auth.service';

const ALEX: Me = { display_name: 'Alex Morgan', language: 'en' };

function runGuard(): Observable<boolean | UrlTree> {
  return TestBed.runInInjectionContext(
    () =>
      authGuard({} as ActivatedRouteSnapshot, { url: '/chat' } as RouterStateSnapshot) as Observable<
        boolean | UrlTree
      >,
  );
}

describe('AuthService', () => {
  let http: HttpTestingController;
  let auth: AuthService;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting(), provideRouter([])],
    });
    http = TestBed.inject(HttpTestingController);
    auth = TestBed.inject(AuthService);
  });

  afterEach(() => http.verify());

  it('login POSTs to /api/auth/login and marks the session as authenticated', async () => {
    expect(auth.isAuthenticated()).toBe(false);

    const result = firstValueFrom(auth.login('alex', 'secret'));
    const req = http.expectOne('/api/auth/login');
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ username: 'alex', password: 'secret' });
    expect(req.request.withCredentials).toBe(true);
    req.flush({ customer: ALEX });

    expect(await result).toEqual(ALEX);
    expect(auth.isAuthenticated()).toBe(true);
    expect(auth.user()).toEqual(ALEX);
  });

  it('a failed login keeps the session unauthenticated', async () => {
    const result = firstValueFrom(auth.login('alex', 'wrong'));
    http
      .expectOne('/api/auth/login')
      .flush(
        { error: { code: 'INVALID_CREDENTIALS', message: 'Invalid', trace_id: 't1' } },
        { status: 401, statusText: 'Unauthorized' },
      );

    await expect(result).rejects.toBeTruthy();
    expect(auth.isAuthenticated()).toBe(false);
  });

  it('me returns null and clears the session on 401', async () => {
    const result = firstValueFrom(auth.me());
    http
      .expectOne('/api/auth/me')
      .flush(
        { error: { code: 'AUTH_REQUIRED', message: 'Sign in', trace_id: 't2' } },
        { status: 401, statusText: 'Unauthorized' },
      );

    expect(await result).toBeNull();
    expect(auth.isAuthenticated()).toBe(false);
  });

  it('logout POSTs to /api/auth/logout and clears the session', async () => {
    const login = firstValueFrom(auth.login('alex', 'secret'));
    http.expectOne('/api/auth/login').flush({ customer: ALEX });
    await login;

    const result = firstValueFrom(auth.logout(), { defaultValue: undefined });
    const req = http.expectOne('/api/auth/logout');
    expect(req.request.method).toBe('POST');
    req.flush(null, { status: 204, statusText: 'No Content' });
    await result;

    expect(auth.isAuthenticated()).toBe(false);
  });
});

describe('authGuard', () => {
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting(), provideRouter([])],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('redirects to /login when /api/auth/me answers 401', async () => {
    const result = firstValueFrom(runGuard());
    http
      .expectOne('/api/auth/me')
      .flush(
        { error: { code: 'AUTH_REQUIRED', message: 'Sign in', trace_id: 't3' } },
        { status: 401, statusText: 'Unauthorized' },
      );

    const outcome = await result;
    expect(outcome instanceof UrlTree).toBe(true);
    expect(TestBed.inject(AuthService).isAuthenticated()).toBe(false);
    expect(String(outcome)).toBe('/login');
  });

  it('lets the navigation through when /api/auth/me answers 200', async () => {
    const result = firstValueFrom(runGuard());
    http.expectOne('/api/auth/me').flush(ALEX);
    expect(await result).toBe(true);
  });

  it('redirects to /login when the session check fails for another reason', async () => {
    const result = firstValueFrom(runGuard());
    http.expectOne('/api/auth/me').flush(null, { status: 500, statusText: 'Server Error' });
    expect(String(await result)).toBe('/login');
  });
});
