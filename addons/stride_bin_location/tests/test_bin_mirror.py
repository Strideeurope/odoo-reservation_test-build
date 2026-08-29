from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class TestBinMirrorManagement(TransactionCase):
    """W2 — mirror link field, find-or-create helper, and rename cascade."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.warehouse = cls.env["stock.warehouse"].search([], limit=1)
        cls.bin_parent = cls.env["stock.location"].create({
            "name": "W2 Test Bin Zone",
            "location_id": cls.warehouse.view_location_id.id,
            "usage": "bin",
        })
        cls.stock_sublocation = cls.env["stock.location"].create({
            "name": "W2-A123B",
            "location_id": cls.warehouse.lot_stock_id.id,
            "usage": "internal",
        })

    def test_get_or_create_creates_and_links_a_new_bin(self):
        bin_location = self.env["stock.location"]._get_or_create_bin_mirror(
            self.stock_sublocation, self.bin_parent
        )
        self.assertEqual(bin_location.usage, "bin")
        self.assertEqual(bin_location.name, self.stock_sublocation.name)
        self.assertEqual(bin_location.location_id, self.bin_parent)
        self.assertEqual(bin_location.stock_sublocation_id, self.stock_sublocation)

    def test_get_or_create_is_idempotent(self):
        first = self.env["stock.location"]._get_or_create_bin_mirror(
            self.stock_sublocation, self.bin_parent
        )
        second = self.env["stock.location"]._get_or_create_bin_mirror(
            self.stock_sublocation, self.bin_parent
        )
        self.assertEqual(first, second)
        self.assertEqual(
            self.env["stock.location"].search_count(
                [("stock_sublocation_id", "=", self.stock_sublocation.id)]
            ),
            1,
        )

    def test_unique_constraint_blocks_a_second_bin_for_the_same_sublocation(self):
        self.env["stock.location"]._get_or_create_bin_mirror(self.stock_sublocation, self.bin_parent)
        with self.assertRaises(Exception):
            self.env["stock.location"].create({
                "name": "Duplicate mirror",
                "location_id": self.bin_parent.id,
                "usage": "bin",
                "stock_sublocation_id": self.stock_sublocation.id,
            })

    def test_barcode_blocked_on_bin_location(self):
        with self.assertRaises(ValidationError):
            self.env["stock.location"].create({
                "name": "W2 Barcoded Bin",
                "location_id": self.bin_parent.id,
                "usage": "bin",
                "barcode": "W2BARCODE1",
            })

    def test_non_bin_location_cannot_carry_the_link(self):
        other_sublocation = self.env["stock.location"].create({
            "name": "W2-Other",
            "location_id": self.warehouse.lot_stock_id.id,
            "usage": "internal",
        })
        with self.assertRaises(ValidationError):
            other_sublocation.write({"stock_sublocation_id": self.stock_sublocation.id})

    def test_bin_cannot_mirror_another_bin(self):
        other_bin = self.env["stock.location"].create({
            "name": "W2 Other Bin",
            "location_id": self.bin_parent.id,
            "usage": "bin",
        })
        bin_location = self.env["stock.location"]._get_or_create_bin_mirror(
            self.stock_sublocation, self.bin_parent
        )
        with self.assertRaises(ValidationError):
            other_bin.write({"stock_sublocation_id": bin_location.id})

    def test_rename_cascades_from_stock_side_to_bin(self):
        bin_location = self.env["stock.location"]._get_or_create_bin_mirror(
            self.stock_sublocation, self.bin_parent
        )
        self.stock_sublocation.write({"name": "W2-RENAMED"})
        self.assertEqual(bin_location.name, "W2-RENAMED")

    def test_rename_cascades_from_bin_side_to_stock(self):
        bin_location = self.env["stock.location"]._get_or_create_bin_mirror(
            self.stock_sublocation, self.bin_parent
        )
        bin_location.write({"name": "W2-RENAMED-FROM-BIN"})
        self.assertEqual(self.stock_sublocation.name, "W2-RENAMED-FROM-BIN")
