# PWS + Vercel session authentication

WSGI and ASGI default to `config.production`. Local manage.py/runserver uses
`config.settings`; a local `.env` does not define the public runtime's security
defaults. Production disables Django debug pages, enables Secure cookies and
includes these exact canonical application endpoints:

- API host: `fiqhi-deski-food-rescue.pws.cs.ui.ac.id`
- frontend origin: `https://food-rescue-fe.vercel.app`

Other deployments can set `PUBLIC_API_HOST` and `PUBLIC_WEB_ORIGIN`. Additional
allowed hosts/CORS/CSRF origins can still be supplied using the existing lists.
If explicitly setting `DJANGO_SETTINGS_MODULE` on PWS, use `config.production`.

Vercel uses `VITE_API_BASE_URL=/api/v1` for production. The repository's
`vercel.json` forwards `/api/*` to the PWS backend, including uploaded photos.
Session/CSRF cookies are therefore first-party to the frontend and keep
SameSite=Lax with Secure enabled. The frontend uses the masked token returned
by `/api/v1/auth/csrf/` for write requests.

After deploying, verify the **public frontend domain**, not just a local test
server: `/api/v1/auth/session/` must return JSON with authenticated=false for a
guest, CSRF bootstrap must set a Secure cookie, and a login followed by a session
lookup must remain authenticated. API paths must not return the SPA HTML shell.
