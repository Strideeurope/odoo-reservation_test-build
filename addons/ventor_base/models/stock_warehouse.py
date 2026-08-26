# Copyright 2020 VentorTech OU
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

from odoo import api, models, fields


class StockWarehouse(models.Model):
    _inherit = 'stock.warehouse'

    is_internal = fields.Boolean(
        string='Is Internal Warehouse',
    )

    @api.model_create_multi
    def create(self, vals_list):
        res = super(StockWarehouse, self).create(vals_list)
        if res:
            res.update_users_calculated_warehouse()
        return res

    def _get_users(self):
        """Get users that should be assigned to warehouses"""
        users = self.env['res.users'].search([
            ('share', '=', False),
            ('active', '=', True),
        ])

        odoo_bot = self.env.ref('base.user_root', raise_if_not_found=False)
        if odoo_bot:
            users |= odoo_bot

        return users

    def _get_warehouses(self, warehouse):
        """Return a set of warehouse IDs of the same company except the given one"""
        return set(
            self.env['stock.warehouse']
            .with_context(active_test=False)
            .search([
                ('company_id', '=', warehouse.company_id.id),
                ('id', '!=', warehouse.id),
            ])
            .ids
        )

    def update_users_calculated_warehouse(self):
        users = self._get_users()

        for warehouse in self:
            wh_ids = self._get_warehouses(warehouse)
            modified_user_ids = []

            for user in users:
                # Because of specifics of how Odoo works with companies on first start,
                # we have to filter warehouses by company
                user_wh_ids = set(
                    user.allowed_warehouse_ids.filtered(lambda wh: wh.company_id.id == warehouse.company_id.id).ids
                )

                if wh_ids == user_wh_ids:
                    user.allowed_warehouse_ids = [(4, warehouse.id, 0)]
                    modified_user_ids.append(user.id)

            # Because access rights are using this field, we need to invalidate cache
            if modified_user_ids:
                self.env['res.users'].browse(modified_user_ids).invalidate_recordset(
                    [
                        'allowed_warehouse_ids',
                    ]
                )
