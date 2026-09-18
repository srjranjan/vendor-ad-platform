import os
import threading
import uuid
import pytest
from fastapi.testclient import TestClient

# Ensure test DB or clean environment
from main import app
from database import SessionLocal, Base, engine
from wallet import Wallet, Transaction

client = TestClient(app)


def make_vendor() -> str:
    """Register a real vendor and return its id.

    wallets.user_id is a foreign key to vendors.id, so tests can no longer
    invent an id: the wallet would be rejected by the database.
    """
    mobile = f"+9198{uuid.uuid4().int % 100000000:08d}"
    client.post("/api/v1/vendors/send-otp", json={"mobile_number": mobile})
    client.post("/api/v1/vendors/verify-otp", json={"mobile_number": mobile, "otp": "1234"})
    res = client.post(
        "/api/v1/vendors/register",
        json={"business_name": "Test Store", "mobile_number": mobile},
    )
    assert res.status_code == 201, res.text
    return res.json()["id"]



@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.create_all(bind=engine)
    yield


def test_auth_required():
    """Endpoints must reject requests without valid authentication."""
    res = client.get("/api/wallet")
    assert res.status_code == 401
    data = res.json()
    assert data["success"] is False
    assert data["error"]["code"] == "UNAUTHORIZED"

    res = client.post("/api/wallet/credit", json={"amount": 1000})
    assert res.status_code == 401


def test_user_isolation():
    """User A should never see or modify User B's wallet or transactions."""
    user_a_header = {"Authorization": f"Bearer {make_vendor()}"}
    user_b_header = {"Authorization": f"Bearer {make_vendor()}"}

    # Credit User A with 5000
    res_a = client.post(
        "/api/wallet/credit",
        json={"amount": 5000, "description": "User A initial"},
        headers=user_a_header,
    )
    assert res_a.status_code == 200
    txn_a_id = res_a.json()["data"]["transaction"]["transaction_id"]

    # User B checks wallet -> should be 0
    res_b = client.get("/api/wallet", headers=user_b_header)
    assert res_b.status_code == 200
    assert res_b.json()["data"]["balance"] == 0

    # User B tries to get User A's transaction -> should be 403 or 404
    res_b_txn = client.get(
        f"/api/wallet/transactions/{txn_a_id}",
        headers=user_b_header,
    )
    assert res_b_txn.status_code in (403, 404)
    assert res_b_txn.json()["success"] is False


def test_api_v1_prefix_parity():
    """Verify both /api/wallet and /api/v1/wallet prefixes work equivalently."""
    user_id = make_vendor()
    headers = {"Authorization": f"Bearer {user_id}"}

    # Credit via /api/v1/wallet/credit
    res = client.post("/api/v1/wallet/credit", json={"amount": 3000}, headers=headers)
    assert res.status_code == 200
    assert res.json()["data"]["wallet"]["current_balance"] == 3000

    # Read via /api/wallet
    res_read = client.get("/api/wallet", headers=headers)
    assert res_read.status_code == 200
    assert res_read.json()["data"]["balance"] == 3000


def test_vendor_registration_auto_wallet():
    """When a new vendor registers, their active INR wallet with balance 0 must be created."""
    mobile = f"+9198{uuid.uuid4().int % 100000000:08d}"

    # OTP flow
    client.post("/api/v1/vendors/send-otp", json={"mobile_number": mobile})
    client.post("/api/v1/vendors/verify-otp", json={"mobile_number": mobile, "otp": "1234"})

    # Register
    res = client.post(
        "/api/v1/vendors/register",
        json={"business_name": "Test Store", "mobile_number": mobile},
    )
    assert res.status_code == 201
    vendor_id = str(res.json()["id"])

    # Wallet must exist with balance 0
    wallet_res = client.get("/api/wallet", headers={"Authorization": f"Bearer {vendor_id}"})
    assert wallet_res.status_code == 200
    assert wallet_res.json()["data"]["balance"] == 0
    assert wallet_res.json()["data"]["currency"] == "INR"
    assert wallet_res.json()["data"]["status"] == "active"


def test_transaction_filtering_and_pagination():
    """Verify transaction pagination and filtering by type."""
    user = make_vendor()
    headers = {"Authorization": f"Bearer {user}"}

    # Create 3 credits and 2 debits
    client.post("/api/wallet/credit", json={"amount": 1000}, headers=headers)
    client.post("/api/wallet/credit", json={"amount": 2000}, headers=headers)
    client.post("/api/wallet/credit", json={"amount": 3000}, headers=headers)
    client.post("/api/wallet/debit", json={"amount": 500}, headers=headers)
    client.post("/api/wallet/debit", json={"amount": 400}, headers=headers)

    # Page 1, limit 2
    res_p1 = client.get("/api/wallet/transactions?page=1&limit=2", headers=headers)
    assert res_p1.status_code == 200
    d1 = res_p1.json()["data"]
    assert len(d1["transactions"]) == 2
    assert d1["pagination"]["total"] == 5
    assert d1["pagination"]["total_pages"] == 3

    # Filter type=debit
    res_debits = client.get("/api/wallet/transactions?type=debit", headers=headers)
    assert res_debits.status_code == 200
    assert res_debits.json()["data"]["pagination"]["total"] == 2
    for t in res_debits.json()["data"]["transactions"]:
        assert t["type"] == "debit"


def test_validation_invalid_amounts():
    """Amounts must be > 0 and cannot be negative or zero."""
    headers = {"Authorization": "Bearer user_val_test"}

    # Zero amount
    res = client.post("/api/wallet/credit", json={"amount": 0}, headers=headers)
    assert res.status_code in (400, 422)
    assert res.json()["success"] is False

    # Negative amount
    res = client.post("/api/wallet/debit", json={"amount": -500}, headers=headers)
    assert res.status_code in (400, 422)
    assert res.json()["success"] is False


def test_complete_prompt_scenario():
    """Test the exact 6-step lifecycle requested in Section 20 of prompt:
    START: Wallet balance = 0
    STEP 1: Credit 15,000 -> Wallet = 15,000, Txn = CREDIT 15,000
    STEP 2: Debit 500     -> Wallet = 14,500, Txn = DEBIT 500
    STEP 3: Debit 1,000   -> Wallet = 13,500, Txn = DEBIT 1,000
    STEP 4: Credit 2,000  -> Wallet = 15,500, Txn = CREDIT 2,000
    STEP 5: Debit 20,000  -> FAILS (Insufficient balance), Wallet remains 15,500
    STEP 6: Debit twice with same Idempotency-Key -> Debited only once
    """
    test_user = make_vendor()
    headers = {"Authorization": f"Bearer {test_user}"}

    # START: Check initial balance = 0
    res_start = client.get("/api/wallet", headers=headers)
    assert res_start.status_code == 200
    data_start = res_start.json()
    assert data_start["success"] is True
    assert data_start["data"]["balance"] == 0
    assert data_start["data"]["currency"] == "INR"
    assert data_start["data"]["status"] == "active"
    wallet_id = data_start["data"]["wallet_id"]

    # STEP 1: Credit 15,000
    res_step1 = client.post(
        "/api/wallet/credit",
        json={"amount": 15000, "description": "Test wallet credit"},
        headers=headers,
    )
    assert res_step1.status_code == 200
    data_step1 = res_step1.json()
    assert data_step1["success"] is True
    assert data_step1["data"]["wallet"]["previous_balance"] == 0
    assert data_step1["data"]["wallet"]["credited_amount"] == 15000
    assert data_step1["data"]["wallet"]["current_balance"] == 15000
    assert data_step1["data"]["transaction"]["type"] == "credit"
    assert data_step1["data"]["transaction"]["amount"] == 15000
    assert data_step1["data"]["transaction"]["status"] == "completed"

    # STEP 2: Debit 500
    res_step2 = client.post(
        "/api/wallet/debit",
        json={"amount": 500, "description": "Campaign usage"},
        headers=headers,
    )
    assert res_step2.status_code == 200
    data_step2 = res_step2.json()
    assert data_step2["success"] is True
    assert data_step2["data"]["wallet"]["previous_balance"] == 15000
    assert data_step2["data"]["wallet"]["debited_amount"] == 500
    assert data_step2["data"]["wallet"]["current_balance"] == 14500
    assert data_step2["data"]["transaction"]["type"] == "debit"
    assert data_step2["data"]["transaction"]["amount"] == 500
    assert data_step2["data"]["transaction"]["status"] == "completed"

    # STEP 3: Debit 1,000
    res_step3 = client.post(
        "/api/wallet/debit",
        json={"amount": 1000, "description": "Campaign refresh"},
        headers=headers,
    )
    assert res_step3.status_code == 200
    data_step3 = res_step3.json()
    assert data_step3["success"] is True
    assert data_step3["data"]["wallet"]["previous_balance"] == 14500
    assert data_step3["data"]["wallet"]["debited_amount"] == 1000
    assert data_step3["data"]["wallet"]["current_balance"] == 13500
    assert data_step3["data"]["transaction"]["type"] == "debit"
    assert data_step3["data"]["transaction"]["amount"] == 1000

    # STEP 4: Credit 2,000
    res_step4 = client.post(
        "/api/wallet/credit",
        json={"amount": 2000, "description": "Top-up credit"},
        headers=headers,
    )
    assert res_step4.status_code == 200
    data_step4 = res_step4.json()
    assert data_step4["success"] is True
    assert data_step4["data"]["wallet"]["previous_balance"] == 13500
    assert data_step4["data"]["wallet"]["credited_amount"] == 2000
    assert data_step4["data"]["wallet"]["current_balance"] == 15500
    assert data_step4["data"]["transaction"]["type"] == "credit"
    assert data_step4["data"]["transaction"]["amount"] == 2000

    # STEP 5: Try to debit 20,000 (Insufficient balance)
    res_step5 = client.post(
        "/api/wallet/debit",
        json={"amount": 20000, "description": "Overdraw attempt"},
        headers=headers,
    )
    assert res_step5.status_code in (400, 409)
    data_step5 = res_step5.json()
    assert data_step5["success"] is False
    assert data_step5["error"]["code"] == "INSUFFICIENT_BALANCE"
    assert data_step5["error"]["details"]["available_balance"] == 15500
    assert data_step5["error"]["details"]["requested_amount"] == 20000

    # Verify wallet balance remained 15,500
    res_check = client.get("/api/wallet", headers=headers)
    assert res_check.json()["data"]["balance"] == 15500

    # STEP 6: Send same debit request twice with the same Idempotency-Key
    idempotency_key = f"idemp_{uuid.uuid4().hex[:10]}"
    headers_idemp = {
        "Authorization": f"Bearer {test_user}",
        "Idempotency-Key": idempotency_key,
    }

    # First attempt: Debit 500
    res_idemp1 = client.post(
        "/api/wallet/debit",
        json={"amount": 500, "description": "Idempotent debit test"},
        headers=headers_idemp,
    )
    assert res_idemp1.status_code == 200
    data_idemp1 = res_idemp1.json()
    assert data_idemp1["data"]["wallet"]["current_balance"] == 15000
    txn_id_first = data_idemp1["data"]["transaction"]["transaction_id"]

    # Second attempt: Same request and Idempotency-Key
    res_idemp2 = client.post(
        "/api/wallet/debit",
        json={"amount": 500, "description": "Idempotent debit test"},
        headers=headers_idemp,
    )
    assert res_idemp2.status_code == 200
    data_idemp2 = res_idemp2.json()
    # Transaction ID must be identical (not a newly generated transaction)
    assert data_idemp2["data"]["transaction"]["transaction_id"] == txn_id_first
    # Wallet balance must still be 15,000, NOT 14,500
    assert data_idemp2["data"]["wallet"]["current_balance"] == 15000

    # Check wallet balance via GET
    res_final_balance = client.get("/api/wallet", headers=headers)
    assert res_final_balance.json()["data"]["balance"] == 15000

    # Check Transaction History
    res_txns = client.get("/api/wallet/transactions?limit=10", headers=headers)
    assert res_txns.status_code == 200
    txns_data = res_txns.json()["data"]
    assert txns_data["pagination"]["total"] == 5  # 2 credits + 3 successful debits (no overdraw)
    assert len(txns_data["transactions"]) == 5

    # Newest transaction first
    assert txns_data["transactions"][0]["transaction_id"] == txn_id_first

    # Check single transaction detail
    res_single = client.get(f"/api/wallet/transactions/{txn_id_first}", headers=headers)
    assert res_single.status_code == 200
    single_data = res_single.json()["data"]
    assert single_data["transaction_id"] == txn_id_first
    assert single_data["amount"] == 500
    assert single_data["type"] == "debit"


def test_refund_scenario():
    """Verify refund creates a new refund record and restores balance without modifying original."""
    test_user = make_vendor()
    headers = {"Authorization": f"Bearer {test_user}"}

    # Credit 5000
    client.post("/api/wallet/credit", json={"amount": 5000}, headers=headers)

    # Debit 1000
    debit_res = client.post(
        "/api/wallet/debit",
        json={"amount": 1000, "description": "Campaign spend"},
        headers=headers,
    )
    txn_id = debit_res.json()["data"]["transaction"]["transaction_id"]
    assert debit_res.json()["data"]["wallet"]["current_balance"] == 4000

    # Refund the 1000
    refund_res = client.post(
        f"/api/wallet/transactions/{txn_id}/refund",
        json={"reason": "Campaign cancelled before launch"},
        headers=headers,
    )
    assert refund_res.status_code == 200
    refund_data = refund_res.json()["data"]
    assert refund_data["wallet"]["current_balance"] == 5000
    assert refund_data["transaction"]["type"] == "refund"
    assert refund_data["transaction"]["amount"] == 1000
    assert refund_data["transaction"]["reference_id"] == txn_id

    # Original transaction still exists as completed debit
    orig_res = client.get(f"/api/wallet/transactions/{txn_id}", headers=headers)
    assert orig_res.status_code == 200
    assert orig_res.json()["data"]["type"] == "debit"
    assert orig_res.json()["data"]["status"] == "completed"


def test_concurrent_debits():
    """Wallet balance = 1,000.
    Two concurrent debit requests: Request A = 800, Request B = 500.
    Both together would exceed 1,000. Exactly one must succeed and one must fail.
    """
    test_user = make_vendor()
    headers = {"Authorization": f"Bearer {test_user}"}

    # Fund wallet with 1,000
    client.post("/api/wallet/credit", json={"amount": 1000}, headers=headers)

    results = []
    errors = []

    def run_debit(amount):
        # Dedicated client instance for thread
        c = TestClient(app)
        res = c.post(
            "/api/wallet/debit",
            json={"amount": amount, "description": f"Debit {amount}"},
            headers=headers,
        )
        if res.status_code == 200:
            results.append((amount, res.json()))
        else:
            errors.append((amount, res.status_code, res.json()))

    t1 = threading.Thread(target=run_debit, args=(800,))
    t2 = threading.Thread(target=run_debit, args=(500,))

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Exactly one request succeeded and one failed with INSUFFICIENT_BALANCE
    assert len(results) == 1
    assert len(errors) == 1
    assert errors[0][1] in (400, 409)
    assert errors[0][2]["error"]["code"] == "INSUFFICIENT_BALANCE"

    # Verify final balance is either 1000 - 800 = 200 OR 1000 - 500 = 500
    final_res = client.get("/api/wallet", headers=headers)
    final_balance = final_res.json()["data"]["balance"]
    assert final_balance in (200, 500)
