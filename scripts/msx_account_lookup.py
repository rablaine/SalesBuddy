"""Look up accounts associated with a seller via MSX Account Access Team."""
import sys
import os
import json
import requests
import urllib.parse

# Ensure project root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.msx_auth import get_msx_token
from app.services.msx_api import _get_headers
from app import create_app

EMAIL = sys.argv[1] if len(sys.argv) > 1 else "remar@microsoft.com"

app = create_app()
with app.app_context():
    token = get_msx_token()
    if not token:
        print("ERROR: No MSX token. Run az login first.")
        sys.exit(1)
    headers = _get_headers(token)

    # Step 1: Resolve email to systemuserid
    print(f"Looking up systemuserid for {EMAIL}...")
    
    # Try internalemailaddress first, then domainname
    user = None
    for field in ("internalemailaddress", "domainname"):
        resp = requests.get(
            "https://microsoftsales.crm.dynamics.com/api/data/v9.0/systemusers",
            headers=headers,
            params={
                "$filter": f"{field} eq '{EMAIL}'",
                "$select": "systemuserid,fullname,domainname,internalemailaddress",
            },
            timeout=30,
        )
        if resp.status_code == 200:
            users = resp.json().get("value", [])
            if users:
                user = users[0]
                break
    
    if not user:
        # Try startswith on domainname as fallback
        alias = EMAIL.split("@")[0]
        resp = requests.get(
            "https://microsoftsales.crm.dynamics.com/api/data/v9.0/systemusers",
            headers=headers,
            params={
                "$filter": f"startswith(domainname,'{alias}')",
                "$select": "systemuserid,fullname,domainname,internalemailaddress",
                "$top": "5",
            },
            timeout=30,
        )
        if resp.status_code == 200:
            users = resp.json().get("value", [])
            if users:
                if len(users) == 1:
                    user = users[0]
                else:
                    print(f"Multiple matches for alias '{alias}':")
                    for u in users:
                        print(f"  {u['fullname']} | {u['domainname']} | {u['internalemailaddress']}")
                    print("Be more specific.")
                    sys.exit(1)
    
    if not user:
        print("No user found.")
        sys.exit(1)

    user = users[0]
    user_id = user["systemuserid"]
    print(f"Found: {user['fullname']} ({user['domainname']}) - {user_id}")

    # Step 2: FetchXML query for accounts where this user is on the Access Team
    fetchxml = f"""<fetch version="1.0" mapping="logical" distinct="true"
        returntotalrecordcount="true" page="1" count="5000" no-lock="true">
      <entity name="account">
        <attribute name="name"/>
        <attribute name="msp_accountnumber"/>
        <attribute name="accountid"/>
        <attribute name="address1_city"/>
        <attribute name="address1_composite"/>
        <attribute name="ownerid"/>
        <attribute name="msp_endcustomersegmentcode"/>
        <attribute name="msp_endcustomersubsegmentcode"/>
        <attribute name="msp_verticalcode"/>
        <attribute name="msp_subverticalcode"/>
        <attribute name="msp_verticalcategorycode"/>
        <attribute name="msp_parentinglevelcode"/>
        <attribute name="msp_mstopparentid"/>
        <attribute name="msp_hq"/>
        <attribute name="msp_gpid"/>
        <attribute name="msp_gpname"/>
        <attribute name="statecode"/>
        <order attribute="name" descending="false"/>
        <filter type="and">
          <condition attribute="statecode" operator="eq" value="0"/>
        </filter>
        <link-entity name="territory" from="territoryid" to="territoryid"
            link-type="outer" alias="terr">
          <attribute name="msp_accountteamunitname"/>
        </link-entity>
        <link-entity name="team" from="regardingobjectid" to="accountid"
            link-type="inner" alias="ac">
          <filter type="and">
            <condition attribute="teamtype" operator="eq" value="1"/>
            <condition attribute="teamtemplateid" operator="eq"
                value="{{3FCC1CFC-3E43-E311-9405-00155DB3BA1E}}"/>
          </filter>
          <link-entity name="teammembership" from="teamid" to="teamid"
              intersect="true">
            <link-entity name="systemuser" from="systemuserid" to="systemuserid"
                alias="aa">
              <filter type="and">
                <condition attribute="systemuserid" operator="eq"
                    value="{{{user_id}}}"/>
              </filter>
            </link-entity>
          </link-entity>
        </link-entity>
      </entity>
    </fetch>"""

    print(f"\nQuerying accounts for {user['fullname']}...")
    resp = requests.get(
        "https://microsoftsales.crm.dynamics.com/api/data/v9.0/accounts",
        headers=headers,
        params={"fetchXml": fetchxml},
        timeout=60,
    )
    if resp.status_code != 200:
        print(f"Account query failed ({resp.status_code}): {resp.text[:1000]}")
        sys.exit(1)

    data = resp.json()
    total = data.get("@Microsoft.Dynamics.CRM.totalrecordcount", "?")
    records = data.get("value", [])
    print(f"Total accounts: {total}")
    print(f"Records returned: {len(records)}")
    print()

    for r in records[:10]:
        name = r.get("name", "?")
        tpid = r.get("msp_accountnumber", "?")
        city = r.get("address1_city", "?")
        owner = r.get("_ownerid_value@OData.Community.Display.V1.FormattedValue", "?")
        seg = r.get("msp_endcustomersegmentcode@OData.Community.Display.V1.FormattedValue", "?")
        atu = r.get("terr.msp_accountteamunitname", "?")
        print(f"  {name} | TPID: {tpid} | City: {city} | Owner: {owner} | Seg: {seg} | ATU: {atu}")

    if len(records) > 10:
        print(f"  ... and {len(records) - 10} more")
