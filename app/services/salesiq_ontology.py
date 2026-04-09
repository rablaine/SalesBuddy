"""Domain ontology resources for Sales Buddy MCP server.

Provides structured domain knowledge that LLMs can read to understand
the Sales Buddy data model, terminology, and common query workflows.
"""

RESOURCES: list[dict] = [
    {
        'uri': 'salesbuddy://domain-model',
        'name': 'Sales Buddy Domain Model',
        'description': (
            'Entity relationships, organizational hierarchy, and data model '
            'for the Sales Buddy CRM. Read this to understand what data is '
            'available and how entities connect.'
        ),
        'content': """\
# Sales Buddy Domain Model

## How the Business Works

### Users and Roles

Sales Buddy is used by two types of Azure technical sellers:

- **Solution Engineers (SEs)** - Technical specialists with a specialty area \
(Data, AI, Infra, etc.). SEs work across ALL territories in their POD and ALL \
customers in those territories.
- **Sellers / Digital Solution Specialists (DSSs)** - Sales reps who own specific \
customers. Two sub-types:
  - **Growth sellers** - One territory, all customers in that territory with \
>$10k/mo Azure spend.
  - **Acquisition sellers** - Multiple territories in the POD, customers with \
<$10k/mo spend.

The database contains only the customers scoped to the current user. "My customers" \
means every customer in the database, regardless of role.

### The POD Structure

A POD (Practice Operating Division) is a working group of territories, sellers, and SEs:
- POD has many Territories
- Territory has many Customers
- Sellers cover one or more Territories
- SEs work all territories in the POD

### What Each Role Focuses On

SEs focus on milestones: Attachment Rate (on_my_team), HOK tasks (MsxTask), \
and U2C (uncommitted to committed). They create Engagements to group work \
before milestones exist in MSX.

Sellers/DSSs focus on opportunities. They create milestones and opportunities \
in MSX (Dynamics CRM).

### Engagements

SEs can't create milestones or opportunities in MSX, so Engagements are their \
grouping tool. An Engagement groups notes, action items, and work on a customer \
topic. Milestones and opportunities get linked later once they exist.

### Projects

Same grouping concept as Engagements but for non-customer work: building apps, \
training, webinars, achievements.

### Partners

Consulting firms introduced during the build phase. Once a customer is ready to \
build a solution, partners are brought in to implement.

## Entity Relationships

### Organizational
- POD has many Territories
- Territory has many Customers, many-to-many with Sellers
- Seller has many Customers (via seller_id)
- SolutionEngineer works across PODs/Territories

### Customer Domain
- Customer belongs to Seller (M:1) and Territory (M:1)
- Customer has many: Notes, Engagements, Milestones, Opportunities, Contacts
- Customer has one: MarketingSummary, many CustomerRevenueData (monthly by bucket)

### Engagement Domain
- Engagement belongs to Customer (required)
- Engagement has many: ActionItems, EngagementContacts
- Engagement many-to-many: Notes, Milestones, Opportunities

### MSX Domain
- Opportunity belongs to Customer, has many Milestones
- Milestone belongs to Customer and Opportunity
- Milestone has many: MsxTasks (HOK), MilestoneComments, MilestoneAudits
- Key fields: on_my_team, workload, monthly_usage, customer_commitment

### Notes Domain
- Note belongs to Customer (optional)
- Note has many: NoteAttendees, ActionItems
- Note many-to-many: Topics, Partners, Milestones, Opportunities, Engagements, Projects

### Action Items
- ActionItem belongs to Engagement or Project or Note
- Statuses: open, completed
- Properties: is_overdue (open + past due_date)
""",
    },
    {
        'uri': 'salesbuddy://glossary',
        'name': 'Sales Buddy Glossary',
        'description': (
            'Definitions of domain-specific terms used in Sales Buddy. '
            'Read this to understand acronyms and business terminology.'
        ),
        'content': """\
# Sales Buddy Glossary

| Term | Definition |
|------|-----------|
| SE | Solution Engineer - technical specialist. Metrics: Attachment Rate, HOK, U2C. |
| Seller / DSS | Digital Solution Specialist - the sales rep. Growth (1 territory, >$10k) or Acquisition (many territories, <$10k). |
| POD | Practice Operating Division - working group of territories, sellers, and SEs. |
| Territory | A grouping of customers. Belongs to a POD. |
| Milestone | MSX sales milestone. SEs join teams for Attachment Rate credit and create HOK tasks. |
| Opportunity | MSX sales opportunity. Created by sellers, contains milestones. |
| Engagement | SE's grouping of work on a customer topic. Groups notes, action items, milestones. |
| Project | Same as Engagement but for non-customer work (app dev, training, webinars). |
| Partner | Consulting firm for the build phase. Tracked with specialties and ratings. |
| Attachment Rate | SE metric - % of milestones where on_my_team = true. |
| HOK | Hands On Keyboard - SE metric from creating MsxTasks on team milestones. |
| U2C | Uncommitted to Committed - SE metric for moving milestones to committed status. |
| ACR | Azure Consumed Revenue - monthly Azure spend in dollars. |
| Note | Record of a call, internal chat, research, or work done. Rich text with attendees. |
| Topic | Technology tag on notes (e.g., Fabric, Cosmos DB, AVD). |
| Bucket | Revenue reporting category (e.g., Core DBs, Analytics, Modern DBs). |
| Workload | Azure technology workload of a milestone (MSX terminology). |
| CSAM | Customer Success Account Manager from MSX account team. |
| Connect | Microsoft self-evaluation process. Impact stories exported from Sales Buddy. |
| TPID | Top Parent ID - Microsoft's unique customer org identifier. |
| Seller Mode | UI filter scoping views to one seller's customers. Does not change DB contents. |
| Stale Customer | Customer whose TPID disappeared from latest MSX sync. |
| Hygiene | Data quality gaps - engagements without milestones, etc. |
| Whitespace | Revenue coverage gaps - tech buckets with no customer revenue. |
""",
    },
    {
        'uri': 'salesbuddy://workflows',
        'name': 'Sales Buddy Multi-Tool Workflows',
        'description': (
            'Common query patterns showing which tools to chain together '
            'for typical user requests.'
        ),
        'content': """\
# Common Multi-Tool Workflows

| User Intent | Tool Chain |
|------------|-----------|
| "How many customers/sellers/etc do I have?" | get_portfolio_overview |
| "Prep for a 1:1 with my manager" | report_one_on_one (optionally with seller_id) |
| "Deep dive on a customer" | search_customers -> get_customer_summary -> get_revenue_customer_detail |
| "What should I work on today?" | get_milestones_due_soon + list_action_items(overdue_only=true) |
| "My largest milestones" | get_milestone_status(on_my_team=true, sort_by='value', limit=N) - sorts by estimated monthly ACR |
| "Compare seller workloads" | list_sellers, then get_seller_workload for each |
| "Find a partner for Fabric work" | search_partners(specialty='Fabric') |
| "Revenue trending down?" | report_revenue_alerts(category='Declining') |
| "What have I been doing?" | report_whats_new + get_analytics_summary |
| "Hygiene check" | report_hygiene |
| "Whitespace in my territory" | report_whitespace |
| "Customer marketing engagement" | get_marketing_insights(customer_id=...) |
| "Who are my sellers?" | list_sellers |
| "Active engagements for customer X" | search_engagements(customer_id=..., status='Active') |
| "Show me all customers" | search_customers (no query, high limit) |
| "Recent notes" | search_notes(days=7) or search_notes(limit=10) |
""",
    },
]
