"""
Database models for Sales Buddy application.
All SQLAlchemy models and association tables.
"""
from datetime import datetime, timezone, date
from typing import Optional
from flask_sqlalchemy import SQLAlchemy

# This will be initialized by the app factory
db = SQLAlchemy()


def utc_now():
    """Return current UTC time with timezone info."""
    return datetime.now(timezone.utc)


def get_single_user():
    """Get the single default user for single-user mode."""
    return User.query.first()


# =============================================================================
# Association Tables
# =============================================================================

# Association table for many-to-many relationship between Note and Topic
notes_topics = db.Table(
    'notes_topics',
    db.Column('note_id', db.Integer, db.ForeignKey('notes.id'), primary_key=True),
    db.Column('topic_id', db.Integer, db.ForeignKey('topics.id'), primary_key=True)
)

# Association table for many-to-many relationship between Note and Partner
notes_partners = db.Table(
    'notes_partners',
    db.Column('note_id', db.Integer, db.ForeignKey('notes.id'), primary_key=True),
    db.Column('partner_id', db.Integer, db.ForeignKey('partners.id'), primary_key=True)
)

# Association table for many-to-many relationship between Note and Milestone
notes_milestones = db.Table(
    'notes_milestones',
    db.Column('note_id', db.Integer, db.ForeignKey('notes.id'), primary_key=True),
    db.Column('milestone_id', db.Integer, db.ForeignKey('milestones.id'), primary_key=True)
)

# Association table for many-to-many relationship between Note and Opportunity
notes_opportunities = db.Table(
    'notes_opportunities',
    db.Column('note_id', db.Integer, db.ForeignKey('notes.id'), primary_key=True),
    db.Column('opportunity_id', db.Integer, db.ForeignKey('opportunities.id'), primary_key=True)
)

# Association table for many-to-many relationship between Partner and Specialty
partners_specialties = db.Table(
    'partners_specialties',
    db.Column('partner_id', db.Integer, db.ForeignKey('partners.id'), primary_key=True),
    db.Column('specialty_id', db.Integer, db.ForeignKey('specialties.id'), primary_key=True)
)

# Association table for many-to-many relationship between Seller and Territory
sellers_territories = db.Table(
    'sellers_territories',
    db.Column('seller_id', db.Integer, db.ForeignKey('sellers.id'), primary_key=True),
    db.Column('territory_id', db.Integer, db.ForeignKey('territories.id'), primary_key=True)
)

# Association table for many-to-many relationship between Note and Engagement
notes_engagements = db.Table(
    'notes_engagements',
    db.Column('note_id', db.Integer, db.ForeignKey('notes.id'), primary_key=True),
    db.Column('engagement_id', db.Integer, db.ForeignKey('engagements.id'), primary_key=True)
)

# Association table for many-to-many relationship between Engagement and Opportunity
engagements_opportunities = db.Table(
    'engagements_opportunities',
    db.Column('engagement_id', db.Integer, db.ForeignKey('engagements.id'), primary_key=True),
    db.Column('opportunity_id', db.Integer, db.ForeignKey('opportunities.id'), primary_key=True)
)

# Association table for many-to-many relationship between Engagement and Milestone
engagements_milestones = db.Table(
    'engagements_milestones',
    db.Column('engagement_id', db.Integer, db.ForeignKey('engagements.id'), primary_key=True),
    db.Column('milestone_id', db.Integer, db.ForeignKey('milestones.id'), primary_key=True)
)

# Association table for many-to-many relationship between Customer and Vertical
customers_verticals = db.Table(
    'customers_verticals',
    db.Column('customer_id', db.Integer, db.ForeignKey('customers.id'), primary_key=True),
    db.Column('vertical_id', db.Integer, db.ForeignKey('verticals.id'), primary_key=True)
)

# Association table for many-to-many relationship between SolutionEngineer and POD
solution_engineers_pods = db.Table(
    'solution_engineers_pods',
    db.Column('solution_engineer_id', db.Integer, db.ForeignKey('solution_engineers.id'), primary_key=True),
    db.Column('pod_id', db.Integer, db.ForeignKey('pods.id'), primary_key=True)
)

# Association table for many-to-many relationship between SolutionEngineer and Territory
solution_engineers_territories = db.Table(
    'solution_engineers_territories',
    db.Column('solution_engineer_id', db.Integer, db.ForeignKey('solution_engineers.id'), primary_key=True),
    db.Column('territory_id', db.Integer, db.ForeignKey('territories.id'), primary_key=True)
)

# Association table for many-to-many relationship between Customer and CustomerCSAM
customers_csams = db.Table(
    'customers_csams',
    db.Column('customer_id', db.Integer, db.ForeignKey('customers.id'), primary_key=True),
    db.Column('csam_id', db.Integer, db.ForeignKey('customer_csams.id'), primary_key=True)
)


# =============================================================================
# User and Authentication Models
# =============================================================================

class User(db.Model):
    """User model for Entra ID (Azure AD) authentication.
    
    Supports multiple account types:
    - microsoft_azure_id: Corporate @microsoft.com account
    - external_azure_id: External tenant account (e.g., partner tenant)
    
    Users can log in with either account and will be associated with the same User record.
    Stub accounts are created when a new user attempts to link to an existing account.
    """
    __tablename__ = 'users'
    
    id = db.Column(db.Integer, primary_key=True)
    microsoft_azure_id = db.Column(db.String(255), unique=True, nullable=True)  # @microsoft.com Entra object ID
    external_azure_id = db.Column(db.String(255), unique=True, nullable=True)  # External tenant Entra object ID
    email = db.Column(db.String(255), nullable=False)  # Primary email (not necessarily unique if using both account types)
    microsoft_email = db.Column(db.String(255), nullable=True)  # Email from Microsoft account
    external_email = db.Column(db.String(255), nullable=True)  # Email from external account
    name = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)  # Admin flag for privileged users
    is_stub = db.Column(db.Boolean, default=False, nullable=False)  # True if this is a stub account awaiting linking
    linked_at = db.Column(db.DateTime, nullable=True)  # When the account linking was completed
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    last_login = db.Column(db.DateTime, default=utc_now, nullable=False)
    
    # User state properties (kept for compatibility)
    @property
    def is_authenticated(self):
        return True
    
    @property
    def is_active(self):
        return True
    
    @property
    def is_anonymous(self):
        return False
    
    def get_id(self):
        return str(self.id)
    
    @property
    def account_type(self) -> str:
        """Return account type: 'microsoft', 'external', or 'dual'."""
        has_microsoft = self.microsoft_azure_id is not None
        has_external = self.external_azure_id is not None
        
        if has_microsoft and has_external:
            return 'dual'
        elif has_microsoft:
            return 'microsoft'
        elif has_external:
            return 'external'
        return 'unknown'
    
    def __repr__(self) -> str:
        return f'<User {self.email}>'


# =============================================================================
# Organizational Structure Models
# =============================================================================

class POD(db.Model):
    """POD (Practice Operating Division) - organizational grouping of territories and personnel."""
    __tablename__ = 'pods'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    
    # Relationships
    territories = db.relationship('Territory', back_populates='pod', lazy='select')
    solution_engineers = db.relationship(
        'SolutionEngineer',
        secondary=solution_engineers_pods,
        back_populates='pods',
        lazy='select'
    )
    
    def __repr__(self) -> str:
        return f'<POD {self.name}>'


class SolutionEngineer(db.Model):
    """Solution Engineer (Azure Technical Seller) assigned to a POD with a specific specialty."""
    __tablename__ = 'solution_engineers'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    alias = db.Column(db.String(100), nullable=True)  # Microsoft email alias
    specialty = db.Column(db.String(50), nullable=True)  # Azure Data, Azure Core and Infra, Azure Apps and AI
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    
    # Relationships
    pods = db.relationship(
        'POD',
        secondary=solution_engineers_pods,
        back_populates='solution_engineers',
        lazy='select'
    )
    territories = db.relationship(
        'Territory',
        secondary=solution_engineers_territories,
        back_populates='solution_engineers',
        lazy='select'
    )
    
    def __repr__(self) -> str:
        return f'<SolutionEngineer {self.name} ({self.specialty})>'
    
    def get_email(self) -> Optional[str]:
        """Get email address from alias."""
        if self.alias:
            return f"{self.alias}@microsoft.com"
        return None


class TerritoryDSSSelection(db.Model):
    """Tracks which DSS is selected per territory per specialty.

    Similar to how CustomerCSAM tracks the user-selected primary CSAM for a
    customer, this records which Digital Solution Specialist the user has
    chosen for a given specialty within a territory.
    """
    __tablename__ = 'territory_dss_selections'

    id = db.Column(db.Integer, primary_key=True)
    territory_id = db.Column(db.Integer, db.ForeignKey('territories.id'), nullable=False)
    specialty = db.Column(db.String(100), nullable=False)
    solution_engineer_id = db.Column(db.Integer, db.ForeignKey('solution_engineers.id'), nullable=False)

    __table_args__ = (
        db.UniqueConstraint('territory_id', 'specialty', name='uq_territory_specialty'),
    )

    territory = db.relationship('Territory', back_populates='dss_selections')
    solution_engineer = db.relationship('SolutionEngineer')

    def __repr__(self) -> str:
        return f'<TerritoryDSSSelection territory={self.territory_id} specialty={self.specialty}>'


class Vertical(db.Model):
    """Industry vertical for customer classification."""
    __tablename__ = 'verticals'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    
    # Relationships
    customers = db.relationship(
        'Customer',
        secondary=customers_verticals,
        back_populates='verticals',
        lazy='select'
    )
    
    def __repr__(self) -> str:
        return f'<Vertical {self.name}>'


class Territory(db.Model):
    """Geographic or organizational territory for organizing customers and sellers."""
    __tablename__ = 'territories'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    pod_id = db.Column(db.Integer, db.ForeignKey('pods.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    
    # Relationships
    pod = db.relationship('POD', back_populates='territories')
    sellers = db.relationship(
        'Seller',
        secondary=sellers_territories,
        back_populates='territories',
        lazy='select'
    )
    customers = db.relationship('Customer', back_populates='territory', lazy='select')
    solution_engineers = db.relationship(
        'SolutionEngineer',
        secondary=solution_engineers_territories,
        back_populates='territories',
        lazy='select'
    )
    dss_selections = db.relationship(
        'TerritoryDSSSelection',
        back_populates='territory',
        lazy='select',
        cascade='all, delete-orphan'
    )
    
    def __repr__(self) -> str:
        return f'<Territory {self.name}>'


class Seller(db.Model):
    """Sales representative who can be assigned to customers and call logs."""
    __tablename__ = 'sellers'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    alias = db.Column(db.String(100), nullable=True)  # Microsoft email alias
    seller_type = db.Column(db.String(20), nullable=False, default='Growth')  # Acquisition or Growth
    # Note: territory_id column kept for backwards compatibility but will be deprecated
    territory_id = db.Column(db.Integer, db.ForeignKey('territories.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    
    # Relationships
    territories = db.relationship(
        'Territory',
        secondary=sellers_territories,
        back_populates='sellers',
        lazy='select'
    )
    customers = db.relationship('Customer', back_populates='seller', lazy='select')
    # Call logs can be accessed via Customer relationship
    
    def __repr__(self) -> str:
        return f'<Seller {self.name} ({self.seller_type})>'
    
    def get_email(self) -> Optional[str]:
        """Get email address from alias."""
        if self.alias:
            return f"{self.alias}@microsoft.com"
        return None


# =============================================================================
# Customer and Call Log Models
# =============================================================================

class CustomerCSAM(db.Model):
    """Customer Success Account Manager (CSAM) from MSX account team.
    
    Multiple CSAMs can be assigned to an account in MSX but there's no
    API-level way to determine which is primary. The user picks the correct
    one via a dropdown on the customer view page.
    """
    __tablename__ = 'customer_csams'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    alias = db.Column(db.String(100), nullable=True)  # Microsoft email alias
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)

    # Relationships — `customers` is the M2M of all customers linked to this CSAM from MSX,
    # while `selected_customers` is the 1:M of customers who picked this CSAM as primary.
    customers = db.relationship(
        'Customer',
        secondary='customers_csams',
        back_populates='available_csams',
        lazy='select'
    )
    selected_customers = db.relationship('Customer', back_populates='csam', foreign_keys='Customer.csam_id')

    def get_email(self) -> Optional[str]:
        """Get email address from alias."""
        if self.alias:
            return f"{self.alias}@microsoft.com"
        return None

    def __repr__(self) -> str:
        return f'<CustomerCSAM {self.name}>'


class Customer(db.Model):
    """Customer account that can be associated with call logs."""
    __tablename__ = 'customers'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    nickname = db.Column(db.String(200), nullable=True)
    tpid = db.Column(db.BigInteger, nullable=False, unique=True)
    tpid_url = db.Column(db.String(500), nullable=True)
    website = db.Column(db.String(500), nullable=True)  # Domain extracted from MSX websiteurl
    favicon_b64 = db.Column(db.Text, nullable=True)  # Base64-encoded 32x32 PNG favicon
    account_context = db.Column(db.Text, nullable=True)  # Persistent freeform account notes
    territory_id = db.Column(db.Integer, db.ForeignKey('territories.id'), nullable=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=True)
    dae_name = db.Column(db.String(200), nullable=True)  # DAE (account owner) display name from MSX
    dae_alias = db.Column(db.String(100), nullable=True)  # DAE email alias (part before @microsoft.com)
    csam_id = db.Column(db.Integer, db.ForeignKey('customer_csams.id'), nullable=True)  # User-selected primary CSAM
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)

    # Relationships
    seller = db.relationship('Seller', back_populates='customers')
    territory = db.relationship('Territory', back_populates='customers')
    csam = db.relationship('CustomerCSAM', back_populates='selected_customers', foreign_keys=[csam_id])
    available_csams = db.relationship(
        'CustomerCSAM',
        secondary='customers_csams',
        back_populates='customers',
        lazy='select'
    )
    notes = db.relationship('Note', back_populates='customer', lazy='select')
    engagements = db.relationship('Engagement', back_populates='customer', lazy='select',
                                  order_by='Engagement.created_at.desc()')
    verticals = db.relationship(
        'Vertical',
        secondary=customers_verticals,
        back_populates='customers',
        lazy='select'
    )
    contacts = db.relationship('CustomerContact', back_populates='customer', lazy='select',
                               order_by='CustomerContact.name')
    
    def __repr__(self) -> str:
        return f'<Customer {self.name} ({self.tpid})>'
    
    def get_most_recent_call_date(self) -> Optional[datetime]:
        """Get the date of the most recent call log for this customer."""
        if not self.notes:
            return None
        most_recent = max(self.notes, key=lambda x: x.call_date)
        return most_recent.call_date
    
    def get_display_name_with_tpid(self) -> str:
        """Get customer name with TPID for display."""
        return f"{self.name} ({self.tpid})"
    
    def get_display_name(self) -> str:
        """Get customer name for display, using nickname if available."""
        return self.nickname if self.nickname else self.name
    
    def get_account_type(self) -> str:
        """Get account type (Acquisition/Growth) from assigned seller."""
        if self.seller:
            return self.seller.seller_type
        return "Unknown"


class CustomerContact(db.Model):
    """Contact person at a customer organization."""
    __tablename__ = 'customer_contacts'

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(255), nullable=True)
    title = db.Column(db.String(200), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)

    # Relationships
    customer = db.relationship('Customer', back_populates='contacts')

    def __repr__(self) -> str:
        return f'<CustomerContact {self.name} at {self.customer.name if self.customer else "Unknown"}>'


class Topic(db.Model):
    """Topic/technology that can be tagged on call logs."""
    __tablename__ = 'topics'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    
    # Relationships
    notes = db.relationship(
        'Note',
        secondary=notes_topics,
        back_populates='topics',
        lazy='select'
    )
    
    def __repr__(self) -> str:
        return f'<Topic {self.name}>'


class Specialty(db.Model):
    """Specialty area that can be associated with partners."""
    __tablename__ = 'specialties'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    
    # Relationships
    partners = db.relationship(
        'Partner',
        secondary=partners_specialties,
        back_populates='specialties',
        lazy='select'
    )
    
    def __repr__(self) -> str:
        return f'<Specialty {self.name}>'


class Partner(db.Model):
    """Partner organization that can be associated with call logs."""
    __tablename__ = 'partners'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    overview = db.Column(db.Text, nullable=True)  # Text notes about the partner
    rating = db.Column(db.Integer, nullable=True)  # 0-5 star rating, null = not rated
    website = db.Column(db.String(255), nullable=True)  # Partner website domain (e.g., 'acme.com')
    favicon_b64 = db.Column(db.Text, nullable=True)  # Base64-encoded 32x32 PNG favicon
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now, nullable=False)
    
    # Relationships
    contacts = db.relationship('PartnerContact', back_populates='partner', lazy='select', cascade='all, delete-orphan')
    specialties = db.relationship(
        'Specialty',
        secondary=partners_specialties,
        back_populates='partners',
        lazy='select'
    )
    notes = db.relationship(
        'Note',
        secondary=notes_partners,
        back_populates='partners',
        lazy='select'
    )
    
    def get_primary_contact(self):
        """Get the primary contact if one is designated."""
        for contact in self.contacts:
            if contact.is_primary:
                return contact
        return self.contacts[0] if self.contacts else None
    
    def __repr__(self) -> str:
        return f'<Partner {self.name}>'


class PartnerContact(db.Model):
    """Contact person at a partner organization."""
    __tablename__ = 'partner_contacts'
    
    id = db.Column(db.Integer, primary_key=True)
    partner_id = db.Column(db.Integer, db.ForeignKey('partners.id'), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(255), nullable=True)
    is_primary = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    
    # Relationships
    partner = db.relationship('Partner', back_populates='contacts')
    
    def __repr__(self) -> str:
        return f'<PartnerContact {self.name} at {self.partner.name if self.partner else "Unknown"}>'


class Note(db.Model):
    """Call log entry with rich text content and associated metadata."""
    __tablename__ = 'notes'
    
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=True)
    # DateTime for full timestamp - date portion for display, time for meeting imports
    call_date = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now())
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now, nullable=False)
    
    # Relationships
    customer = db.relationship('Customer', back_populates='notes')
    topics = db.relationship(
        'Topic',
        secondary=notes_topics,
        back_populates='notes',
        lazy='select'
    )
    partners = db.relationship(
        'Partner',
        secondary=notes_partners,
        back_populates='notes',
        lazy='select'
    )
    milestones = db.relationship(
        'Milestone',
        secondary=notes_milestones,
        back_populates='notes',
        lazy='select'
    )
    opportunities = db.relationship(
        'Opportunity',
        secondary=notes_opportunities,
        back_populates='notes',
        lazy='select'
    )
    engagements = db.relationship(
        'Engagement',
        secondary=notes_engagements,
        back_populates='notes',
        lazy='select'
    )
    action_items = db.relationship('ActionItem', back_populates='note', lazy='select')
    
    @property
    def seller(self):
        """Get seller from customer relationship."""
        return self.customer.seller if self.customer else None
    
    @property
    def territory(self):
        """Get territory from customer relationship."""
        return self.customer.territory if self.customer else None
    
    def __repr__(self) -> str:
        name = self.customer.name if self.customer else 'General'
        return f'<Note {self.id} for {name}>'


class Engagement(db.Model):
    """Engagement thread representing a workstream or project with a customer.

    Captures the seller's narrative around an active effort using structured
    story fields (key individuals, problem, impact, solution, outcome, timeline).
    Links to notes, opportunities, and milestones for full context.
    """
    __tablename__ = 'engagements'

    STATUSES = ['Active', 'On Hold', 'Won', 'Lost']

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False)
    title = db.Column(db.String(300), nullable=False)
    status = db.Column(db.String(50), nullable=False, default='Active')

    # Story fields
    key_individuals = db.Column(db.Text, nullable=True)  # "I've been working with..."
    technical_problem = db.Column(db.Text, nullable=True)  # "...they have run into..."
    business_impact = db.Column(db.Text, nullable=True)  # "...and it's impacting..."
    solution_resources = db.Column(db.Text, nullable=True)  # "We are addressing the Opportunity with..."
    estimated_acr = db.Column(db.Integer, nullable=True)  # Monthly ACR in dollars
    target_date = db.Column(db.Date, nullable=True)  # "...by..."

    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    # Relationships
    customer = db.relationship('Customer', back_populates='engagements')
    notes = db.relationship(
        'Note',
        secondary=notes_engagements,
        back_populates='engagements',
        lazy='select'
    )
    opportunities = db.relationship(
        'Opportunity',
        secondary=engagements_opportunities,
        backref=db.backref('engagements', lazy='select'),
        lazy='select'
    )
    milestones = db.relationship(
        'Milestone',
        secondary=engagements_milestones,
        backref=db.backref('engagements', lazy='select'),
        lazy='select'
    )
    action_items = db.relationship(
        'ActionItem',
        back_populates='engagement',
        lazy='select',
        cascade='all, delete-orphan',
        order_by='ActionItem.sort_order, ActionItem.created_at'
    )

    @property
    def story_completeness(self) -> int:
        """Return percentage of story fields filled (0-100)."""
        fields = [
            self.key_individuals, self.technical_problem, self.business_impact,
            self.solution_resources, self.estimated_acr, self.target_date
        ]
        filled = sum(1 for f in fields if f)
        return int((filled / len(fields)) * 100)

    @property
    def linked_note_count(self) -> int:
        """Return count of linked notes."""
        return len(self.notes)

    @property
    def open_action_item_count(self) -> int:
        """Return count of open (not completed) action items."""
        return sum(1 for t in self.action_items if t.status == 'open')

    def __repr__(self) -> str:
        return f'<Engagement {self.id}: {self.title[:50]}>'


class ActionItem(db.Model):
    """Action item tracked against an engagement.

    Action items can optionally link back to the note they were created from.
    Description supports rich HTML (Quill editor with image paste).
    """
    __tablename__ = 'action_items'

    STATUSES = ['open', 'completed']
    PRIORITIES = ['normal', 'high']

    id = db.Column(db.Integer, primary_key=True)
    engagement_id = db.Column(db.Integer, db.ForeignKey('engagements.id'), nullable=False)
    note_id = db.Column(db.Integer, db.ForeignKey('notes.id'), nullable=True)
    title = db.Column(db.String(300), nullable=False)
    description = db.Column(db.Text, nullable=True)  # Rich HTML from Quill
    due_date = db.Column(db.Date, nullable=True)
    contact = db.Column(db.String(200), nullable=True)  # Point of contact
    status = db.Column(db.String(20), nullable=False, default='open')
    priority = db.Column(db.String(20), nullable=False, default='normal')
    completed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0)

    # Relationships
    engagement = db.relationship('Engagement', back_populates='action_items')
    note = db.relationship('Note', back_populates='action_items')

    @property
    def is_overdue(self) -> bool:
        """Return True if task is open and past due date."""
        if self.status == 'completed' or not self.due_date:
            return False
        return self.due_date < date.today()

    def __repr__(self) -> str:
        return f'<ActionItem {self.id}: {self.title[:50]}>'


class Opportunity(db.Model):
    """Opportunity from MSX that groups milestones under a customer."""
    __tablename__ = 'opportunities'
    
    id = db.Column(db.Integer, primary_key=True)
    
    # MSX identifiers
    msx_opportunity_id = db.Column(db.String(50), nullable=False, unique=True)  # GUID from MSX
    opportunity_number = db.Column(db.String(50), nullable=True)  # e.g., "7-3FU4Q45URI"
    name = db.Column(db.String(500), nullable=False)
    
    # MSX details (cached on read from the MSX API)
    statecode = db.Column(db.Integer, nullable=True)  # 0=Open, 1=Won, 2=Lost
    state = db.Column(db.String(50), nullable=True)  # Formatted: "Open", "Won", "Lost"
    status_reason = db.Column(db.String(100), nullable=True)  # Detailed status: "In Progress", etc.
    estimated_value = db.Column(db.Float, nullable=True)  # Dollar value
    estimated_close_date = db.Column(db.String(30), nullable=True)  # ISO date string from MSX
    owner_name = db.Column(db.String(200), nullable=True)  # Opportunity owner display name
    compete_threat = db.Column(db.String(100), nullable=True)  # Compete threat level
    customer_need = db.Column(db.Text, nullable=True)  # Customer need description
    description = db.Column(db.Text, nullable=True)  # Opportunity description
    msx_url = db.Column(db.String(500), nullable=True)  # Direct link to MSX record
    cached_comments_json = db.Column(db.Text, nullable=True)  # Forecast comments cached as JSON string
    details_fetched_at = db.Column(db.DateTime, nullable=True)  # When MSX details were last cached
    
    # Relationships
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now, nullable=False)
    
    # Team membership
    on_deal_team = db.Column(db.Boolean, default=False, nullable=False, server_default='0')  # Am I on the opportunity deal team?
    
    # Relationships
    customer = db.relationship('Customer', backref=db.backref('opportunities', lazy='dynamic'))
    milestones = db.relationship('Milestone', back_populates='opportunity', lazy='dynamic')
    notes = db.relationship(
        'Note',
        secondary=notes_opportunities,
        back_populates='opportunities',
        lazy='select'
    )
    
    def __repr__(self) -> str:
        return f'<Opportunity {self.id}: {self.name[:50]}>'


class Milestone(db.Model):
    """Milestone from MSX sales platform that can be linked to call logs."""
    __tablename__ = 'milestones'
    
    id = db.Column(db.Integer, primary_key=True)
    
    # MSX identifiers
    msx_milestone_id = db.Column(db.String(50), nullable=True, unique=True)  # GUID from MSX
    milestone_number = db.Column(db.String(50), nullable=True)  # e.g., "7-503412553"
    url = db.Column(db.String(2000), nullable=False)
    
    # Milestone details from MSX
    title = db.Column(db.String(500), nullable=True)  # msp_name from MSX
    msx_status = db.Column(db.String(50), nullable=True)  # "On Track", "Cancelled", etc.
    msx_status_code = db.Column(db.Integer, nullable=True)  # Numeric status code
    opportunity_name = db.Column(db.String(500), nullable=True)  # Parent opportunity name (legacy, kept for compat)
    
    # Milestone tracker fields (populated from MSX sync)
    due_date = db.Column(db.DateTime, nullable=True)  # Target completion date from MSX
    dollar_value = db.Column(db.Float, nullable=True)  # Estimated revenue/value from MSX
    workload = db.Column(db.String(200), nullable=True)  # Workload name from MSX
    monthly_usage = db.Column(db.Float, nullable=True)  # Monthly usage amount from MSX
    last_synced_at = db.Column(db.DateTime, nullable=True)  # Last time synced from MSX
    on_my_team = db.Column(db.Boolean, default=False, nullable=False, server_default='0')  # Am I on the milestone access team?
    cached_comments_json = db.Column(db.Text, nullable=True)  # MSX forecast comments cached as JSON
    details_fetched_at = db.Column(db.DateTime, nullable=True)  # When MSX details were last fetched
    
    # Relationships
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=True)
    opportunity_id = db.Column(db.Integer, db.ForeignKey('opportunities.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now, nullable=False)
    
    # Relationships
    customer = db.relationship('Customer', backref=db.backref('milestones', lazy='dynamic'))
    opportunity = db.relationship('Opportunity', back_populates='milestones')
    notes = db.relationship(
        'Note',
        secondary=notes_milestones,
        back_populates='milestones',
        lazy='select'
    )
    tasks = db.relationship('MsxTask', back_populates='milestone', lazy='dynamic')
    
    @property
    def display_text(self):
        """Return title if set, otherwise milestone number or default."""
        if self.title:
            return self.title
        if self.milestone_number:
            return self.milestone_number
        return 'View in MSX'
    
    @property
    def status_sort_order(self):
        """Return sort order for status (lower = more important)."""
        status_order = {
            'On Track': 1,
            'At Risk': 2,
            'Blocked': 3,
            'Completed': 4,
            'Cancelled': 5,
            'Lost to Competitor': 6,
            'Hygiene/Duplicate': 7,
        }
        return status_order.get(self.msx_status, 99)
    
    @property
    def is_active(self) -> bool:
        """Return True if milestone is in an active (uncommitted) status."""
        active_statuses = {'On Track', 'At Risk', 'Blocked'}
        return self.msx_status in active_statuses
    
    @property
    def due_date_urgency(self) -> str:
        """Return urgency level based on due date: 'past_due', 'this_week', 'this_month', 'future', 'no_date'."""
        if not self.due_date:
            return 'no_date'
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        due = self.due_date if self.due_date.tzinfo else self.due_date.replace(tzinfo=timezone.utc)
        days_until = (due - now).days
        if days_until < 0:
            return 'past_due'
        elif days_until <= 7:
            return 'this_week'
        elif days_until <= 30:
            return 'this_month'
        else:
            return 'future'
    
    def __repr__(self) -> str:
        return f'<Milestone {self.id}: {self.title or self.milestone_number or self.url[:50]}>'


class MsxTask(db.Model):
    """Task created in MSX linked to a milestone and call log."""
    __tablename__ = 'msx_tasks'
    
    id = db.Column(db.Integer, primary_key=True)
    
    # MSX identifiers
    msx_task_id = db.Column(db.String(50), nullable=False, unique=True)  # GUID from MSX
    msx_task_url = db.Column(db.String(2000), nullable=True)
    
    # Task details
    subject = db.Column(db.String(500), nullable=False)
    description = db.Column(db.Text, nullable=True)
    task_category = db.Column(db.Integer, nullable=False)  # Numeric code
    task_category_name = db.Column(db.String(100), nullable=True)  # Display name
    duration_minutes = db.Column(db.Integer, default=60, nullable=False)
    is_hok = db.Column(db.Boolean, default=False, nullable=False)  # Is this a HOK task?
    due_date = db.Column(db.DateTime, nullable=True)  # scheduledend from MSX
    
    # Relationships
    note_id = db.Column(db.Integer, db.ForeignKey('notes.id'), nullable=True)
    milestone_id = db.Column(db.Integer, db.ForeignKey('milestones.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    
    # Relationships
    note = db.relationship('Note', backref=db.backref('msx_tasks', lazy='dynamic'))
    milestone = db.relationship('Milestone', back_populates='tasks')
    
    def __repr__(self) -> str:
        return f'<MsxTask {self.id}: {self.subject[:50]}>'


class MilestoneComment(db.Model):
    """Comment on a milestone, either auto-generated from note/engagement updates or manual."""
    __tablename__ = 'milestone_comments'

    id = db.Column(db.Integer, primary_key=True)
    milestone_id = db.Column(db.Integer, db.ForeignKey('milestones.id'), nullable=False)
    content = db.Column(db.Text, nullable=False)
    source_type = db.Column(db.String(20), nullable=False, default='manual')  # 'note', 'engagement', 'manual'
    source_id = db.Column(db.Integer, nullable=True)  # note.id or engagement.id (null for manual)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    # Relationships
    milestone = db.relationship(
        'Milestone',
        backref=db.backref('comments', lazy='select', cascade='all, delete-orphan')
    )

    def __repr__(self) -> str:
        return f'<MilestoneComment {self.id} on milestone {self.milestone_id} ({self.source_type})>'


# =============================================================================
# User Preferences Model
# =============================================================================

class UserPreference(db.Model):
    """User preferences including dark mode and customer view settings."""
    __tablename__ = 'user_preferences'
    
    id = db.Column(db.Integer, primary_key=True)
    dark_mode = db.Column(db.Boolean, default=True, nullable=False)
    customer_view_grouped = db.Column(db.Boolean, default=False, nullable=False)
    customer_sort_by = db.Column(db.String(20), default='alphabetical', nullable=False)  # 'alphabetical', 'grouped', or 'by_calls'
    topic_sort_by_calls = db.Column(db.Boolean, default=False, nullable=False)
    territory_view_accounts = db.Column(db.Boolean, default=False, nullable=False)  # False = recent calls, True = accounts
    show_customers_without_calls = db.Column(db.Boolean, default=True, nullable=False)  # False = hide customers with no calls, True = show all customers
    first_run_modal_dismissed = db.Column(db.Boolean, default=False, nullable=False)  # Track if welcome modal has been dismissed
    guided_tour_completed = db.Column(db.Boolean, default=False, nullable=False)  # Track if product tour has been shown
    dismissed_update_commit = db.Column(db.String(7), nullable=True)  # Last dismissed update commit hash (short)
    workiq_summary_prompt = db.Column(db.Text, nullable=True)  # Custom WorkIQ meeting summary prompt (null = use default)
    workiq_connect_impact = db.Column(db.Boolean, default=True, nullable=False)  # Extract Connect impact signals from WorkIQ summaries
    ai_enabled = db.Column(db.Boolean, default=False, nullable=False)  # True once user explicitly grants AI gateway consent
    default_template_customer_id = db.Column(db.Integer, db.ForeignKey('note_templates.id', ondelete='SET NULL'), nullable=True)  # Default template for customer notes
    default_template_noncustomer_id = db.Column(db.Integer, db.ForeignKey('note_templates.id', ondelete='SET NULL'), nullable=True)  # Default template for non-customer notes
    fy_transition_active = db.Column(db.Boolean, default=False, nullable=False)  # True during FY changeover period
    fy_transition_label = db.Column(db.String(10), nullable=True)  # e.g. "FY27"
    fy_transition_started = db.Column(db.DateTime, nullable=True)  # When FY transition was started
    fy_sync_complete = db.Column(db.Boolean, default=False, nullable=False)  # True after FY account sync finishes
    fy_last_completed = db.Column(db.String(10), nullable=True)  # e.g. "FY27" - set when finalization completes
    onedrive_path = db.Column(db.String(500), nullable=True)  # Cached OneDrive for Business root path
    backup_retention_daily = db.Column(db.Integer, default=7, nullable=False)  # Number of daily backups to keep
    backup_retention_weekly = db.Column(db.Integer, default=4, nullable=False)  # Number of weekly backups to keep
    backup_retention_monthly = db.Column(db.Integer, default=3, nullable=False)  # Number of monthly backups to keep
    user_role = db.Column(db.String(10), nullable=True)  # 'se' or 'dss' - null until set during onboarding
    my_seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=True)  # DSS's own Seller record
    my_seller_alias = db.Column(db.String(100), nullable=True)  # DSS's az login alias (persisted for offline use)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now, nullable=False)
    
    # Relationships
    default_template_customer = db.relationship('NoteTemplate', foreign_keys=[default_template_customer_id])
    default_template_noncustomer = db.relationship('NoteTemplate', foreign_keys=[default_template_noncustomer_id])
    my_seller = db.relationship('Seller', foreign_keys=[my_seller_id])
    
    def __repr__(self) -> str:
        return f'<UserPreference dark_mode={self.dark_mode} customer_view_grouped={self.customer_view_grouped} customer_sort_by={self.customer_sort_by} topic_sort_by_calls={self.topic_sort_by_calls} territory_view_accounts={self.territory_view_accounts} show_customers_without_calls={self.show_customers_without_calls} first_run_modal_dismissed={self.first_run_modal_dismissed}>'


# =============================================================================
# Note Templates Model
# =============================================================================

class NoteTemplate(db.Model):
    """Reusable template for pre-filling note content in the Quill editor."""
    __tablename__ = 'note_templates'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, nullable=False)  # Stored as HTML (Quill output)
    is_builtin = db.Column(db.Boolean, default=False, nullable=False)  # Seed templates
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    def __repr__(self) -> str:
        return f'<NoteTemplate {self.id}: {self.name}>'


# =============================================================================
# AI Features Models
# =============================================================================

class AIQueryLog(db.Model):
    """Audit log of all AI API calls for debugging and prompt improvement."""
    __tablename__ = 'ai_query_log'
    
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=utc_now, nullable=False)
    request_text = db.Column(db.Text, nullable=False)
    response_text = db.Column(db.Text, nullable=True)
    success = db.Column(db.Boolean, nullable=False)
    error_message = db.Column(db.Text, nullable=True)
    
    # Token and model tracking
    model = db.Column(db.String(100), nullable=True)
    prompt_tokens = db.Column(db.Integer, nullable=True)
    completion_tokens = db.Column(db.Integer, nullable=True)
    total_tokens = db.Column(db.Integer, nullable=True)
    
    def __repr__(self) -> str:
        status = 'success' if self.success else 'failed'
        return f'<AIQueryLog {status} at {self.timestamp}>'


# =============================================================================
# Revenue Integration Models
# =============================================================================

class RevenueImport(db.Model):
    """Tracks each revenue data import from MSXI CSV."""
    __tablename__ = 'revenue_imports'
    
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(500), nullable=False)
    imported_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    
    # Stats about this import
    record_count = db.Column(db.Integer, nullable=False, default=0)  # Customer/bucket rows in CSV
    new_months_added = db.Column(db.Integer, default=0)  # New month columns we hadn't seen
    records_updated = db.Column(db.Integer, default=0)  # Existing records updated
    records_created = db.Column(db.Integer, default=0)  # New records created
    
    # Month range in this import
    earliest_month = db.Column(db.Date, nullable=True)
    latest_month = db.Column(db.Date, nullable=True)
    
    # Relationships
    data_points = db.relationship('CustomerRevenueData', back_populates='last_import', lazy='select')
    
    def __repr__(self) -> str:
        return f'<RevenueImport {self.filename} at {self.imported_at}>'


class CustomerRevenueData(db.Model):
    """Monthly revenue data point for a customer/bucket combination.
    
    This is the RAW DATA layer - stores every customer's monthly revenue
    whether they're flagged for engagement or not. Accumulates over imports.
    """
    __tablename__ = 'customer_revenue_data'
    
    id = db.Column(db.Integer, primary_key=True)
    
    # Customer identification (from CSV)
    customer_name = db.Column(db.String(500), nullable=False, index=True)
    tpid = db.Column(db.String(50), nullable=True, index=True)
    seller_name = db.Column(db.String(200), nullable=True)  # From territory alignment in CSV
    bucket = db.Column(db.String(50), nullable=False)  # e.g., Analytics, Core DBs
    
    # Link to Sales Buddy customer (nullable until matched)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=True, index=True)
    
    # Month identifier
    fiscal_month = db.Column(db.String(20), nullable=False)  # e.g., "FY26-Jan" for display
    month_date = db.Column(db.Date, nullable=False, index=True)  # First of month, for sorting
    
    # Revenue value
    revenue = db.Column(db.Float, nullable=False, default=0.0)
    
    # Tracking
    first_imported_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    last_updated_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    last_import_id = db.Column(db.Integer, db.ForeignKey('revenue_imports.id'), nullable=False)
    
    # Relationships
    last_import = db.relationship('RevenueImport', back_populates='data_points')
    customer = db.relationship('Customer', backref='revenue_data_points')
    
    # Unique constraint: one record per customer/bucket/month
    __table_args__ = (
        db.UniqueConstraint('customer_name', 'bucket', 'month_date', name='uq_customer_bucket_month'),
        db.Index('ix_revenue_data_lookup', 'customer_name', 'bucket'),
    )
    
    def __repr__(self) -> str:
        return f'<CustomerRevenueData {self.customer_name} {self.bucket} {self.fiscal_month}: ${self.revenue:,.0f}>'


class ProductRevenueData(db.Model):
    """Monthly revenue data for a specific product within a customer/bucket.
    
    This provides drill-down capability - when a bucket is flagged for attention,
    you can see which specific products are driving the revenue changes.
    """
    __tablename__ = 'product_revenue_data'
    
    id = db.Column(db.Integer, primary_key=True)
    
    # Customer/bucket identification
    customer_name = db.Column(db.String(500), nullable=False, index=True)
    bucket = db.Column(db.String(50), nullable=False)  # e.g., Analytics, Core DBs
    product = db.Column(db.String(200), nullable=False, index=True)  # ServiceLevel4 from CSV
    
    # Link to Sales Buddy customer (nullable until matched)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=True, index=True)
    
    # Month identifier
    fiscal_month = db.Column(db.String(20), nullable=False)
    month_date = db.Column(db.Date, nullable=False, index=True)
    
    # Revenue value
    revenue = db.Column(db.Float, nullable=False, default=0.0)
    
    # Tracking
    first_imported_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    last_updated_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    last_import_id = db.Column(db.Integer, db.ForeignKey('revenue_imports.id'), nullable=False)
    
    # Relationships
    last_import = db.relationship('RevenueImport', backref='product_data_points')
    customer = db.relationship('Customer', backref='product_revenue_data_points')
    
    # Unique constraint: one record per customer/bucket/product/month
    __table_args__ = (
        db.UniqueConstraint('customer_name', 'bucket', 'product', 'month_date', name='uq_customer_bucket_product_month'),
        db.Index('ix_product_revenue_lookup', 'customer_name', 'bucket', 'product'),
        db.Index('ix_product_name', 'product'),
    )
    
    def __repr__(self) -> str:
        return f'<ProductRevenueData {self.customer_name} {self.bucket}/{self.product} {self.fiscal_month}: ${self.revenue:,.0f}>'


class RevenueAnalysis(db.Model):
    """Computed analysis for a customer/bucket - regenerated on demand or after import.
    
    This is the ANALYSIS layer - computed from CustomerRevenueData.
    Can be regenerated anytime from the raw data.
    """
    __tablename__ = 'revenue_analyses'
    
    id = db.Column(db.Integer, primary_key=True)
    
    # Link to customer (by name for unmatched, by ID when matched)
    customer_name = db.Column(db.String(500), nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=True, index=True)
    tpid = db.Column(db.String(50), nullable=True)
    seller_name = db.Column(db.String(200), nullable=True)
    bucket = db.Column(db.String(50), nullable=False)
    
    # When was this analysis computed?
    analyzed_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    months_analyzed = db.Column(db.Integer, nullable=False)  # How many months of data used
    
    # Revenue summary
    avg_revenue = db.Column(db.Float, nullable=False)
    latest_revenue = db.Column(db.Float, nullable=False)  # Most recent final month
    
    # Analysis results
    category = db.Column(db.String(50), nullable=False)  # CHURN_RISK, RECENT_DIP, etc.
    recommended_action = db.Column(db.String(50), nullable=False)  # CHECK-IN (Urgent), etc.
    confidence = db.Column(db.String(20), nullable=False)  # LOW, MEDIUM, HIGH
    priority_score = db.Column(db.Integer, nullable=False)  # 0-100
    
    # Dollar impact
    dollars_at_risk = db.Column(db.Float, default=0.0)
    dollars_opportunity = db.Column(db.Float, default=0.0)
    
    # Statistical signals
    trend_slope = db.Column(db.Float, default=0.0)  # %/month
    last_month_change = db.Column(db.Float, default=0.0)
    last_2month_change = db.Column(db.Float, default=0.0)
    volatility_cv = db.Column(db.Float, default=0.0)
    max_drawdown = db.Column(db.Float, default=0.0)
    current_vs_max = db.Column(db.Float, default=0.0)
    current_vs_avg = db.Column(db.Float, default=0.0)
    
    # Engagement rationale (plain English)
    engagement_rationale = db.Column(db.Text, nullable=True)
    
    # For change tracking - previous analysis values
    previous_category = db.Column(db.String(50), nullable=True)
    previous_priority_score = db.Column(db.Integer, nullable=True)
    status_changed_at = db.Column(db.DateTime, nullable=True)
    
    # Review/actioning status (user marks alerts as reviewed/actioned)
    review_status = db.Column(db.String(20), default='new', nullable=False)
    # new = not yet reviewed
    # to_be_reviewed = flagged for review (triaged but not yet looked at)
    # reviewed = acknowledged, no action needed (explained away)
    # actioned = follow-up action taken or in progress
    # dismissed = false positive / not relevant
    review_notes = db.Column(db.Text, nullable=True)  # Why it was marked this way
    reviewed_at = db.Column(db.DateTime, nullable=True)  # When status was last changed
    
    # Previous review snapshot (set when auto-reset triggers re-review)
    previous_review_status = db.Column(db.String(20), nullable=True)
    previous_review_notes = db.Column(db.Text, nullable=True)
    
    # Relationships
    customer = db.relationship('Customer', backref='revenue_analyses')
    engagements = db.relationship('RevenueEngagement', back_populates='analysis', lazy='select')
    
    # Unique constraint: one active analysis per customer/bucket
    __table_args__ = (
        db.UniqueConstraint('customer_name', 'bucket', name='uq_analysis_customer_bucket'),
    )
    
    def __repr__(self) -> str:
        return f'<RevenueAnalysis {self.customer_name} {self.bucket}: {self.category} ({self.priority_score})>'


class RevenueConfig(db.Model):
    """User-configurable thresholds for revenue analysis."""
    __tablename__ = 'revenue_config'
    
    id = db.Column(db.Integer, primary_key=True)
    
    # Revenue gates
    min_revenue_for_outreach = db.Column(db.Integer, default=3000)
    min_dollar_impact = db.Column(db.Integer, default=1000)
    dollar_at_risk_override = db.Column(db.Integer, default=2000)
    dollar_opportunity_override = db.Column(db.Integer, default=1500)
    
    # Revenue tiers
    high_value_threshold = db.Column(db.Integer, default=25000)
    strategic_threshold = db.Column(db.Integer, default=50000)
    
    # Category thresholds
    volatile_min_revenue = db.Column(db.Integer, default=5000)
    recent_drop_threshold = db.Column(db.Float, default=-0.15)  # -15%
    expansion_growth_threshold = db.Column(db.Float, default=0.08)  # 8%
    
    def __repr__(self) -> str:
        return f'<RevenueConfig id={self.id}>'


class RevenueEngagement(db.Model):
    """Tracks follow-up on a specific revenue recommendation.
    
    Created when you export/send recommendations to a seller.
    Each time a customer is flagged and sent out = one engagement record.
    Allows tracking history: "Flagged 3 times, here's what we did each time."
    """
    __tablename__ = 'revenue_engagements'
    
    id = db.Column(db.Integer, primary_key=True)
    analysis_id = db.Column(db.Integer, db.ForeignKey('revenue_analyses.id'), nullable=False)
    
    # When was this sent out for follow-up?
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    assigned_to_seller = db.Column(db.String(200), nullable=True)  # Seller name from analysis
    
    # What was the recommendation when sent? (snapshot in case analysis updates)
    category_when_sent = db.Column(db.String(50), nullable=False)
    action_when_sent = db.Column(db.String(50), nullable=False)
    rationale_when_sent = db.Column(db.Text, nullable=True)
    
    # Tracking status
    status = db.Column(db.String(20), default='pending', nullable=False)
    # pending = sent out, awaiting response
    # in_progress = seller acknowledged, working on it
    # resolved = issue addressed
    # dismissed = not actionable / false positive
    
    # What did the seller report back?
    seller_response = db.Column(db.Text, nullable=True)
    response_date = db.Column(db.DateTime, nullable=True)
    
    # Your notes on resolution
    resolution_notes = db.Column(db.Text, nullable=True)
    resolved_at = db.Column(db.DateTime, nullable=True)
    
    # Optional link to a call log if one was created from this engagement
    note_id = db.Column(db.Integer, db.ForeignKey('notes.id'), nullable=True)
    
    # Relationships
    analysis = db.relationship('RevenueAnalysis', back_populates='engagements')
    note = db.relationship('Note', backref='revenue_engagement')
    
    def __repr__(self) -> str:
        return f'<RevenueEngagement {self.id} for analysis {self.analysis_id}: {self.status}>'


# =============================================================================
# Sync Status Tracking
# =============================================================================

class SyncStatus(db.Model):
    """Tracks sync start/completion times to detect interrupted syncs.
    
    A sync is considered successfully completed only when completed_at >= started_at
    and success is True. If the user reloads mid-sync, started_at will be set but
    completed_at will be null, correctly indicating the sync didn't finish.
    """
    __tablename__ = 'sync_status'
    
    id = db.Column(db.Integer, primary_key=True)
    sync_type = db.Column(db.String(50), unique=True, nullable=False)  # 'milestones', 'accounts', etc.
    started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    heartbeat_at = db.Column(db.DateTime, nullable=True)
    success = db.Column(db.Boolean, nullable=True)
    items_synced = db.Column(db.Integer, nullable=True)
    details = db.Column(db.Text, nullable=True)  # JSON string for extra stats
    
    # A heartbeat within this many seconds means the sync is actively running
    HEARTBEAT_ALIVE_SECONDS = 60
    
    @classmethod
    def is_complete(cls, sync_type: str) -> bool:
        """Check if the given sync type has completed successfully."""
        status = cls.query.filter_by(sync_type=sync_type).first()
        if not status:
            return False
        return (
            status.success is True
            and status.completed_at is not None
            and status.started_at is not None
            and status.completed_at >= status.started_at
        )

    @classmethod
    def get_status(cls, sync_type: str) -> dict:
        """Get detailed status info for a sync type.

        Returns:
            Dict with keys:
                state: 'never_run' | 'in_progress' | 'failed' | 'incomplete' | 'complete'
                started_at: datetime or None
                completed_at: datetime or None
                items_synced: int or None
                details: str or None
        """
        status = cls.query.filter_by(sync_type=sync_type).first()
        if not status or status.started_at is None:
            return {'state': 'never_run', 'started_at': None,
                    'completed_at': None, 'items_synced': None, 'details': None}

        base = {
            'started_at': status.started_at,
            'completed_at': status.completed_at,
            'items_synced': status.items_synced,
            'details': status.details,
        }

        # Started but never completed — check heartbeat to distinguish running vs crashed
        if status.completed_at is None:
            if status.heartbeat_at is not None:
                now = utc_now().replace(tzinfo=None)
                hb = (status.heartbeat_at.replace(tzinfo=None)
                      if status.heartbeat_at.tzinfo else status.heartbeat_at)
                if (now - hb).total_seconds() < cls.HEARTBEAT_ALIVE_SECONDS:
                    base['state'] = 'in_progress'
                    return base
            base['state'] = 'incomplete'
            return base

        # Completed but failed
        if not status.success:
            base['state'] = 'failed'
            return base

        # Completed successfully
        if status.completed_at >= status.started_at:
            base['state'] = 'complete'
            return base

        # Edge case: completed_at < started_at (shouldn't happen)
        base['state'] = 'incomplete'
        return base
    
    @classmethod
    def reset(cls, sync_type: str) -> None:
        """Delete sync status for the given type, returning it to 'never_run' state."""
        cls.query.filter_by(sync_type=sync_type).delete()
        db.session.flush()

    @classmethod
    def mark_started(cls, sync_type: str) -> 'SyncStatus':
        """Mark a sync as started (clears previous completion)."""
        status = cls.query.filter_by(sync_type=sync_type).first()
        if not status:
            status = cls(sync_type=sync_type)
            db.session.add(status)
        status.started_at = utc_now()
        status.completed_at = None
        status.heartbeat_at = None
        status.success = None
        status.items_synced = None
        status.details = None
        db.session.commit()
        return status
    
    @classmethod
    def mark_completed(cls, sync_type: str, success: bool,
                       items_synced: int = None, details: str = None) -> 'SyncStatus':
        """Mark a sync as completed."""
        status = cls.query.filter_by(sync_type=sync_type).first()
        if not status:
            status = cls(sync_type=sync_type, started_at=utc_now())
            db.session.add(status)
        status.completed_at = utc_now()
        status.success = success
        status.items_synced = items_synced
        status.details = details
        db.session.commit()
        return status

    @classmethod
    def update_heartbeat(cls, sync_type: str) -> None:
        """Update the heartbeat timestamp to signal the sync is still running."""
        status = cls.query.filter_by(sync_type=sync_type).first()
        if status:
            status.heartbeat_at = utc_now()
            db.session.commit()
    
    def __repr__(self) -> str:
        return f'<SyncStatus {self.sync_type} success={self.success}>'


class ConnectExport(db.Model):
    """Record of a Connect self-evaluation export for tracking date ranges."""
    __tablename__ = 'connect_exports'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    note_count = db.Column(db.Integer, nullable=False, default=0)
    customer_count = db.Column(db.Integer, nullable=False, default=0)
    ai_summary = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)

    def __repr__(self) -> str:
        return f'<ConnectExport {self.name} ({self.start_date} to {self.end_date})>'


# =============================================================================
# Usage Telemetry
# =============================================================================

class UsageEvent(db.Model):
    """Local telemetry log for tracking feature usage and API calls.

    Stores one row per HTTP request. No PII is recorded -- no IP addresses,
    user-agents, session IDs, or usernames. The ``referrer_path`` field keeps
    only the URL *path* (query string stripped) so we can correlate an API call
    back to the page/feature that triggered it.
    """
    __tablename__ = 'usage_events'

    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=utc_now, nullable=False, index=True)

    # Request info
    method = db.Column(db.String(10), nullable=False)          # GET, POST, DELETE, ...
    endpoint = db.Column(db.String(500), nullable=False)       # URL path (no query string)
    blueprint = db.Column(db.String(100), nullable=True)       # Flask blueprint name
    view_function = db.Column(db.String(200), nullable=True)   # Flask endpoint/view name
    is_api = db.Column(db.Boolean, nullable=False, default=False)  # True for /api/* routes

    # Response info
    status_code = db.Column(db.Integer, nullable=False)
    response_time_ms = db.Column(db.Float, nullable=True)      # Wall-clock ms

    # Feature context -- where the user was when they triggered this call
    referrer_path = db.Column(db.String(500), nullable=True)   # Path-only from Referer header

    # Error details (non-PII)
    error_type = db.Column(db.String(200), nullable=True)      # Exception class name
    error_message = db.Column(db.Text, nullable=True)          # Sanitized error message

    # Metadata
    category = db.Column(db.String(50), nullable=True)         # Derived category for grouping

    def __repr__(self) -> str:
        return f'<UsageEvent {self.method} {self.endpoint} {self.status_code}>'


class DailyFeatureStats(db.Model):
    """Aggregated daily feature usage statistics.

    One row per (date, category, endpoint, method) combination. Produced by
    rolling up raw ``UsageEvent`` rows so we can track long-term feature
    popularity without keeping every individual request forever.

    No PII is stored -- just counts and averages.
    """
    __tablename__ = 'daily_feature_stats'
    __table_args__ = (
        db.UniqueConstraint('date', 'category', 'endpoint', 'method', name='uq_daily_feature'),
    )

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, index=True)

    # Feature identification
    category = db.Column(db.String(50), nullable=False, index=True)
    endpoint = db.Column(db.String(500), nullable=False)
    method = db.Column(db.String(10), nullable=False)
    is_api = db.Column(db.Boolean, nullable=False, default=False)

    # Aggregated metrics
    event_count = db.Column(db.Integer, nullable=False, default=0)
    error_count = db.Column(db.Integer, nullable=False, default=0)
    avg_response_ms = db.Column(db.Float, nullable=True)
    unique_referrers = db.Column(db.Integer, nullable=False, default=0)

    def __repr__(self) -> str:
        return f'<DailyFeatureStats {self.date} {self.category} {self.endpoint} ({self.event_count})>'