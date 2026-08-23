# Odoo 18 setup — clearance_reservation

Docker Compose stack: Postgres 16, Odoo 18, Caddy (reverse proxy/tunnel front).

## Before first run

Real, secret-bearing config is kept **local and gitignored** — only
`.example` templates are tracked. Copy each one and fill in real values
before starting the stack:

```
cp .env.example .env                          # DB_PASSWORD
cp config/odoo.conf.example config/odoo.conf  # db_password (must match .env), admin_passwd
cp Caddyfile.example Caddyfile                # your actual domain/IP
```

- `.env`'s `DB_PASSWORD` is read by `docker-compose.yml` for both the
  Postgres container and Odoo's own DB connection — one value, used twice.
- `config/odoo.conf`'s `db_password` must be the **same** value as
  `.env`'s `DB_PASSWORD` — a mismatch here breaks the DB connection (this
  has actually happened once: editing one without the other, or editing
  either after the containers are already running, silently leaves the
  live containers on whatever password they were originally created
  with, while newly-read config disagrees — see the warning below).
- `config/odoo.conf`'s `admin_passwd` is the Odoo master password
  (protects database management/backup/restore) — generate a fresh
  random value; it doesn't need to match anything else.
- `Caddyfile`'s domain/IP is whatever actually fronts this instance.

**Important**: `docker-compose.yml`'s `environment:` values only take
effect when a container is *created* — editing `.env` or
`docker-compose.yml` after `docker compose up` has already run does
**not** change the password on an already-running Postgres container.
If you need to change `DB_PASSWORD` on a live stack, either update it
inside Postgres itself to match, or recreate the containers
(`docker compose up -d --force-recreate`, which will need the data
volumes reset too since Postgres won't accept a password change to
existing data without one).

Then:

```
docker compose up -d
```

Odoo listens on `127.0.0.1:8069` by default (bound to localhost only —
see the Caddyfile for how this was exposed further, if at all).

## The module

`addons/clearance_reservation` — a payment-driven stock reservation
priority system for sale orders. See the module's own docstrings
(especially `tests/test_clearance_reservation.py`'s class docstring) for
the full list of behaviors and the bugs/decisions behind them.

To deploy a code change to this module:

```
docker exec <odoo_container> odoo -u clearance_reservation -d <db_name> --stop-after-init
docker exec <odoo_container> odoo --test-enable --test-tags /clearance_reservation -d <db_name> --stop-after-init --http-port=8070
# clear the asset cache (ir.attachment, unlink /web/assets/%) if JS/XML changed
docker restart <odoo_container>
```
