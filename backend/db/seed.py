import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Customer, Order

logger = logging.getLogger(__name__)

_DEMO_CUSTOMERS = [
    {"name": "Acme Corp", "email": "ops@acme.example", "segment": "enterprise"},
    {"name": "Globex Inc", "email": "billing@globex.example", "segment": "mid-market"},
    {"name": "Initech", "email": "finance@initech.example", "segment": "smb"},
]

_DEMO_ORDERS = [
    {"customer": "Acme Corp", "product": "Enterprise QA Platform - Annual", "amount": 48000.00, "status": "completed"},
    {"customer": "Acme Corp", "product": "Professional Services", "amount": 12000.00, "status": "completed"},
    {"customer": "Globex Inc", "product": "Enterprise QA Platform - Annual", "amount": 24000.00, "status": "completed"},
    {"customer": "Initech", "product": "Starter Plan - Monthly", "amount": 900.00, "status": "pending"},
]


def seed_demo_data(db: Session) -> None:
    if db.scalar(select(Customer.id).limit(1)):
        return

    logger.info("Seeding demo structured data (customers/orders)")
    name_to_customer: dict[str, Customer] = {}
    for row in _DEMO_CUSTOMERS:
        customer = Customer(**row)
        db.add(customer)
        name_to_customer[row["name"]] = customer
    db.flush()

    for row in _DEMO_ORDERS:
        db.add(
            Order(
                customer_id=name_to_customer[row["customer"]].id,
                product=row["product"],
                amount=row["amount"],
                status=row["status"],
            )
        )
    db.commit()
