"""Domain enums, and the PostgreSQL ENUM types they map onto.

`create_type=False` everywhere: Alembic owns type creation, so the models never
try to create a type that already exists.
"""

import enum

from sqlalchemy.dialects.postgresql import ENUM as PgEnum


class WalletType(str, enum.Enum):
    USER = "USER"
    SYSTEM = "SYSTEM"


class TxnType(str, enum.Enum):
    SIGNUP_GRANT = "SIGNUP_GRANT"
    TRANSFER = "TRANSFER"
    REQUEST_SETTLEMENT = "REQUEST_SETTLEMENT"
    REVERSAL = "REVERSAL"


class TxnStatus(str, enum.Enum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    REVERSED = "REVERSED"


class RequestStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DECLINED = "DECLINED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


def _values(enum_cls):
    return [member.value for member in enum_cls]


wallet_type_pg = PgEnum(
    WalletType, name="wallet_type", create_type=False, values_callable=_values
)
txn_type_pg = PgEnum(
    TxnType, name="txn_type", create_type=False, values_callable=_values
)
txn_status_pg = PgEnum(
    TxnStatus, name="txn_status", create_type=False, values_callable=_values
)
request_status_pg = PgEnum(
    RequestStatus, name="request_status", create_type=False, values_callable=_values
)
