# -*- coding: utf-8 -*-
from odoo import api, fields, models, _


class ResUsersAllowedOperationTypeWizard(models.TransientModel):
    _name = 'res.users.allowed.operation.type.wizard'
    _description = 'Configure Allowed Operation Types'

    user_id = fields.Many2one('res.users', required=True, ondelete='cascade')

    warehouse_ids = fields.Many2many(
        'stock.warehouse',
        string='Allowed Warehouses',
        readonly=True,
    )

    operation_type_ids = fields.Many2many(
        'stock.picking.type',
        string='Operation Types',
        help='Selected operation types that will be written to the user',
    )

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        user_id = res.get('user_id') or self.env.context.get('default_user_id')
        if not user_id:
            return res

        user = self.env['res.users'].browse(user_id).exists()
        if not user:
            return res

        res['user_id'] = user.id
        res['warehouse_ids'] = [(6, 0, user.allowed_warehouse_ids.ids)]
        res['operation_type_ids'] = [(6, 0, user.allowed_operation_type_ids.ids)]
        return res

    def _get_domain_picking_types(self):
        self.ensure_one()
        return [('warehouse_id', 'in', self.warehouse_ids.ids)]

    def action_select_all(self):
        self.ensure_one()
        picking_types = self.env['stock.picking.type'].search(self._get_domain_picking_types())
        self.operation_type_ids = [(6, 0, picking_types.ids)]
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_unselect_all(self):
        self.ensure_one()
        self.operation_type_ids = [(5, 0, 0)]
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_save(self):
        self.ensure_one()
        user = self.user_id

        allowed_wh = user.allowed_warehouse_ids
        selected_pts = self.operation_type_ids.filtered(lambda pt: pt.warehouse_id in allowed_wh)

        user.allowed_operation_type_ids = [(6, 0, selected_pts.ids)]
        return {'type': 'ir.actions.act_window_close'}
