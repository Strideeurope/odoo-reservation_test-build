# Clearance Reservation — Technical Report

This document covers three things for whoever inherits this system:

1. How the Odoo stack was set up from scratch (infrastructure).
2. Every piece of custom business logic in the `clearance_reservation` module, and *why* it exists.
3. What's needed to reproduce or maintain this exact working state.

---

## 1. Infrastructure — setting this up from scratch

### Stack

Three Docker containers, defined in `docker-compose.yml`:

| Service | Image | Purpose |
|---|---|---|
| `db` | `postgres:16` | Odoo's database |
| `odoo` | `odoo:18` | The application itself |
| `caddy` | `caddy:2` | Reverse proxy (also used as the tunnel front for external access) |

Odoo's own web server binds to `127.0.0.1:8069` only (not exposed to the network directly) — Caddy is what actually fronts it on ports 80/443, per `Caddyfile`.

The custom module lives on the host at `./addons/clearance_reservation` and is bind-mounted into the Odoo container at `/mnt/extra-addons` — this is what makes the module a "custom addon" rather than something built into the Odoo image. `config/odoo.conf` is likewise bind-mounted to `/etc/odoo/odoo.conf` and sets `addons_path`, `dbfilter`, `admin_passwd`, and DB connection settings.

### Bringing it up from nothing

```bash
git clone <this repo>
cd odoo18-setup
# Fill in docker-compose.yml's POSTGRES_PASSWORD / PASSWORD (must match each
# other) and config/odoo.conf's db_password (must match those) and
# admin_passwd (independent, generate a fresh random value).
docker compose up -d
```

On first boot there's no database yet — visit `http://localhost:8069` (or through whatever Caddy exposes) and Odoo's own "create database" screen handles the rest. Once a database named to match `dbfilter` in `odoo.conf` (currently `^odoo$`, i.e. a database literally named `odoo`) exists, the module needs to be installed:

```bash
docker exec <odoo_container> odoo -i clearance_reservation -d odoo --stop-after-init
```

(`-i` installs; later changes use `-u` to upgrade — see section 3.)

Two required app dependencies come along automatically since they're declared in the manifest: `sale_management`, `stock`, `account`, and `purchase` (the last one specifically because the "Scheduled Future Stock" feature reads `stock.move.purchase_line_id`, a field that only exists once the Purchase app is installed).

### Permissions

One security group is added: **Reservation Override** (`clearance_reservation.group_reservation_override`). It gates every manual bypass in the module — overriding the fulfillment stage or clearance timestamp directly, and releasing a hard lock or a forced reservation. Odoo Administrators (`base.group_system`) are granted this automatically and self-heal into it if group membership is ever reset (see `security/security.xml`).

### Scheduled jobs

Two cron jobs (`data/ir_cron.xml`), both daily:

- **`_cron_reserve_by_clearance`** — an unscoped, full run of the reservation engine. This is a *safety net*, not the primary mechanism — see section 2.7 for why it's needed at all.
- **`_cron_expire_grace_period`** — demotes any order that's been sitting unpaid in the grace period past its window (see 2.2).

---

## 2. The business logic

### 2.0 The core idea

Under vanilla Odoo, stock reservation for sale orders happens more or less first-come-first-served, with no concept of "this customer paid, that one didn't" affecting who gets scarce stock first. This module replaces that with a payment-driven priority queue: **an order only competes for stock once it has a `clearance_date`**, and whoever has the earliest `clearance_date` for a given product wins newly available stock first. Everything else in the module exists to define what earns a `clearance_date`, what can jump the queue anyway, and how that queue self-corrects as circumstances change (payments, refunds, locks, incoming shipments).

### 2.1 Fulfillment stages

A new field, `fulfillment_stage`, replaces "confirmed or not" as the meaningful state of an order for warehouse purposes:

| Stage | Meaning |
|---|---|
| **No Invoice** | Not competing for stock at all. |
| **Grace Period** | Freshly confirmed, competing provisionally (see 2.2). |
| **Order / Pick** | Genuinely paid (in part or reconciling), competing for real. |
| **Ship** | Fully paid — allowed to actually ship. |

This is shown as a badge on the sale order form, on the Pick/Ship transfer header, and in the Inventory forecast view — always the same four colors (muted/info/warning/success).

### 2.2 Grace period

Confirming an order (`action_confirm`) doesn't require payment to join the queue — it gets a `clearance_date` immediately and competes like a paid order, but only for `GRACE_PERIOD_DAYS` (15). The timestamp used is the order's **`create_date`**, not the moment of confirmation — a quotation that sat around for a week before being confirmed keeps its original place in line.

If no genuine payment arrives within the window, the daily cron (`_cron_expire_grace_period`) demotes the order back to No Invoice, clears its `clearance_date` with **no backup kept** — losing the grace period is a fresh start, not something to resume from later.

### 2.3 Payment-driven promotion (`account_move.py`)

Reacting to payment state changes has to hook `_compute_payment_state()` rather than `write()` — `payment_state` is a stored compute field driven by reconciliation, updated through the ORM's internal flush, which never goes through `write()`.

- The **first** time real payment becomes active (`partial`/`in_payment`/`paid`) on an invoice, its orders get a genuine `clearance_date` — either a fresh timestamp, or a backed-up original one if the order previously lost its clearance to a full refund (see 2.5).
- The exact moment used isn't "now" — it's the real reconciliation timestamp (`account.partial.reconcile.create_date`), not the payment's own day-granularity Date field, so two same-day payments still queue in the correct order.
- No Invoice / Grace Period orders advance to Order/Pick the moment payment becomes active.
- **Ship depends on payment status alone** — reversing an earlier design decision where Ship also required the Pick step to be fully reserved. An order can now ship whatever it does have as soon as it's fully paid, even if one line never got stock at all.
- A **refund** (credit note) is invisible to the invoice-side logic entirely — it's handled by a separate `_clearance_apply_refund_payment_state()`, since a refund can only ever *un-clear* an order, never clear one.
- `_is_fully_paid()` / `_has_active_payment()` compare **net amounts** (paid total vs. settled-refund total) rather than "any refund exists at all" — so a later payment cycle that outweighs an old, already-resolved refund is correctly treated as fully paid again, not permanently disqualified by history.

### 2.4 Manual overrides (`sale_order.py: write()`)

An admin (in the Reservation Override group) can force any stage directly, including Ship, on an unpaid order:

- Overriding **to No Invoice** backs up the current `clearance_date` rather than discarding it.
- Overriding **into Grace Period / Order-Pick / Ship** *does* stamp a timestamp — the order competes and validates exactly as if it had genuinely cleared. This is flagged `clearance_is_override = True`, purely for display (so a fabricated timestamp is never shown as if it were a real payment moment).
- The moment genuine payment actually arrives afterward, it **replaces** the fabricated timestamp with the real one and clears the flag — an override is a stand-in, never something that outlives real payment showing up.
- An override-elevated order is **exempt from payment-based demotion** until real payment intervenes — otherwise a brand-new invoice's `payment_state` starting at `not_paid` the instant it's posted (before anyone even attempts payment) would look identical to "payment was just reversed" and wipe out the override.

### 2.5 Refunds

Fixed a real bug where refunding a fully-paid, shipped order left it stuck at Ship forever — refunds were invisible both to `_is_fully_paid()` and to the payment-change hook. Now:

- A **full** refund (nothing paid at all any more) drops the order all the way to No Invoice — it has no more genuine claim on stock than any other unpaid order. Its `clearance_date` is **backed up**, not discarded.
- A **partial** refund (some money still paid) only drops Ship → Order/Pick, keeping its original `clearance_date` — payment status changed, but its place in line didn't.
- Getting paid again (or a later override) restores the exact backed-up timestamp — never a fresh one that would send the order to the back of the line.

### 2.6 Hard lock and force-reserve — two different overrides

Both bypass the payment gate, but they are fundamentally different:

- **Hard lock** (order-level `is_reservation_hard_locked`, or the same field scoped to one line) has **zero acquisition ability**. Its only job is protecting whatever an order already holds — however it got it — from ever being unreserved by any process, including a manual override. A hard-locked order with no genuine clearance and no force-reserve is *never even attempted* by the queue; it can only hold stock some other path granted it (a native manual reservation, or payment gained later).
- **Force-reserve** (line-level `is_force_reserved`) is an *active* override — it genuinely competes for stock, and outranks ordinary payment-tier priority for anything not already spoken for.

Both are enforced at the `stock.move` level too (`_do_unreserve` / `_action_cancel` raise if you try to release a hard-locked or force-reserve-locked move without an explicit override context), so the protection holds even outside this module's own reservation pass.

Releasing either one requires the Reservation Override group, and immediately re-runs the queue for the affected product so the freed stock finds its next rightful claimant right away.

### 2.7 The reservation engine (`_reserve_by_clearance`)

This is the heart of the module — a full release-and-reallocate pass, run per affected product, that:

1. Builds the domain of orders/moves that are actually eligible to compete (queued stage, or force-reserved).
2. Identifies stock held by ineligible ("foreign") orders — e.g. an unpaid order holding stock via some path this module doesn't gate — and reclaims it into the pool.
3. Excludes far-future orders (scheduled more than `FAR_FUTURE_MONTHS` (6) out) from competing for today's stock, unless overridden by a hard lock or force-reserve.
4. Releases everything **not explicitly protected** and reallocates strictly by priority tier (see below) — this is what lets an order with an earlier clearance date reclaim stock a later-clearance order happened to grab first. A reservation that only ever got what was free at the moment it ran could never self-correct an ordering mistake after the fact.
5. Runs a second, separate targeted pass for "Scheduled Future Stock" releases (2.8).

**Priority tiers** (`move_priority`), highest first:

| Tier | Who | Note |
|---|---|---|
| 0 | Scheduled Future Stock reclaim | Outranks even force-reserve — see 2.8. |
| 1 | Force-reserved | Only ever takes stock that's genuinely available. |
| 2 | Genuine clearance (payment/grace period) | Ordered by `clearance_date`, earliest first. |
| 3 | Everything else | No real claim; picks up leftovers only. |

Hard lock is **deliberately absent** from this ranking — it grants no claim on new stock at all, only protects what's already held (guaranteed unconditionally, separately, via the `protected` set and the move-level checks above).

**Performance note**: this can run on almost every mutation in the system (a lock toggle, a payment, a receipt, a quantity edit) without becoming expensive, because of a fast path — if every relevant move is already `assigned`, nothing foreign is squatting, and nothing far-future is holding stock, the method returns immediately with no release/reallocate work at all. Most calls hit this path.

Scoped by `product_ids` when possible — an order's turn for product A never competes with, or is affected by, its own demand for product B, so a "reserve this product only" trigger only ever does the (irrelevant-elsewhere) work for that product.

### 2.8 Scheduled Future Stock — the newest mechanism

**The problem it solves**: an order scheduled far in the future (say, 3 months out) might currently be holding stock that an order needing it *sooner* can't get. If a **committed** incoming purchase order will comfortably cover the far-future order's need before it's actually due, there's no reason for it to sit on stock today that someone else needs now.

**How it works**:

- A line qualifies to release its current stock if: it's currently holding some (`assigned`/`partially_available`), **and** `_has_safe_future_replacement()` returns true.
- `_has_safe_future_replacement()` finds the committed future incoming shipment nearest to `scheduled_date + 10 days` (`SCHEDULED_FUTURE_STOCK_MATCH_BUFFER_DAYS`), then requires that shipment to land at least `14 days` (`SCHEDULED_FUTURE_STOCK_RELEASE_BUFFER_DAYS`) before the line's own scheduled date, **and** that the cumulative committed incoming quantity up to that date actually covers the line's demand.
- "Committed" is a hard requirement — the shipment must come from a purchase order in state `purchase` or `done`, never a draft/sent RFQ that could fall through. This is checked via `stock.move.purchase_line_id.order_id.state`.
- Such a line is tagged `clearance_defer_reason = "Scheduled Future Stock"` and is **protected** from the ordinary blanket reallocation pass (2.7's step 4) — it can only ever be released through the dedicated targeted pass, to a **specifically earlier-scheduled** competitor for the same product, never swept up by ordinary clearance-date logic that knows nothing about scheduled dates.

**The safety guarantee** (added after explicit review — "another order steals his stock from this future incoming PO, that should not happen"):

Once a line actually gives up its stock this way, it needs to reliably get the promised future shipment back, ahead of anyone else — including a force-reserve that shows up afterward wanting the same product. Two things make this work:

1. A **persistent flag**, `is_scheduled_future_stock_release`, set the instant the release actually happens. Without this, the "Scheduled Future Stock" tag would vanish the moment the line's own moves left the assigned state (since the tag was originally derived purely from currently-held state) — exactly the instant a competitor could otherwise cut in for the very shipment this mechanism was staking its guarantee on. The flag survives holding *nothing at all*, and only clears once the line reclaims enough stock again, or its backing PO evaporates (cancelled/rescheduled) in the meantime.
2. **Tier 0 ranks above force-reserve (tier 1)** in `move_priority` — force-reserve only ever takes stock that's genuinely available, never the specific shipment a released holder is owed. This required two separate fixes: the tier ordering itself, *and* excluding force-reserved lines from the targeted release pass's own demand pool (a force-reserved order with an earlier scheduled date could otherwise still trigger a release from a holder through that separate code path, even after the tier fix).

### 2.9 Automatic re-triggering

The engine re-runs itself automatically from every relevant mutation point, so nothing needs a manual "recheck" button in normal use:

- `sale_order.write()` — on any `fulfillment_stage`, `clearance_date`, or hard-lock change.
- `sale_order_line.write()` — on `is_force_reserved`, `is_reservation_hard_locked`, or `product_uom_qty` changes.
- `stock_move._action_assign()` — closes a gap where the native forecast view's "Reserve" link bypasses the queue entirely; re-runs it for the affected product immediately after.
- `stock_quant._apply_inventory()` — a manual inventory count (up or down) re-derives the right allocation from current on-hand.
- `stock_picking.button_validate()` — a completed incoming receipt is exactly the moment a pending order might now be satisfiable.
- The daily cron — a pure safety net for anything outside all of the above (bulk data imports, direct DB/API mutation, a genuine gap).

All of these carry internal context flags (`_within_reserve_by_clearance`, `clearance_internal_write`) to prevent the engine from recursively re-triggering itself mid-run.

### 2.10 Chatter logging

Every order now gets automatic log notes (visible in the standard Odoo chatter) for:

- Hard lock enabled/released (order-level and per-line).
- Force-reserve enabled/released (per-line).
- Every manual stage override, noting whether a timestamp was freshly stamped, restored from backup, or left unchanged.
- Grace period entry and expiry.
- Payment-driven promotion/demotion, including whether a fresh timestamp was used or a backup restored.
- Reservation queue runs — **one consolidated note per affected order per run** (not one per move) listing what was reserved or released and why (which priority tier won it), so a single event that ripples across several lines doesn't spam the log.

### 2.11 Guardrails on physical operations (`stock_picking.py`)

- A Pick or Ship transfer cannot be validated for an order with no `clearance_date` at all — a hard lock only earns the right to *hold* stock in advance, it never authorizes completing a physical warehouse operation for an order that's never actually joined the queue.
- The Ship leg specifically also requires `fulfillment_stage == "ship"` (i.e., fully paid) — disambiguated against the warehouse's actual Output location rather than relying on the picking type's code alone, since that code isn't guaranteed unique to the Ship leg on every routing configuration.

### 2.12 Frontend surfaces

- **Sale order form**: fulfillment stage badge, an editable "Override Stage" field, the clearance timestamp, scheduled date, and the hard-lock toggle — plus, on the order lines list, per-line Force Reserve / Hard Lock toggles, force-reserve action buttons, and a "Reservation Hold" badge showing `clearance_defer_reason` (Scheduled Far Out / Scheduled Future Stock).
- **Pick/Ship transfer form**: a header badge summarizing the order's fulfillment stage, clearance status (including "(override)" wording when the timestamp is fabricated), and the strongest lock reason across the transfer's lines. The Source Document field becomes a clickable link straight to the sale order.
- **Inventory Forecast report** (`stock.forecasted_product_product`, patched both server-side and via OWL component patches):
  - An "Unreserved" figure alongside the native header stats.
  - A "Reserve by Clearance" button that runs the engine for just that product and reports how many moves/orders it touched.
  - Per-line, the same fulfillment-stage badge as the sale order, a "Clearance Override" badge when relevant, and a lock/defer badge (Scheduled Far Out, Scheduled Future Stock, or a generic lock) with its own icon and tooltip.
  - A three-way sort toggle (**Clearance** / **Delivery Date** / **Locked First**) — Clearance is the server's own default order; the other two are purely client-side re-sorts of the same already-loaded lines, so switching is instant with no re-fetch.
  - A picking excluded entirely if its move is sourced from the Output location — that's the Ship leg of an order whose Pick already completed, no longer meaningful "demand against inventory."
  - **Note**: Odoo's own native "Not Available" replenishment verdict, and the red highlighting that comes with it, is a separate, native simulation that doesn't know about this module's priority logic — it was evaluated whether to override that display for "Scheduled Future Stock" lines specifically, but this was **reverted** at the time of writing; the native text can currently show "Not Available" for a line this engine has, in fact, already guaranteed a reservation on.

---

## 3. Reproducing / maintaining this state

### Deploying a code change

```bash
# 1. Upgrade the module (always, for any Python/XML/view change)
docker exec <odoo_container> odoo -u clearance_reservation -d odoo --stop-after-init

# 2. Run the test suite
docker exec <odoo_container> odoo --test-enable --test-tags /clearance_reservation \
  -d odoo --stop-after-init --http-port=8070

# 3. If any JS/XML (frontend) file changed, clear the asset bundle cache —
#    otherwise the browser keeps serving the old bundled JS:
#    delete ir.attachment records where url like '/web/assets/%'

# 4. Restart the container so the running web server process picks up the
#    new Python (an upgrade run is a separate one-off process; the long-
#    running server process doesn't hot-reload Python modules):
docker restart <odoo_container>
```

### Tests

`tests/test_clearance_reservation.py` — 24 tests, all passing at the time of writing. The class docstring is a running log of every bug found and explicit product decision made during development; treat it as the authoritative changelog for *why* the logic looks the way it does. Every new mechanism in this module was built with accompanying tests, including the trickiest parts (timestamp precedence between override and real payment, the full-refund backup/restore cycle, and the Scheduled Future Stock safety guarantee end-to-end).

### Known constants worth knowing about

| Constant | Value | Meaning |
|---|---|---|
| `GRACE_PERIOD_DAYS` | 15 | How long a confirmed-but-unpaid order competes provisionally. |
| `FAR_FUTURE_MONTHS` | 6 | Orders scheduled beyond this don't compete for today's stock. |
| `SCHEDULED_FUTURE_STOCK_RELEASE_BUFFER_DAYS` | 14 | Minimum safety margin between an incoming shipment and the need date, to justify releasing current stock. |
| `SCHEDULED_FUTURE_STOCK_MATCH_BUFFER_DAYS` | 10 | Offset used to pick which incoming shipment is "the" match for a given line. |

All four live at the top of `models/sale_order.py`.
