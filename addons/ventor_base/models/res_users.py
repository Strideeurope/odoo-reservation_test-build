# Copyright 2020 VentorTech OU
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).

import json

from odoo import _, api, models, fields


class ResUsers(models.Model):
    _inherit = 'res.users'

    allowed_warehouse_ids = fields.Many2many(
        comodel_name='stock.warehouse',
        string='Allowed Warehouses',
        help='List of all warehouses user has access to',
    )

    allowed_operation_type_ids = fields.Many2many(
        comodel_name='stock.picking.type',
        string='Allowed Operation Types',
        readonly=True,
        help='List of all operation types user has access to',
    )

    custom_package_name = fields.Char(
        string='Custom Build Name',
        compute="_compute_custom_package_name",
        compute_sudo=True,
    )

    ventor_base_version = fields.Char(
        compute="_compute_ventor_base_version",
        compute_sudo=True,
        readonly=True,
    )

    ventor_global_settings = fields.Text(
        string='Global Settings',
        readonly=True,
        compute='_compute_global_settings'
    )

    ventor_user_settings = fields.Text(
        string='User Settings'
    )

    ventor_filter_settings = fields.Text(
        string='Filter Settings'
    )

    @property
    def SELF_READABLE_FIELDS(self):
        readable_fields = [
            'ventor_global_settings',
            'ventor_user_settings',
            'custom_package_name',
            'ventor_base_version',
            'ventor_filter_settings',
        ]
        return super().SELF_READABLE_FIELDS + readable_fields

    @property
    def SELF_WRITEABLE_FIELDS(self):
        writable_fields = ['ventor_user_settings', 'ventor_filter_settings']
        return super().SELF_WRITEABLE_FIELDS + writable_fields

    def action_configure_allowed_operation_types(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Allowed Operation Types'),
            'res_model': 'res.users.allowed.operation.type.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_user_id': self.id,
            },
        }

    def _compute_custom_package_name(self):
        custom_package_name = (
            self.env["ir.config_parameter"]
            .get_param("ventor_base.custom_package_name", "")
        )
        self.custom_package_name = custom_package_name

    def _compute_ventor_base_version(self):
        ventor_base_version = (
            self.env["ir.module.module"]
            .search([("name", "=", "ventor_base"), ("state", "=", "installed")])
            .latest_version
        )
        for user in self:
            if ventor_base_version:
                user.ventor_base_version = ventor_base_version
            else:
                user.ventor_base_version = ""

    def _compute_global_settings(self):
        settings = []

        for stock_picking_type in self.env['stock.picking.type'].search([]):
            stock_picking_type_settings = stock_picking_type.get_warehouse_operation_settings()
            if stock_picking_type.code != 'outgoing':
                stock_picking_type_settings['settings'].pop('check_shipping_information')
            if stock_picking_type.code != 'incoming':
                stock_picking_type_settings['settings'].pop('hide_qty_to_receive')
            settings.append(stock_picking_type_settings)

        ventor_option_settings = self._get_ventor_option_settings()

        obj = {'operation_types': settings}
        obj.update(ventor_option_settings)

        self.ventor_global_settings = json.dumps(
            obj=obj,
            indent='    ',
            sort_keys=True
        )

    def _get_ventor_option_settings(self):
        ventor_option_settings = self.env['ventor.option.setting'].sudo().get_general_settings()
        if self.env.ref('ventor_base.merp_wave_picking_menu') not in self.groups_id:
            ventor_option_settings.pop('wave_picking')
        return ventor_option_settings

    def _set_operation_types(self, previous_wh_ids_by_user):
        for user in self:
            previous_wh_ids = previous_wh_ids_by_user[user.id]
            current_wh_ids = set(user.allowed_warehouse_ids.ids)

            added_wh_ids = current_wh_ids - previous_wh_ids
            removed_wh_ids = previous_wh_ids - current_wh_ids

            # 1) Add warehouse -> add active operation types
            if added_wh_ids:
                added_operation_types = self.env['stock.picking.type'].search([
                    ('warehouse_id', 'in', list(added_wh_ids)),
                    ('active', '=', True),
                ])
                user.allowed_operation_type_ids |= added_operation_types

            # 2) Remove warehouse -> remove operation types
            if removed_wh_ids:
                removed_operation_types = self.env['stock.picking.type'].with_context(active_test=False).search([
                    ('warehouse_id', 'in', list(removed_wh_ids)),
                ])
                user.allowed_operation_type_ids -= removed_operation_types

    @api.model_create_multi
    def create(self, vals_list):
        result = super().create(vals_list)
        if not result.allowed_warehouse_ids:
            warehouse_ids = self.env["stock.warehouse"].sudo().with_context(active_test=False).search([('company_id', 'in', result.company_ids.ids)]).ids
            result.write(
                {
                    'allowed_warehouse_ids': warehouse_ids
                }
            )
        return result

    def write(self, vals):
        if self.env.context.get('skip_allowed_wh_sync'):
            return super().write(vals)

        warehouses_changed = 'allowed_warehouse_ids' in vals
        operation_types_changed = 'allowed_operation_type_ids' in vals

        if not (warehouses_changed or operation_types_changed):
            return super().write(vals)

        previous_wh_ids_by_user = {}
        if warehouses_changed:
            previous_wh_ids_by_user = {
                user.id: set(user.allowed_warehouse_ids.ids)
                for user in self
            }

        result = super().write(vals)
        if result:
            self.env.registry.clear_cache()

            if warehouses_changed:
                self.with_context(skip_allowed_wh_sync=True)._set_operation_types(previous_wh_ids_by_user)

        return result
