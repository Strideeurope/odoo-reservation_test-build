from . import models


def post_init_hook(env):
    warehouses = env["stock.warehouse"].search([])
    warehouses.mapped("pick_type_id").write({"reservation_method": "manual"})
