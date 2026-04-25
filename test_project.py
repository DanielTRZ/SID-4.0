import os
import sqlite3
import tempfile
import unittest

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

    def login(self):
        return self.client.post("/login", data={"username": "admin", "password": "admin123"}, follow_redirects=True)

    def db_one(self, query, params=()):
        with sqlite3.connect(project.DATABASE) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(query, params).fetchone()
            return dict(row) if row else None

    def db_all(self, query, params=()):
        with sqlite3.connect(project.DATABASE) as connection:
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

    def test_users_settings_create_user_and_save_company_name(self):
        self.login()
        self.client.post("/admin/users-settings", data={"action": "create_user", "username": "kasjer", "full_name": "Jan Kasjer", "password": "tajne123", "role": "sales"}, follow_redirects=True)
        response = self.client.post("/admin/users-settings", data={"action": "save_settings", "company_name": "Sklep Testowy", "company_tax_id": "1234567890", "company_address": "ul. Testowa 1", "default_warehouse_name": "Magazyn główny", "currency": "PLN", "invoice_prefix": "FV", "goods_issue_prefix": "WZ", "goods_receipt_prefix": "PZ", "stock_alert_days": "7"}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.db_one("SELECT role FROM users WHERE username = 'kasjer'")["role"], "sales")
        self.assertEqual(self.db_one("SELECT value FROM system_settings WHERE key = 'company_name'")["value"], "Sklep Testowy")


if __name__ == "__main__":
    unittest.main()
