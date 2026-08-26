from odoo import SUPERUSER_ID, _, api


def migrate(cr, version):

    env = api.Environment(cr, SUPERUSER_ID, {})

    show_next_product_wave = env.ref("ventor_base.show_next_product_batch_picking", False)
    show_next_product_batch = env.ref("ventor_base.show_next_product_wave_picking", False)

    value = {
        "description": "Product field will show the next product to be picked. Use the setting during "
                       "picking and delivery. It is recommended to disable the setting for the reception area"
    }

    if show_next_product_wave:
        show_next_product_wave.write(value)
    if show_next_product_batch:
        show_next_product_batch.write(value)

    operation_type_model = env['stock.picking.type'].with_context(active_test=False)
    users = env['res.users'].with_context(active_test=False).search([
        ('share', '=', False),
    ])

    for user in users:
        allowed_warehouse_ids = user.allowed_warehouse_ids.ids
        if not allowed_warehouse_ids:
            continue

        allowed_operation_types = operation_type_model.search([
            ('warehouse_id', 'in', allowed_warehouse_ids),
        ])

        user.allowed_operation_type_ids = [(6, 0, allowed_operation_types.ids)]

    # Update stock picking type rule for inventiry users
    rule = env.ref('ventor_base.stock_picking_type_rule_stock_user', raise_if_not_found=False)
    if rule:
        rule.write({
            'domain_force': "[('id', 'in', user.allowed_operation_type_ids.ids)]"
        })
