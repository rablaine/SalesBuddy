"""
Diagnostic script: Hit the MSX audit API with a real milestone GUID
and dump the raw response so we can see the actual data format.

Usage:
    python scripts/test_audit_api.py
    python scripts/test_audit_api.py <milestone_guid>
"""
import sys
import os
import json

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from app.models import Milestone, db


def get_sample_guids(limit: int = 5) -> list:
    """Get a few milestone GUIDs to test with."""
    milestones = (
        Milestone.query
        .filter(Milestone.on_my_team == True, Milestone.msx_milestone_id.isnot(None))
        .limit(limit)
        .all()
    )
    return [(m.msx_milestone_id, m.title, m.msx_status, m.customer_commitment) for m in milestones]


def fetch_and_dump(guid: str):
    """Call the audit API and dump everything we get back."""
    from app.services.msx_api import get_milestone_audit_history

    print(f"\n{'='*80}")
    print(f"Fetching audit history for: {guid}")
    print(f"{'='*80}")

    result = get_milestone_audit_history(guid)

    print(f"\nSuccess: {result.get('success')}")
    print(f"Count: {result.get('count', 'N/A')}")

    if not result.get('success'):
        print(f"Error: {result.get('error')}")
        return result

    records = result.get('records', [])
    if not records:
        print("No audit records returned.")
        return result

    # Dump first record fully to see all fields
    print(f"\n--- First record (full JSON) ---")
    print(json.dumps(records[0], indent=2, default=str))

    # Show all unique keys across records
    all_keys = set()
    for r in records:
        all_keys.update(r.keys())
    print(f"\n--- All keys across {len(records)} records ---")
    print(sorted(all_keys))

    # Show changedata for first few records
    print(f"\n--- changedata samples (first 5 records) ---")
    for i, record in enumerate(records[:5]):
        changedata = record.get('changedata', '')
        createdon = record.get('createdon', '?')
        action = record.get('action', '?')
        operation = record.get('operation', '?')
        print(f"\n  Record {i} | createdon={createdon} | action={action} | operation={operation}")
        print(f"  changedata type: {type(changedata).__name__}")
        if changedata:
            # Print raw, truncated if huge
            cd_str = str(changedata)
            if len(cd_str) > 2000:
                print(f"  changedata (first 2000 chars):\n{cd_str[:2000]}...")
            else:
                print(f"  changedata:\n{cd_str}")
        else:
            print("  changedata: (empty)")

    # Specifically look for status/commitment changes
    print(f"\n--- Scanning all {len(records)} records for status/commitment fields ---")
    status_keywords = [
        'statuscode', 'msdyn_status', 'msdyn_customercommitment',
        'statecode', 'status', 'commitment', 'completed', 'committed',
        'On Track', 'At Risk', 'Blocked', 'Completed', 'Cancelled',
    ]
    for i, record in enumerate(records):
        cd = str(record.get('changedata', ''))
        matches = [kw for kw in status_keywords if kw.lower() in cd.lower()]
        if matches:
            print(f"\n  Record {i} | {record.get('createdon')} | matched: {matches}")
            if len(cd) > 3000:
                print(f"  changedata (first 3000 chars):\n{cd[:3000]}...")
            else:
                print(f"  changedata:\n{cd}")

    return result


def main():
    app = create_app()
    with app.app_context():
        if len(sys.argv) > 1:
            # Use provided GUID
            guid = sys.argv[1]
            print(f"Using provided GUID: {guid}")
            fetch_and_dump(guid)
        else:
            # Find some milestones to test with
            guids = get_sample_guids(5)
            if not guids:
                print("No on_my_team milestones with MSX GUIDs found in database.")
                return

            print(f"Found {len(guids)} milestones to test:")
            for guid, title, status, commitment in guids:
                print(f"  {guid} | {status} | {commitment} | {title}")

            # Test the first one
            first_guid = guids[0][0]
            result = fetch_and_dump(first_guid)

            # If first one had records, we're done. Otherwise try more.
            if result.get('count', 0) == 0 and len(guids) > 1:
                print("\nFirst milestone had no audit records, trying others...")
                for guid, title, status, commitment in guids[1:]:
                    result = fetch_and_dump(guid)
                    if result.get('count', 0) > 0:
                        break

        # Also try one that we know should have status changes
        # (Committed or Completed milestones)
        print(f"\n\n{'#'*80}")
        print("Looking for milestones with Committed or Completed status...")
        print(f"{'#'*80}")
        interesting = (
            Milestone.query
            .filter(
                Milestone.on_my_team == True,
                Milestone.msx_milestone_id.isnot(None),
                db.or_(
                    Milestone.customer_commitment == 'Committed',
                    Milestone.msx_status == 'Completed',
                )
            )
            .limit(3)
            .all()
        )
        if interesting:
            for m in interesting:
                print(f"\n  {m.msx_milestone_id} | {m.msx_status} | {m.customer_commitment} | {m.title}")
                fetch_and_dump(m.msx_milestone_id)
        else:
            print("No Committed/Completed milestones found on team.")


if __name__ == '__main__':
    main()
