import { HttpErrorResponse, provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { firstValueFrom } from 'rxjs';

import { FeedbackApiService } from './feedback-api.service';

describe('FeedbackApiService', () => {
  let http: HttpTestingController;
  let api: FeedbackApiService;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    http = TestBed.inject(HttpTestingController);
    api = TestBed.inject(FeedbackApiService);
  });

  afterEach(() => http.verify());

  it('send POSTs the rating to /api/messages/{id}/feedback with credentials', async () => {
    const result = firstValueFrom(api.send(42, 'up'), { defaultValue: undefined });

    const req = http.expectOne('/api/messages/42/feedback');
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ rating: 'up' });
    expect(req.request.withCredentials).toBe(true);
    req.flush(null, { status: 204, statusText: 'No Content' });

    await expect(result).resolves.toBeUndefined();
  });

  it('send propagates a 404 MESSAGE_NOT_FOUND as an error', async () => {
    const result = firstValueFrom(api.send(7, 'down'));

    const req = http.expectOne('/api/messages/7/feedback');
    expect(req.request.body).toEqual({ rating: 'down' });
    req.flush(
      { error: { code: 'MESSAGE_NOT_FOUND', message: 'Message not found.', trace_id: 't' } },
      { status: 404, statusText: 'Not Found' },
    );

    await expect(result).rejects.toBeInstanceOf(HttpErrorResponse);
  });
});
