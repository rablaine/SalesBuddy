"""
Seed fake customers and related data for FY cutover testing.

Run with:  flask shell < scripts/seed_fy_test_data.py
   or:     python scripts/seed_fy_test_data.py

Creates ~5 fake customers with notes, engagements, milestones, and
opportunities — all tied to TPIDs that aren't in your real alignment.
When you run "Finalize & Purge", these should all get cleaned up.
"""

import sys
import os
from datetime import datetime, date, timezone, timedelta

# Add project root to path so we can import the app
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from app.models import (
    db, Customer, Note, Engagement, Milestone, MsxTask,
    Opportunity, Seller, Territory, Topic,
)


FAKE_TPIDS = [9990001, 9990002, 9990003, 9990004, 9990005]

FAKE_CUSTOMERS = [
    {"name": "FY-TEST Contoso Electronics", "tpid": 9990001},
    {"name": "FY-TEST Fabrikam Industries", "tpid": 9990002},
    {"name": "FY-TEST Northwind Traders", "tpid": 9990003},
    {"name": "FY-TEST Tailspin Toys", "tpid": 9990004},
    {"name": "FY-TEST Woodgrove Bank", "tpid": 9990005},
]


def seed():
    """Insert fake customers and related data for FY cutover testing."""
    app = create_app()
    with app.app_context():
        # Check if already seeded
        existing = Customer.query.filter(Customer.tpid.in_(FAKE_TPIDS)).first()
        if existing:
            print(f"Fake data already exists (found {existing.name}). Skipping.")
            return

        # Grab a real seller and territory to attach to (or create test ones)
        seller = Seller.query.first()
        territory = Territory.query.first()
        topic = Topic.query.first()

        now = datetime.now(timezone.utc)
        customers = []

        for i, cdata in enumerate(FAKE_CUSTOMERS):
            c = Customer(
                name=cdata["name"],
                tpid=cdata["tpid"],
                seller_id=seller.id if seller else None,
                territory_id=territory.id if territory else None,
            )
            db.session.add(c)
            db.session.flush()  # get the ID
            customers.append(c)
            print(f"  Created customer: {c.name} (TPID {c.tpid})")

            # 2-3 notes per customer
            for j in range(2 + (i % 2)):
                note = Note(
                    customer_id=c.id,
                    call_date=now - timedelta(days=j * 7 + i),
                    content=f"Test note #{j+1} for {c.name}. "
                            f"Discussed Azure migration plans and cost optimization. "
                            f"Next steps: schedule follow-up with engineering team.",
                )
                if topic:
                    note.topics.append(topic)
                db.session.add(note)
                db.session.flush()

            # 1 engagement per customer
            eng = Engagement(
                customer_id=c.id,
                title=f"{c.name} - Cloud Migration",
                status="Active",
                technical_problem=f"Legacy workloads need migration to Azure for {c.name}.",
                business_impact="Reduce infrastructure costs by 30%.",
                estimated_acr="$50K",
                target_date=date.today() + timedelta(days=90),
            )
            db.session.add(eng)
            db.session.flush()

            # 1 opportunity per customer
            opp = Opportunity(
                msx_opportunity_id=f"FAKE-OPP-{c.tpid}",
                name=f"{c.name} Azure Deal",
                customer_id=c.id,
                statecode=0,
                state="Open",
                status_reason="In Progress",
                estimated_value=50000.0 + (i * 10000),
            )
            db.session.add(opp)
            db.session.flush()

            # 1 milestone per customer
            ms = Milestone(
                msx_milestone_id=f"FAKE-MS-{c.tpid}",
                url=f"https://fake-msx.example.com/milestone/{c.tpid}",
                title=f"{c.name} - Azure Deployment",
                msx_status="On Track",
                customer_id=c.id,
                opportunity_id=opp.id,
                dollar_value=25000.0,
                workload="Azure Compute",
                due_date=now + timedelta(days=60),
            )
            db.session.add(ms)
            db.session.flush()

            # 1 MSX task per milestone
            task = MsxTask(
                msx_task_id=f"FAKE-TASK-{c.tpid}",
                subject=f"Technical review for {c.name}",
                task_category=1,
                task_category_name="Technical Engagement",
                milestone_id=ms.id,
                duration_minutes=60,
            )
            db.session.add(task)

        db.session.commit()
        print(f"\nDone! Created {len(customers)} fake customers with notes, "
              f"engagements, milestones, opportunities, and tasks.")
        print(f"Fake TPIDs: {FAKE_TPIDS}")
        print(f"\nTo test FY cutover:")
        print(f"  1. Go to Admin Panel → Fiscal Year Management")
        print(f"  2. Start a new FY (e.g. FY26)")
        print(f"  3. Finalize — the fake customers should get purged")


if __name__ == "__main__":
    seed()
