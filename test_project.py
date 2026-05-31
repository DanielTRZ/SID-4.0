import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

import project


class SidSystemTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.original_database = project.DATABASE
        self.original_upload = project.UPLOAD_FOLDER
        self.original_backup = project.BACKUP_FOLDER
        self.original_csv = project.CSV_FILE

        project.DATABASE = os.path.join(self.temp_dir.name, "test_sid.db")
        project.UPLOAD_FOLDER = os.path.join(self.temp_dir.name, "uploads")
        project.BACKUP_FOLDER = os.path.join(self.temp_dir.name, "backups")
        project.CSV_FILE = os.path.join(self.temp_dir.name, "inventory.csv")
        os.makedirs(project.UPLOAD_FOLDER, exist_ok=True)
        os.makedirs(project.BACKUP_FOLDER, exist_ok=True)
        project.app.config["UPLOAD_FOLDER"] = project.UPLOAD_FOLDER
        project.app.config["TESTING"] = True

        with project.app.app_context():
            project.init_db()

        self.client = project.app.test_client()

    def tearDown(self):
        project.DATABASE = self.original_database
        project.UPLOAD_FOLDER = self.original_upload
        project.BACKUP_FOLDER = self.original_backup
        project.CSV_FILE = self.original_csv
        project.app.config["UPLOAD_FOLDER"] = project.UPLOAD_FOLDER
        try:
            self.client = None
        except Exception:
            pass
        self.temp_dir.cleanup()

    def login(self):
        return self.client.post("/login", data={"username": "admin", "password": "admin123"}, follow_redirects=True)

    def db_one(self, query, params=()):
        with closing(sqlite3.connect(project.DATABASE)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(query, params).fetchone()
            return dict(row) if row else None

    def db_all(self, query, params=()):
        with closing(sqlite3.connect(project.DATABASE)) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(query, params).fetchall()]

    def add_inventory_item(self, title="Mleko", quantity="5", sale_price="4.99"):
        self.login()
        return self.client.post("/inventory/add", data={"title": title, "category": "Spożywcze", "quantity": quantity, "min_quantity": "2", "purchase_price": "3.50", "sale_price": sale_price, "location": "Sklep", "ean": f"EAN-{title}"}, follow_redirects=True)

    def test_default_admin_login(self):
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Dashboard", response.get_data(as_text=True))

    def test_add_inventory_creates_batch(self):
        response = self.add_inventory_item()
        self.assertEqual(response.status_code, 200)
        batch = self.db_one("SELECT quantity FROM stock_batches")
        product = self.db_one("SELECT title FROM products")
        self.assertEqual(product["title"], "Mleko")
        self.assertEqual(batch["quantity"], 5)

    def test_reservation_reduces_available_stock(self):
        self.add_inventory_item(title="Woda", quantity="8", sale_price="2.00")
        batch = self.db_one("SELECT id FROM stock_batches")
        response = self.client.post("/reservations", data={"batch_id": str(batch["id"]), "quantity": "3", "notes": "test"}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        with project.app.app_context():
            item = project.get_inventory_items()[0]
        self.assertEqual(item["available_quantity_int"], 5)
        self.assertEqual(item["reserved_quantity_int"], 3)

    def test_pos_checkout_creates_documents_and_discounted_sale(self):
        self.add_inventory_item(title="Sok", quantity="8", sale_price="3.00")
        batch = self.db_one("SELECT id FROM stock_batches")
        add_response = self.client.post("/sales/cart/add", data={"batch_id": str(batch["id"]), "quantity": "2", "unit_price": "3.00", "discount_percent": "10"}, follow_redirects=True)
        self.assertEqual(add_response.status_code, 200)
        checkout_response = self.client.post("/sales/checkout", data={"payment_method": "cash", "issue_invoice": "on"}, follow_redirects=True)
        self.assertEqual(checkout_response.status_code, 200)
        sale_item = self.db_one("SELECT quantity, unit_price, discount_percent, total_price FROM sale_items")
        self.assertEqual(sale_item["quantity"], 2)
        self.assertAlmostEqual(sale_item["discount_percent"], 10.0, places=2)
        self.assertAlmostEqual(sale_item["total_price"], 5.40, places=2)
        self.assertEqual([doc["doc_type"] for doc in self.db_all("SELECT doc_type FROM documents ORDER BY doc_type")], ["FV", "WZ"])
        self.assertEqual(self.db_one("SELECT quantity FROM stock_batches WHERE id = ?", (batch["id"],))["quantity"], 6)
        wz_id = self.db_one("SELECT id FROM documents WHERE doc_type = 'WZ'")["id"]
        pdf_response = self.client.get(f"/documents/{wz_id}/pdf")
        self.assertEqual(pdf_response.status_code, 200)
        self.assertEqual(pdf_response.mimetype, "application/pdf")

    def test_stocktake_finalize_updates_quantity(self):
        self.add_inventory_item(title="Makaron", quantity="8", sale_price="6.00")
        batch = self.db_one("SELECT id, warehouse_id FROM stock_batches")
        create_response = self.client.post("/stocktakes", data={"name": "Spis testowy", "warehouse_id": str(batch["warehouse_id"]), "notes": "test"}, follow_redirects=True)
        self.assertEqual(create_response.status_code, 200)
        stocktake = self.db_one("SELECT id FROM stocktakes ORDER BY id DESC LIMIT 1")
        detail_response = self.client.post(f"/stocktakes/{stocktake['id']}", data={"batch_id": str(batch["id"]), "counted_quantity": "5", "note": "korekta"}, follow_redirects=True)
        self.assertEqual(detail_response.status_code, 200)
        finalize_response = self.client.get(f"/stocktakes/{stocktake['id']}/finalize", follow_redirects=True)
        self.assertEqual(finalize_response.status_code, 200)
        self.assertEqual(self.db_one("SELECT quantity FROM stock_batches WHERE id = ?", (batch["id"],))["quantity"], 5)
        self.assertEqual(self.db_one("SELECT status FROM stocktakes WHERE id = ?", (stocktake['id'],))["status"], "closed")

    def test_pos_scanner_adds_product_by_ean_to_cart(self):
        self.add_inventory_item(title="Herbata", quantity="7", sale_price="8.50")
        response = self.client.post("/sales/cart/scan", data={"scan_code": "EAN-Herbata", "quantity": "2"}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as session:
            cart = session.get("pos_cart", [])
        self.assertEqual(len(cart), 1)
        self.assertEqual(cart[0]["quantity"], 2)
        batch = self.db_one("SELECT id FROM stock_batches")
        self.assertEqual(cart[0]["batch_id"], batch["id"])

    def test_reverse_sale_restores_stock_and_marks_audit(self):
        self.add_inventory_item(title="Kawa", quantity="6", sale_price="12.00")
        batch = self.db_one("SELECT id FROM stock_batches")
        self.client.post("/sales/cart/add", data={"batch_id": str(batch["id"]), "quantity": "2", "unit_price": "12.00", "discount_percent": "0"}, follow_redirects=True)
        self.client.post("/sales/checkout", data={"payment_method": "cash"}, follow_redirects=True)
        sale = self.db_one("SELECT id, status FROM sales ORDER BY id DESC LIMIT 1")
        self.assertEqual(self.db_one("SELECT quantity FROM stock_batches WHERE id = ?", (batch["id"],))["quantity"], 4)

        response = self.client.post(f"/sales/{sale['id']}/reverse", data={"reason": "test cofniecia"}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.db_one("SELECT quantity FROM stock_batches WHERE id = ?", (batch["id"],))["quantity"], 6)
        reversed_sale = self.db_one("SELECT status, reverse_reason FROM sales WHERE id = ?", (sale["id"],))
        self.assertEqual(reversed_sale["status"], "reversed")
        self.assertEqual(reversed_sale["reverse_reason"], "test cofniecia")
        audit = self.db_one("SELECT action, entity, before_state, after_state FROM audit_log WHERE entity = 'sale' AND entity_id = ? ORDER BY id DESC LIMIT 1", (str(sale["id"]),))
        self.assertEqual(audit["action"], "reverse")
        self.assertIn("status=completed", audit["before_state"])
        self.assertIn("status=reversed", audit["after_state"])

    def test_reverse_manual_movement_restores_quantity(self):
        self.add_inventory_item(title="Ry?", quantity="10", sale_price="5.00")
        batch = self.db_one("SELECT id FROM stock_batches")
        self.client.post("/movements", data={"batch_id": str(batch["id"]), "movement_type": "adjustment_out", "quantity": "3", "notes": "test korekty"}, follow_redirects=True)
        movement = self.db_one("SELECT id, movement_type FROM stock_movements WHERE movement_type = 'adjustment_out' ORDER BY id DESC LIMIT 1")
        self.assertEqual(self.db_one("SELECT quantity FROM stock_batches WHERE id = ?", (batch["id"],))["quantity"], 7)

        response = self.client.post(f"/movements/{movement['id']}/reverse", data={"reason": "bledna korekta"}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.db_one("SELECT quantity FROM stock_batches WHERE id = ?", (batch["id"],))["quantity"], 10)
        reversed_move = self.db_one("SELECT reversed_at, reverse_reason FROM stock_movements WHERE id = ?", (movement["id"],))
        self.assertTrue(reversed_move["reversed_at"])
        self.assertEqual(reversed_move["reverse_reason"], "bledna korekta")
        reverse_log = self.db_one("SELECT movement_type FROM stock_movements WHERE reference_type = 'movement_reversal' AND reference_id = ? ORDER BY id DESC LIMIT 1", (str(movement["id"]),))
        self.assertEqual(reverse_log["movement_type"], "adjustment_out_reversal")

    def test_users_settings_create_user_and_save_company_name(self):
        self.login()
        self.client.post("/admin/users-settings", data={"action": "create_user", "username": "kasjer", "full_name": "Jan Kasjer", "password": "tajne123", "role": "sales"}, follow_redirects=True)
        response = self.client.post("/admin/users-settings", data={"action": "save_settings", "company_name": "Sklep Testowy", "company_tax_id": "1234567890", "company_address": "ul. Testowa 1", "default_warehouse_name": "Magazyn główny", "currency": "PLN", "invoice_prefix": "FV", "goods_issue_prefix": "WZ", "goods_receipt_prefix": "PZ", "stock_alert_days": "7"}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.db_one("SELECT role FROM users WHERE username = 'kasjer'")["role"], "sales")
        self.assertEqual(self.db_one("SELECT value FROM system_settings WHERE key = 'company_name'")["value"], "Sklep Testowy")


if __name__ == "__main__":
    unittest.main()
