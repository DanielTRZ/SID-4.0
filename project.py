import csv
import datetime
import io
import os
import sqlite3
import zipfile
from functools import wraps

from flask import (
    Flask,
    flash,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from fpdf import FPDF
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = "projekt_daniela_cs50p"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(BASE_DIR, "sid.db")
CSV_FILE = os.path.join(BASE_DIR, "inventory.csv")
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
BACKUP_FOLDER = os.path.join(BASE_DIR, "backups")
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
FILE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "pdf", "doc", "docx", "txt"}
ROLES = ["admin", "manager", "warehouse", "sales"]
SUPPORTED_LANGUAGES = ("pl", "en")


os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(BACKUP_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER


class ManagedSQLiteConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            self.close()
        return False


def now_iso():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_iso():
    return datetime.date.today().strftime("%Y-%m-%d")


def int_safe(value, default=0):
    try:
        if value is None or str(value).strip() == "":
            return default
        return int(float(str(value).replace(",", ".")))
    except (TypeError, ValueError):
        return default


def float_safe(value, default=0.0):
    try:
        if value is None or str(value).strip() == "":
            return default
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return default


def clean_value(value, default=""):
    if value is None:
        return default
    return str(value).strip()




def get_current_language():
    lang = clean_value(session.get("lang"), "pl").lower()
    return lang if lang in SUPPORTED_LANGUAGES else "pl"


def tr(pl_text, en_text):
    return en_text if get_current_language() == "en" else pl_text


def trf(pl_text, en_text, **kwargs):
    template = tr(pl_text, en_text)
    return template.format(**kwargs)


def current_language_label():
    return "EN" if get_current_language() == "en" else "PL"


def opposite_language():
    return "pl" if get_current_language() == "en" else "en"

def clean_pdf_text(text):
    replacements = {
        "ą": "a",
        "ć": "c",
        "ę": "e",
        "ł": "l",
        "ń": "n",
        "ó": "o",
        "ś": "s",
        "ź": "z",
        "ż": "z",
        "Ą": "A",
        "Ć": "C",
        "Ę": "E",
        "Ł": "L",
        "Ń": "N",
        "Ó": "O",
        "Ś": "S",
        "Ź": "Z",
        "Ż": "Z",
    }
    value = str(text or "-")
    for source, target in replacements.items():
        value = value.replace(source, target)
    return value.encode("latin-1", "ignore").decode("latin-1")


def save_uploaded_file(file_storage, allowed_extensions=None):
    if not file_storage or not file_storage.filename:
        return ""

    original_name = secure_filename(file_storage.filename)
    extension = original_name.rsplit(".", 1)[1].lower() if "." in original_name else ""
    if allowed_extensions and extension not in allowed_extensions:
        return ""

    filename = f"{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}_{original_name}"
    file_storage.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
    return filename


def remove_uploaded_file(filename):
    if not filename:
        return
    path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def file_url(filename):
    if not filename:
        return ""
    return f"/static/uploads/{filename}"


def get_db():
    connection = sqlite3.connect(DATABASE, factory=ManagedSQLiteConnection)
    connection.row_factory = sqlite3.Row
    return connection


def query_all(sql, params=()):
    with get_db() as connection:
        return connection.execute(sql, params).fetchall()


def query_one(sql, params=()):
    with get_db() as connection:
        return connection.execute(sql, params).fetchone()


def execute_db(sql, params=()):
    with get_db() as connection:
        cursor = connection.execute(sql, params)
        connection.commit()
        return cursor.lastrowid


def get_setting(key, default=""):
    row = query_one("SELECT value FROM system_settings WHERE key = ?", (key,))
    return row["value"] if row else default


def set_setting(key, value):
    execute_db(
        """
        INSERT INTO system_settings (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, clean_value(value), now_iso()),
    )


def next_document_number(doc_type):
    prefix_map = {
        "FV": get_setting("invoice_prefix", "FV"),
        "WZ": get_setting("goods_issue_prefix", "WZ"),
        "PZ": get_setting("goods_receipt_prefix", "PZ"),
    }
    prefix = prefix_map.get(doc_type, doc_type)
    year = datetime.date.today().year
    like_pattern = f"{prefix}/{year}/%"
    row = query_one(
        "SELECT doc_number FROM documents WHERE doc_type = ? AND doc_number LIKE ? ORDER BY id DESC LIMIT 1",
        (doc_type, like_pattern),
    )
    if row:
        try:
            serial = int(row["doc_number"].split("/")[-1]) + 1
        except (TypeError, ValueError, IndexError):
            serial = 1
    else:
        serial = 1
    return f"{prefix}/{year}/{serial:04d}"


def log_audit(action, entity, entity_id, details, before_state="", after_state=""):
    user_id = session.get("user_id")
    with get_db() as connection:
        audit_columns = {row["name"] for row in connection.execute("PRAGMA table_info(audit_log)").fetchall()}
        if {"before_state", "after_state"}.issubset(audit_columns):
            connection.execute(
                """
                INSERT INTO audit_log (user_id, action, entity, entity_id, details, before_state, after_state, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, action, entity, str(entity_id), details, clean_value(before_state), clean_value(after_state), now_iso()),
            )
        else:
            connection.execute(
                """
                INSERT INTO audit_log (user_id, action, entity, entity_id, details, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, action, entity, str(entity_id), details, now_iso()),
            )
        connection.commit()


def format_state_pairs(data):
    if not data:
        return ""
    return "; ".join(f"{key}={value}" for key, value in data.items())


def init_db():
    schema = """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        full_name TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS warehouses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        code TEXT,
        description TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        sku TEXT UNIQUE,
        ean TEXT,
        category TEXT,
        brand TEXT,
        unit TEXT NOT NULL DEFAULT 'szt.',
        min_quantity INTEGER NOT NULL DEFAULT 0,
        purchase_price REAL NOT NULL DEFAULT 0,
        sale_price REAL NOT NULL DEFAULT 0,
        image TEXT,
        document TEXT,
        notes TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS stock_batches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL,
        warehouse_id INTEGER NOT NULL,
        quantity INTEGER NOT NULL DEFAULT 0,
        expiry_date TEXT,
        purchase_date TEXT,
        lot_number TEXT,
        serial_number TEXT,
        document TEXT,
        image TEXT,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(product_id) REFERENCES products(id),
        FOREIGN KEY(warehouse_id) REFERENCES warehouses(id)
    );

    CREATE TABLE IF NOT EXISTS suppliers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        tax_id TEXT,
        email TEXT,
        phone TEXT,
        address TEXT,
        notes TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        tax_id TEXT,
        email TEXT,
        phone TEXT,
        address TEXT,
        notes TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS purchase_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        supplier_id INTEGER,
        warehouse_id INTEGER NOT NULL,
        status TEXT NOT NULL,
        total_amount REAL NOT NULL DEFAULT 0,
        notes TEXT,
        created_at TEXT NOT NULL,
        user_id INTEGER,
        FOREIGN KEY(supplier_id) REFERENCES suppliers(id),
        FOREIGN KEY(warehouse_id) REFERENCES warehouses(id),
        FOREIGN KEY(user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS purchase_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        purchase_order_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        quantity INTEGER NOT NULL,
        unit_cost REAL NOT NULL,
        total_cost REAL NOT NULL,
        expiry_date TEXT,
        purchase_date TEXT,
        lot_number TEXT,
        FOREIGN KEY(purchase_order_id) REFERENCES purchase_orders(id),
        FOREIGN KEY(product_id) REFERENCES products(id)
    );

    CREATE TABLE IF NOT EXISTS sales (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER,
        warehouse_id INTEGER NOT NULL,
        payment_method TEXT NOT NULL,
        status TEXT NOT NULL,
        total_amount REAL NOT NULL DEFAULT 0,
        notes TEXT,
        created_at TEXT NOT NULL,
        user_id INTEGER,
        FOREIGN KEY(customer_id) REFERENCES customers(id),
        FOREIGN KEY(warehouse_id) REFERENCES warehouses(id),
        FOREIGN KEY(user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS sale_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sale_id INTEGER NOT NULL,
        batch_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        quantity INTEGER NOT NULL,
        unit_price REAL NOT NULL,
        total_price REAL NOT NULL,
        FOREIGN KEY(sale_id) REFERENCES sales(id),
        FOREIGN KEY(batch_id) REFERENCES stock_batches(id),
        FOREIGN KEY(product_id) REFERENCES products(id)
    );

    CREATE TABLE IF NOT EXISTS stock_movements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL,
        batch_id INTEGER,
        warehouse_id INTEGER NOT NULL,
        movement_type TEXT NOT NULL,
        quantity INTEGER NOT NULL,
        reference_type TEXT,
        reference_id TEXT,
        notes TEXT,
        created_at TEXT NOT NULL,
        user_id INTEGER,
        FOREIGN KEY(product_id) REFERENCES products(id),
        FOREIGN KEY(batch_id) REFERENCES stock_batches(id),
        FOREIGN KEY(warehouse_id) REFERENCES warehouses(id),
        FOREIGN KEY(user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        action TEXT NOT NULL,
        entity TEXT NOT NULL,
        entity_id TEXT,
        details TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        description TEXT,
        status TEXT NOT NULL DEFAULT 'open',
        priority TEXT NOT NULL DEFAULT 'medium',
        due_date TEXT,
        assigned_user_id INTEGER,
        entity_type TEXT,
        entity_id TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(assigned_user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS entity_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL,
        user_id INTEGER,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS entity_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        label TEXT NOT NULL,
        file_name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        user_id INTEGER,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS reservations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id INTEGER NOT NULL,
        customer_id INTEGER,
        quantity INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'reserved',
        notes TEXT,
        created_at TEXT NOT NULL,
        user_id INTEGER,
        FOREIGN KEY(batch_id) REFERENCES stock_batches(id),
        FOREIGN KEY(customer_id) REFERENCES customers(id),
        FOREIGN KEY(user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS report_archive (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        file_name TEXT NOT NULL,
        file_path TEXT,
        created_at TEXT NOT NULL,
        user_id INTEGER,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS dictionary_values (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dict_type TEXT NOT NULL,
        value TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(dict_type, value)
    );

    CREATE TABLE IF NOT EXISTS documents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        doc_type TEXT NOT NULL,
        doc_number TEXT NOT NULL UNIQUE,
        related_type TEXT,
        related_id TEXT,
        customer_id INTEGER,
        supplier_id INTEGER,
        warehouse_id INTEGER,
        total_amount REAL NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'issued',
        notes TEXT,
        created_at TEXT NOT NULL,
        user_id INTEGER,
        FOREIGN KEY(customer_id) REFERENCES customers(id),
        FOREIGN KEY(supplier_id) REFERENCES suppliers(id),
        FOREIGN KEY(warehouse_id) REFERENCES warehouses(id),
        FOREIGN KEY(user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS stocktakes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        warehouse_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        notes TEXT,
        created_at TEXT NOT NULL,
        closed_at TEXT,
        user_id INTEGER,
        FOREIGN KEY(warehouse_id) REFERENCES warehouses(id),
        FOREIGN KEY(user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS stocktake_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        stocktake_id INTEGER NOT NULL,
        batch_id INTEGER NOT NULL,
        expected_quantity INTEGER NOT NULL,
        counted_quantity INTEGER,
        difference INTEGER,
        note TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(stocktake_id) REFERENCES stocktakes(id),
        FOREIGN KEY(batch_id) REFERENCES stock_batches(id)
    );

    CREATE TABLE IF NOT EXISTS system_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """

    with get_db() as connection:
        connection.executescript(schema)
        connection.commit()

        admin = connection.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()
        if not admin:
            connection.execute(
                """
                INSERT INTO users (username, full_name, password_hash, role, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("admin", "Administrator SID", generate_password_hash("admin123"), "admin", now_iso()),
            )

        warehouse = connection.execute("SELECT id FROM warehouses WHERE name = 'Magazyn główny'").fetchone()
        if not warehouse:
            connection.execute(
                """
                INSERT INTO warehouses (name, code, description, created_at)
                VALUES (?, ?, ?, ?)
                """,
                ("Magazyn główny", "MAIN", "Domyślny magazyn systemowy", now_iso()),
            )

        default_settings = {
            "company_name": "SID 4.0",
            "company_tax_id": "",
            "company_address": "",
            "default_warehouse_name": "Magazyn główny",
            "currency": "PLN",
            "invoice_prefix": "FV",
            "goods_issue_prefix": "WZ",
            "goods_receipt_prefix": "PZ",
            "stock_alert_days": "7",
        }
        for key, value in default_settings.items():
            existing_setting = connection.execute("SELECT key FROM system_settings WHERE key = ?", (key,)).fetchone()
            if not existing_setting:
                connection.execute(
                    "INSERT INTO system_settings (key, value, updated_at) VALUES (?, ?, ?)",
                    (key, value, now_iso()),
                )

        sale_item_columns = {row["name"] for row in connection.execute("PRAGMA table_info(sale_items)").fetchall()}
        if "discount_percent" not in sale_item_columns:
            connection.execute("ALTER TABLE sale_items ADD COLUMN discount_percent REAL NOT NULL DEFAULT 0")

        sales_columns = {row["name"] for row in connection.execute("PRAGMA table_info(sales)").fetchall()}
        if "reversed_at" not in sales_columns:
            connection.execute("ALTER TABLE sales ADD COLUMN reversed_at TEXT")
        if "reversed_by" not in sales_columns:
            connection.execute("ALTER TABLE sales ADD COLUMN reversed_by INTEGER")
        if "reverse_reason" not in sales_columns:
            connection.execute("ALTER TABLE sales ADD COLUMN reverse_reason TEXT")

        movement_columns = {row["name"] for row in connection.execute("PRAGMA table_info(stock_movements)").fetchall()}
        if "reversed_at" not in movement_columns:
            connection.execute("ALTER TABLE stock_movements ADD COLUMN reversed_at TEXT")
        if "reversed_by" not in movement_columns:
            connection.execute("ALTER TABLE stock_movements ADD COLUMN reversed_by INTEGER")
        if "reverse_reason" not in movement_columns:
            connection.execute("ALTER TABLE stock_movements ADD COLUMN reverse_reason TEXT")

        audit_columns = {row["name"] for row in connection.execute("PRAGMA table_info(audit_log)").fetchall()}
        if "before_state" not in audit_columns:
            connection.execute("ALTER TABLE audit_log ADD COLUMN before_state TEXT NOT NULL DEFAULT ''")
        if "after_state" not in audit_columns:
            connection.execute("ALTER TABLE audit_log ADD COLUMN after_state TEXT NOT NULL DEFAULT ''")

        connection.commit()

    migrate_csv_inventory()


def get_or_create_warehouse(connection, name):
    warehouse_name = clean_value(name, "Magazyn główny") or "Magazyn główny"
    row = connection.execute("SELECT id FROM warehouses WHERE name = ?", (warehouse_name,)).fetchone()
    if row:
        return row["id"]

    cursor = connection.execute(
        "INSERT INTO warehouses (name, code, description, created_at) VALUES (?, ?, ?, ?)",
        (warehouse_name, warehouse_name[:4].upper(), "Utworzono automatycznie", now_iso()),
    )
    return cursor.lastrowid


def get_or_create_product(connection, title, category, ean="", sku="", brand="", purchase_price=0.0, sale_price=0.0, min_quantity=0):
    title_val = clean_value(title)
    ean_val = clean_value(ean)
    sku_val = clean_value(sku)

    existing = None
    if sku_val:
        existing = connection.execute("SELECT * FROM products WHERE sku = ?", (sku_val,)).fetchone()
    if not existing and ean_val:
        existing = connection.execute("SELECT * FROM products WHERE ean = ?", (ean_val,)).fetchone()
    if not existing:
        existing = connection.execute(
            "SELECT * FROM products WHERE lower(title) = lower(?) AND lower(COALESCE(category, '')) = lower(?)",
            (title_val, clean_value(category)),
        ).fetchone()

    if existing:
        connection.execute(
            """
            UPDATE products
            SET category = ?, ean = ?, sku = ?, brand = ?, min_quantity = ?, purchase_price = ?, sale_price = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                clean_value(category),
                ean_val,
                sku_val,
                clean_value(brand),
                int_safe(min_quantity, 0),
                float_safe(purchase_price, 0.0),
                float_safe(sale_price, 0.0),
                now_iso(),
                existing["id"],
            ),
        )
        return existing["id"]

    cursor = connection.execute(
        """
        INSERT INTO products (title, sku, ean, category, brand, unit, min_quantity, purchase_price, sale_price, image, document, notes, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            title_val,
            sku_val or None,
            ean_val,
            clean_value(category),
            clean_value(brand),
            "szt.",
            int_safe(min_quantity, 0),
            float_safe(purchase_price, 0.0),
            float_safe(sale_price, 0.0),
            "",
            "",
            "",
            now_iso(),
            now_iso(),
        ),
    )
    return cursor.lastrowid


def migrate_csv_inventory():
    if not os.path.exists(CSV_FILE):
        return

    existing_batches = query_one("SELECT COUNT(*) AS total FROM stock_batches")
    if existing_batches and existing_batches["total"] > 0:
        return

    with open(CSV_FILE, "r", encoding="utf-8", newline="") as file:
        rows = [row for row in csv.reader(file) if row]

    if not rows:
        return

    with get_db() as connection:
        for row in rows:
            row = list(row)
            while len(row) < 12:
                row.append("")

            product_id = get_or_create_product(
                connection,
                row[0],
                row[1],
                ean=row[9],
                purchase_price=row[5],
                sale_price=row[5],
                min_quantity=row[3],
            )
            warehouse_id = get_or_create_warehouse(connection, row[4] or "Magazyn główny")
            connection.execute(
                """
                INSERT INTO stock_batches (product_id, warehouse_id, quantity, expiry_date, purchase_date, lot_number, serial_number, document, image, is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    product_id,
                    warehouse_id,
                    int_safe(row[2], 0),
                    clean_value(row[6]),
                    clean_value(row[7]),
                    "",
                    "",
                    clean_value(row[10]),
                    clean_value(row[11]),
                    now_iso(),
                    now_iso(),
                ),
            )
        connection.commit()


@app.before_request
def load_logged_user():
    user_id = session.get("user_id")
    g.user = None
    if user_id:
        g.user = query_one("SELECT * FROM users WHERE id = ? AND active = 1", (user_id,))


@app.context_processor
def inject_globals():
    return {
        "current_user": g.user,
        "roles": ROLES,
        "demo_mode": session.get("demo_mode", False),
        "current_language": get_current_language(),
        "current_language_label": current_language_label(),
        "opposite_language": opposite_language(),
        "supported_languages": SUPPORTED_LANGUAGES,
        "tr": tr,
    }


def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            return redirect(url_for("login"))
        return view(**kwargs)

    return wrapped_view


def role_required(*allowed_roles):
    def decorator(view):
        @wraps(view)
        def wrapped_view(**kwargs):
            if g.user is None:
                return redirect(url_for("login"))
            if g.user["role"] not in allowed_roles:
                flash("Brak uprawnień do tej sekcji.", "error")
                return redirect(url_for("dashboard"))
            return view(**kwargs)

        return wrapped_view

    return decorator


def inventory_row_to_dict(row):
    item = dict(row)
    quantity = int_safe(item["quantity"], 0)
    reserved_quantity = int_safe(item.get("reserved_quantity", 0), 0)
    available_quantity = max(quantity - reserved_quantity, 0)
    min_quantity = int_safe(item["min_quantity"], 0)
    purchase_price = float_safe(item["purchase_price"], 0.0)
    sale_price = float_safe(item["sale_price"], 0.0)
    expiry_date = clean_value(item["expiry_date"])
    days_to_expiry = None
    status_class = ""

    if expiry_date:
        try:
            expiry = datetime.datetime.strptime(expiry_date, "%Y-%m-%d").date()
            days_to_expiry = (expiry - datetime.date.today()).days
            if days_to_expiry < 0:
                status_class = "expired"
            elif days_to_expiry <= 7:
                status_class = "warning-7"
            elif days_to_expiry <= 30:
                status_class = "warning-30"
        except ValueError:
            status_class = ""

    is_low = min_quantity > 0 and available_quantity <= min_quantity
    image_name = clean_value(item["image"])
    document_name = clean_value(item["document"])

    item.update(
        {
            "quantity_int": quantity,
            "reserved_quantity_int": reserved_quantity,
            "available_quantity_int": available_quantity,
            "min_quantity_int": min_quantity,
            "purchase_price_float": purchase_price,
            "sale_price_float": sale_price,
            "purchase_price": f"{purchase_price:.2f}",
            "sale_price": f"{sale_price:.2f}",
            "total_value": round(quantity * purchase_price, 2),
            "status_class": status_class,
            "days_to_expiry": days_to_expiry,
            "is_low": is_low,
            "missing_location": clean_value(item["warehouse_name"], "Brak") == "Brak",
            "missing_price": purchase_price <= 0,
            "image_url": file_url(image_name),
            "document_url": file_url(document_name),
            "has_image": bool(image_name),
            "has_document": bool(document_name),
            "title": clean_value(item["title"]),
            "category": clean_value(item["category"], "Inne") or "Inne",
            "warehouse_name": clean_value(item["warehouse_name"], "Magazyn główny"),
            "sku": clean_value(item["sku"]),
            "ean": clean_value(item["ean"]),
            "brand": clean_value(item["brand"]),
        }
    )
    return item


def get_inventory_items():
    rows = query_all(
        """
        SELECT
            b.id AS batch_id,
            b.quantity,
            b.expiry_date,
            b.purchase_date,
            b.lot_number,
            b.serial_number,
            b.document,
            b.image,
            p.id AS product_id,
            p.title,
            p.sku,
            p.ean,
            p.category,
            p.brand,
            p.unit,
            p.min_quantity,
            p.purchase_price,
            p.sale_price,
            w.id AS warehouse_id,
            w.name AS warehouse_name,
            COALESCE((
                SELECT SUM(r.quantity)
                FROM reservations r
                WHERE r.batch_id = b.id AND r.status = 'reserved'
            ), 0) AS reserved_quantity
        FROM stock_batches b
        JOIN products p ON p.id = b.product_id
        JOIN warehouses w ON w.id = b.warehouse_id
        WHERE b.is_active = 1
        ORDER BY p.title COLLATE NOCASE ASC, b.expiry_date ASC
        """
    )
    return [inventory_row_to_dict(row) for row in rows]


def get_products():
    rows = query_all(
        """
        SELECT p.*, COALESCE(SUM(b.quantity), 0) AS stock_total,
               COALESCE((
                    SELECT SUM(r.quantity)
                    FROM reservations r
                    JOIN stock_batches sb ON sb.id = r.batch_id
                    WHERE sb.product_id = p.id AND r.status = 'reserved'
               ), 0) AS reserved_total
        FROM products p
        LEFT JOIN stock_batches b ON b.product_id = p.id AND b.is_active = 1
        GROUP BY p.id
        ORDER BY p.title COLLATE NOCASE ASC
        """
    )
    data = []
    for row in rows:
        item = dict(row)
        item["available_total"] = int_safe(item["stock_total"], 0) - int_safe(item["reserved_total"], 0)
        data.append(item)
    return data


def get_warehouses():
    return [dict(row) for row in query_all("SELECT * FROM warehouses ORDER BY name COLLATE NOCASE ASC")]


def get_suppliers():
    return [dict(row) for row in query_all("SELECT * FROM suppliers ORDER BY name COLLATE NOCASE ASC")]


def get_customers():
    return [dict(row) for row in query_all("SELECT * FROM customers ORDER BY name COLLATE NOCASE ASC")]


def get_recent_sales(limit=10):
    rows = query_all(
        """
        SELECT s.id, s.total_amount, s.payment_method, s.status, s.created_at,
               s.reversed_at, s.reverse_reason,
               COALESCE(c.name, 'Klient detaliczny') AS customer_name,
               w.name AS warehouse_name,
               u.full_name AS user_name,
               COALESCE(SUM(si.quantity), 0) AS items_count,
               COALESCE(SUM(si.total_price), 0) AS line_total,
               COALESCE(SUM(si.quantity * p.purchase_price), 0) AS purchase_total
        FROM sales s
        LEFT JOIN customers c ON c.id = s.customer_id
        JOIN warehouses w ON w.id = s.warehouse_id
        LEFT JOIN users u ON u.id = s.user_id
        LEFT JOIN sale_items si ON si.sale_id = s.id
        LEFT JOIN products p ON p.id = si.product_id
        GROUP BY s.id, c.name, w.name, u.full_name
        ORDER BY s.created_at DESC
        LIMIT ?
        """,
        (limit,),
    )
    data = []
    for row in rows:
        entry = dict(row)
        entry["estimated_margin"] = round(float_safe(entry.get("line_total"), 0.0) - float_safe(entry.get("purchase_total"), 0.0), 2)
        data.append(entry)
    return data


def get_recent_audit(limit=12):
    rows = query_all(
        """
        SELECT a.*, COALESCE(u.full_name, 'System') AS user_name
        FROM audit_log a
        LEFT JOIN users u ON u.id = a.user_id
        ORDER BY a.created_at DESC
        LIMIT ?
        """,
        (limit,),
    )
    data = []
    for row in rows:
        entry = dict(row)
        entry["before_state"] = clean_value(entry.get("before_state"))
        entry["after_state"] = clean_value(entry.get("after_state"))
        data.append(entry)
    return data


def get_recent_movements(limit=15):
    rows = query_all(
        """
        SELECT m.*, p.title AS product_title, w.name AS warehouse_name, COALESCE(u.full_name, 'System') AS user_name
        FROM stock_movements m
        JOIN products p ON p.id = m.product_id
        JOIN warehouses w ON w.id = m.warehouse_id
        LEFT JOIN users u ON u.id = m.user_id
        ORDER BY m.created_at DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [dict(row) for row in rows]


def get_purchase_orders(limit=15):
    rows = query_all(
        """
        SELECT po.id, po.status, po.total_amount, po.notes, po.created_at,
               COALESCE(s.name, 'Brak dostawcy') AS supplier_name,
               w.name AS warehouse_name,
               COALESCE(u.full_name, 'System') AS user_name
        FROM purchase_orders po
        LEFT JOIN suppliers s ON s.id = po.supplier_id
        JOIN warehouses w ON w.id = po.warehouse_id
        LEFT JOIN users u ON u.id = po.user_id
        ORDER BY po.created_at DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [dict(row) for row in rows]


def get_documents(limit=50):
    rows = query_all(
        """
        SELECT d.*, COALESCE(c.name, 'Brak klienta') AS customer_name,
               COALESCE(s.name, 'Brak dostawcy') AS supplier_name,
               COALESCE(w.name, 'Brak magazynu') AS warehouse_name,
               COALESCE(u.full_name, 'System') AS user_name
        FROM documents d
        LEFT JOIN customers c ON c.id = d.customer_id
        LEFT JOIN suppliers s ON s.id = d.supplier_id
        LEFT JOIN warehouses w ON w.id = d.warehouse_id
        LEFT JOIN users u ON u.id = d.user_id
        ORDER BY d.created_at DESC, d.id DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [dict(row) for row in rows]


def create_document(connection, doc_type, related_type, related_id, total_amount, warehouse_id=None, customer_id=None, supplier_id=None, notes="", status="issued"):
    doc_number = next_document_number(doc_type)
    document_id = connection.execute(
        """
        INSERT INTO documents (doc_type, doc_number, related_type, related_id, customer_id, supplier_id, warehouse_id, total_amount, status, notes, created_at, user_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            doc_type,
            doc_number,
            related_type,
            str(related_id) if related_id is not None else "",
            customer_id,
            supplier_id,
            warehouse_id,
            round(float_safe(total_amount, 0.0), 2),
            status,
            notes,
            now_iso(),
            session.get("user_id"),
        ),
    ).lastrowid
    return document_id, doc_number


def get_cart():
    return session.setdefault("pos_cart", [])


def save_cart(cart_items):
    session["pos_cart"] = cart_items
    session.modified = True


def clear_cart():
    session["pos_cart"] = []
    session.modified = True


def add_item_to_pos_cart(item, quantity=1, unit_price=None, discount_percent=0.0, replace=False):
    if not item:
        raise ValueError("Nie znaleziono partii do dodania do koszyka.")
    quantity = int_safe(quantity, 1)
    if quantity <= 0:
        raise ValueError("IloÅÄ musi byÄ wiÄksza od zera.")

    existing_items = build_cart_items()
    if existing_items and any(entry["warehouse_id"] != item["warehouse_id"] for entry in existing_items):
        raise ValueError("Koszyk POS moÅ¼e zawieraÄ pozycje tylko z jednego magazynu.")

    cart = get_cart()
    existing = next((entry for entry in cart if entry["batch_id"] == item["batch_id"]), None)
    safe_unit_price = float_safe(unit_price, item["sale_price_float"])
    safe_discount = max(0.0, min(100.0, float_safe(discount_percent, 0.0)))
    target_quantity = quantity if replace or not existing else int_safe(existing.get("quantity"), 0) + quantity

    if target_quantity > item["available_quantity_int"]:
        raise ValueError("Koszyk przekracza dostÄpny stan magazynowy.")

    if existing:
        existing["quantity"] = target_quantity
        existing["unit_price"] = safe_unit_price or item["sale_price_float"]
        existing["discount_percent"] = safe_discount
    else:
        cart.append({
            "batch_id": item["batch_id"],
            "quantity": target_quantity,
            "unit_price": safe_unit_price or item["sale_price_float"],
            "discount_percent": safe_discount,
        })
    save_cart(cart)
    return target_quantity




def summarize_line_items(items):
    subtotal = round(sum(float_safe(item.get("subtotal_price"), int_safe(item.get("quantity"), 0) * float_safe(item.get("unit_price"), 0.0)) for item in items), 2)
    discount_total = round(sum(float_safe(item.get("discount_value"), 0.0) for item in items), 2)
    total = round(sum(float_safe(item.get("total_price"), 0.0) for item in items), 2)
    estimated_margin_total = round(sum(float_safe(item.get("estimated_margin"), 0.0) for item in items), 2)
    return {"items_count": len(items), "units_count": sum(int_safe(item.get("quantity"), 0) for item in items), "subtotal": subtotal, "discount_total": discount_total, "total": total, "estimated_margin_total": estimated_margin_total}




def build_cart_items():
    cart = get_cart()
    if not cart:
        return []

    batch_ids = [item["batch_id"] for item in cart]
    placeholders = ",".join("?" for _ in batch_ids)
    rows = query_all(
        f"""
        SELECT b.id AS batch_id, b.quantity, b.expiry_date, b.purchase_date, b.lot_number, b.serial_number, b.document, b.image,
               p.id AS product_id, p.title, p.sku, p.ean, p.category, p.brand, p.unit, p.min_quantity, p.purchase_price, p.sale_price,
               w.id AS warehouse_id, w.name AS warehouse_name,
               COALESCE((SELECT SUM(r.quantity) FROM reservations r WHERE r.batch_id = b.id AND r.status = 'reserved'), 0) AS reserved_quantity
        FROM stock_batches b
        JOIN products p ON p.id = b.product_id
        JOIN warehouses w ON w.id = b.warehouse_id
        WHERE b.id IN ({placeholders}) AND b.is_active = 1
        """,
        tuple(batch_ids),
    )
    rows_map = {row["batch_id"]: inventory_row_to_dict(row) for row in rows}

    items = []
    for raw_item in cart:
        batch = rows_map.get(raw_item["batch_id"])
        if not batch:
            continue
        quantity = int_safe(raw_item.get("quantity"), 1)
        unit_price = float_safe(raw_item.get("unit_price"), batch["sale_price_float"])
        discount_percent = max(0.0, min(100.0, float_safe(raw_item.get("discount_percent"), 0.0)))
        subtotal_price = round(quantity * unit_price, 2)
        discount_value = round(subtotal_price * discount_percent / 100, 2)
        total_price = round(subtotal_price - discount_value, 2)
        purchase_price = float_safe(batch.get("purchase_price_float"), 0.0)
        items.append({
            "batch_id": batch["batch_id"],
            "product_id": batch["product_id"],
            "title": batch["title"],
            "warehouse_id": batch["warehouse_id"],
            "warehouse_name": batch["warehouse_name"],
            "available_quantity": batch["available_quantity_int"],
            "quantity": quantity,
            "unit_price": unit_price,
            "purchase_price": purchase_price,
            "discount_percent": discount_percent,
            "subtotal_price": subtotal_price,
            "discount_value": discount_value,
            "total_price": total_price,
            "estimated_margin": round(total_price - (quantity * purchase_price), 2),
        })
    return items




def create_sale_transaction(connection, cart_items, customer_id, payment_method, notes="", issue_invoice=False):
    if not cart_items:
        raise ValueError("Koszyk jest pusty.")

    warehouse_id = cart_items[0]["warehouse_id"]
    if any(item["warehouse_id"] != warehouse_id for item in cart_items):
        raise ValueError("Wszystkie pozycje w koszyku muszÄ pochodziÄ z jednego magazynu.")

    total_amount = round(sum(item["total_price"] for item in cart_items), 2)
    sale_id = connection.execute(
        """
        INSERT INTO sales (customer_id, warehouse_id, payment_method, status, total_amount, notes, created_at, user_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (customer_id or None, warehouse_id, payment_method, "completed", total_amount, notes, now_iso(), session.get("user_id")),
    ).lastrowid

    for item in cart_items:
        batch = connection.execute(
            """
            SELECT b.id, b.quantity,
                   COALESCE((SELECT SUM(r.quantity) FROM reservations r WHERE r.batch_id = b.id AND r.status = 'reserved'), 0) AS reserved_quantity
            FROM stock_batches b
            WHERE b.id = ? AND b.is_active = 1
            """,
            (item["batch_id"],),
        ).fetchone()
        if not batch:
            raise ValueError(f"Nie znaleziono partii #{item['batch_id']}.")

        available_quantity = int_safe(batch["quantity"], 0) - int_safe(batch["reserved_quantity"], 0)
        if item["quantity"] <= 0 or item["quantity"] > available_quantity:
            raise ValueError(f"Za maÅo dostÄpnego stanu dla pozycji {item['title']}.")

        connection.execute(
            """
            INSERT INTO sale_items (sale_id, batch_id, product_id, quantity, unit_price, discount_percent, total_price)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (sale_id, item["batch_id"], item["product_id"], item["quantity"], item["unit_price"], max(0.0, min(100.0, float_safe(item.get("discount_percent"), 0.0))), item["total_price"]),
        )
        connection.execute("UPDATE stock_batches SET quantity = quantity - ?, updated_at = ? WHERE id = ?", (item["quantity"], now_iso(), item["batch_id"]))
        create_stock_movement(connection, item["product_id"], item["batch_id"], item["warehouse_id"], "sale", item["quantity"], "sale", sale_id, f"SprzedaÅ¼ z POS: {item['title']}")

    wz_id, wz_number = create_document(connection, "WZ", "sale", sale_id, total_amount, warehouse_id=warehouse_id, customer_id=customer_id, notes="Dokument wydania zewnÄtrznego")

    fv_id = None
    fv_number = ""
    if issue_invoice:
        fv_id, fv_number = create_document(connection, "FV", "sale", sale_id, total_amount, warehouse_id=warehouse_id, customer_id=customer_id, notes="Faktura sprzedaÅ¼y")

    return {"sale_id": sale_id, "total_amount": total_amount, "wz_id": wz_id, "wz_number": wz_number, "fv_id": fv_id, "fv_number": fv_number}


def normalize_lookup_code(value):
    return "".join(character for character in clean_value(value).lower() if character.isalnum())


def find_inventory_item_by_code(code):
    raw_code = clean_value(code)
    normalized_code = normalize_lookup_code(raw_code)
    if not raw_code:
        return None
    for item in get_inventory_items():
        lookup_values = {
            normalize_lookup_code(item.get("ean")),
            normalize_lookup_code(item.get("sku")),
            normalize_lookup_code(item.get("title")),
        }
        if normalized_code and normalized_code in lookup_values:
            return item
        if raw_code.lower() == clean_value(item.get("title")).lower():
            return item
    return None




def reverse_sale_transaction(sale_id, reason):
    with get_db() as connection:
        sale = connection.execute("SELECT * FROM sales WHERE id = ?", (sale_id,)).fetchone()
        if not sale:
            raise ValueError("Nie znaleziono sprzedaÅ¼y do cofniÄcia.")
        if clean_value(sale["status"]).lower() == "reversed":
            raise ValueError("Ta sprzedaÅ¼ zostaÅa juÅ¼ cofniÄta.")

        items = connection.execute(
            """
            SELECT si.*, p.title, b.warehouse_id
            FROM sale_items si
            JOIN products p ON p.id = si.product_id
            JOIN stock_batches b ON b.id = si.batch_id
            WHERE si.sale_id = ?
            """,
            (sale_id,),
        ).fetchall()
        if not items:
            raise ValueError("SprzedaÅ¼ nie ma pozycji do cofniÄcia.")

        summary_before = {"status": sale["status"], "total_amount": sale["total_amount"], "reason": clean_value(reason)}
        for item in items:
            connection.execute(
                "UPDATE stock_batches SET quantity = quantity + ?, updated_at = ? WHERE id = ?",
                (item["quantity"], now_iso(), item["batch_id"]),
            )
            create_stock_movement(
                connection,
                item["product_id"],
                item["batch_id"],
                item["warehouse_id"],
                "sale_reversal",
                item["quantity"],
                "sale_reversal",
                sale_id,
                f"CofniÄcie sprzedaÅ¼y: {item['title']}",
            )

        connection.execute(
            "UPDATE sales SET status = 'reversed', reversed_at = ?, reversed_by = ?, reverse_reason = ? WHERE id = ?",
            (now_iso(), session.get("user_id"), clean_value(reason), sale_id),
        )
        connection.execute(
            "UPDATE documents SET status = 'reversed', notes = COALESCE(notes, '') || ? WHERE related_type = 'sale' AND related_id = ?",
            (f" | CofniÄto: {clean_value(reason)}", str(sale_id)),
        )
        connection.commit()

    log_audit(
        "reverse",
        "sale",
        sale_id,
        f"CofniÄto sprzedaÅ¼ #{sale_id}: {clean_value(reason) or 'bez podanego powodu'}",
        before_state=format_state_pairs(summary_before),
        after_state=format_state_pairs({"status": "reversed", "reversed_at": now_iso()}),
    )


def reverse_manual_movement(movement_id, reason):
    with get_db() as connection:
        movement = connection.execute(
            """
            SELECT m.*, b.quantity AS batch_quantity, p.title AS product_title
            FROM stock_movements m
            JOIN stock_batches b ON b.id = m.batch_id
            JOIN products p ON p.id = m.product_id
            WHERE m.id = ?
            """,
            (movement_id,),
        ).fetchone()
        if not movement:
            raise ValueError("Nie znaleziono ruchu magazynowego.")
        if clean_value(movement["reversed_at"]):
            raise ValueError("Ten ruch zostaÅ juÅ¼ cofniÄty.")
        if movement["movement_type"] not in {"adjustment_in", "adjustment_out"}:
            raise ValueError("Cofanie jest dostÄpne tylko dla rÄcznych korekt plus/minus.")

        batch_quantity = int_safe(movement["batch_quantity"], 0)
        quantity = int_safe(movement["quantity"], 0)
        if movement["movement_type"] == "adjustment_in":
            if batch_quantity < quantity:
                raise ValueError("Brak wystarczajÄcego stanu do cofniÄcia korekty na plus.")
            connection.execute("UPDATE stock_batches SET quantity = quantity - ?, updated_at = ? WHERE id = ?", (quantity, now_iso(), movement["batch_id"]))
            reverse_type = "adjustment_in_reversal"
        else:
            connection.execute("UPDATE stock_batches SET quantity = quantity + ?, updated_at = ? WHERE id = ?", (quantity, now_iso(), movement["batch_id"]))
            reverse_type = "adjustment_out_reversal"

        create_stock_movement(
            connection,
            movement["product_id"],
            movement["batch_id"],
            movement["warehouse_id"],
            reverse_type,
            quantity,
            "movement_reversal",
            movement_id,
            f"CofniÄcie ruchu #{movement_id}: {clean_value(reason) or movement['notes']}",
        )
        connection.execute(
            "UPDATE stock_movements SET reversed_at = ?, reversed_by = ?, reverse_reason = ? WHERE id = ?",
            (now_iso(), session.get("user_id"), clean_value(reason), movement_id),
        )
        connection.commit()

    log_audit(
        "reverse",
        "movement",
        movement_id,
        f"CofniÄto ruch magazynowy #{movement_id}: {clean_value(reason) or movement['product_title']}",
        before_state=format_state_pairs({"movement_type": movement["movement_type"], "quantity": quantity}),
        after_state=format_state_pairs({"reversed_at": now_iso(), "reverse_type": reverse_type}),
    )


def get_inventory_stats(items):
    total_value = sum(item["total_value"] for item in items)
    low_stock = [item for item in items if item["is_low"]]
    expired = [item for item in items if item["status_class"] == "expired"]
    expiring_soon = [item for item in items if item["status_class"] in {"warning-7", "warning-30"}]
    missing_price = [item for item in items if item["missing_price"]]
    reserved_total = sum(item["reserved_quantity_int"] for item in items)
    available_total = sum(item["available_quantity_int"] for item in items)

    categories = {}
    for item in items:
        categories[item["category"]] = categories.get(item["category"], 0) + item["total_value"]

    return {
        "total_items": len(items),
        "total_value": round(total_value, 2),
        "low_stock_count": len(low_stock),
        "expired_count": len(expired),
        "expiring_count": len(expiring_soon),
        "missing_price_count": len(missing_price),
        "reserved_units": reserved_total,
        "available_units": available_total,
        "category_labels": list(categories.keys()),
        "category_values": [round(value, 2) for value in categories.values()],
        "low_stock_items": low_stock[:6],
        "expired_items": expired[:6],
    }


def get_dashboard_metrics():
    inventory_items = get_inventory_items()
    inventory_stats = get_inventory_stats(inventory_items)
    sales_today = query_one(
        "SELECT COALESCE(SUM(total_amount), 0) AS total FROM sales WHERE date(created_at) = ?",
        (today_iso(),),
    )
    purchases_today = query_one(
        "SELECT COALESCE(SUM(total_amount), 0) AS total FROM purchase_orders WHERE date(created_at) = ?",
        (today_iso(),),
    )
    open_orders = query_one(
        "SELECT COUNT(*) AS total FROM purchase_orders WHERE status IN ('new', 'ordered', 'received')"
    )
    customers_count = query_one("SELECT COUNT(*) AS total FROM customers")
    suppliers_count = query_one("SELECT COUNT(*) AS total FROM suppliers")
    products_count = query_one("SELECT COUNT(*) AS total FROM products")
    top_sellers = query_all(
        """
        SELECT p.title, COALESCE(SUM(si.quantity), 0) AS sold_quantity
        FROM sale_items si
        JOIN products p ON p.id = si.product_id
        GROUP BY p.id
        ORDER BY sold_quantity DESC, p.title ASC
        LIMIT 5
        """
    )

    return {
        "sales_today": round(float_safe(sales_today["total"], 0.0), 2),
        "purchases_today": round(float_safe(purchases_today["total"], 0.0), 2),
        "open_orders": int_safe(open_orders["total"], 0),
        "customers_count": int_safe(customers_count["total"], 0),
        "suppliers_count": int_safe(suppliers_count["total"], 0),
        "products_count": int_safe(products_count["total"], 0),
        "inventory": inventory_stats,
        "top_seller_labels": [row["title"] for row in top_sellers],
        "top_seller_values": [int_safe(row["sold_quantity"], 0) for row in top_sellers],
    }


def record_archive(kind, title, file_name, file_path=""):
    execute_db(
        """
        INSERT INTO report_archive (kind, title, file_name, file_path, created_at, user_id)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (kind, title, file_name, file_path, now_iso(), session.get("user_id")),
    )


def get_alert_center():
    items = get_inventory_items()
    low_stock = [item for item in items if item["is_low"]]
    expired = [item for item in items if item["status_class"] == "expired"]
    expiring = [item for item in items if item["status_class"] in {"warning-7", "warning-30"}]
    missing_price = [item for item in items if item["missing_price"]]
    missing_image = [item for item in items if not item["has_image"]]

    supplierless = query_all(
        """
        SELECT p.title, p.sku
        FROM products p
        LEFT JOIN purchase_items pi ON pi.product_id = p.id
        WHERE pi.id IS NULL
        ORDER BY p.title COLLATE NOCASE ASC
        LIMIT 20
        """
    )

    return {
        "low_stock": low_stock,
        "expired": expired,
        "expiring": expiring,
        "missing_price": missing_price,
        "missing_image": missing_image,
        "supplierless": [dict(row) for row in supplierless],
        "counts": {
            "low_stock": len(low_stock),
            "expired": len(expired),
            "expiring": len(expiring),
            "missing_price": len(missing_price),
            "missing_image": len(missing_image),
            "supplierless": len(supplierless),
        },
    }


def get_calendar_entries():
    entries = []
    for item in get_inventory_items():
        if item["expiry_date"]:
            entries.append(
                {
                    "date": item["expiry_date"],
                    "type": "expiry",
                    "title": item["title"],
                    "meta": item["warehouse_name"],
                }
            )

    for task in query_all(
        "SELECT title, due_date, priority FROM tasks WHERE due_date IS NOT NULL ORDER BY due_date ASC LIMIT 50"
    ):
        entries.append(
            {
                "date": task["due_date"],
                "type": "task",
                "title": task["title"],
                "meta": task["priority"],
            }
        )

    for purchase in get_purchase_orders(50):
        entries.append(
            {
                "date": purchase["created_at"][:10],
                "type": "purchase",
                "title": f"Zakup #{purchase['id']}",
                "meta": purchase["supplier_name"],
            }
        )

    return sorted(entries, key=lambda entry: entry["date"] or "9999-12-31")


def get_data_quality_report():
    products_rows = query_all("SELECT * FROM products ORDER BY title COLLATE NOCASE ASC")
    products_list = [dict(row) for row in products_rows]
    return {
        "missing_sku": [product for product in products_list if not clean_value(product["sku"])],
        "missing_ean": [product for product in products_list if not clean_value(product["ean"])],
        "missing_brand": [product for product in products_list if not clean_value(product["brand"])],
        "missing_prices": [product for product in products_list if float_safe(product["purchase_price"], 0.0) <= 0 or float_safe(product["sale_price"], 0.0) <= 0],
        "missing_minimums": [product for product in products_list if int_safe(product["min_quantity"], 0) <= 0],
        "missing_images": [product for product in products_list if not clean_value(product["image"])],
    }


def get_tasks_data():
    rows = query_all(
        """
        SELECT t.*, COALESCE(u.full_name, 'Nieprzypisane') AS assigned_name
        FROM tasks t
        LEFT JOIN users u ON u.id = t.assigned_user_id
        ORDER BY
            CASE t.status WHEN 'open' THEN 0 WHEN 'in_progress' THEN 1 ELSE 2 END,
            t.due_date ASC,
            t.created_at DESC
        """
    )
    return [dict(row) for row in rows]


def get_notes_data(limit=50):
    rows = query_all(
        """
        SELECT n.*, COALESCE(u.full_name, 'System') AS user_name
        FROM entity_notes n
        LEFT JOIN users u ON u.id = n.user_id
        ORDER BY n.created_at DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [dict(row) for row in rows]


def get_attachments_data(limit=50):
    rows = query_all(
        """
        SELECT f.*, COALESCE(u.full_name, 'System') AS user_name
        FROM entity_files f
        LEFT JOIN users u ON u.id = f.user_id
        ORDER BY f.created_at DESC
        LIMIT ?
        """,
        (limit,),
    )
    data = []
    for row in rows:
        entry = dict(row)
        entry["file_url"] = file_url(entry["file_name"])
        data.append(entry)
    return data


def get_archives_data():
    rows = query_all(
        """
        SELECT a.*, COALESCE(u.full_name, 'System') AS user_name
        FROM report_archive a
        LEFT JOIN users u ON u.id = a.user_id
        ORDER BY a.created_at DESC
        LIMIT 100
        """
    )
    return [dict(row) for row in rows]


def get_owner_dashboard_metrics():
    metrics = get_dashboard_metrics()
    margin_row = query_one(
        """
        SELECT COALESCE(SUM(si.total_price), 0) AS sales_total,
               COALESCE(SUM(si.quantity * p.purchase_price), 0) AS purchase_total,
               COUNT(DISTINCT s.id) AS sales_count
        FROM sale_items si
        JOIN products p ON p.id = si.product_id
        JOIN sales s ON s.id = si.sale_id
        WHERE COALESCE(s.status, 'completed') != 'reversed'
        """
    )
    today_margin_row = query_one(
        """
        SELECT COALESCE(SUM(si.total_price), 0) AS sales_total,
               COALESCE(SUM(si.quantity * p.purchase_price), 0) AS purchase_total
        FROM sale_items si
        JOIN products p ON p.id = si.product_id
        JOIN sales s ON s.id = si.sale_id
        WHERE COALESCE(s.status, 'completed') != 'reversed' AND date(s.created_at) = ?
        """,
        (today_iso(),),
    )
    dead_stock = query_one("SELECT COUNT(*) AS total FROM products p LEFT JOIN sale_items si ON si.product_id = p.id WHERE si.id IS NULL")
    reservations = query_one("SELECT COALESCE(SUM(quantity), 0) AS total FROM reservations WHERE status = 'reserved'")
    open_tasks = query_one("SELECT COUNT(*) AS total FROM tasks WHERE status != 'done'")
    reversed_sales = query_one("SELECT COUNT(*) AS total FROM sales WHERE status = 'reversed'")

    def enrich(row):
        entry = dict(row)
        entry["sales_total"] = round(float_safe(entry.get("sales_total"), 0.0), 2)
        entry["purchase_total"] = round(float_safe(entry.get("purchase_total"), 0.0), 2)
        entry["margin_total"] = round(entry["sales_total"] - entry["purchase_total"], 2)
        entry["margin_percent"] = round((entry["margin_total"] / entry["sales_total"] * 100.0), 2) if entry["sales_total"] else 0.0
        return entry

    top_margin_products = [
        enrich(row)
        for row in query_all(
            """
            SELECT p.title, COALESCE(SUM(si.quantity), 0) AS units_sold,
                   COALESCE(SUM(si.total_price), 0) AS sales_total,
                   COALESCE(SUM(si.quantity * p.purchase_price), 0) AS purchase_total
            FROM sale_items si
            JOIN products p ON p.id = si.product_id
            JOIN sales s ON s.id = si.sale_id
            WHERE COALESCE(s.status, 'completed') != 'reversed'
            GROUP BY p.id
            ORDER BY (COALESCE(SUM(si.total_price), 0) - COALESCE(SUM(si.quantity * p.purchase_price), 0)) DESC, units_sold DESC, p.title ASC
            LIMIT 5
            """
        )
    ]
    low_margin_products = [
        enrich(row)
        for row in query_all(
            """
            SELECT p.title, COALESCE(SUM(si.quantity), 0) AS units_sold,
                   COALESCE(SUM(si.total_price), 0) AS sales_total,
                   COALESCE(SUM(si.quantity * p.purchase_price), 0) AS purchase_total
            FROM sale_items si
            JOIN products p ON p.id = si.product_id
            JOIN sales s ON s.id = si.sale_id
            WHERE COALESCE(s.status, 'completed') != 'reversed'
            GROUP BY p.id
            HAVING COALESCE(SUM(si.quantity), 0) > 0
            ORDER BY (COALESCE(SUM(si.total_price), 0) - COALESCE(SUM(si.quantity * p.purchase_price), 0)) ASC, units_sold DESC, p.title ASC
            LIMIT 5
            """
        )
    ]
    margin_by_day = [
        enrich(row)
        for row in query_all(
            """
            SELECT date(s.created_at) AS sale_day,
                   COALESCE(SUM(si.total_price), 0) AS sales_total,
                   COALESCE(SUM(si.quantity * p.purchase_price), 0) AS purchase_total
            FROM sales s
            LEFT JOIN sale_items si ON si.sale_id = s.id
            LEFT JOIN products p ON p.id = si.product_id
            WHERE date(s.created_at) >= date(?, '-6 day')
              AND COALESCE(s.status, 'completed') != 'reversed'
            GROUP BY date(s.created_at)
            ORDER BY sale_day ASC
            """,
            (today_iso(),),
        )
    ]

    sales_total = round(float_safe(margin_row["sales_total"], 0.0), 2)
    purchase_total = round(float_safe(margin_row["purchase_total"], 0.0), 2)
    estimated_margin = round(sales_total - purchase_total, 2)
    sales_count = int_safe(margin_row["sales_count"], 0)
    margin_percent = round((estimated_margin / sales_total * 100.0), 2) if sales_total else 0.0
    today_sales_total = round(float_safe(today_margin_row["sales_total"], 0.0), 2)
    today_purchase_total = round(float_safe(today_margin_row["purchase_total"], 0.0), 2)
    today_margin = round(today_sales_total - today_purchase_total, 2)
    today_margin_percent = round((today_margin / today_sales_total * 100.0), 2) if today_sales_total else 0.0

    metrics.update({
        "estimated_margin": estimated_margin,
        "sales_total_all": sales_total,
        "purchase_total_all": purchase_total,
        "margin_percent": margin_percent,
        "sales_count": sales_count,
        "average_margin_per_sale": round((estimated_margin / sales_count), 2) if sales_count else 0.0,
        "today_margin": today_margin,
        "today_margin_percent": today_margin_percent,
        "dead_stock_count": int_safe(dead_stock["total"], 0),
        "reserved_units": int_safe(reservations["total"], 0),
        "open_tasks_count": int_safe(open_tasks["total"], 0),
        "reversed_sales_count": int_safe(reversed_sales["total"], 0),
        "top_margin_products": top_margin_products,
        "low_margin_products": low_margin_products,
        "margin_by_day": margin_by_day,
    })
    return metrics




def get_dictionary_values():
    rows = query_all("SELECT * FROM dictionary_values ORDER BY dict_type ASC, value COLLATE NOCASE ASC")
    grouped = {"category": [], "brand": [], "location": []}
    for row in rows:
        grouped.setdefault(row["dict_type"], []).append(dict(row))
    return grouped


def get_user_activity():
    summary = query_all(
        """
        SELECT COALESCE(u.full_name, 'System') AS user_name, COUNT(*) AS actions_count, MAX(a.created_at) AS last_action
        FROM audit_log a
        LEFT JOIN users u ON u.id = a.user_id
        GROUP BY COALESCE(u.full_name, 'System')
        ORDER BY actions_count DESC, last_action DESC
        """
    )
    return [dict(row) for row in summary]


def get_system_settings():
    rows = query_all("SELECT key, value FROM system_settings ORDER BY key ASC")
    return {row["key"]: row["value"] for row in rows}


def get_users(active_only=False):
    sql = "SELECT id, username, full_name, role, active, created_at FROM users"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY full_name COLLATE NOCASE ASC"
    return [dict(row) for row in query_all(sql)]


def get_stocktakes():
    rows = query_all(
        """
        SELECT s.*, w.name AS warehouse_name, COALESCE(u.full_name, 'System') AS user_name
        FROM stocktakes s
        JOIN warehouses w ON w.id = s.warehouse_id
        LEFT JOIN users u ON u.id = s.user_id
        ORDER BY s.created_at DESC, s.id DESC
        """
    )
    return [dict(row) for row in rows]


def get_reservations_data():
    rows = query_all(
        """
        SELECT r.*, p.title AS product_title, COALESCE(c.name, 'Klient detaliczny') AS customer_name, w.name AS warehouse_name,
               b.quantity AS batch_quantity
        FROM reservations r
        JOIN stock_batches b ON b.id = r.batch_id
        JOIN products p ON p.id = b.product_id
        JOIN warehouses w ON w.id = b.warehouse_id
        LEFT JOIN customers c ON c.id = r.customer_id
        ORDER BY r.created_at DESC
        """
    )
    return [dict(row) for row in rows]


def get_demo_snapshot():
    return {
        "sales_today": 5820.50,
        "orders": 24,
        "alerts": 6,
        "margin": 1730.20,
        "labels": ["Nabiał", "Chemia", "Napoje", "Elektronika"],
        "values": [1840, 920, 1360, 1700],
    }


def serialize_inventory_item(item):
    return {
        "batch_id": item["batch_id"],
        "product_id": item["product_id"],
        "title": item["title"],
        "sku": item["sku"],
        "ean": item["ean"],
        "category": item["category"],
        "brand": item["brand"],
        "quantity": item["quantity_int"],
        "reserved_quantity": item["reserved_quantity_int"],
        "available_quantity": item["available_quantity_int"],
        "min_quantity": item["min_quantity_int"],
        "warehouse_id": item["warehouse_id"],
        "warehouse_name": item["warehouse_name"],
        "purchase_price": item["purchase_price_float"],
        "sale_price": item["sale_price_float"],
        "expiry_date": item["expiry_date"],
        "purchase_date": item["purchase_date"],
        "lot_number": item["lot_number"],
        "serial_number": item["serial_number"],
        "document_url": item["document_url"],
        "image_url": item["image_url"],
        "has_document": item["has_document"],
        "has_image": item["has_image"],
        "total_value": item["total_value"],
        "status_class": item["status_class"],
        "is_low": item["is_low"],
        "missing_price": item["missing_price"],
    }


def create_stock_movement(connection, product_id, batch_id, warehouse_id, movement_type, quantity, reference_type, reference_id, notes):
    connection.execute(
        """
        INSERT INTO stock_movements (product_id, batch_id, warehouse_id, movement_type, quantity, reference_type, reference_id, notes, created_at, user_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            product_id,
            batch_id,
            warehouse_id,
            movement_type,
            quantity,
            reference_type,
            str(reference_id) if reference_id else "",
            notes,
            now_iso(),
            session.get("user_id"),
        ),
    )


@app.route("/")
def home():
    if g.user:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = clean_value(request.form.get("username"))
        password = request.form.get("password") or ""
        user = query_one("SELECT * FROM users WHERE username = ? AND active = 1", (username,))

        if user and check_password_hash(user["password_hash"], password):
            selected_lang = get_current_language()
            session.clear()
            session["lang"] = selected_lang
            session["user_id"] = user["id"]
            log_audit("login", "user", user["id"], f"Użytkownik {user['username']} zalogował się do systemu")
            return redirect(url_for("dashboard"))

        flash("Nieprawidłowy login lub hasło.", "error")

    return render_template("login.html")




@app.route("/language/<string:lang>")
def set_language(lang):
    lang = clean_value(lang).lower()
    if lang in SUPPORTED_LANGUAGES:
        session["lang"] = lang
    next_url = clean_value(request.args.get("next"))
    if not next_url:
        next_url = request.referrer or url_for("login")
    return redirect(next_url)

@app.route("/logout")
def logout():
    user_id = session.get("user_id")
    selected_lang = get_current_language()
    if user_id:
        log_audit("logout", "user", user_id, "Wylogowano z systemu")
    session.clear()
    session["lang"] = selected_lang
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    metrics = get_dashboard_metrics()
    return render_template(
        "dashboard.html",
        metrics=metrics,
        recent_sales=get_recent_sales(),
        recent_audit=get_recent_audit(8),
    )


@app.route("/inventory")
@login_required
def inventory():
    items = get_inventory_items()
    return render_template(
        "inventory.html",
        items=items,
        stats=get_inventory_stats(items),
        warehouses=get_warehouses(),
        recent_movements=get_recent_movements(8),
    )


@app.route("/inventory/add", methods=["POST"])
@login_required
def add_inventory():
    title = clean_value(request.form.get("title"))
    category = clean_value(request.form.get("category"), "Inne") or "Inne"
    quantity = int_safe(request.form.get("quantity"), 1)
    min_quantity = int_safe(request.form.get("min_quantity"), 0)
    purchase_price = float_safe(request.form.get("purchase_price"), 0.0)
    sale_price = float_safe(request.form.get("sale_price"), purchase_price)
    warehouse_name = clean_value(request.form.get("location"), "Magazyn główny") or "Magazyn główny"
    expiry_date = clean_value(request.form.get("expiry"))
    purchase_date = clean_value(request.form.get("purchase")) or today_iso()
    lot_number = clean_value(request.form.get("lot_number"))
    serial_number = clean_value(request.form.get("serial_number"))
    sku = clean_value(request.form.get("sku"))
    ean = clean_value(request.form.get("ean"))
    brand = clean_value(request.form.get("brand"))

    if not title:
        flash("Nazwa produktu jest wymagana.", "error")
        return redirect(url_for("inventory"))

    document_name = save_uploaded_file(request.files.get("document"), FILE_EXTENSIONS)
    image_name = save_uploaded_file(request.files.get("image"), IMAGE_EXTENSIONS)

    with get_db() as connection:
        product_id = get_or_create_product(
            connection,
            title,
            category,
            ean=ean,
            sku=sku,
            brand=brand,
            purchase_price=purchase_price,
            sale_price=sale_price,
            min_quantity=min_quantity,
        )
        warehouse_id = get_or_create_warehouse(connection, warehouse_name)
        cursor = connection.execute(
            """
            INSERT INTO stock_batches (product_id, warehouse_id, quantity, expiry_date, purchase_date, lot_number, serial_number, document, image, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                product_id,
                warehouse_id,
                quantity,
                expiry_date,
                purchase_date,
                lot_number,
                serial_number,
                document_name,
                image_name,
                now_iso(),
                now_iso(),
            ),
        )
        batch_id = cursor.lastrowid
        create_stock_movement(connection, product_id, batch_id, warehouse_id, "receipt", quantity, "inventory_add", batch_id, "Dodano stan przez formularz inwentaryzacji")
        connection.commit()

    log_audit("create", "stock_batch", batch_id, f"Dodano stan dla produktu {title} ({quantity} szt.)")
    flash("Stan magazynowy został dodany.", "success")
    return redirect(url_for("inventory"))


@app.route("/inventory/update_quantity/<int:batch_id>/<action>")
@login_required
def update_quantity(batch_id, action):
    with get_db() as connection:
        row = connection.execute(
            """
            SELECT b.id AS batch_id, b.quantity, b.warehouse_id, p.id AS product_id, p.title
            FROM stock_batches b
            JOIN products p ON p.id = b.product_id
            WHERE b.id = ? AND b.is_active = 1
            """,
            (batch_id,),
        ).fetchone()
        if not row:
            return jsonify({"success": False, "message": "Nie znaleziono pozycji."}), 404

        quantity = int_safe(row["quantity"], 0)
        delta = 1 if action == "inc" else -1
        if action not in {"inc", "dec"}:
            return jsonify({"success": False, "message": "Nieznana akcja."}), 400
        if action == "dec" and quantity <= 0:
            return jsonify({"success": False, "message": "Stan nie może spaść poniżej zera."}), 400

        new_quantity = quantity + delta
        connection.execute(
            "UPDATE stock_batches SET quantity = ?, updated_at = ? WHERE id = ?",
            (new_quantity, now_iso(), batch_id),
        )
        create_stock_movement(
            connection,
            row["product_id"],
            batch_id,
            row["warehouse_id"],
            "adjustment_in" if delta > 0 else "adjustment_out",
            abs(delta),
            "quick_adjust",
            batch_id,
            "Szybka zmiana ilości z tabeli",
        )
        connection.commit()

    item = next((entry for entry in get_inventory_items() if entry["batch_id"] == batch_id), None)
    if item:
        log_audit("update", "stock_batch", batch_id, f"Zmieniono ilość dla {item['title']} na {item['quantity_int']}")
        return jsonify(
            {
                "success": True,
                "item": serialize_inventory_item(item),
                "stats": get_inventory_stats(get_inventory_items()),
            }
        )
    return jsonify({"success": False, "message": "Nie udało się odświeżyć danych."}), 500


@app.route("/inventory/edit/<int:batch_id>", methods=["POST"])
@login_required
def edit_inventory(batch_id):
    title = clean_value(request.form.get("title"))
    if not title:
        return jsonify({"success": False, "message": "Nazwa produktu jest wymagana."}), 400

    with get_db() as connection:
        existing = connection.execute(
            """
            SELECT b.*, p.id AS product_id
            FROM stock_batches b
            JOIN products p ON p.id = b.product_id
            WHERE b.id = ? AND b.is_active = 1
            """,
            (batch_id,),
        ).fetchone()
        if not existing:
            return jsonify({"success": False, "message": "Nie znaleziono pozycji."}), 404

        warehouse_id = get_or_create_warehouse(connection, request.form.get("location"))
        connection.execute(
            """
            UPDATE products
            SET title = ?, category = ?, sku = ?, ean = ?, brand = ?, min_quantity = ?, purchase_price = ?, sale_price = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                title,
                clean_value(request.form.get("category"), "Inne") or "Inne",
                clean_value(request.form.get("sku")),
                clean_value(request.form.get("ean")),
                clean_value(request.form.get("brand")),
                int_safe(request.form.get("min_quantity"), 0),
                float_safe(request.form.get("purchase_price"), 0.0),
                float_safe(request.form.get("sale_price"), 0.0),
                now_iso(),
                existing["product_id"],
            ),
        )
        connection.execute(
            """
            UPDATE stock_batches
            SET warehouse_id = ?, quantity = ?, expiry_date = ?, purchase_date = ?, lot_number = ?, serial_number = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                warehouse_id,
                int_safe(request.form.get("quantity"), 0),
                clean_value(request.form.get("expiry")),
                clean_value(request.form.get("purchase")),
                clean_value(request.form.get("lot_number")),
                clean_value(request.form.get("serial_number")),
                now_iso(),
                batch_id,
            ),
        )
        connection.commit()

    item = next((entry for entry in get_inventory_items() if entry["batch_id"] == batch_id), None)
    if item:
        log_audit("edit", "stock_batch", batch_id, f"Zapisano edycję pozycji {item['title']}")
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify(
                {
                    "success": True,
                    "item": serialize_inventory_item(item),
                    "stats": get_inventory_stats(get_inventory_items()),
                }
            )

    flash("Pozycja została zaktualizowana.", "success")
    return redirect(url_for("inventory"))


@app.route("/inventory/delete/<int:batch_id>")
@login_required
def delete_inventory(batch_id):
    with get_db() as connection:
        row = connection.execute(
            """
            SELECT b.*, p.title
            FROM stock_batches b
            JOIN products p ON p.id = b.product_id
            WHERE b.id = ? AND b.is_active = 1
            """,
            (batch_id,),
        ).fetchone()
        if row:
            remove_uploaded_file(row["document"])
            remove_uploaded_file(row["image"])
            connection.execute("UPDATE stock_batches SET is_active = 0, updated_at = ? WHERE id = ?", (now_iso(), batch_id))
            connection.commit()
            log_audit("delete", "stock_batch", batch_id, f"Usunięto pozycję {row['title']}")
            flash("Pozycja została usunięta.", "info")
    return redirect(url_for("inventory"))


@app.route("/products", methods=["GET", "POST"])
@login_required
def products():
    if request.method == "POST":
        title = clean_value(request.form.get("title"))
        if not title:
            flash("Nazwa produktu jest wymagana.", "error")
            return redirect(url_for("products"))

        image_name = save_uploaded_file(request.files.get("image"), IMAGE_EXTENSIONS)
        document_name = save_uploaded_file(request.files.get("document"), FILE_EXTENSIONS)

        try:
            product_id = execute_db(
                """
                INSERT INTO products (title, sku, ean, category, brand, unit, min_quantity, purchase_price, sale_price, image, document, notes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    title,
                    clean_value(request.form.get("sku")) or None,
                    clean_value(request.form.get("ean")),
                    clean_value(request.form.get("category"), "Inne") or "Inne",
                    clean_value(request.form.get("brand")),
                    clean_value(request.form.get("unit"), "szt.") or "szt.",
                    int_safe(request.form.get("min_quantity"), 0),
                    float_safe(request.form.get("purchase_price"), 0.0),
                    float_safe(request.form.get("sale_price"), 0.0),
                    image_name,
                    document_name,
                    clean_value(request.form.get("notes")),
                    now_iso(),
                    now_iso(),
                ),
            )
        except sqlite3.IntegrityError:
            flash("Produkt z takim SKU już istnieje.", "error")
            return redirect(url_for("products"))

        log_audit("create", "product", product_id, f"Dodano kartotekę produktu {title}")
        flash("Produkt został dodany do katalogu.", "success")
        return redirect(url_for("products"))

    return render_template("products.html", products=get_products())


@app.route("/suppliers", methods=["GET", "POST"])
@login_required
def suppliers():
    if request.method == "POST":
        name = clean_value(request.form.get("name"))
        if not name:
            flash("Nazwa dostawcy jest wymagana.", "error")
            return redirect(url_for("suppliers"))

        supplier_id = execute_db(
            """
            INSERT INTO suppliers (name, tax_id, email, phone, address, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                clean_value(request.form.get("tax_id")),
                clean_value(request.form.get("email")),
                clean_value(request.form.get("phone")),
                clean_value(request.form.get("address")),
                clean_value(request.form.get("notes")),
                now_iso(),
            ),
        )
        log_audit("create", "supplier", supplier_id, f"Dodano dostawcę {name}")
        flash("Dostawca został zapisany.", "success")
        return redirect(url_for("suppliers"))

    return render_template("suppliers.html", suppliers=get_suppliers())


@app.route("/customers", methods=["GET", "POST"])
@login_required
def customers():
    if request.method == "POST":
        name = clean_value(request.form.get("name"))
        if not name:
            flash("Nazwa klienta jest wymagana.", "error")
            return redirect(url_for("customers"))

        customer_id = execute_db(
            """
            INSERT INTO customers (name, tax_id, email, phone, address, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                clean_value(request.form.get("tax_id")),
                clean_value(request.form.get("email")),
                clean_value(request.form.get("phone")),
                clean_value(request.form.get("address")),
                clean_value(request.form.get("notes")),
                now_iso(),
            ),
        )
        log_audit("create", "customer", customer_id, f"Dodano klienta {name}")
        flash("Klient został zapisany.", "success")
        return redirect(url_for("customers"))

    return render_template("customers.html", customers=get_customers())


@app.route("/purchases", methods=["GET", "POST"])
@login_required
def purchases():
    if request.method == "POST":
        product_id = int_safe(request.form.get("product_id"), 0)
        warehouse_id = int_safe(request.form.get("warehouse_id"), 0)
        supplier_id = int_safe(request.form.get("supplier_id"), 0)
        quantity = int_safe(request.form.get("quantity"), 0)
        unit_cost = float_safe(request.form.get("unit_cost"), 0.0)
        sale_price = float_safe(request.form.get("sale_price"), 0.0)
        expiry_date = clean_value(request.form.get("expiry_date"))
        purchase_date = clean_value(request.form.get("purchase_date")) or today_iso()
        lot_number = clean_value(request.form.get("lot_number"))

        if not (product_id and warehouse_id and quantity > 0):
            flash("Wybierz produkt, magazyn i ilość większą od zera.", "error")
            return redirect(url_for("purchases"))

        with get_db() as connection:
            purchase_id = connection.execute(
                """
                INSERT INTO purchase_orders (supplier_id, warehouse_id, status, total_amount, notes, created_at, user_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    supplier_id or None,
                    warehouse_id,
                    "received",
                    round(quantity * unit_cost, 2),
                    clean_value(request.form.get("notes")),
                    now_iso(),
                    session.get("user_id"),
                ),
            ).lastrowid

            connection.execute(
                """
                INSERT INTO purchase_items (purchase_order_id, product_id, quantity, unit_cost, total_cost, expiry_date, purchase_date, lot_number)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    purchase_id,
                    product_id,
                    quantity,
                    unit_cost,
                    round(quantity * unit_cost, 2),
                    expiry_date,
                    purchase_date,
                    lot_number,
                ),
            )

            connection.execute(
                """
                UPDATE products
                SET purchase_price = ?, sale_price = ?, updated_at = ?
                WHERE id = ?
                """,
                (unit_cost, sale_price, now_iso(), product_id),
            )

            batch_id = connection.execute(
                """
                INSERT INTO stock_batches (product_id, warehouse_id, quantity, expiry_date, purchase_date, lot_number, serial_number, document, image, is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, '', '', '', 1, ?, ?)
                """,
                (
                    product_id,
                    warehouse_id,
                    quantity,
                    expiry_date,
                    purchase_date,
                    lot_number,
                    now_iso(),
                    now_iso(),
                ),
            ).lastrowid

            create_stock_movement(connection, product_id, batch_id, warehouse_id, "purchase", quantity, "purchase_order", purchase_id, "Przyjęcie towaru po zakupie")
            pz_id, pz_number = create_document(
                connection,
                "PZ",
                "purchase_order",
                purchase_id,
                round(quantity * unit_cost, 2),
                warehouse_id=warehouse_id,
                supplier_id=supplier_id or None,
                notes="Przyjęcie zewnętrzne po zakupie",
            )
            connection.commit()

        log_audit("create", "purchase_order", purchase_id, f"Przyjęto zakup na kwotę {round(quantity * unit_cost, 2):.2f} zł")
        flash(f"Zakup i przyjęcie towaru zostały zapisane. Utworzono dokument {pz_number}.", "success")
        return redirect(url_for("purchases"))

    return render_template(
        "purchases.html",
        purchase_orders=get_purchase_orders(),
        products=get_products(),
        warehouses=get_warehouses(),
        suppliers=get_suppliers(),
    )


@app.route("/sales", methods=["GET", "POST"])
@login_required
def sales():
    if request.method == "POST":
        batch_id = int_safe(request.form.get("batch_id"), 0)
        customer_id = int_safe(request.form.get("customer_id"), 0)
        quantity = int_safe(request.form.get("quantity"), 0)
        payment_method = clean_value(request.form.get("payment_method"), "cash") or "cash"
        discount_percent = max(0.0, min(100.0, float_safe(request.form.get("discount_percent"), 0.0)))

        with get_db() as connection:
            batch = connection.execute(
                """
                SELECT b.id AS batch_id, b.quantity, b.warehouse_id, p.id AS product_id, p.title, p.sale_price,
                       COALESCE((SELECT SUM(r.quantity) FROM reservations r WHERE r.batch_id = b.id AND r.status = 'reserved'), 0) AS reserved_quantity
                FROM stock_batches b
                JOIN products p ON p.id = b.product_id
                WHERE b.id = ? AND b.is_active = 1
                """,
                (batch_id,),
            ).fetchone()
            if not batch:
                flash("Nie znaleziono partii do sprzedaÅ¼y.", "error")
                return redirect(url_for("sales"))
            available_quantity = int_safe(batch["quantity"], 0) - int_safe(batch["reserved_quantity"], 0)
            if quantity <= 0 or quantity > available_quantity:
                flash("NieprawidÅowa iloÅÄ do sprzedaÅ¼y.", "error")
                return redirect(url_for("sales"))

            unit_price = float_safe(request.form.get("unit_price"), float_safe(batch["sale_price"], 0.0))
            subtotal_price = round(quantity * unit_price, 2)
            discount_value = round(subtotal_price * discount_percent / 100, 2)
            total_price = round(subtotal_price - discount_value, 2)
            result = create_sale_transaction(
                connection,
                [{"batch_id": batch_id, "product_id": batch["product_id"], "title": batch["title"], "warehouse_id": batch["warehouse_id"], "warehouse_name": "", "available_quantity": available_quantity, "quantity": quantity, "unit_price": unit_price, "discount_percent": discount_percent, "subtotal_price": subtotal_price, "discount_value": discount_value, "total_price": total_price}],
                customer_id,
                payment_method,
                clean_value(request.form.get("notes")),
                request.form.get("issue_invoice") == "on",
            )
            connection.commit()

        log_audit("create", "sale", result["sale_id"], f"Sprzedano {quantity} szt. produktu {batch['title']}", after_state=format_state_pairs({"total": total_price, "payment": payment_method, "discount_percent": discount_percent}))
        flash(f"SprzedaÅ¼ zostaÅa zapisana. Utworzono dokument {result['wz_number']}.", "success")
        return redirect(url_for("sales"))

    cart_items = build_cart_items()
    cart_summary = summarize_line_items(cart_items)
    return render_template("sales.html", sales_rows=get_recent_sales(20), inventory_items=get_inventory_items(), customers=get_customers(), warehouses=get_warehouses(), cart_items=cart_items, cart_summary=cart_summary, documents=get_documents(15))




@app.route("/sales/cart/add", methods=["POST"])
@login_required
def sales_cart_add():
    batch_id = int_safe(request.form.get("batch_id"), 0)
    quantity = int_safe(request.form.get("quantity"), 1)
    unit_price = float_safe(request.form.get("unit_price"), 0.0)
    discount_percent = max(0.0, min(100.0, float_safe(request.form.get("discount_percent"), 0.0)))

    item = next((entry for entry in get_inventory_items() if entry["batch_id"] == batch_id), None)
    if not item:
        flash("Nie znaleziono partii do dodania do koszyka.", "error")
        return redirect(url_for("sales"))

    try:
        add_item_to_pos_cart(item, quantity=quantity, unit_price=unit_price, discount_percent=discount_percent)
        flash(f"Dodano {item['title']} do koszyka POS.", "success")
    except ValueError as error:
        flash(str(error), "error")
    return redirect(url_for("sales"))


@app.route("/sales/cart/scan", methods=["POST"])
@login_required
def sales_cart_scan():
    scan_code = clean_value(request.form.get("scan_code"))
    quantity = int_safe(request.form.get("quantity"), 1)

    item = find_inventory_item_by_code(scan_code)
    if not item:
        flash("Nie znaleziono produktu po kodzie EAN, SKU ani nazwie.", "error")
        return redirect(url_for("sales"))

    try:
        add_item_to_pos_cart(item, quantity=quantity)
        flash(f"Zeskanowano {item['title']} i dodano do koszyka POS.", "success")
    except ValueError as error:
        flash(str(error), "error")
    return redirect(url_for("sales"))




@app.route("/sales/cart/remove/<int:batch_id>")
@login_required
def sales_cart_remove(batch_id):
    cart = [item for item in get_cart() if item["batch_id"] != batch_id]
    save_cart(cart)
    flash("UsuniÄto pozycjÄ z koszyka.", "info")
    return redirect(url_for("sales"))


@app.route("/sales/cart/update/<int:batch_id>", methods=["POST"])
@login_required
def sales_cart_update(batch_id):
    quantity = int_safe(request.form.get("quantity"), 0)
    unit_price = float_safe(request.form.get("unit_price"), 0.0)
    discount_percent = max(0.0, min(100.0, float_safe(request.form.get("discount_percent"), 0.0)))

    item = next((entry for entry in get_inventory_items() if entry["batch_id"] == batch_id), None)
    if not item:
        flash("Nie znaleziono partii do aktualizacji koszyka.", "error")
        return redirect(url_for("sales"))

    cart = get_cart()
    existing = next((entry for entry in cart if entry["batch_id"] == batch_id), None)
    if not existing:
        flash("Ta pozycja nie znajduje siÄ juÅ¼ w koszyku.", "error")
        return redirect(url_for("sales"))

    if quantity <= 0:
        cart = [entry for entry in cart if entry["batch_id"] != batch_id]
        save_cart(cart)
        flash("Pozycja zostaÅa usuniÄta z koszyka.", "info")
        return redirect(url_for("sales"))

    try:
        add_item_to_pos_cart(item, quantity=quantity, unit_price=unit_price, discount_percent=discount_percent, replace=True)
        flash("Zaktualizowano pozycjÄ w koszyku POS.", "success")
    except ValueError as error:
        flash(str(error), "error")
    return redirect(url_for("sales"))




@app.route("/sales/cart/clear")
@login_required
def sales_cart_clear():
    clear_cart()
    flash("Wyczyszczono koszyk POS.", "info")
    return redirect(url_for("sales"))


@app.route("/sales/checkout", methods=["POST"])
@login_required
def sales_checkout():
    cart_items = build_cart_items()
    cart_summary = summarize_line_items(cart_items)
    customer_id = int_safe(request.form.get("customer_id"), 0)
    payment_method = clean_value(request.form.get("payment_method"), "cash") or "cash"
    notes = clean_value(request.form.get("notes"))
    issue_invoice = request.form.get("issue_invoice") == "on"

    if not cart_items:
        flash("Koszyk POS jest pusty.", "error")
        return redirect(url_for("sales"))

    try:
        with get_db() as connection:
            result = create_sale_transaction(connection, cart_items, customer_id, payment_method, notes, issue_invoice)
            connection.commit()
        clear_cart()
        log_audit("create", "sale", result["sale_id"], f"ZamkniÄto koszyk POS, dokument {result['wz_number']}", after_state=format_state_pairs({"items_count": cart_summary["items_count"], "units_count": cart_summary["units_count"], "total": cart_summary["total"], "estimated_margin": cart_summary["estimated_margin_total"], "payment": payment_method, "invoice": "tak" if result["fv_number"] else "nie"}))
        message = f"SprzedaÅ¼ POS zostaÅa zapisana. Utworzono {result['wz_number']}."
        if result["fv_number"]:
            message += f" Faktura: {result['fv_number']}."
        flash(message, "success")
    except ValueError as error:
        flash(str(error), "error")

    return redirect(url_for("sales"))


@app.route("/sales/<int:sale_id>/reverse", methods=["POST"])
@login_required
@role_required("admin", "manager")
def sales_reverse(sale_id):
    reason = clean_value(request.form.get("reason"))
    try:
        reverse_sale_transaction(sale_id, reason)
        flash("SprzedaÅ¼ zostaÅa cofniÄta, a stan magazynowy przywrÃ³cony.", "success")
    except ValueError as error:
        flash(str(error), "error")
    return redirect(url_for("sales"))



def get_document_payload(document_id):
    document = query_one(
        """
        SELECT d.*, COALESCE(c.name, 'Brak klienta') AS customer_name,
               COALESCE(s.name, 'Brak dostawcy') AS supplier_name,
               COALESCE(w.name, 'Brak magazynu') AS warehouse_name,
               COALESCE(u.full_name, 'System') AS user_name
        FROM documents d
        LEFT JOIN customers c ON c.id = d.customer_id
        LEFT JOIN suppliers s ON s.id = d.supplier_id
        LEFT JOIN warehouses w ON w.id = d.warehouse_id
        LEFT JOIN users u ON u.id = d.user_id
        WHERE d.id = ?
        """,
        (document_id,),
    )
    if not document:
        return None, []

    related_id = int_safe(document["related_id"], 0)
    related_items = []
    if document["related_type"] == "sale":
        rows = query_all(
            """
            SELECT p.title, si.quantity, si.unit_price,
                   COALESCE(si.discount_percent, 0) AS discount_percent,
                   ROUND(si.quantity * si.unit_price, 2) AS subtotal_price,
                   ROUND((si.quantity * si.unit_price) - si.total_price, 2) AS discount_value,
                   si.total_price
            FROM sale_items si
            JOIN products p ON p.id = si.product_id
            WHERE si.sale_id = ?
            """,
            (related_id,),
        )
        related_items = [dict(row) for row in rows]
    elif document["related_type"] == "purchase_order":
        rows = query_all(
            """
            SELECT p.title, pi.quantity, pi.unit_cost AS unit_price,
                   0 AS discount_percent,
                   pi.total_cost AS subtotal_price,
                   0 AS discount_value,
                   pi.total_cost AS total_price
            FROM purchase_items pi
            JOIN products p ON p.id = pi.product_id
            WHERE pi.purchase_order_id = ?
            """,
            (related_id,),
        )
        related_items = [dict(row) for row in rows]

    return dict(document), related_items


def build_document_pdf(document, items, settings):
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Arial", "B", 16)
    pdf.cell(
        0,
        10,
        clean_pdf_text(
            trf(
                "Dokument {doc_type} {doc_number}",
                "Document {doc_type} {doc_number}",
                doc_type=document["doc_type"],
                doc_number=document["doc_number"],
            )
        ),
        ln=True,
    )
    pdf.set_font("Arial", size=10)
    pdf.cell(0, 7, clean_pdf_text(settings.get("company_name") or "SID 4.0"), ln=True)
    if settings.get("company_tax_id"):
        pdf.cell(0, 6, clean_pdf_text(f"NIP: {settings.get('company_tax_id')}"), ln=True)
    if settings.get("company_address"):
        pdf.multi_cell(0, 6, clean_pdf_text(settings.get("company_address")))
    pdf.ln(2)

    counterparty = document.get("customer_name")
    if counterparty == "Brak klienta":
        counterparty = document.get("supplier_name")
    if counterparty == "Brak dostawcy":
        counterparty = "-"

    for label, value in [
        (tr("Numer", "Number"), document.get("doc_number", "-")),
        (tr("Typ", "Type"), document.get("doc_type", "-")),
        (tr("Data", "Date"), document.get("created_at", "-")),
        (tr("Magazyn", "Warehouse"), document.get("warehouse_name", "-")),
        (tr("Strona", "Counterparty"), counterparty or "-"),
        (tr("Operator", "Operator"), document.get("user_name", "System")),
        (tr("Status", "Status"), document.get("status", "issued")),
    ]:
        pdf.cell(38, 7, clean_pdf_text(f"{label}:"), 0, 0)
        pdf.cell(0, 7, clean_pdf_text(value), ln=True)

    if document.get("notes"):
        pdf.ln(2)
        pdf.set_font("Arial", "B", 10)
        pdf.cell(0, 7, clean_pdf_text(tr("Uwagi", "Notes")), ln=True)
        pdf.set_font("Arial", size=10)
        pdf.multi_cell(0, 6, clean_pdf_text(document.get("notes") or "-"))

    pdf.ln(4)
    pdf.set_font("Arial", "B", 9)
    for label, width in [
        (tr("Produkt", "Product"), 72),
        (tr("Ilosc", "Qty"), 18),
        (tr("Cena", "Price"), 24),
        (tr("Rabat %", "Discount %"), 22),
        (tr("Wartosc", "Value"), 28),
    ]:
        pdf.cell(width, 8, clean_pdf_text(label), 1, 0, "C", True)
    pdf.ln()
    pdf.set_font("Arial", size=9)
    for item in items:
        pdf.cell(72, 8, clean_pdf_text(item.get("title", "-"))[:36], 1)
        pdf.cell(18, 8, str(int_safe(item.get("quantity"), 0)), 1, 0, "C")
        pdf.cell(24, 8, f"{float_safe(item.get('unit_price'), 0.0):.2f}", 1, 0, "R")
        pdf.cell(22, 8, f"{float_safe(item.get('discount_percent'), 0.0):.2f}", 1, 0, "R")
        pdf.cell(28, 8, f"{float_safe(item.get('total_price'), 0.0):.2f}", 1, 0, "R")
        pdf.ln()

    summary = summarize_line_items(items)
    currency = clean_pdf_text(settings.get("currency") or "PLN")
    pdf.ln(4)
    for label, value in [
        (tr("Suma przed rabatem", "Subtotal"), summary["subtotal"]),
        (tr("Rabat", "Discount"), summary["discount_total"]),
        (tr("Razem", "Total"), summary["total"]),
    ]:
        pdf.set_font("Arial", "B", 10)
        pdf.cell(124, 8, clean_pdf_text(label), 1, 0, "R", True)
        pdf.cell(40, 8, f"{value:.2f} {currency}", 1, 0, "R")
        pdf.ln()

    out = pdf.output(dest="S")
    return out.encode("latin-1") if isinstance(out, str) else out


@app.route("/documents")
@login_required
def documents():
    return render_template("documents.html", documents=get_documents(100))


@app.route("/documents/<int:document_id>")
@login_required
def document_detail(document_id):
    document, related_items = get_document_payload(document_id)
    if not document:
        flash("Nie znaleziono dokumentu.", "error")
        return redirect(url_for("documents"))
    return render_template("document_detail.html", document=document, items=related_items, summary=summarize_line_items(related_items), settings=get_system_settings())


@app.route("/documents/<int:document_id>/pdf")
@login_required
def document_pdf(document_id):
    document, related_items = get_document_payload(document_id)
    if not document:
        flash("Nie znaleziono dokumentu.", "error")
        return redirect(url_for("documents"))
    settings = get_system_settings()
    pdf_output = build_document_pdf(document, related_items, settings)
    file_name = f"{clean_value(document.get('doc_number'), f'document_{document_id}').replace('/', '_')}.pdf"
    file_path = os.path.join(BACKUP_FOLDER, file_name)
    with open(file_path, "wb") as output_file:
        output_file.write(pdf_output)
    record_archive("document", trf("Dokument {doc_number}", "Document {doc_number}", doc_number=document['doc_number']), file_name, file_path)
    response = make_response(pdf_output)
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = f"attachment; filename={file_name}"
    return response


@app.route("/stocktakes", methods=["GET", "POST"])
@login_required
def stocktakes():
    if request.method == "POST":
        name = clean_value(request.form.get("name")) or f"Inwentaryzacja {today_iso()}"
        warehouse_id = int_safe(request.form.get("warehouse_id"), 0)
        if not warehouse_id:
            flash("Wybierz magazyn dla inwentaryzacji.", "error")
            return redirect(url_for("stocktakes"))

        with get_db() as connection:
            stocktake_id = connection.execute(
                """
                INSERT INTO stocktakes (name, warehouse_id, status, notes, created_at, user_id)
                VALUES (?, ?, 'open', ?, ?, ?)
                """,
                (name, warehouse_id, clean_value(request.form.get("notes")), now_iso(), session.get("user_id")),
            ).lastrowid

            batches = connection.execute(
                """
                SELECT id, quantity
                FROM stock_batches
                WHERE warehouse_id = ? AND is_active = 1
                ORDER BY id ASC
                """,
                (warehouse_id,),
            ).fetchall()
            for batch in batches:
                connection.execute(
                    """
                    INSERT INTO stocktake_items (stocktake_id, batch_id, expected_quantity, counted_quantity, difference, note, created_at, updated_at)
                    VALUES (?, ?, ?, NULL, NULL, '', ?, ?)
                    """,
                    (stocktake_id, batch["id"], batch["quantity"], now_iso(), now_iso()),
                )
            connection.commit()

        log_audit("create", "stocktake", stocktake_id, f"Rozpoczęto inwentaryzację {name}")
        flash("Utworzono nową inwentaryzację z natury.", "success")
        return redirect(url_for("stocktake_detail", stocktake_id=stocktake_id))

    return render_template("stocktakes.html", stocktakes=get_stocktakes(), warehouses=get_warehouses())


@app.route("/stocktakes/<int:stocktake_id>", methods=["GET", "POST"])
@login_required
def stocktake_detail(stocktake_id):
    stocktake = query_one(
        """
        SELECT s.*, w.name AS warehouse_name
        FROM stocktakes s
        JOIN warehouses w ON w.id = s.warehouse_id
        WHERE s.id = ?
        """,
        (stocktake_id,),
    )
    if not stocktake:
        flash("Nie znaleziono inwentaryzacji.", "error")
        return redirect(url_for("stocktakes"))

    if request.method == "POST":
        batch_id = int_safe(request.form.get("batch_id"), 0)
        counted_quantity = int_safe(request.form.get("counted_quantity"), 0)
        expected = query_one(
            "SELECT expected_quantity FROM stocktake_items WHERE stocktake_id = ? AND batch_id = ?",
            (stocktake_id, batch_id),
        )
        if expected:
            difference = counted_quantity - int_safe(expected["expected_quantity"], 0)
            execute_db(
                """
                UPDATE stocktake_items
                SET counted_quantity = ?, difference = ?, note = ?, updated_at = ?
                WHERE stocktake_id = ? AND batch_id = ?
                """,
                (counted_quantity, difference, clean_value(request.form.get("note")), now_iso(), stocktake_id, batch_id),
            )
            flash("Zapisano wynik liczenia pozycji.", "success")
        return redirect(url_for("stocktake_detail", stocktake_id=stocktake_id))

    items = query_all(
        """
        SELECT si.*, p.title, p.sku, b.lot_number, b.serial_number
        FROM stocktake_items si
        JOIN stock_batches b ON b.id = si.batch_id
        JOIN products p ON p.id = b.product_id
        WHERE si.stocktake_id = ?
        ORDER BY p.title COLLATE NOCASE ASC
        """,
        (stocktake_id,),
    )
    return render_template("stocktake_detail.html", stocktake=dict(stocktake), items=[dict(row) for row in items])




@app.route("/stocktakes/<int:stocktake_id>/mobile", methods=["GET", "POST"])
@login_required
def stocktake_mobile(stocktake_id):
    stocktake = query_one(
        """
        SELECT s.*, w.name AS warehouse_name
        FROM stocktakes s
        JOIN warehouses w ON w.id = s.warehouse_id
        WHERE s.id = ?
        """,
        (stocktake_id,),
    )
    if not stocktake:
        flash("Nie znaleziono inwentaryzacji.", "error")
        return redirect(url_for("stocktakes"))

    if request.method == "POST" and stocktake["status"] == "open":
        batch_id = int_safe(request.form.get("batch_id"), 0)
        counted_quantity = int_safe(request.form.get("counted_quantity"), 0)
        expected = query_one("SELECT expected_quantity FROM stocktake_items WHERE stocktake_id = ? AND batch_id = ?", (stocktake_id, batch_id))
        if expected:
            difference = counted_quantity - int_safe(expected["expected_quantity"], 0)
            execute_db("""
                UPDATE stocktake_items
                SET counted_quantity = ?, difference = ?, note = ?, updated_at = ?
                WHERE stocktake_id = ? AND batch_id = ?
                """, (counted_quantity, difference, clean_value(request.form.get("note")), now_iso(), stocktake_id, batch_id))
            flash("Zapisano pozycjÄ w mobilnym widoku spisu.", "success")
        return redirect(url_for("stocktake_mobile", stocktake_id=stocktake_id))

    items = query_all("""
        SELECT si.*, p.title, p.sku, b.lot_number, b.serial_number
        FROM stocktake_items si
        JOIN stock_batches b ON b.id = si.batch_id
        JOIN products p ON p.id = b.product_id
        WHERE si.stocktake_id = ?
        ORDER BY p.title COLLATE NOCASE ASC
        """, (stocktake_id,))
    return render_template("stocktake_mobile.html", stocktake=dict(stocktake), items=[dict(row) for row in items])


@app.route("/stocktakes/<int:stocktake_id>/finalize")
@login_required
def stocktake_finalize(stocktake_id):
    stocktake = query_one("SELECT * FROM stocktakes WHERE id = ? AND status = 'open'", (stocktake_id,))
    if not stocktake:
        flash("Nie można zamknąć tej inwentaryzacji.", "error")
        return redirect(url_for("stocktakes"))

    with get_db() as connection:
        items = connection.execute(
            """
            SELECT si.*, b.product_id, b.warehouse_id
            FROM stocktake_items si
            JOIN stock_batches b ON b.id = si.batch_id
            WHERE si.stocktake_id = ? AND si.counted_quantity IS NOT NULL
            """,
            (stocktake_id,),
        ).fetchall()
        for item in items:
            counted = int_safe(item["counted_quantity"], 0)
            connection.execute(
                "UPDATE stock_batches SET quantity = ?, updated_at = ? WHERE id = ?",
                (counted, now_iso(), item["batch_id"]),
            )
            difference = int_safe(item["difference"], 0)
            if difference != 0:
                create_stock_movement(
                    connection,
                    item["product_id"],
                    item["batch_id"],
                    item["warehouse_id"],
                    "stocktake_adjustment",
                    abs(difference),
                    "stocktake",
                    stocktake_id,
                    "Korekta po inwentaryzacji z natury",
                )
        connection.execute(
            "UPDATE stocktakes SET status = 'closed', closed_at = ? WHERE id = ?",
            (now_iso(), stocktake_id),
        )
        connection.commit()

    log_audit("update", "stocktake", stocktake_id, "Zamknięto i zastosowano wyniki inwentaryzacji")
    flash("Inwentaryzacja została zamknięta i zastosowana.", "success")
    return redirect(url_for("stocktakes"))


@app.route("/admin/users-settings", methods=["GET", "POST"])
@login_required
@role_required("admin")
def users_settings():
    action = clean_value(request.form.get("action"))
    if request.method == "POST":
        if action == "create_user":
            username = clean_value(request.form.get("username"))
            full_name = clean_value(request.form.get("full_name"))
            password = request.form.get("password") or ""
            role = clean_value(request.form.get("role"), "sales")
            if username and full_name and password:
                try:
                    user_id = execute_db(
                        """
                        INSERT INTO users (username, full_name, password_hash, role, active, created_at)
                        VALUES (?, ?, ?, ?, 1, ?)
                        """,
                        (username, full_name, generate_password_hash(password), role, now_iso()),
                    )
                    log_audit("create", "user", user_id, f"Dodano użytkownika {username}")
                    flash("Użytkownik został dodany.", "success")
                except sqlite3.IntegrityError:
                    flash("Login użytkownika już istnieje.", "error")
            else:
                flash("Wypełnij login, nazwę i hasło.", "error")
            return redirect(url_for("users_settings"))

        if action == "toggle_user":
            user_id = int_safe(request.form.get("user_id"), 0)
            user = query_one("SELECT active, username FROM users WHERE id = ?", (user_id,))
            if user:
                new_active = 0 if int_safe(user["active"], 0) == 1 else 1
                execute_db("UPDATE users SET active = ? WHERE id = ?", (new_active, user_id))
                log_audit("update", "user", user_id, f"Zmieniono aktywność użytkownika {user['username']}")
                flash("Zmieniono aktywność użytkownika.", "success")
            return redirect(url_for("users_settings"))

        if action == "reset_password":
            user_id = int_safe(request.form.get("user_id"), 0)
            new_password = request.form.get("new_password") or ""
            if user_id and new_password:
                execute_db("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(new_password), user_id))
                log_audit("update", "user", user_id, "Zresetowano hasło użytkownika")
                flash("Hasło użytkownika zostało zresetowane.", "success")
            return redirect(url_for("users_settings"))

        if action == "save_settings":
            for key in [
                "company_name",
                "company_tax_id",
                "company_address",
                "default_warehouse_name",
                "currency",
                "invoice_prefix",
                "goods_issue_prefix",
                "goods_receipt_prefix",
                "stock_alert_days",
            ]:
                set_setting(key, request.form.get(key, ""))
            log_audit("update", "settings", "-", "Zapisano ustawienia systemowe")
            flash("Ustawienia systemowe zostały zapisane.", "success")
            return redirect(url_for("users_settings"))

    return render_template(
        "users_settings.html",
        users=get_users(),
        settings=get_system_settings(),
    )


@app.route("/movements", methods=["GET", "POST"])
@login_required
def movements():
    if request.method == "POST":
        batch_id = int_safe(request.form.get("batch_id"), 0)
        movement_type = clean_value(request.form.get("movement_type"))
        quantity = int_safe(request.form.get("quantity"), 0)
        notes = clean_value(request.form.get("notes"))
        target_warehouse_id = int_safe(request.form.get("target_warehouse_id"), 0)

        with get_db() as connection:
            batch = connection.execute(
                """
                SELECT b.*, p.id AS product_id, p.title
                FROM stock_batches b
                JOIN products p ON p.id = b.product_id
                WHERE b.id = ? AND b.is_active = 1
                """,
                (batch_id,),
            ).fetchone()
            if not batch or quantity <= 0:
                flash("Wybierz partiÄ i iloÅÄ wiÄkszÄ od zera.", "error")
                return redirect(url_for("movements"))

            current_quantity = int_safe(batch["quantity"], 0)
            if movement_type in {"adjustment_out", "transfer"} and quantity > current_quantity:
                flash("Brak wystarczajÄcej iloÅci do wykonania ruchu.", "error")
                return redirect(url_for("movements"))

            if movement_type == "adjustment_in":
                connection.execute("UPDATE stock_batches SET quantity = quantity + ?, updated_at = ? WHERE id = ?", (quantity, now_iso(), batch_id))
                create_stock_movement(connection, batch["product_id"], batch_id, batch["warehouse_id"], movement_type, quantity, "manual", batch_id, notes or "RÄczne zwiÄkszenie stanu")
            elif movement_type == "adjustment_out":
                connection.execute("UPDATE stock_batches SET quantity = quantity - ?, updated_at = ? WHERE id = ?", (quantity, now_iso(), batch_id))
                create_stock_movement(connection, batch["product_id"], batch_id, batch["warehouse_id"], movement_type, quantity, "manual", batch_id, notes or "RÄczne zmniejszenie stanu")
            elif movement_type == "transfer" and target_warehouse_id:
                connection.execute("UPDATE stock_batches SET quantity = quantity - ?, updated_at = ? WHERE id = ?", (quantity, now_iso(), batch_id))
                new_batch_id = connection.execute(
                    """
                    INSERT INTO stock_batches (product_id, warehouse_id, quantity, expiry_date, purchase_date, lot_number, serial_number, document, image, is_active, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (batch["product_id"], target_warehouse_id, quantity, batch["expiry_date"], batch["purchase_date"], batch["lot_number"], batch["serial_number"], batch["document"], batch["image"], now_iso(), now_iso()),
                ).lastrowid
                create_stock_movement(connection, batch["product_id"], batch_id, batch["warehouse_id"], "transfer_out", quantity, "transfer", new_batch_id, notes or "PrzesuniÄcie z magazynu")
                create_stock_movement(connection, batch["product_id"], new_batch_id, target_warehouse_id, "transfer_in", quantity, "transfer", batch_id, notes or "PrzesuniÄcie do magazynu")
            else:
                flash("NieprawidÅowy typ ruchu magazynowego.", "error")
                return redirect(url_for("movements"))

            connection.commit()

        log_audit("create", "movement", batch_id, f"Zapisano ruch magazynowy typu {movement_type}", after_state=format_state_pairs({"movement_type": movement_type, "quantity": quantity, "target_warehouse_id": target_warehouse_id or "-"}))
        flash("Ruch magazynowy zostaÅ zapisany.", "success")
        return redirect(url_for("movements"))

    return render_template("movements.html", inventory_items=get_inventory_items(), warehouses=get_warehouses(), movements=get_recent_movements(30))


@app.route("/movements/<int:movement_id>/reverse", methods=["POST"])
@login_required
@role_required("admin", "manager")
def movement_reverse(movement_id):
    reason = clean_value(request.form.get("reason"))
    try:
        reverse_manual_movement(movement_id, reason)
        flash("Ruch magazynowy zostaÅ cofniÄty.", "success")
    except ValueError as error:
        flash(str(error), "error")
    return redirect(url_for("movements"))




@app.route("/alerts")
@login_required
def alerts():
    return render_template("alerts.html", alert_data=get_alert_center())


@app.route("/calendar")
@login_required
def calendar_view():
    return render_template("calendar.html", entries=get_calendar_entries())


@app.route("/prints")
@login_required
def prints():
    return render_template("prints.html", inventory_items=get_inventory_items())


@app.route("/prints/labels", methods=["POST"])
@login_required
def print_labels():
    selected_ids = request.form.getlist("batch_ids")
    if not selected_ids:
        flash("Wybierz co najmniej jedną pozycję do wydruku.", "error")
        return redirect(url_for("prints"))

    selected = [item for item in get_inventory_items() if str(item["batch_id"]) in selected_ids]
    record_archive("print", "Etykiety produktów", f"etykiety_{today_iso()}.html", "")
    return render_template("print_labels.html", items=selected)


@app.route("/notes", methods=["GET", "POST"])
@login_required
def notes():
    if request.method == "POST":
        title = clean_value(request.form.get("title"))
        content = clean_value(request.form.get("content"))
        entity_type = clean_value(request.form.get("entity_type"), "product")
        entity_id = clean_value(request.form.get("entity_id"), "0")
        if title and content:
            note_id = execute_db(
                """
                INSERT INTO entity_notes (entity_type, entity_id, title, content, created_at, user_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (entity_type, entity_id, title, content, now_iso(), session.get("user_id")),
            )
            log_audit("create", "note", note_id, f"Dodano notatkę: {title}")
            flash("Notatka została dodana.", "success")
        else:
            flash("Tytuł i treść notatki są wymagane.", "error")
        return redirect(url_for("notes"))

    return render_template("notes.html", notes=get_notes_data(), products=get_products(), inventory_items=get_inventory_items())


@app.route("/attachments", methods=["GET", "POST"])
@login_required
def attachments():
    if request.method == "POST":
        uploaded = save_uploaded_file(request.files.get("file"), FILE_EXTENSIONS)
        if not uploaded:
            flash("Wybierz poprawny plik do załączenia.", "error")
            return redirect(url_for("attachments"))

        attachment_id = execute_db(
            """
            INSERT INTO entity_files (entity_type, entity_id, label, file_name, created_at, user_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                clean_value(request.form.get("entity_type"), "product"),
                clean_value(request.form.get("entity_id"), "0"),
                clean_value(request.form.get("label"), "Załącznik"),
                uploaded,
                now_iso(),
                session.get("user_id"),
            ),
        )
        log_audit("create", "attachment", attachment_id, f"Dodano załącznik {uploaded}")
        flash("Załącznik został zapisany.", "success")
        return redirect(url_for("attachments"))

    return render_template("attachments.html", attachments=get_attachments_data(), products=get_products(), inventory_items=get_inventory_items())


@app.route("/data-quality")
@login_required
def data_quality():
    return render_template("data_quality.html", quality=get_data_quality_report())


@app.route("/tasks", methods=["GET", "POST"])
@login_required
def tasks_view():
    if request.method == "POST":
        title = clean_value(request.form.get("title"))
        if not title:
            flash("Tytuł zadania jest wymagany.", "error")
            return redirect(url_for("tasks_view"))

        task_id = execute_db(
            """
            INSERT INTO tasks (title, description, status, priority, due_date, assigned_user_id, entity_type, entity_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title,
                clean_value(request.form.get("description")),
                clean_value(request.form.get("status"), "open"),
                clean_value(request.form.get("priority"), "medium"),
                clean_value(request.form.get("due_date")),
                int_safe(request.form.get("assigned_user_id"), 0) or None,
                clean_value(request.form.get("entity_type")),
                clean_value(request.form.get("entity_id")),
                now_iso(),
            ),
        )
        log_audit("create", "task", task_id, f"Dodano zadanie {title}")
        flash("Zadanie zostało zapisane.", "success")
        return redirect(url_for("tasks_view"))

    return render_template("tasks.html", tasks=get_tasks_data(), users=query_all("SELECT * FROM users WHERE active = 1 ORDER BY full_name ASC"))


@app.route("/tasks/<int:task_id>/done")
@login_required
def task_done(task_id):
    execute_db("UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,))
    log_audit("update", "task", task_id, "Oznaczono zadanie jako wykonane")
    flash("Zadanie oznaczono jako wykonane.", "success")
    return redirect(url_for("tasks_view"))


@app.route("/archives")
@login_required
def archives():
    return render_template("archives.html", archives=get_archives_data())


@app.route("/archives/download/<int:archive_id>")
@login_required
def archive_download(archive_id):
    archive = query_one("SELECT * FROM report_archive WHERE id = ?", (archive_id,))
    if archive and archive["file_path"] and os.path.exists(archive["file_path"]):
        return send_file(archive["file_path"], as_attachment=True, download_name=archive["file_name"])
    flash("Nie udało się odnaleźć pliku w archiwum.", "error")
    return redirect(url_for("archives"))


@app.route("/owner")
@login_required
@role_required("admin", "manager")
def owner_dashboard():
    return render_template("owner.html", metrics=get_owner_dashboard_metrics())


@app.route("/mobile")
@login_required
def mobile_view():
    return render_template("mobile.html", items=get_inventory_items())


@app.route("/dictionaries", methods=["GET", "POST"])
@login_required
def dictionaries():
    if request.method == "POST":
        dict_type = clean_value(request.form.get("dict_type"))
        value = clean_value(request.form.get("value"))
        if dict_type and value:
            try:
                execute_db(
                    "INSERT INTO dictionary_values (dict_type, value, created_at) VALUES (?, ?, ?)",
                    (dict_type, value, now_iso()),
                )
                flash("Wartość słownika została dodana.", "success")
            except sqlite3.IntegrityError:
                flash("Ta wartość już istnieje w słowniku.", "error")
        else:
            flash("Wybierz typ słownika i wpisz wartość.", "error")
        return redirect(url_for("dictionaries"))

    return render_template("dictionaries.html", dictionaries=get_dictionary_values())


@app.route("/user-activity")
@login_required
@role_required("admin", "manager")
def user_activity():
    return render_template("user_activity.html", activity=get_user_activity(), recent_audit=get_recent_audit(30))


@app.route("/reservations", methods=["GET", "POST"])
@login_required
def reservations_view():
    if request.method == "POST":
        batch_id = int_safe(request.form.get("batch_id"), 0)
        quantity = int_safe(request.form.get("quantity"), 0)
        customer_id = int_safe(request.form.get("customer_id"), 0) or None
        item = next((entry for entry in get_inventory_items() if entry["batch_id"] == batch_id), None)
        if batch_id and quantity > 0 and item and quantity <= item["available_quantity_int"]:
            reservation_id = execute_db(
                """
                INSERT INTO reservations (batch_id, customer_id, quantity, status, notes, created_at, user_id)
                VALUES (?, ?, ?, 'reserved', ?, ?, ?)
                """,
                (
                    batch_id,
                    customer_id,
                    quantity,
                    clean_value(request.form.get("notes")),
                    now_iso(),
                    session.get("user_id"),
                ),
            )
            log_audit("create", "reservation", reservation_id, "Dodano rezerwację towaru")
            flash("Rezerwacja została zapisana.", "success")
        else:
            flash("Wybierz partię i ilość większą od zera, mieszczącą się w stanie dostępnym.", "error")
        return redirect(url_for("reservations_view"))

    return render_template(
        "reservations.html",
        reservations=get_reservations_data(),
        inventory_items=get_inventory_items(),
        customers=get_customers(),
    )


@app.route("/reservations/<int:reservation_id>/release")
@login_required
def reservation_release(reservation_id):
    execute_db("UPDATE reservations SET status = 'released' WHERE id = ?", (reservation_id,))
    log_audit("update", "reservation", reservation_id, "Zwolniono rezerwację")
    flash("Rezerwacja została zwolniona.", "success")
    return redirect(url_for("reservations_view"))


@app.route("/demo")
@login_required
def demo():
    return render_template("demo.html", demo=get_demo_snapshot())


@app.route("/demo/toggle")
@login_required
def demo_toggle():
    session["demo_mode"] = not session.get("demo_mode", False)
    flash("Przełączono tryb demo.", "success")
    return redirect(url_for("demo"))


@app.route("/manager-exports")
@login_required
@role_required("admin", "manager")
def manager_exports():
    return render_template("manager_exports.html")


@app.route("/manager-exports/<string:kind>")
@login_required
@role_required("admin", "manager")
def manager_export_file(kind):
    output = io.StringIO()
    writer = csv.writer(output)
    filename = f"manager_{kind}_{today_iso()}.csv"

    if kind == "daily-sales":
        writer.writerow(["id", tr("klient", "customer"), tr("platnosc", "payment"), tr("status", "status"), tr("kwota", "amount"), tr("utworzono", "created_at")])
        for sale in get_recent_sales(100):
            writer.writerow([sale["id"], sale["customer_name"], sale["payment_method"], sale["status"], sale["total_amount"], sale["created_at"]])
    elif kind == "top-products":
        writer.writerow([tr("produkt", "title"), tr("sprzedana_ilosc", "sold_quantity")])
        for row in query_all(
            """
            SELECT p.title, COALESCE(SUM(si.quantity), 0) AS sold_quantity
            FROM sale_items si
            JOIN products p ON p.id = si.product_id
            GROUP BY p.id
            ORDER BY sold_quantity DESC, p.title ASC
            LIMIT 100
            """
        ):
            writer.writerow([row["title"], row["sold_quantity"]])
    elif kind == "dead-stock":
        writer.writerow([tr("produkt", "title"), "sku", tr("kategoria", "category")])
        for row in query_all(
            """
            SELECT p.title, p.sku, p.category
            FROM products p
            LEFT JOIN sale_items si ON si.product_id = p.id
            WHERE si.id IS NULL
            ORDER BY p.title COLLATE NOCASE ASC
            """
        ):
            writer.writerow([row["title"], row["sku"], row["category"]])
    elif kind == "shortages":
        writer.writerow([tr("produkt", "title"), tr("magazyn", "warehouse"), tr("ilosc", "quantity"), tr("minimum", "minimum")])
        for item in get_inventory_items():
            if item["is_low"]:
                writer.writerow([item["title"], item["warehouse_name"], item["quantity_int"], item["min_quantity_int"]])
    elif kind == "inventory-value":
        writer.writerow([tr("produkt", "title"), tr("magazyn", "warehouse"), tr("ilosc", "quantity"), tr("cena_zakupu", "purchase_price"), tr("wartosc", "value")])
        for item in get_inventory_items():
            writer.writerow([item["title"], item["warehouse_name"], item["quantity_int"], item["purchase_price_float"], item["total_value"]])
    else:
        flash(tr("Nieznany typ eksportu.", "Unknown export type."), "error")
        return redirect(url_for("manager_exports"))

    data = output.getvalue()
    export_path = os.path.join(BACKUP_FOLDER, filename)
    with open(export_path, "w", encoding="utf-8", newline="") as export_file:
        export_file.write(data)
    record_archive("manager-export", trf("Eksport menedzerski: {kind}", "Manager export: {kind}", kind=kind), filename, export_path)

    response = make_response(data)
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return response


@app.route("/inventory/export/csv")
@login_required
def export_inventory_csv():
    items = get_inventory_items()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "title",
            "category",
            "sku",
            "ean",
            "brand",
            "warehouse",
            "quantity",
            "min_quantity",
            "purchase_price",
            "sale_price",
            "expiry_date",
            "purchase_date",
            "lot_number",
            "serial_number",
        ]
    )
    for item in items:
        writer.writerow(
            [
                item["title"],
                item["category"],
                item["sku"],
                item["ean"],
                item["brand"],
                item["warehouse_name"],
                item["quantity_int"],
                item["min_quantity_int"],
                item["purchase_price"],
                item["sale_price"],
                item["expiry_date"],
                item["purchase_date"],
                item["lot_number"],
                item["serial_number"],
            ]
        )

    response = make_response(output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = "attachment; filename=sid_inventory.csv"
    return response


@app.route("/inventory/import", methods=["POST"])
@login_required
def import_inventory_csv():
    file_storage = request.files.get("import_file")
    if not file_storage or not file_storage.filename:
        flash("Wybierz plik CSV do importu.", "error")
        return redirect(url_for("inventory"))

    raw_content = file_storage.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(raw_content))
    imported = 0

    with get_db() as connection:
        for row in reader:
            title = clean_value(row.get("title") or row.get("nazwa"))
            if not title:
                continue
            product_id = get_or_create_product(
                connection,
                title,
                row.get("category") or row.get("kategoria") or "Inne",
                ean=row.get("ean"),
                sku=row.get("sku"),
                brand=row.get("brand") or row.get("marka"),
                purchase_price=row.get("purchase_price") or row.get("cena_zakupu"),
                sale_price=row.get("sale_price") or row.get("cena_sprzedazy"),
                min_quantity=row.get("min_quantity") or row.get("min"),
            )
            warehouse_id = get_or_create_warehouse(connection, row.get("warehouse") or row.get("location") or "Magazyn główny")
            batch_id = connection.execute(
                """
                INSERT INTO stock_batches (product_id, warehouse_id, quantity, expiry_date, purchase_date, lot_number, serial_number, document, image, is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, '', '', 1, ?, ?)
                """,
                (
                    product_id,
                    warehouse_id,
                    int_safe(row.get("quantity"), 0),
                    clean_value(row.get("expiry_date")),
                    clean_value(row.get("purchase_date")),
                    clean_value(row.get("lot_number")),
                    clean_value(row.get("serial_number")),
                    now_iso(),
                    now_iso(),
                ),
            ).lastrowid
            create_stock_movement(connection, product_id, batch_id, warehouse_id, "import", int_safe(row.get("quantity"), 0), "csv_import", batch_id, "Import CSV")
            imported += 1
        connection.commit()

    log_audit("import", "inventory", "-", f"Zaimportowano {imported} pozycji z pliku CSV")
    flash(f"Import zakończony. Dodano {imported} pozycji.", "success")
    return redirect(url_for("inventory"))


@app.route("/inventory/report")
@login_required
def inventory_report():
    items = get_inventory_items()
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", "B", 16)
    pdf.cell(190, 10, txt=clean_pdf_text(tr("RAPORT SID 4.0 - STAN MAGAZYNU", "SID 4.0 REPORT - INVENTORY STATUS")), ln=True, align="C")
    pdf.set_font("Arial", size=10)
    pdf.cell(190, 10, txt=clean_pdf_text(trf("Wygenerowano: {date}", "Generated: {date}", date=today_iso())), ln=True, align="C")
    pdf.ln(8)

    pdf.set_font("Arial", "B", 8)
    pdf.set_fill_color(230, 230, 230)
    headers = [
        (tr("Produkt", "Product"), 45),
        (tr("Magazyn", "Warehouse"), 32),
        (tr("Ilosc", "Qty"), 15),
        (tr("Zakup", "Purchase"), 20),
        (tr("Sprzedaz", "Sale"), 22),
        (tr("Wartosc", "Value"), 24),
        (tr("Waznosc", "Expiry"), 28),
    ]
    for label, width in headers:
        pdf.cell(width, 9, clean_pdf_text(label), 1, 0, "C", True)
    pdf.ln()

    pdf.set_font("Arial", size=8)
    total_value = 0
    for item in items:
        total_value += item["total_value"]
        pdf.cell(45, 8, clean_pdf_text(item["title"])[:24], 1)
        pdf.cell(32, 8, clean_pdf_text(item["warehouse_name"])[:18], 1)
        pdf.cell(15, 8, str(item["quantity_int"]), 1, 0, "C")
        pdf.cell(20, 8, f"{item['purchase_price_float']:.2f}", 1, 0, "C")
        pdf.cell(22, 8, f"{item['sale_price_float']:.2f}", 1, 0, "C")
        pdf.cell(24, 8, f"{item['total_value']:.2f}", 1, 0, "C")
        pdf.cell(28, 8, clean_pdf_text(item["expiry_date"] or "-"), 1, 0, "C")
        pdf.ln()

    pdf.ln(4)
    pdf.set_font("Arial", "B", 10)
    pdf.cell(160, 10, clean_pdf_text(tr("Laczna wartosc magazynu:", "Total inventory value:")), 1, 0, "R", True)
    pdf.cell(30, 10, clean_pdf_text(f"{total_value:.2f} zl"), 1, 0, "C", True)

    pdf_output = pdf.output(dest="S")
    if isinstance(pdf_output, str):
        pdf_output = pdf_output.encode("latin-1")

    report_name = f"sid_inventory_report_{today_iso()}.pdf" if get_current_language() == "en" else f"sid_raport_magazynu_{today_iso()}.pdf"
    report_path = os.path.join(BACKUP_FOLDER, report_name)
    with open(report_path, "wb") as report_file:
        report_file.write(pdf_output)

    response = make_response(pdf_output)
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = f"attachment; filename={report_name}"
    record_archive("report", tr("Raport magazynu", "Inventory report"), report_name, report_path)
    return response


@app.route("/backup")
@login_required
@role_required("admin", "manager")
def backup():
    filename = f"sid_backup_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    memory_file = io.BytesIO()

    with zipfile.ZipFile(memory_file, "w", zipfile.ZIP_DEFLATED) as zip_file:
        if os.path.exists(DATABASE):
            zip_file.write(DATABASE, arcname="sid.db")
        if os.path.exists(CSV_FILE):
            zip_file.write(CSV_FILE, arcname="inventory.csv")

        for file_name in os.listdir(UPLOAD_FOLDER):
            full_path = os.path.join(UPLOAD_FOLDER, file_name)
            if os.path.isfile(full_path):
                zip_file.write(full_path, arcname=os.path.join("uploads", file_name))

    memory_file.seek(0)
    with open(os.path.join(BACKUP_FOLDER, filename), "wb") as file:
        file.write(memory_file.getvalue())

    log_audit("backup", "system", filename, f"Utworzono kopię bezpieczeństwa {filename}")
    record_archive("backup", "Backup systemu", filename, os.path.join(BACKUP_FOLDER, filename))
    memory_file.seek(0)
    return send_file(memory_file, as_attachment=True, download_name=filename, mimetype="application/zip")


init_db()


if __name__ == "__main__":
    app.run(debug=True, port=5000)
