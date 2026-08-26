from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class TestBinLocationExclusion(TransactionCase):
    """T2 — quants on a 'bin'-usage location must be invisible to on-hand,
    forecast, and reordering computation."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.warehouse = cls.env["stock.warehouse"].search([], limit=1)
        cls.bin_location = cls.env["stock.location"].create({
            "name": "T2 Test Bin",
            "location_id": cls.warehouse.view_location_id.id,
            "usage": "bin",
        })
        cls.product = cls.env["product.product"].create({
            "name": "T2 Bin Exclusion Widget",
            "is_storable": True,
        })
        cls.env["stock.quant"].create({
            "product_id": cls.product.id,
            "location_id": cls.bin_location.id,
            "quantity": 50,
        })

    def test_qty_available_excludes_bin_quants(self):
        self.assertEqual(self.product.qty_available, 0)
        scoped = self.product.with_context(location=self.warehouse.lot_stock_id.id)
        self.assertEqual(scoped.qty_available, 0)
        self.assertEqual(scoped.free_qty, 0)

    def test_virtual_available_excludes_bin_quants(self):
        scoped = self.product.with_context(location=self.warehouse.lot_stock_id.id)
        self.assertEqual(scoped.virtual_available, 0)

    def test_reordering_computation_excludes_bin_quants(self):
        orderpoint = self.env["stock.warehouse.orderpoint"].create({
            "name": "T2 Test Orderpoint",
            "warehouse_id": self.warehouse.id,
            "location_id": self.warehouse.lot_stock_id.id,
            "product_id": self.product.id,
            "product_min_qty": 10,
            "product_max_qty": 20,
        })
        self.assertEqual(orderpoint.qty_on_hand, 0)

    def test_bin_quant_still_directly_readable(self):
        quant = self.env["stock.quant"].search([
            ("product_id", "=", self.product.id),
            ("location_id", "=", self.bin_location.id),
        ])
        self.assertEqual(quant.quantity, 50)
