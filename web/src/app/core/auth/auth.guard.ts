import { inject } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';
import { catchError, map, of } from 'rxjs';

import { AuthService } from './auth.service';

/** Lets the route through with a valid session; otherwise redirects to `/login` (CT-25). */
export const authGuard: CanActivateFn = () => {
  const auth = inject(AuthService);
  const router = inject(Router);
  const toLogin = router.createUrlTree(['/login']);

  if (auth.isAuthenticated()) return true;

  return auth.me().pipe(
    map((me) => (me ? true : toLogin)),
    catchError(() => of(toLogin)),
  );
};
