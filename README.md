# Newtonite Ops

A compact, full-stack operational work queue built for the Newtonite Software Engineering Challenge. It uses Python's standard library, SQLite, and a responsive browser interface; no package installation or external service is required.

## Run it

Requirements: Python 3.10 or newer.

1. Open a terminal in this folder.
2. Run `python server.py` (Windows: `py server.py` also works).
3. Visit [http://127.0.0.1:8000](http://127.0.0.1:8000).

The app creates `newtonite.db` on first start and loads a small demo dataset. Set `PORT` or `NEWTONITE_DB` to change the listening port or database path. Use the **View as** selector to explore the demo roles and team scopes. This identity selector is a demo convenience and must be replaced by trusted single sign-on before deployment.

## What is included

- Create, search, filter, browse, claim, assign, and update operational work items, including a named next action.
- Status and priority overview, team-aware queue, pagination, item detail, and chronological activity history.
- Users can belong to multiple teams and hold different roles in each: viewer, member, or manager. Admins see all teams. Managers can reassign; viewers are read-only. The API enforces scope and role on each resource.
- Compare-and-swap version checks reject stale updates; a transaction makes claims atomic; idempotency keys make retried actions safe.
- Indexed, server-side filtering and pagination avoid loading the full work history into the browser.

## Automated tests

Run `python -m unittest discover -s tests -v`. The tests cover team boundaries, read-only roles, stale writes, retry-safe and simultaneous claims, next-action persistence/search, and assignment validation.

## Public demo deployment

The repository includes `render.yaml` for a Render web service. Push the project to GitHub, connect that repository in Render as a Blueprint, and deploy. The service binds to the public host and uses `/health` for health checks. The demo database is stored under `/tmp`, so it is intentionally reset when the free service restarts or redeploys. The public demo contains only synthetic sample data. Anyone who opens it can switch demo identities and modify sample work; never use it for real company information.

GitHub Actions runs the automated test suite for pushes and pull requests.

## Known limitations

- Authentication is simulated by a client-selected demo identity. Use a trusted identity provider and signed sessions in a real deployment.
- SQLite is appropriate for a single-node assessment demo, but a shared PostgreSQL service is a better fit for sustained concurrent use and growing history.
- The UI demonstrates the core workflows; notifications, approval policy configuration, attachments, audit export, and background jobs are not implemented.
- The queue supports a 100-item maximum page and simple substring search. Production search would use a dedicated index or database full-text search.
- The local demo omits CSRF/session hardening and rate limiting; add these before deployment.
