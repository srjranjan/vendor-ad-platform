import math
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    JSON,
    String,
    UniqueConstraint,
    and_,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, relationship

from database import Base, IS_SQLITE, get_db

# ==============================================================================
# 1. DATABASE MODELS (Direct INR, rupees - no paise conversion)
# ==============================================================================


def generate_wallet_id() -> str:
    return f"wallet_{uuid.uuid4().hex[:12]}"


def generate_transaction_id() -> str:
    return f"txn_{uuid.uuid4().hex[:12]}"


class Wallet(Base):
    __tablename__ = "wallets"

    id = Column(String(64), primary_key=True, default=generate_wallet_id)
    user_id = Column(String(128), nullable=False, unique=True, index=True)
    # Stored directly in INR (rupees). Never converted to paise.
    balance = Column(Float, nullable=False, default=0.0)
    currency = Column(String(10), nullable=False, default="INR")
    status = Column(String(32), nullable=False, default="active")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    transactions = relationship(
        "Transaction", back_populates="wallet", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("balance >= 0", name="check_wallet_balance_non_negative"),
    )


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(String(64), primary_key=True, default=generate_transaction_id)
    wallet_id = Column(
        String(64), ForeignKey("wallets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    user_id = Column(String(128), nullable=False, index=True)
    type = Column(String(32), nullable=False, index=True)  # credit, debit, refund, reversal, adjustment
    amount = Column(Float, nullable=False)  # Direct INR (rupees)
    balance_before = Column(Float, nullable=False)  # Direct INR
    balance_after = Column(Float, nullable=False)  # Direct INR
    status = Column(String(32), nullable=False, default="completed", index=True)  # completed, pending, failed, reversed
    reference_id = Column(String(128), nullable=True, index=True)
    idempotency_key = Column(String(128), nullable=True, index=True)
    description = Column(String(512), nullable=True)
    metadata_ = Column("metadata", JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    wallet = relationship("Wallet", back_populates="transactions")

    __table_args__ = (
        UniqueConstraint("wallet_id", "idempotency_key", name="uq_wallet_idempotency_key"),
        Index("ix_transactions_wallet_created", "wallet_id", "created_at"),
    )


# ==============================================================================
# 2. EXCEPTIONS & STANDARDIZED ERROR HANDLER
# ==============================================================================


class WalletException(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        details: Optional[Dict[str, Any]] = None,
    ):
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        super().__init__(message)


def wallet_exception_handler(request: Request, exc: WalletException) -> JSONResponse:
    content = {
        "success": False,
        "error": {
            "code": exc.code,
            "message": exc.message,
            "details": exc.details,
        },
    }
    return JSONResponse(status_code=exc.status_code, content=content)


# ==============================================================================
# 3. AUTHENTICATION & AUTHORIZATION DEPENDENCY
# ==============================================================================


def get_current_user(
    authorization: Optional[str] = Header(None, alias="Authorization"),
    x_user_id: Optional[str] = Header(None, alias="X-User-ID"),
    x_vendor_id: Optional[str] = Header(None, alias="X-Vendor-ID"),
) -> str:
    """Validate and extract the authenticated user identity.
    Accepts:
      - 'Authorization: Bearer <token_or_user_id>'
      - 'X-User-ID' or 'X-Vendor-ID' header
    Rejects unauthorized requests with 401 UNAUTHORIZED.
    """
    user_id: Optional[str] = None

    if authorization:
        parts = authorization.strip().split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1].strip()
            if token:
                user_id = token
        elif len(parts) == 1 and parts[0].strip():
            user_id = parts[0].strip()

    if not user_id and x_user_id and x_user_id.strip():
        user_id = x_user_id.strip()

    if not user_id and x_vendor_id and x_vendor_id.strip():
        user_id = x_vendor_id.strip()

    if not user_id:
        raise WalletException(
            code="UNAUTHORIZED",
            message="Authentication credentials missing or invalid. Please provide 'Authorization: Bearer <token>'",
            status_code=401,
            details={"required_header": "Authorization: Bearer <token>"},
        )

    return user_id


# ==============================================================================
# 4. PYDANTIC SCHEMAS (Standard JSON Response formats)
# ==============================================================================


class WalletData(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    wallet_id: str
    balance: float
    currency: str = "INR"
    status: str = "active"


class WalletResponse(BaseModel):
    success: bool = True
    message: str = "Wallet fetched successfully"
    data: WalletData


class CreditRequest(BaseModel):
    amount: float = Field(..., description="Amount directly in INR (rupees)")
    currency: Optional[str] = Field("INR", description="Currency code (must be INR)")
    description: Optional[str] = Field("Wallet credit", max_length=512)
    reference_id: Optional[str] = Field(None, max_length=128)
    metadata: Optional[Dict[str, Any]] = None

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, v: float) -> float:
        if v is None or v <= 0:
            raise ValueError("Amount must be greater than 0")
        return round(float(v), 2)

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, v: Optional[str]) -> str:
        if v and v.upper() != "INR":
            raise ValueError("Currency must be INR")
        return "INR"


class DebitRequest(BaseModel):
    amount: float = Field(..., description="Amount directly in INR (rupees)")
    currency: Optional[str] = Field("INR", description="Currency code (must be INR)")
    description: Optional[str] = Field("Wallet debit", max_length=512)
    reference_id: Optional[str] = Field(None, max_length=128)
    metadata: Optional[Dict[str, Any]] = None

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, v: float) -> float:
        if v is None or v <= 0:
            raise ValueError("Amount must be greater than 0")
        return round(float(v), 2)

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, v: Optional[str]) -> str:
        if v and v.upper() != "INR":
            raise ValueError("Currency must be INR")
        return "INR"


class WalletBalanceInfo(BaseModel):
    wallet_id: str
    previous_balance: float
    credited_amount: Optional[float] = None
    debited_amount: Optional[float] = None
    current_balance: float
    currency: str = "INR"


class TransactionItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    transaction_id: str
    wallet_id: Optional[str] = None
    type: str
    amount: float
    balance_before: float
    balance_after: float
    status: str
    description: Optional[str] = None
    reference_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    created_at: datetime
    updated_at: Optional[datetime] = None


class CreditDebitData(BaseModel):
    wallet: WalletBalanceInfo
    transaction: TransactionItem


class CreditDebitResponse(BaseModel):
    success: bool = True
    message: str
    data: CreditDebitData


class PaginationInfo(BaseModel):
    page: int
    limit: int
    total: int
    total_pages: int


class TransactionListData(BaseModel):
    transactions: List[TransactionItem]
    pagination: PaginationInfo


class TransactionListResponse(BaseModel):
    success: bool = True
    message: str = "Transactions fetched successfully"
    data: TransactionListData


class TransactionDetailResponse(BaseModel):
    success: bool = True
    message: str = "Transaction fetched successfully"
    data: TransactionItem


class RefundRequest(BaseModel):
    amount: Optional[float] = Field(None, gt=0, description="Amount to refund (defaults to original full amount)")
    reason: Optional[str] = Field("Refund", max_length=512)


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: Dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    success: bool = False
    error: ErrorDetail


# ==============================================================================
# 5. WALLET SERVICE (Atomic business logic, concurrency control, idempotency)
# ==============================================================================


class WalletService:
    @staticmethod
    def get_or_create_wallet(db: Session, user_id: str) -> Wallet:
        """Fetch the active wallet for user_id or initialize a new active INR wallet with balance 0."""
        wallet = db.query(Wallet).filter(Wallet.user_id == user_id).first()
        if not wallet:
            try:
                wallet = Wallet(
                    id=generate_wallet_id(),
                    user_id=user_id,
                    balance=0.0,
                    currency="INR",
                    status="active",
                )
                db.add(wallet)
                db.commit()
                db.refresh(wallet)
            except IntegrityError:
                db.rollback()
                wallet = db.query(Wallet).filter(Wallet.user_id == user_id).first()
        return wallet

    @staticmethod
    def get_wallet_response(db: Session, user_id: str) -> WalletResponse:
        wallet = WalletService.get_or_create_wallet(db, user_id)
        return WalletResponse(
            success=True,
            message="Wallet fetched successfully",
            data=WalletData(
                wallet_id=wallet.id,
                balance=round(wallet.balance, 2),
                currency=wallet.currency,
                status=wallet.status,
            ),
        )

    @staticmethod
    def credit_wallet(
        db: Session,
        user_id: str,
        payload: CreditRequest,
        idempotency_key: Optional[str] = None,
    ) -> CreditDebitResponse:
        """Credit money directly in INR to wallet atomically."""
        amount = payload.amount
        if amount <= 0:
            raise WalletException(
                code="INVALID_AMOUNT",
                message="Credit amount must be greater than 0",
                status_code=400,
                details={"amount": amount},
            )

        wallet = WalletService.get_or_create_wallet(db, user_id)

        # Idempotency replay check
        if idempotency_key:
            existing_txn = (
                db.query(Transaction)
                .filter(
                    Transaction.wallet_id == wallet.id,
                    Transaction.idempotency_key == idempotency_key,
                )
                .first()
            )
            if existing_txn:
                return CreditDebitResponse(
                    success=True,
                    message="Wallet credited successfully (idempotent replay)",
                    data=CreditDebitData(
                        wallet=WalletBalanceInfo(
                            wallet_id=wallet.id,
                            previous_balance=round(existing_txn.balance_before, 2),
                            credited_amount=round(existing_txn.amount, 2),
                            current_balance=round(existing_txn.balance_after, 2),
                            currency=wallet.currency,
                        ),
                        transaction=TransactionItem.model_validate(
                            {
                                "transaction_id": existing_txn.id,
                                "wallet_id": existing_txn.wallet_id,
                                "type": existing_txn.type,
                                "amount": existing_txn.amount,
                                "balance_before": existing_txn.balance_before,
                                "balance_after": existing_txn.balance_after,
                                "status": existing_txn.status,
                                "description": existing_txn.description,
                                "reference_id": existing_txn.reference_id,
                                "metadata": existing_txn.metadata_,
                                "created_at": existing_txn.created_at,
                                "updated_at": existing_txn.updated_at,
                            }
                        ),
                    ),
                )

        try:
            query = db.query(Wallet).filter(Wallet.id == wallet.id)
            if not IS_SQLITE:
                query = query.with_for_update()
            locked_wallet = query.first()

            if not locked_wallet:
                raise WalletException(code="WALLET_NOT_FOUND", message="Wallet not found", status_code=404)

            if locked_wallet.status != "active":
                raise WalletException(
                    code="WALLET_INACTIVE",
                    message=f"Wallet is currently {locked_wallet.status}",
                    status_code=403,
                )

            prev_balance = round(locked_wallet.balance, 2)
            new_balance = round(prev_balance + amount, 2)
            locked_wallet.balance = new_balance
            locked_wallet.updated_at = datetime.utcnow()

            txn = Transaction(
                id=generate_transaction_id(),
                wallet_id=locked_wallet.id,
                user_id=user_id,
                type="credit",
                amount=amount,
                balance_before=prev_balance,
                balance_after=new_balance,
                status="completed",
                reference_id=payload.reference_id,
                idempotency_key=idempotency_key,
                description=payload.description or "Wallet credit",
                metadata_=payload.metadata,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            db.add(txn)
            db.commit()
            db.refresh(locked_wallet)
            db.refresh(txn)

            return CreditDebitResponse(
                success=True,
                message="Wallet credited successfully",
                data=CreditDebitData(
                    wallet=WalletBalanceInfo(
                        wallet_id=locked_wallet.id,
                        previous_balance=prev_balance,
                        credited_amount=amount,
                        current_balance=new_balance,
                        currency=locked_wallet.currency,
                    ),
                    transaction=TransactionItem.model_validate(
                        {
                            "transaction_id": txn.id,
                            "wallet_id": txn.wallet_id,
                            "type": txn.type,
                            "amount": txn.amount,
                            "balance_before": txn.balance_before,
                            "balance_after": txn.balance_after,
                            "status": txn.status,
                            "description": txn.description,
                            "reference_id": txn.reference_id,
                            "metadata": txn.metadata_,
                            "created_at": txn.created_at,
                            "updated_at": txn.updated_at,
                        }
                    ),
                ),
            )
        except WalletException:
            db.rollback()
            raise
        except IntegrityError as ie:
            db.rollback()
            if idempotency_key:
                existing_txn = (
                    db.query(Transaction)
                    .filter(
                        Transaction.wallet_id == wallet.id,
                        Transaction.idempotency_key == idempotency_key,
                    )
                    .first()
                )
                if existing_txn:
                    return CreditDebitResponse(
                        success=True,
                        message="Wallet credited successfully (idempotent replay)",
                        data=CreditDebitData(
                            wallet=WalletBalanceInfo(
                                wallet_id=wallet.id,
                                previous_balance=round(existing_txn.balance_before, 2),
                                credited_amount=round(existing_txn.amount, 2),
                                current_balance=round(existing_txn.balance_after, 2),
                                currency=wallet.currency,
                            ),
                            transaction=TransactionItem.model_validate(
                                {
                                    "transaction_id": existing_txn.id,
                                    "wallet_id": existing_txn.wallet_id,
                                    "type": existing_txn.type,
                                    "amount": existing_txn.amount,
                                    "balance_before": existing_txn.balance_before,
                                    "balance_after": existing_txn.balance_after,
                                    "status": existing_txn.status,
                                    "description": existing_txn.description,
                                    "reference_id": existing_txn.reference_id,
                                    "metadata": existing_txn.metadata_,
                                    "created_at": existing_txn.created_at,
                                    "updated_at": existing_txn.updated_at,
                                }
                            ),
                        ),
                    )
            raise WalletException(
                code="DUPLICATE_TRANSACTION",
                message=f"Transaction integrity error: {str(ie)}",
                status_code=409,
            )
        except Exception as e:
            db.rollback()
            raise WalletException(
                code="INTERNAL_SERVER_ERROR",
                message=f"Credit failed: {str(e)}",
                status_code=500,
            )

    @staticmethod
    def debit_wallet(
        db: Session,
        user_id: str,
        payload: DebitRequest,
        idempotency_key: Optional[str] = None,
    ) -> CreditDebitResponse:
        """Debit money directly in INR from wallet atomically with concurrency protection."""
        amount = payload.amount
        if amount <= 0:
            raise WalletException(
                code="INVALID_AMOUNT",
                message="Debit amount must be greater than 0",
                status_code=400,
                details={"amount": amount},
            )

        wallet = WalletService.get_or_create_wallet(db, user_id)

        # Idempotency replay check
        if idempotency_key:
            existing_txn = (
                db.query(Transaction)
                .filter(
                    Transaction.wallet_id == wallet.id,
                    Transaction.idempotency_key == idempotency_key,
                )
                .first()
            )
            if existing_txn:
                return CreditDebitResponse(
                    success=True,
                    message="Wallet debited successfully (idempotent replay)",
                    data=CreditDebitData(
                        wallet=WalletBalanceInfo(
                            wallet_id=wallet.id,
                            previous_balance=round(existing_txn.balance_before, 2),
                            debited_amount=round(existing_txn.amount, 2),
                            current_balance=round(existing_txn.balance_after, 2),
                            currency=wallet.currency,
                        ),
                        transaction=TransactionItem.model_validate(
                            {
                                "transaction_id": existing_txn.id,
                                "wallet_id": existing_txn.wallet_id,
                                "type": existing_txn.type,
                                "amount": existing_txn.amount,
                                "balance_before": existing_txn.balance_before,
                                "balance_after": existing_txn.balance_after,
                                "status": existing_txn.status,
                                "description": existing_txn.description,
                                "reference_id": existing_txn.reference_id,
                                "metadata": existing_txn.metadata_,
                                "created_at": existing_txn.created_at,
                                "updated_at": existing_txn.updated_at,
                            }
                        ),
                    ),
                )

        try:
            # Atomic conditional update prevents race conditions across all SQL engines
            now = datetime.utcnow()
            result = db.execute(
                update(Wallet)
                .where(
                    and_(
                        Wallet.id == wallet.id,
                        Wallet.balance >= amount,
                        Wallet.status == "active",
                    )
                )
                .values(
                    balance=Wallet.balance - amount,
                    updated_at=now,
                )
            )

            if result.rowcount == 0:
                current_wallet = db.query(Wallet).filter(Wallet.id == wallet.id).first()
                if not current_wallet:
                    raise WalletException(code="WALLET_NOT_FOUND", message="Wallet not found", status_code=404)
                if current_wallet.status != "active":
                    raise WalletException(
                        code="WALLET_INACTIVE",
                        message=f"Wallet is currently {current_wallet.status}",
                        status_code=403,
                    )
                raise WalletException(
                    code="INSUFFICIENT_BALANCE",
                    message="Insufficient wallet balance",
                    status_code=400,
                    details={
                        "available_balance": round(current_wallet.balance, 2),
                        "requested_amount": amount,
                    },
                )

            locked_wallet = db.query(Wallet).filter(Wallet.id == wallet.id).first()
            new_balance = round(locked_wallet.balance, 2)
            prev_balance = round(new_balance + amount, 2)

            txn = Transaction(
                id=generate_transaction_id(),
                wallet_id=locked_wallet.id,
                user_id=user_id,
                type="debit",
                amount=amount,
                balance_before=prev_balance,
                balance_after=new_balance,
                status="completed",
                reference_id=payload.reference_id,
                idempotency_key=idempotency_key,
                description=payload.description or "Wallet debit",
                metadata_=payload.metadata,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            db.add(txn)
            db.commit()
            db.refresh(locked_wallet)
            db.refresh(txn)

            return CreditDebitResponse(
                success=True,
                message="Wallet debited successfully",
                data=CreditDebitData(
                    wallet=WalletBalanceInfo(
                        wallet_id=locked_wallet.id,
                        previous_balance=prev_balance,
                        debited_amount=amount,
                        current_balance=new_balance,
                        currency=locked_wallet.currency,
                    ),
                    transaction=TransactionItem.model_validate(
                        {
                            "transaction_id": txn.id,
                            "wallet_id": txn.wallet_id,
                            "type": txn.type,
                            "amount": txn.amount,
                            "balance_before": txn.balance_before,
                            "balance_after": txn.balance_after,
                            "status": txn.status,
                            "description": txn.description,
                            "reference_id": txn.reference_id,
                            "metadata": txn.metadata_,
                            "created_at": txn.created_at,
                            "updated_at": txn.updated_at,
                        }
                    ),
                ),
            )
        except WalletException:
            db.rollback()
            raise
        except IntegrityError as ie:
            db.rollback()
            if idempotency_key:
                existing_txn = (
                    db.query(Transaction)
                    .filter(
                        Transaction.wallet_id == wallet.id,
                        Transaction.idempotency_key == idempotency_key,
                    )
                    .first()
                )
                if existing_txn:
                    return CreditDebitResponse(
                        success=True,
                        message="Wallet debited successfully (idempotent replay)",
                        data=CreditDebitData(
                            wallet=WalletBalanceInfo(
                                wallet_id=wallet.id,
                                previous_balance=round(existing_txn.balance_before, 2),
                                debited_amount=round(existing_txn.amount, 2),
                                current_balance=round(existing_txn.balance_after, 2),
                                currency=wallet.currency,
                            ),
                            transaction=TransactionItem.model_validate(
                                {
                                    "transaction_id": existing_txn.id,
                                    "wallet_id": existing_txn.wallet_id,
                                    "type": existing_txn.type,
                                    "amount": existing_txn.amount,
                                    "balance_before": existing_txn.balance_before,
                                    "balance_after": existing_txn.balance_after,
                                    "status": existing_txn.status,
                                    "description": existing_txn.description,
                                    "reference_id": existing_txn.reference_id,
                                    "metadata": existing_txn.metadata_,
                                    "created_at": existing_txn.created_at,
                                    "updated_at": existing_txn.updated_at,
                                }
                            ),
                        ),
                    )
            raise WalletException(
                code="DUPLICATE_TRANSACTION",
                message=f"Transaction integrity error: {str(ie)}",
                status_code=409,
            )
        except Exception as e:
            db.rollback()
            raise WalletException(
                code="INTERNAL_SERVER_ERROR",
                message=f"Debit failed: {str(e)}",
                status_code=500,
            )

    @staticmethod
    def get_transactions(
        db: Session,
        user_id: str,
        page: int = 1,
        limit: int = 20,
        txn_type: Optional[str] = None,
        status: Optional[str] = None,
        reference_id: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> TransactionListResponse:
        """Fetch transactions strictly scoped to the authenticated user's wallet with filters, pagination, newest-first."""
        wallet = WalletService.get_or_create_wallet(db, user_id)

        query = db.query(Transaction).filter(Transaction.wallet_id == wallet.id)

        if txn_type:
            query = query.filter(Transaction.type == txn_type.lower())
        if status:
            query = query.filter(Transaction.status == status.lower())
        if reference_id:
            query = query.filter(Transaction.reference_id == reference_id)
        if start_date:
            query = query.filter(Transaction.created_at >= start_date)
        if end_date:
            query = query.filter(Transaction.created_at <= end_date)

        total = query.count()
        total_pages = math.ceil(total / limit) if total > 0 else 1

        offset = (page - 1) * limit
        transactions = (
            query.order_by(Transaction.created_at.desc(), Transaction.id.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

        item_list = [
            TransactionItem.model_validate(
                {
                    "transaction_id": t.id,
                    "wallet_id": t.wallet_id,
                    "type": t.type,
                    "amount": t.amount,
                    "balance_before": t.balance_before,
                    "balance_after": t.balance_after,
                    "status": t.status,
                    "description": t.description,
                    "reference_id": t.reference_id,
                    "metadata": t.metadata_,
                    "created_at": t.created_at,
                    "updated_at": t.updated_at,
                }
            )
            for t in transactions
        ]

        return TransactionListResponse(
            success=True,
            message="Transactions fetched successfully",
            data=TransactionListData(
                transactions=item_list,
                pagination=PaginationInfo(
                    page=page,
                    limit=limit,
                    total=total,
                    total_pages=total_pages,
                ),
            ),
        )

    @staticmethod
    def get_transaction(db: Session, user_id: str, transaction_id: str) -> TransactionItem:
        """Retrieve single transaction with strict ownership check."""
        wallet = WalletService.get_or_create_wallet(db, user_id)

        txn = db.query(Transaction).filter(Transaction.id == transaction_id).first()
        if not txn:
            raise WalletException(
                code="TRANSACTION_NOT_FOUND",
                message="Transaction not found",
                status_code=404,
                details={"transaction_id": transaction_id},
            )

        if txn.wallet_id != wallet.id:
            raise WalletException(
                code="FORBIDDEN",
                message="You do not have permission to view this transaction",
                status_code=403,
                details={"transaction_id": transaction_id},
            )

        return TransactionItem.model_validate(
            {
                "transaction_id": txn.id,
                "wallet_id": txn.wallet_id,
                "type": txn.type,
                "amount": txn.amount,
                "balance_before": txn.balance_before,
                "balance_after": txn.balance_after,
                "status": txn.status,
                "description": txn.description,
                "reference_id": txn.reference_id,
                "metadata": txn.metadata_,
                "created_at": txn.created_at,
                "updated_at": txn.updated_at,
            }
        )

    @staticmethod
    def refund_transaction(
        db: Session,
        user_id: str,
        original_txn_id: str,
        amount: Optional[float] = None,
        reason: Optional[str] = "Refund",
    ) -> CreditDebitResponse:
        """Refund a previous transaction by creating a new refund record without modifying the original."""
        wallet = WalletService.get_or_create_wallet(db, user_id)

        original_txn = (
            db.query(Transaction).filter(Transaction.id == original_txn_id).first()
        )
        if not original_txn:
            raise WalletException(
                code="TRANSACTION_NOT_FOUND",
                message="Original transaction not found",
                status_code=404,
                details={"transaction_id": original_txn_id},
            )

        if original_txn.wallet_id != wallet.id:
            raise WalletException(
                code="FORBIDDEN",
                message="You do not have permission to refund this transaction",
                status_code=403,
            )

        if original_txn.type not in ("debit", "adjustment"):
            raise WalletException(
                code="VALIDATION_ERROR",
                message=f"Cannot refund a transaction of type '{original_txn.type}'",
                status_code=400,
            )

        refund_amount = amount if amount is not None else original_txn.amount
        if refund_amount <= 0 or refund_amount > original_txn.amount:
            raise WalletException(
                code="INVALID_AMOUNT",
                message=f"Refund amount must be between 0 and {original_txn.amount}",
                status_code=400,
                details={
                    "requested_refund": refund_amount,
                    "original_amount": original_txn.amount,
                },
            )

        try:
            query = db.query(Wallet).filter(Wallet.id == wallet.id)
            if not IS_SQLITE:
                query = query.with_for_update()
            locked_wallet = query.first()

            prev_balance = round(locked_wallet.balance, 2)
            new_balance = round(prev_balance + refund_amount, 2)
            locked_wallet.balance = new_balance
            locked_wallet.updated_at = datetime.utcnow()

            refund_txn = Transaction(
                id=generate_transaction_id(),
                wallet_id=locked_wallet.id,
                user_id=user_id,
                type="refund",
                amount=refund_amount,
                balance_before=prev_balance,
                balance_after=new_balance,
                status="completed",
                reference_id=original_txn_id,
                description=f"{reason} (Ref: {original_txn_id})",
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            db.add(refund_txn)
            db.commit()
            db.refresh(locked_wallet)
            db.refresh(refund_txn)

            return CreditDebitResponse(
                success=True,
                message="Refund processed successfully",
                data=CreditDebitData(
                    wallet=WalletBalanceInfo(
                        wallet_id=locked_wallet.id,
                        previous_balance=prev_balance,
                        credited_amount=refund_amount,
                        current_balance=new_balance,
                        currency=locked_wallet.currency,
                    ),
                    transaction=TransactionItem.model_validate(
                        {
                            "transaction_id": refund_txn.id,
                            "wallet_id": refund_txn.wallet_id,
                            "type": refund_txn.type,
                            "amount": refund_txn.amount,
                            "balance_before": refund_txn.balance_before,
                            "balance_after": refund_txn.balance_after,
                            "status": refund_txn.status,
                            "description": refund_txn.description,
                            "reference_id": refund_txn.reference_id,
                            "metadata": refund_txn.metadata_,
                            "created_at": refund_txn.created_at,
                            "updated_at": refund_txn.updated_at,
                        }
                    ),
                ),
            )
        except Exception as e:
            db.rollback()
            raise WalletException(
                code="INTERNAL_SERVER_ERROR",
                message=f"Refund failed: {str(e)}",
                status_code=500,
            )


# ==============================================================================
# 6. FASTAPI ROUTER WITH ALL WALLET API ENDPOINTS
# ==============================================================================

wallet_router = APIRouter(tags=["Wallet"])


@wallet_router.get("", response_model=WalletResponse)
@wallet_router.get("/", response_model=WalletResponse, include_in_schema=False)
def get_wallet(
    user_id: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Retrieve current wallet balance, currency (INR), and active status."""
    return WalletService.get_wallet_response(db, user_id=user_id)


@wallet_router.post("/credit", response_model=CreditDebitResponse)
def credit_wallet(
    payload: CreditRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    user_id: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Add / credit money directly in INR to wallet atomically."""
    return WalletService.credit_wallet(
        db,
        user_id=user_id,
        payload=payload,
        idempotency_key=idempotency_key,
    )


@wallet_router.post("/debit", response_model=CreditDebitResponse)
def debit_wallet(
    payload: DebitRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    user_id: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Deduct / debit money directly in INR from wallet atomically."""
    return WalletService.debit_wallet(
        db,
        user_id=user_id,
        payload=payload,
        idempotency_key=idempotency_key,
    )


@wallet_router.get("/transactions", response_model=TransactionListResponse)
def get_transactions(
    page: int = Query(1, ge=1, description="Page number"),
    limit: int = Query(20, ge=1, le=100, description="Items per page"),
    type: Optional[str] = Query(None, description="Filter by transaction type (credit, debit, etc.)"),
    status: Optional[str] = Query(None, description="Filter by transaction status (completed, failed, etc.)"),
    reference_id: Optional[str] = Query(None, description="Filter by reference ID"),
    start_date: Optional[datetime] = Query(None, description="Filter transactions after this ISO date"),
    end_date: Optional[datetime] = Query(None, description="Filter transactions before this ISO date"),
    user_id: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Retrieve paginated transaction history sorted newest first."""
    return WalletService.get_transactions(
        db,
        user_id=user_id,
        page=page,
        limit=limit,
        txn_type=type,
        status=status,
        reference_id=reference_id,
        start_date=start_date,
        end_date=end_date,
    )


@wallet_router.get("/transactions/{transaction_id}", response_model=TransactionDetailResponse)
def get_transaction(
    transaction_id: str = Path(..., description="Transaction ID"),
    user_id: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Retrieve details for a single transaction. Strictly validates user ownership."""
    txn_item = WalletService.get_transaction(db, user_id=user_id, transaction_id=transaction_id)
    return TransactionDetailResponse(
        success=True,
        message="Transaction fetched successfully",
        data=txn_item,
    )


@wallet_router.post("/transactions/{transaction_id}/refund", response_model=CreditDebitResponse)
def refund_transaction(
    transaction_id: str = Path(..., description="Original transaction ID to refund"),
    payload: Optional[RefundRequest] = None,
    user_id: str = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Refund a previously debited transaction by adding a new refund ledger entry."""
    refund_amount = payload.amount if payload else None
    reason = payload.reason if payload and payload.reason else "Refund"
    return WalletService.refund_transaction(
        db,
        user_id=user_id,
        original_txn_id=transaction_id,
        amount=refund_amount,
        reason=reason,
    )
