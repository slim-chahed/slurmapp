# Plan: Refined Landing + Dynamic Server Status + Esprit UI Overhaul

## Goal
- Landing page shows dynamic UP/DOWN indicator with downtime duration.
- `/login` always allows LDAP auth, but routes users based on role and system health.
- Regular user + system down → gets JWT session, then sees a "please wait" system-down page.
- Regular user + system up → normal dashboard flow.
- Admin + system down → after login, sees admin panel with full per-service breakdown.
- All HTML templates receive a cohesive black/white/red Esprit-branded UI.

## Confirmed Decisions
- SSH username/path for VM: already configured and working.
- Container names match `docker-compose.yml`: `app-mysql`, `ldap-server`.
- System-down state does **not** block JWT issuance; it only changes post-login routing and UI.

## Affected Routes / Behavior
| Route | Behavior |
|---|---|
| `/` | Public landing with Esprit theme, UP/DOWN indicator, downtime text, login link, contact text. |
| `/login` (GET) | Public login form (always accessible). |
| `/login` (POST) | Always attempts LDAP auth. On success: create JWT. If system down → redirect to `/system-down`; if system up → redirect to `/dashboard`. |
| `/system-down` | Public page. Regular users see: "server is down, please wait". Admin sees: same page plus full per-service status table + admin log-back-in link. Unauthenticated users see landing page. |
| `/dashboard`, `/request`, `/reservation/{id}`, `/admin` | Middleware-gated: if overall_up=False, authenticated users are redirected to `/system-down`. |
| `/admin` (GET/POST) | Existing admin approve/reject behavior unchanged. When down, admin is redirected to `/system-down` (which also shows the detail table). |

## New Files
- None — reuse `templates/landing.html`, `templates/server_down.html`.

## Modified Files
- `config.py` — no change needed; current SSH config is sufficient.
- `main.py` — 
  - Remove current HealthGateMiddleware.
  - Add simple health-gate dependency logic per-route or middleware that preserves public paths, landings, and login.
  - Add `/system-down` route with `role`-aware template context.
  - Ensure `/login` POST assigns JWT then redirects based on health.
- `templates/landing.html` — keep Esprit theme, enhance status badge styling, ensure dynamic uptime/downtime text.
- `templates/server_down.html` — gate content by role. Show generic "please wait" message for non-admins; for admins append service breakdown table + return-to-admin link.
- `templates/login.html` — Esprit theme styling.
- `templates/dashboard.html` — Esprit navbar + status summary + refined tables.
- `templates/request_form.html` — Esprit form styling.
- `templates/reservation_detail.html` — Esprit styling.
- `templates/admin_panel.html` — Esprit table, badges, alert banners.
- `static/style.css` — centralized Esprit variables, navbar, tables, buttons, badges, responsive tweaks.

## Config Already Present
- `VM_SSH_USER`, `VM_SSH_KEY_PATH`, `VM_HEALTH_CACHE_SECONDS` — implemented and validated.

## UI/UX Scope (Esprit Theme)
- Colors: `#c8102e` (red), `#1a1a1a` (black), `#ffffff` (white).
- Shared navbar on all authenticated pages: Esprit logo wordmark, current user + role, logout.
- Shared footer.
- Buttons, cards, tables, alerts, forms all using Esprit palette.
- Status indicators: green `UP`, red `DOWN`.

## Health-Gate Logic
```
Request → middleware sees non-public path
  → system down?
      yes → if authenticated → redirect /system-down
      no  → allow
```
Exception: `/login` POST must bypass middleware so login/auth still issues JWT.

## Validation Plan
- `/` renders landing, shows DOWN indicator, downtime text, login button, contact note.
- `/login` loads and submits.
- `docker/ldap/name` issue is already fixed in vm_monitor.
- Test matrix:
  1. System up + regular user → `/dashboard`.
  2. System down + regular user → JWT set, redirect `/system-down` (generic message).
  3. System down + admin → JWT set, redirect `/system-down` (service detail table visible).
  4. `/dashboard` with system down → redirect to `/system-down`.
  5. All pages visually match Esprit theme.
