import unittest

from customers import find_customer
from id_utils import canonical_id
from orders import import_order


class IdTests(unittest.TestCase):
    def setUp(self):
        self.customers = {"alice": {"name": "Alice"}}

    def test_canonical_ascii(self):
        self.assertEqual(canonical_id("  ALICE  "), "alice")

    def test_canonical_rejects_invalid_input(self):
        with self.assertRaises(TypeError):
            canonical_id(None)
        with self.assertRaises(ValueError):
            canonical_id("  ")

    def test_find_customer_ascii(self):
        self.assertEqual(find_customer(self.customers, " ALICE "), {"name": "Alice"})

    def test_import_order_ascii(self):
        self.assertEqual(import_order({"customer_id": " ALICE ", "quantity": 2},
                                      self.customers),
                         {"customer_id": "alice", "customer_name": "Alice",
                          "quantity": 2})

    def test_lookup_failures(self):
        with self.assertRaises(KeyError):
            find_customer(self.customers, "missing")
        with self.assertRaises(KeyError):
            import_order({"customer_id": "missing", "quantity": 2}, self.customers)

    def test_order_rejects_invalid_quantity(self):
        for quantity in (0, -1, "2"):
            with self.subTest(quantity=quantity), self.assertRaises(ValueError):
                import_order({"customer_id": "alice", "quantity": quantity},
                             self.customers)


if __name__ == "__main__":
    unittest.main()
