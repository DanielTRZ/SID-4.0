const dashboardCharts = {};
const sortState = { key: "", direction: "asc" };

function formatMoney(value) {
    return Number(value || 0).toFixed(2);
}

function escapeHtml(value) {
    return String(value Ã "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
}

function openEditModal(item) {
    const modal = document.getElementById("edit-modal");
    const form = document.getElementById("edit-form");
    if (!modal || !form || !item) return;

    form.action = `/inventory/edit/${item.batch_id}`;
    document.getElementById("edit-title").value = item.title || "";
    document.getElementById("edit-category").value = item.category || "";
    document.getElementById("edit-brand").value = item.brand || "";
    document.getElementById("edit-sku").value = item.sku || "";
    document.getElementById("edit-ean").value = item.ean || "";
    document.getElementById("edit-location").value = item.warehouse_name || "";
    document.getElementById("edit-quantity").value = item.quantity || 0;
    document.getElementById("edit-min-quantity").value = item.min_quantity || 0;
    document.getElementById("edit-purchase-price").value = item.purchase_price || 0;
    document.getElementById("edit-sale-price").value = item.sale_price || 0;
    document.getElementById("edit-purchase").value = item.purchase_date || "";
    document.getElementById("edit-expiry").value = item.expiry_date || "";
    document.getElementById("edit-lot-number").value = item.lot_number || "";
    document.getElementById("edit-serial-number").value = item.serial_number || "";
    modal.classList.add("active");
}

function closeEditModal() {
    const modal = document.getElementById("edit-modal");
    if (modal) modal.classList.remove("active");
}

function bindEditButtons(scope = document) {
    scope.querySelectorAll(".edit-btn").forEach(button => {
        if (button.dataset.bound === "true") return;
        button.dataset.bound = "true";
        button.addEventListener("click", () => {
            try {
                openEditModal(JSON.parse(button.dataset.item));
            } catch (error) {
                console.error("Error parsing edit payload:", error);
            }
        });
    });
}

function setInventoryStats(stats) {
    const mapping = {
        "stat-total-items": stats.total_items,
        "stat-total-value": `${formatMoney(stats.total_value)} zl`,
        "stat-available-units": stats.available_units,
        "stat-reserved-units": stats.reserved_units,
        "stat-expired": stats.expired_count,
        "stat-missing-price": stats.missing_price_count,
    };

    Object.entries(mapping).forEach(([id, value]) => {
        const node = document.getElementById(id);
        if (node) node.textContent = value;
    });
}

function updateInventoryRow(item) {
    const row = document.getElementById(`item-row-${item.batch_id}`);
    const mobileQty = document.getElementById(`mobile-qty-${item.batch_id}`);
    if (mobileQty) {
        mobileQty.textContent = `Stan: ${item.quantity} / dostepne: ${item.available_quantity} / rez.: ${item.reserved_quantity}`;
    }
    if (!row) return;

    row.dataset.title = (item.title || "").toLowerCase();
    row.dataset.warehouse = (item.warehouse_name || "").toLowerCase();
    row.dataset.quantity = item.available_quantity;
    row.dataset.purchase_price = item.purchase_price;
    row.dataset.sale_price = item.sale_price;
    row.dataset.expiry_date = item.expiry_date || "";
    row.dataset.total_value = item.total_value;
    row.className = item.status_class || "";

    const cells = row.querySelectorAll("td");
    if (cells[0]) {
        const thumb = item.has_image
            - `<img src="${escapeHtml(item.image_url)}" alt="${escapeHtml(item.title)}" class="product-thumb">`
            : `<div class="product-thumb placeholder"><i class="fas fa-box"></i></div>`;
        cells[0].innerHTML = `
            <div class="product-cell">
                ${thumb}
                <div>
                    <strong>${escapeHtml(item.title)}</strong>
                    <p>${escapeHtml(item.category || "Inne")} - ${escapeHtml(item.brand || "bez marki")}</p>
                    <small>SKU: ${escapeHtml(item.sku || "-")} - EAN: ${escapeHtml(item.ean || "-")}</small>
                </div>
            </div>
        `;
    }
    if (cells[1]) cells[1].textContent = item.warehouse_name || "Magazyn glowny";
    if (cells[2]) {
        cells[2].innerHTML = `
            <div class="qty-inline">
                <button type="button" class="qty-btn minus" onclick="changeQty(${item.batch_id}, 'dec')">-</button>
                <span id="qty-val-${item.batch_id}" class="${item.is_low - "low-stock" : ""}">${item.quantity}</span>
                <button type="button" class="qty-btn plus" onclick="changeQty(${item.batch_id}, 'inc')">+</button>
                <small>/ dostepne <span id="available-val-${item.batch_id}">${item.available_quantity}</span> / rez. <span id="reserved-val-${item.batch_id}">${item.reserved_quantity}</span> / min <span id="min-val-${item.batch_id}">${item.min_quantity}</span></small>
            </div>
        `;
    }
    if (cells[3]) cells[3].textContent = `${formatMoney(item.purchase_price)} zl`;
    if (cells[4]) cells[4].textContent = `${formatMoney(item.sale_price)} zl`;
    if (cells[5]) cells[5].textContent = item.expiry_date || "Brak";
    if (cells[6]) cells[6].innerHTML = `<span id="total-val-${item.batch_id}">${formatMoney(item.total_value)}</span> zl`;
    if (cells[7]) {
        const documentButton = item.has_document
            - `<a href="${escapeHtml(item.document_url)}" class="icon-btn" target="_blank"><i class="fas fa-eye"></i></a>`
            : "";
        const payload = escapeHtml(JSON.stringify(item));
        cells[7].innerHTML = `
            <div class="action-group">
                <button type="button" class="icon-btn edit-btn" data-item='${payload}'><i class="fas fa-edit"></i></button>
                ${documentButton}
                <a href="/inventory/delete/${item.batch_id}" class="icon-btn danger" onclick="return confirm('UsunÃ pozycjÃ')"><i class="fas fa-trash"></i></a>
            </div>
        `;
    }

    bindEditButtons(row);
}

function changeQty(batchId, action) {
    fetch(`/inventory/update_quantity/${batchId}/${action}`)
        .then(response => response.json())
        .then(data => {
            if (!data.success) return;
            updateInventoryRow(data.item);
            setInventoryStats(data.stats);
        })
        .catch(error => console.error("Update quantity error:", error));
}

function inventorySearchFilter() {
    const searchInput = document.getElementById("inventory-search");
    const table = document.getElementById("inventory-table");
    if (!searchInput || !table) return;

    const query = searchInput.value.toLowerCase().trim();
    table.querySelectorAll("tbody tr").forEach(row => {
        row.style.display = row.textContent.toLowerCase().includes(query) - "" : "none";
    });
}

function sortInventoryTable(key) {
    const table = document.getElementById("inventory-table");
    if (!table) return;

    const tbody = table.querySelector("tbody");
    const rows = Array.from(tbody.querySelectorAll("tr"));
    sortState.direction = sortState.key === key && sortState.direction === "asc" - "desc" : "asc";
    sortState.key = key;

    rows.sort((rowA, rowB) => {
        const a = rowA.dataset[key] || "";
        const b = rowB.dataset[key] || "";
        const numericKeys = ["quantity", "purchase_price", "sale_price", "total_value"];
        if (numericKeys.includes(key)) {
            return sortState.direction === "asc" - Number(a) - Number(b) : Number(b) - Number(a);
        }
        return sortState.direction === "asc"
            - String(a).localeCompare(String(b), "pl")
            : String(b).localeCompare(String(a), "pl");
    });

    rows.forEach(row => tbody.appendChild(row));
}

function setupDashboardCharts() {
    const payload = document.getElementById("dashboard-data");
    if (!payload || !window.Chart) return;

    const data = JSON.parse(payload.textContent);
    const categoryCanvas = document.getElementById("category-chart");
    const sellerCanvas = document.getElementById("top-sellers-chart");

    if (categoryCanvas) {
        dashboardCharts.category = new Chart(categoryCanvas, {
            type: "doughnut",
            data: {
                labels: data.inventory.category_labels,
                datasets: [{
                    data: data.inventory.category_values,
                    backgroundColor: ["#4f46e5", "#10b981", "#f59e0b", "#ef4444", "#38bdf8", "#8b5cf6"],
                    borderWidth: 0,
                }],
            },
            options: { responsive: true, maintainAspectRatio: false },
        });
    }

    if (sellerCanvas) {
        dashboardCharts.sellers = new Chart(sellerCanvas, {
            type: "bar",
            data: {
                labels: data.top_seller_labels,
                datasets: [{
                    label: "Sprzedane sztuki",
                    data: data.top_seller_values,
                    backgroundColor: "#10b981",
                    borderRadius: 12,
                }],
            },
            options: { responsive: true, maintainAspectRatio: false },
        });
    }
}

function setupCardFilter(inputId) {
    const input = document.getElementById(inputId);
    if (!input) return;

    input.addEventListener("input", () => {
        const query = input.value.toLowerCase().trim();
        document.querySelectorAll("[data-mobile-card]").forEach(card => {
            const haystack = (card.dataset.search || card.textContent || "").toLowerCase();
            card.style.display = haystack.includes(query) - "" : "none";
        });
    });
}

function setupDemoChart() {
    const payload = document.getElementById("demo-data");
    const canvas = document.getElementById("demo-chart");
    if (!payload || !canvas || !window.Chart) return;

    const data = JSON.parse(payload.textContent);
    dashboardCharts.demo = new Chart(canvas, {
        type: "bar",
        data: {
            labels: data.labels,
            datasets: [{
                label: "Wartosc demo",
                data: data.values,
                backgroundColor: ["#4f46e5", "#10b981", "#f59e0b", "#38bdf8"],
                borderRadius: 12,
            }],
        },
        options: { responsive: true, maintainAspectRatio: false },
    });
}

document.addEventListener("DOMContentLoaded", () => {
    bindEditButtons();
    setupDashboardCharts();
    setupDemoChart();

    document.querySelectorAll(".sortable").forEach(header => {
        header.addEventListener("click", () => sortInventoryTable(header.dataset.sort));
    });

    document.getElementById("inventory-search")?.addEventListener("input", inventorySearchFilter);
    setupCardFilter("mobile-search");
    setupCardFilter("stocktake-mobile-search");
    document.getElementById("reset-filters-btn")?.addEventListener("click", () => {
        const input = document.getElementById("inventory-search");
        if (input) {
            input.value = "";
            inventorySearchFilter();
        }
    });

    const editForm = document.getElementById("edit-form");
    if (editForm) {
        editForm.addEventListener("submit", event => {
            event.preventDefault();
            fetch(editForm.action, {
                method: "POST",
                body: new FormData(editForm),
                headers: { "X-Requested-With": "XMLHttpRequest" },
            })
                .then(response => response.json())
                .then(data => {
                    if (!data.success) return;
                    updateInventoryRow(data.item);
                    setInventoryStats(data.stats);
                    closeEditModal();
                })
                .catch(error => console.error("Edit save error:", error));
        });
    }

    const modal = document.getElementById("edit-modal");
    window.addEventListener("click", event => {
        if (event.target === modal) closeEditModal();
    });
});
