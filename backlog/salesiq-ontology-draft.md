# SalesIQ Domain Ontology - Draft

This is what the MCP resource `salesbuddy://domain-model` would contain. Review and tell me what's missing or wrong.

---

## How the Business Works

### Users and Roles

Sales Buddy is used by two types of Azure technical sellers:

- **Solution Engineers (SEs)** - Technical specialists with a specialty area (Data, AI, Infra, etc.). SEs work across ALL territories in their POD and ALL customers in those territories.
- **Sellers / Digital Solution Specialists (DSSs)** - Sales reps who own specific customers. Two sub-types:
  - **Growth sellers** - One territory, all customers in that territory with >$10k/mo Azure spend.
  - **Acquisition sellers** - Multiple territories in the POD, customers with <$10k/mo spend.

The database has a `user_role` value (in UserPreference) that identifies whether the current user is an SE or DSS. The database contains only the customers properly scoped to the user - "my customers" means every customer in the database, regardless of role.

### The POD Structure

A **POD** (Practice Operating Division) is a working group of territories, sellers, and SEs:

```
POD
 ├── has many → Territory
 │    └── has many → Customer
 ├── has many → Seller/DSS (via territories)
 │    ├── Growth: one territory, high-spend customers
 │    └── Acquisition: multiple territories, lower-spend customers
 └── has many → SolutionEngineer (works all territories in the POD)
```

### What Each Role Focuses On

**SEs focus on milestones.** They:
- Add themselves to milestone teams to get **Attachment Rate / Pipeline Influenced** credit
- Create **tasks (HOK)** on milestones where they're on the team to get **Hands On Keyboard** credit
- Move milestones to committed status for **U2C (Uncommitted to Committed)** credit
- Sometimes look at opportunities but milestones are the primary unit of work

**Sellers/DSSs focus on opportunities.** They:
- Create milestones and opportunities in MSX to track their customer engagements
- Less focused on milestones, more on the opportunity pipeline

### Engagements - Why They Exist

SEs don't create milestones or opportunities in MSX - sellers do. But SEs need a way to group their work on a customer topic before there's a relevant milestone or opportunity to tie it to.

**Engagements** solve this. An SE creates an engagement for each topic they're working on with a customer. The engagement groups:
- Call notes from meetings about that topic
- Action items and tasks
- Eventually, milestones and opportunities get linked to the engagement once they exist

This means notes, milestones, and action items all get grouped together. From there, it's easy to keep milestone comments updated and create HOK tasks as needed.

An engagement has a "story" - the technical problem, business impact, solution resources, target date, and estimated ACR. This narrative captures what the SE is actually doing.

### Projects - Non-Customer Work

Projects are the same grouping concept as engagements but for non-customer work: building this app, creating training materials, prepping for and delivering webinars, achievements, etc.

### Partners - The Build Phase

Once a DSS/SE combo gets a customer to a point where they're ready to build a solution, they introduce **partners** - consulting firms with experience building solutions in customer environments. Multiple partners may be presented so the customer can choose.

The partner directory tracks:
- Which partners we work with
- What they specialize in (Fabric, Cosmos DB, etc.)
- Who our contacts are at each partner
- How good a job they did (ratings)

---

## Entities and Relationships

### Organizational Hierarchy

```
POD (Practice Operating Division)
 └── has many → Territory
      ├── has many → Customer (assigned via territory_id)
      └── many-to-many → Seller/DSS (covers one or more territories)
                          └── has many → Customer (assigned via seller_id)

SolutionEngineer (SE)
 ├── many-to-many → POD
 ├── many-to-many → Territory
 └── selected per territory+specialty via → TerritoryDSSSelection
```

### Customer Domain

```
Customer
 ├── belongs to → Seller/DSS (M:1, the assigned seller)
 ├── belongs to → Territory (M:1)
 ├── has one selected → CustomerCSAM (M:1, primary CSAM)
 ├── many-to-many → CustomerCSAM (all CSAMs from MSX account team)
 ├── many-to-many → Vertical (industry classifications)
 ├── has many → CustomerContact (people at the customer org)
 ├── has many → Note (notes about interactions, research, work done)
 ├── has many → Engagement (active workstreams)
 ├── has many → Milestone (MSX milestones)
 ├── has many → Opportunity (MSX opportunities)
 ├── has one → MarketingSummary (marketing engagement rollup)
 ├── has many → MarketingInteraction (per-sales-play marketing)
 ├── has many → MarketingContact (per-contact marketing)
 └── has many → CustomerRevenueData (monthly revenue by bucket)
```

### Engagement Domain

```
Engagement (SE's grouping of work on a customer topic)
 ├── belongs to → Customer (M:1, required)
 ├── has many → ActionItem (tasks, to-dos)
 ├── has many → EngagementContact (polymorphic attendees)
 ├── many-to-many → Note (linked call notes)
 ├── many-to-many → Milestone (linked once they exist in MSX)
 ├── many-to-many → Opportunity (linked once they exist in MSX)
 └── has many → PartnerRecommendation (AI partner suggestions)

Statuses: Active, On Hold, Won, Lost
Story fields: key_individuals, technical_problem, business_impact,
              solution_resources, estimated_acr, target_date
```

### MSX Domain (Dynamics CRM)

```
Opportunity (created by sellers in MSX)
 ├── belongs to → Customer (M:1, optional)
 ├── has many → Milestone
 ├── many-to-many → Note
 └── many-to-many → Engagement

Milestone (tracked by SEs for attachment, HOK, and U2C credit)
 ├── belongs to → Customer (M:1, optional)
 ├── belongs to → Opportunity (M:1, optional)
 ├── has many → MsxTask (HOK tasks created by SEs)
 ├── has many → MilestoneComment (manual or auto-generated)
 ├── has many → MilestoneAudit (change history from Dataverse)
 ├── many-to-many → Note
 └── many-to-many → Engagement

Key fields: on_my_team (SE attachment), dollar_value, monthly_usage,
            workload, due_date, customer_commitment
Statuses: On Track, At Risk, Blocked, Won, Lost, Expired
```

### Notes Domain

```
Note (record of a call, internal chat, research, or work done)
 ├── belongs to → Customer (M:1, optional)
 ├── has many → NoteAttendee (polymorphic attendees)
 ├── has many → ActionItem (tasks from the note)
 ├── many-to-many → Topic (technology tags)
 ├── many-to-many → Partner
 ├── many-to-many → Milestone
 ├── many-to-many → Opportunity
 ├── many-to-many → Engagement
 └── many-to-many → Project
```

### Project Domain

```
Project (non-customer work: app dev, training, webinars, achievements)
 ├── has many → ActionItem
 ├── many-to-many → Note
 Statuses: Active, On Hold, Completed
 Types: general, copilot_saved, training, achievement
```

### Action Items

```
ActionItem (task/to-do)
 ├── belongs to → Engagement (M:1, optional)
 ├── belongs to → Project (M:1, optional)
 ├── belongs to → Note (M:1, optional - source note)
 Statuses: open, completed
 Priorities: normal, high
 Sources: engagement, copilot, project
 Properties: is_overdue (open + past due_date)
```

### Partner Domain

```
Partner (consulting firm for the build phase)
 ├── has many → PartnerContact
 ├── many-to-many → Specialty (areas of expertise)
 └── many-to-many → Note (linked call notes)
```

### Revenue Domain

```
CustomerRevenueData (monthly revenue per customer per bucket)
 ├── belongs to → Customer (M:1)
 └── belongs to → RevenueImport (M:1, which CSV import)

ProductRevenueData (monthly revenue per product within bucket)
 ├── belongs to → Customer (M:1)

RevenueAnalysis (computed trends and alerts)
 ├── belongs to → Customer (M:1)
 └── has many → RevenueEngagement (follow-up tracking)

Categories: Declining, Expanding, Stable, Volatile, New, Lost
Actions: Check-In, Upsell, Investigate, Monitor, Celebrate, Win-back
```

### People (Polymorphic Contacts)

```
NoteAttendee and EngagementContact share the same polymorphic pattern.
Each row points to ONE of:
 - CustomerContact (person at customer org)
 - PartnerContact (person at partner org)
 - SolutionEngineer (SE)
 - Seller/DSS
 - InternalContact (other Microsoft person - DAE, DSE, etc.)
 - External (freeform name + email, not in any table)
```

### Marketing Domain

```
MarketingSummary (account-level marketing rollup from MSX)
 └── belongs to → Customer (1:1)

MarketingInteraction (per-sales-play breakdown)
 └── belongs to → Customer (M:1)

MarketingContact (per-contact marketing engagement)
 └── belongs to → Customer (M:1)
```

### Administrative/System

```
UserPreference - Single-row settings (user_role, dark mode, sort prefs, sync config)
NoteTemplate - Reusable note content templates
Favorite - Polymorphic favorites (milestones, engagements, opportunities)
HygieneNote - Free-text notes on hygiene report items
SyncStatus - Tracks MSX sync lifecycle
AIQueryLog - Audit log of AI API calls
ConnectExport - Connect self-evaluation export records
U2CSnapshot / U2CSnapshotItem - Fiscal quarter milestone snapshots
UsageEvent / DailyFeatureStats - Telemetry
```

---

## Glossary

| Term | Meaning in Sales Buddy |
|------|----------------------|
| **Solution Engineer (SE)** | Technical specialist with a specialty area (Data, AI, Infra, etc.). Works across ALL territories in their POD and ALL customers in those territories. Primary metrics: Attachment Rate, HOK, U2C. |
| **Seller / DSS** | Digital Solution Specialist - the sales rep. Growth sellers have one territory with high-spend customers (>$10k/mo). Acquisition sellers have multiple territories with lower-spend customers (<$10k/mo). Creates milestones and opportunities in MSX. |
| **POD** | Practice Operating Division - a working group of territories, sellers, and SEs. |
| **Territory** | A grouping of customers. Belongs to a POD. Growth sellers get one, Acquisition sellers get multiple. |
| **Milestone** | An MSX (Dynamics CRM) sales milestone tracked against an opportunity. NOT a project checkpoint. SEs add themselves to milestone teams for Attachment Rate credit and create tasks for HOK credit. Has dollar value, monthly ACR, workload, and status. |
| **Opportunity** | An MSX (Dynamics CRM) sales opportunity. Created by sellers. Contains multiple milestones. Sellers focus on these, SEs less so. |
| **Engagement** | A grouping concept created by SEs (who can't create milestones/opportunities in MSX) to organize notes, action items, and work on a specific customer topic. Milestones and opportunities get linked later once they exist. NOT an email or meeting. |
| **Project** | Same grouping concept as Engagement but for non-customer work: building apps, training, webinars, achievements. |
| **Partner** | A consulting firm introduced during the build phase. Once the SE/DSS get a customer ready to build, partners are brought in to implement. Tracked with specialties, contacts, and ratings. |
| **Attachment Rate** | SE metric - percentage of milestones where the SE is on the team (on_my_team = true). |
| **HOK (Hands On Keyboard)** | SE metric - creating tasks on milestones where they're on the team to prove technical involvement. |
| **U2C (Uncommitted to Committed)** | SE metric - moving milestones to committed status. Measured per fiscal quarter. |
| **ACR** | Azure Consumed Revenue - the dollar amount of Azure services a customer uses monthly. |
| **Note** | A record of a call, internal chat, research done, or other work related to a customer or engagement. Usually a call note, but not always. Rich text with attendees, linked to topics/engagements/milestones. |
| **Topic** | A technology tag (e.g., "Fabric", "Cosmos DB", "AVD") applied to notes. |
| **Bucket** | A revenue reporting category (e.g., "Core DBs", "Analytics", "Modern DBs"). |
| **Workload** | The Azure technology workload of a milestone (MSX's terminology, similar to bucket). |
| **CSAM** | Customer Success Account Manager - from MSX account team. |
| **DAE** | Digital Account Executive - internal Microsoft contact role. |
| **Connect** | Microsoft's self-evaluation process - impact stories exported from Sales Buddy. |
| **TPID** | Top Parent ID - Microsoft's unique identifier for a customer organization. |
| **Copilot** | AI-generated action items and suggestions from WorkIQ integration. Source is "copilot" on ActionItems. |
| **Seller Mode** | An optional UI filter that scopes views to a single seller's customers. Does NOT change what's in the database - just narrows the view. |
| **Stale Customer** | A customer whose TPID disappeared from the latest MSX sync - may have been reassigned. |
| **Hygiene** | Data quality gaps - engagements without milestones, milestones without engagements, etc. |
| **Whitespace** | Revenue coverage gaps - tech buckets where a customer has no revenue but could. |

---

## Common Multi-Tool Workflows

| Intent | Tool Chain |
|--------|-----------|
| "Prep for a 1:1 with my manager" | `report_one_on_one` (optionally with `seller_id`) |
| "Deep dive on a customer" | `search_customers` → `get_customer_summary` → `get_revenue_customer_detail` |
| "What should I work on today?" | `get_milestones_due_soon` + `list_action_items(overdue_only=true)` |
| "Compare seller workloads" | `get_seller_workload` for each seller |
| "Find a partner for Fabric work" | `search_partners(specialty='Fabric')` |
| "Revenue trending down?" | `report_revenue_alerts(category='Declining')` |
| "What have I been doing?" | `report_whats_new` + `get_analytics_summary` |
| "Hygiene check" | `report_hygiene` |
| "Whitespace in my territory" | `report_whitespace` |
| "Customer marketing engagement" | `report_marketing_insights(customer_id=...)` |
| "How many X do I have?" | `get_portfolio_overview` (NEW - not yet built) |
| "Who are my sellers?" | `list_sellers` (NEW - not yet built) |
| "Active engagements for customer X" | `search_engagements(customer_id=..., status='Active')` (NEW - not yet built) |
