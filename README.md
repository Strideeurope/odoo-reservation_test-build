# Odoo 18 setup — clearance_reservation

Docker Compose stack: Postgres 16, Odoo 18, Caddy (reverse proxy/tunnel front).

## Before first run

Both `docker-compose.yml` and `config/odoo.conf` have `CHANGE_ME` placeholders
that must be filled in with **matching** values before starting the stack:

- `docker-compose.yml`: `db.environment.POSTGRES_PASSWORD` and
  `odoo.environment.PASSWORD` — must be identical to each other.
- `config/odoo.conf`: `db_password` — must match the value above.
- `config/odoo.conf`: `admin_passwd` — the Odoo master password (protects
  database management/backup/restore). Generate a fresh random value; it
  does not need to match anything else.

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
