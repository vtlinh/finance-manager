"""Tests for the /api/transactions/upload endpoint."""
import io
import pytest
from server import app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test-secret"
    with app.test_client() as c:
        yield c


def _upload(client, csv_content: str):
    data = {"file": (io.BytesIO(csv_content.encode()), "transactions.csv")}
    return client.post(
        "/api/transactions/upload",
        data=data,
        content_type="multipart/form-data",
    )


def test_upload_valid_csv(client):
    csv = "Date,Merchant,Amount,Category\n2024-01-05,Starbucks,-5.50,Food & Drink\n"
    resp = _upload(client, csv)
    assert resp.get_json()["status"] == "ok"


def test_upload_missing_required_columns(client):
    # Only Date and Merchant — missing Amount and Category
    csv = "Date,Merchant\n2024-01-05,Starbucks\n"
    resp = _upload(client, csv)
    data = resp.get_json()
    assert data["status"] == "error"
    assert "Missing columns" in data["message"]
    assert "Amount" in data["message"] or "Category" in data["message"]


def test_upload_extra_columns_accepted(client):
    # Full Monarch export has extra columns — should still be accepted
    csv = (
        "Date,Merchant,Amount,Category,Account,Original Statement,Notes,Tags,Owner\n"
        "2024-01-05,Starbucks,-5.50,Food & Drink,Chase,STARBUCKS,,, \n"
    )
    resp = _upload(client, csv)
    assert resp.get_json()["status"] == "ok"


def test_upload_empty_csv(client):
    csv = "Date,Merchant,Amount,Category\n"
    resp = _upload(client, csv)
    data = resp.get_json()
    assert data["status"] == "error"
    assert "no transactions" in data["message"].lower()


def test_upload_no_file(client):
    resp = client.post("/api/transactions/upload", data={}, content_type="multipart/form-data")
    data = resp.get_json()
    assert data["status"] == "error"
    assert "No file" in data["message"]
