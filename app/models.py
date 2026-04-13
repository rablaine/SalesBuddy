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

# Association table for many-to-many relationship between Note and Project
notes_projects = db.Table(
    'notes_projects',
    db.Column('note_id', db.Integer, db.ForeignKey('notes.id'), primary_key=True),
    db.Column('project_id', db.Integer, db.ForeignKey('projects.id'), primary_key=True)
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
    """Sales representative who can be assigned to customers and notes."""
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
    # Notes can be accessed via Customer relationship
    
    def __repr__(self) -> str:
        return f'<Seller {self.name} ({self.seller_type})>'
    
    def get_email(self) -> Optional[str]:
        """Get email address from alias."""
        if self.alias:
            return f"{self.alias}@microsoft.com"
        return None


# =============================================================================
# Customer and Note Models
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
    """Customer account that can be associated with notes."""
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
    stale_since = db.Column(db.DateTime, nullable=True)  # Set when TPID disappears from MSX sync

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
        """Get the date of the most recent note for this customer."""
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
    photo_b64 = db.Column(db.Text, nullable=True)  # Base64-encoded cropped face thumbnail
    photo_full_b64 = db.Column(db.Text, nullable=True)  # Base64-encoded full pasted image
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)

    # Relationships
    customer = db.relationship('Customer', back_populates='contacts')

    def __repr__(self) -> str:
        return f'<CustomerContact {self.name} at {self.customer.name if self.customer else "Unknown"}>'


class Topic(db.Model):
    """Topic/technology that can be tagged on notes."""
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
    """Partner organization that can be associated with notes."""
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
    title = db.Column(db.String(200), nullable=True)
    email = db.Column(db.String(255), nullable=True)
    is_primary = db.Column(db.Boolean, default=False, nullable=False)
    photo_b64 = db.Column(db.Text, nullable=True)  # Base64-encoded cropped face thumbnail
    photo_full_b64 = db.Column(db.Text, nullable=True)  # Base64-encoded full pasted image
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    
    # Relationships
    partner = db.relationship('Partner', back_populates='contacts')
    
    def __repr__(self) -> str:
        return f'<PartnerContact {self.name} at {self.partner.name if self.partner else "Unknown"}>'


class Note(db.Model):
    """Note entry with rich text content and associated metadata."""
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
    projects = db.relationship(
        'Project',
        secondary=notes_projects,
        back_populates='notes',
        lazy='select'
    )
    action_items = db.relationship('ActionItem', back_populates='note', lazy='select')
    attendees = db.relationship('NoteAttendee', back_populates='note', lazy='select',
                                cascade='all, delete-orphan')
    
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


class InternalContact(db.Model):
    """Microsoft internal contact not tracked as a Seller or Solution Engineer.

    Covers DAEs, DSS, DSEs from other solution areas, and other Microsoft
    employees who show up in meetings but aren't in the user's pod.
    Reusable across notes (unlike external attendees which are per-note).
    """
    __tablename__ = 'internal_contacts'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    alias = db.Column(db.String(100), nullable=True, unique=True)
    role = db.Column(db.String(100), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)

    def get_email(self) -> Optional[str]:
        """Return full email from alias."""
        return f'{self.alias}@microsoft.com' if self.alias else None

    def __repr__(self) -> str:
        return f'<InternalContact {self.name} ({self.role or "unknown"})>'


class NoteAttendee(db.Model):
    """Person who attended a call. Polymorphic - links to one person type."""
    __tablename__ = 'note_attendees'

    id = db.Column(db.Integer, primary_key=True)
    note_id = db.Column(db.Integer, db.ForeignKey('notes.id'), nullable=False)

    # Exactly one of these should be set (or none for external)
    customer_contact_id = db.Column(db.Integer, db.ForeignKey('customer_contacts.id'), nullable=True)
    partner_contact_id = db.Column(db.Integer, db.ForeignKey('partner_contacts.id'), nullable=True)
    solution_engineer_id = db.Column(db.Integer, db.ForeignKey('solution_engineers.id'), nullable=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=True)
    internal_contact_id = db.Column(db.Integer, db.ForeignKey('internal_contacts.id'), nullable=True)

    # For ad-hoc attendees not in any contact list
    external_name = db.Column(db.String(200), nullable=True)
    external_email = db.Column(db.String(255), nullable=True)

    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)

    # Relationships
    note = db.relationship('Note', back_populates='attendees')
    customer_contact = db.relationship('CustomerContact', lazy='select')
    partner_contact = db.relationship('PartnerContact', lazy='select')
    solution_engineer = db.relationship('SolutionEngineer', lazy='select')
    seller = db.relationship('Seller', lazy='select')
    internal_contact = db.relationship('InternalContact', lazy='select')

    @property
    def person_type(self) -> str:
        """Return the type of person this attendee represents."""
        if self.customer_contact_id:
            return 'customer_contact'
        elif self.partner_contact_id:
            return 'partner_contact'
        elif self.solution_engineer_id:
            return 'se'
        elif self.seller_id:
            return 'seller'
        elif self.internal_contact_id:
            return 'internal_contact'
        return 'external'

    @property
    def display_name(self) -> str:
        """Return the display name for this attendee."""
        if self.customer_contact_id and self.customer_contact:
            return self.customer_contact.name
        elif self.partner_contact_id and self.partner_contact:
            return self.partner_contact.name
        elif self.solution_engineer_id and self.solution_engineer:
            return self.solution_engineer.name
        elif self.seller_id and self.seller:
            return self.seller.name
        elif self.internal_contact_id and self.internal_contact:
            return self.internal_contact.name
        return self.external_name or 'Unknown'

    @property
    def email(self) -> Optional[str]:
        """Return the email for this attendee."""
        if self.customer_contact_id and self.customer_contact:
            return self.customer_contact.email
        elif self.partner_contact_id and self.partner_contact:
            return self.partner_contact.email
        elif self.solution_engineer_id and self.solution_engineer:
            return self.solution_engineer.get_email()
        elif self.seller_id and self.seller:
            return self.seller.get_email()
        elif self.internal_contact_id and self.internal_contact:
            return self.internal_contact.get_email()
        return self.external_email

    @property
    def ref_id(self) -> Optional[int]:
        """Return the FK id for this attendee's person type."""
        return (self.customer_contact_id or self.partner_contact_id
                or self.solution_engineer_id or self.seller_id
                or self.internal_contact_id)

    def to_dict(self) -> dict:
        """Serialize for API responses."""
        return {
            'id': self.id,
            'person_type': self.person_type,
            'display_name': self.display_name,
            'email': self.email,
            'ref_id': self.ref_id,
        }

    def __repr__(self) -> str:
        return f'<NoteAttendee {self.id}: {self.display_name}>'


class EngagementContact(db.Model):
    """Person associated with an engagement. Polymorphic - links to one person type."""
    __tablename__ = 'engagement_contacts'

    id = db.Column(db.Integer, primary_key=True)
    engagement_id = db.Column(db.Integer, db.ForeignKey('engagements.id'), nullable=False)

    # Exactly one of these should be set (or none for external)
    customer_contact_id = db.Column(db.Integer, db.ForeignKey('customer_contacts.id'), nullable=True)
    partner_contact_id = db.Column(db.Integer, db.ForeignKey('partner_contacts.id'), nullable=True)
    solution_engineer_id = db.Column(db.Integer, db.ForeignKey('solution_engineers.id'), nullable=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=True)
    internal_contact_id = db.Column(db.Integer, db.ForeignKey('internal_contacts.id'), nullable=True)

    # For ad-hoc contacts not in any contact list
    external_name = db.Column(db.String(200), nullable=True)
    external_email = db.Column(db.String(255), nullable=True)

    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)

    # Relationships
    engagement = db.relationship('Engagement', back_populates='contacts')
    customer_contact = db.relationship('CustomerContact', lazy='select')
    partner_contact = db.relationship('PartnerContact', lazy='select')
    solution_engineer = db.relationship('SolutionEngineer', lazy='select')
    seller = db.relationship('Seller', lazy='select')
    internal_contact = db.relationship('InternalContact', lazy='select')

    @property
    def person_type(self) -> str:
        """Return the type of person this contact represents."""
        if self.customer_contact_id:
            return 'customer_contact'
        elif self.partner_contact_id:
            return 'partner_contact'
        elif self.solution_engineer_id:
            return 'se'
        elif self.seller_id:
            return 'seller'
        elif self.internal_contact_id:
            return 'internal_contact'
        return 'external'

    @property
    def display_name(self) -> str:
        """Return the display name for this contact."""
        if self.customer_contact_id and self.customer_contact:
            return self.customer_contact.name
        elif self.partner_contact_id and self.partner_contact:
            return self.partner_contact.name
        elif self.solution_engineer_id and self.solution_engineer:
            return self.solution_engineer.name
        elif self.seller_id and self.seller:
            return self.seller.name
        elif self.internal_contact_id and self.internal_contact:
            return self.internal_contact.name
        return self.external_name or 'Unknown'

    @property
    def email(self) -> Optional[str]:
        """Return the email for this contact."""
        if self.customer_contact_id and self.customer_contact:
            return self.customer_contact.email
        elif self.partner_contact_id and self.partner_contact:
            return self.partner_contact.email
        elif self.solution_engineer_id and self.solution_engineer:
            return self.solution_engineer.get_email()
        elif self.seller_id and self.seller:
            return self.seller.get_email()
        elif self.internal_contact_id and self.internal_contact:
            return self.internal_contact.get_email()
        return self.external_email

    @property
    def ref_id(self) -> Optional[int]:
        """Return the FK id for this contact's person type."""
        return (self.customer_contact_id or self.partner_contact_id
                or self.solution_engineer_id or self.seller_id
                or self.internal_contact_id)

    def to_dict(self) -> dict:
        """Serialize for API responses."""
        return {
            'id': self.id,
            'person_type': self.person_type,
            'display_name': self.display_name,
            'email': self.email,
            'ref_id': self.ref_id,
        }

    def __repr__(self) -> str:
        return f'<EngagementContact {self.id}: {self.display_name}>'


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

    # Story fields (human-authored)
    key_individuals = db.Column(db.Text, nullable=True)  # "I've been working with..."
    technical_problem = db.Column(db.Text, nullable=True)  # "...they have run into..."
    business_impact = db.Column(db.Text, nullable=True)  # "...and it's impacting..."
    solution_resources = db.Column(db.Text, nullable=True)  # "We are addressing the Opportunity with..."
    estimated_acr = db.Column(db.Integer, nullable=True)  # Monthly ACR in dollars
    target_date = db.Column(db.Date, nullable=True)  # "...by..."

    # AI-generated story fields (written only by the Generate function)
    ai_key_individuals = db.Column(db.Text, nullable=True)
    ai_technical_problem = db.Column(db.Text, nullable=True)
    ai_business_impact = db.Column(db.Text, nullable=True)
    ai_solution_resources = db.Column(db.Text, nullable=True)
    ai_estimated_acr = db.Column(db.Integer, nullable=True)
    ai_target_date = db.Column(db.Date, nullable=True)

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
    contacts = db.relationship(
        'EngagementContact',
        back_populates='engagement',
        lazy='select',
        cascade='all, delete-orphan',
    )

    @property
    def story_completeness(self) -> int:
        """Return percentage of story fields filled (0-100)."""
        fields = [
            bool(self.contacts), self.technical_problem, self.business_impact,
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

    @property
    def last_note_date(self) -> Optional[datetime]:
        """Most recent call_date from linked notes, or None."""
        if not self.notes:
            return None
        dates = [n.call_date for n in self.notes if n.call_date]
        return max(dates) if dates else None

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
    SOURCES = ['engagement', 'copilot', 'project']

    id = db.Column(db.Integer, primary_key=True)
    engagement_id = db.Column(db.Integer, db.ForeignKey('engagements.id'), nullable=True)  # Nullable for copilot/project items
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=True)  # For project-sourced action items
    note_id = db.Column(db.Integer, db.ForeignKey('notes.id'), nullable=True)
    title = db.Column(db.String(300), nullable=False)
    description = db.Column(db.Text, nullable=True)  # Rich HTML from Quill
    due_date = db.Column(db.Date, nullable=True)
    contact = db.Column(db.String(200), nullable=True)  # Point of contact
    status = db.Column(db.String(20), nullable=False, default='open')
    priority = db.Column(db.String(20), nullable=False, default='normal')
    source = db.Column(db.String(20), nullable=False, default='engagement', server_default='engagement')  # engagement, copilot, project
    source_url = db.Column(db.String(2000), nullable=True)  # External link (Teams/Outlook URL for copilot items)
    completed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0)

    # Relationships
    engagement = db.relationship('Engagement', back_populates='action_items')
    project = db.relationship('Project', back_populates='action_items')
    note = db.relationship('Note', back_populates='action_items')

    @property
    def is_overdue(self) -> bool:
        """Return True if task is open and past due date."""
        if self.status == 'completed' or not self.due_date:
            return False
        return self.due_date < date.today()

    def __repr__(self) -> str:
        return f'<ActionItem {self.id}: {self.title[:50]}>'


class DismissedCopilotTask(db.Model):
    """Copilot task dismissed or rejected by the user.

    Reason tracks why it was dismissed so the prompt builder can
    handle each type differently (e.g. 'not_useful' items get
    included as negative examples in the prompt).
    """
    __tablename__ = 'dismissed_copilot_tasks'

    REASONS = ['dismissed', 'not_useful']

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(300), nullable=False)
    reason = db.Column(db.String(20), nullable=False, default='dismissed')
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)

    def __repr__(self) -> str:
        return f'<DismissedCopilotTask {self.id}: {self.reason} - {self.title[:50]}>'


class Project(db.Model):
    """Internal project for tracking non-customer work.

    Built-in project types enable special behaviors:
    - general: user-created internal projects
    - copilot_saved: preserved Copilot action items the user wants to keep
    - training: learning, certifications, skill development
    - achievement: completed goals, awards, accomplishments

    Custom types can be added by users for any internal initiative.
    """
    __tablename__ = 'projects'

    STATUSES = ['Active', 'On Hold', 'Completed']
    BUILT_IN_TYPES = ['general', 'copilot_saved', 'training', 'achievement']

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(300), nullable=False)
    description = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(50), nullable=False, default='Active')
    project_type = db.Column(db.String(50), nullable=False, default='general')
    due_date = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    # Relationships
    notes = db.relationship(
        'Note',
        secondary=notes_projects,
        back_populates='projects',
        lazy='select'
    )
    action_items = db.relationship(
        'ActionItem',
        back_populates='project',
        lazy='select',
        cascade='all, delete-orphan',
        order_by='ActionItem.sort_order, ActionItem.created_at'
    )

    @property
    def is_built_in_type(self) -> bool:
        """Return True if this project uses a built-in type."""
        return self.project_type in self.BUILT_IN_TYPES

    @property
    def open_task_count(self) -> int:
        """Count of open action items."""
        return sum(1 for t in self.action_items if t.status == 'open')

    @property
    def is_overdue(self) -> bool:
        """Return True if project is active and past due date."""
        if self.status == 'Completed' or not self.due_date:
            return False
        return self.due_date < date.today()

    def __repr__(self) -> str:
        return f'<Project {self.id}: {self.title[:50]} ({self.project_type})>'


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
    """Milestone from MSX sales platform that can be linked to notes."""
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
    customer_commitment = db.Column(db.String(50), nullable=True)  # "Committed", "Uncommitted", etc.
    opportunity_name = db.Column(db.String(500), nullable=True)  # Parent opportunity name (legacy, kept for compat)
    
    # Milestone tracker fields (populated from MSX sync)
    due_date = db.Column(db.DateTime, nullable=True)  # Target completion date from MSX
    dollar_value = db.Column(db.Float, nullable=True)  # Estimated revenue/value from MSX
    workload = db.Column(db.String(200), nullable=True)  # Workload name from MSX
    monthly_usage = db.Column(db.Float, nullable=True)  # Monthly usage amount from MSX
    last_synced_at = db.Column(db.DateTime, nullable=True)  # Last time synced from MSX
    owner_name = db.Column(db.String(200), nullable=True)  # Milestone owner display name from MSX
    on_my_team = db.Column(db.Boolean, default=False, nullable=False, server_default='0')  # Am I on the milestone access team?
    cached_comments_json = db.Column(db.Text, nullable=True)  # MSX forecast comments cached as JSON
    details_fetched_at = db.Column(db.DateTime, nullable=True)  # When MSX details were last fetched
    committed_at = db.Column(db.DateTime, nullable=True)  # When commitment changed to Committed (detected by sync)
    completed_at = db.Column(db.DateTime, nullable=True)  # When status changed to Completed (detected by sync)
    msx_created_on = db.Column(db.DateTime, nullable=True)  # When milestone was created in MSX (createdon)
    msx_modified_on = db.Column(db.DateTime, nullable=True)  # When milestone was last modified in MSX (modifiedon)
    
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
        """Return urgency level based on due date: 'past_due', 'this_week', 'this_month', 'future', 'no_date'.

        Committed milestones are never 'past_due' - they already landed.
        """
        if not self.due_date:
            return 'no_date'
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        due = self.due_date if self.due_date.tzinfo else self.due_date.replace(tzinfo=timezone.utc)
        days_until = (due - now).days
        if days_until < 0:
            if self.customer_commitment == 'Committed':
                return 'future'
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
    """Task created in MSX linked to a milestone and note."""
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


class MilestoneAudit(db.Model):
    """Audit trail record from MSX showing what changed on a milestone."""
    __tablename__ = 'milestone_audits'

    id = db.Column(db.Integer, primary_key=True)
    milestone_id = db.Column(db.Integer, db.ForeignKey('milestones.id'), nullable=False)
    audit_id = db.Column(db.String(50), nullable=False)  # MSX audit GUID
    changed_on = db.Column(db.DateTime, nullable=False)  # When the change happened in MSX
    changed_by = db.Column(db.String(50), nullable=True)  # MSX user ID who made the change
    operation = db.Column(db.Integer, nullable=True)  # 1=Create, 2=Update, 3=Delete
    field_name = db.Column(db.String(100), nullable=False)  # Dataverse logical name (e.g., msp_monthlyuse)
    old_value = db.Column(db.Text, nullable=True)  # Previous value (as string)
    new_value = db.Column(db.Text, nullable=True)  # New value (as string)

    __table_args__ = (
        db.UniqueConstraint('audit_id', 'field_name', name='uq_audit_field'),
    )

    # Relationships
    milestone = db.relationship(
        'Milestone',
        backref=db.backref('audits', lazy='dynamic', cascade='all, delete-orphan')
    )

    # Map Dataverse field names to human-readable labels.
    # Only fields listed here are saved during audit sync - unlisted fields are skipped.
    FIELD_LABELS = {
        'msp_monthlyuse': 'Est. ACR/mo',
        'msp_milestonedate': 'Due Date',
        'msp_commitmentrecommendation': 'Commitment',
        'msp_milestonestatus': 'Status',
        'msp_name': 'Title',
        'msp_bacvrate': 'BACV',
        'msp_forecastcommentsjsonfield': 'Forecast Comments',
        'msp_tags': 'Tags',
        'ownerid': 'Owner',
        'msp_workload': 'Workload',
        'msp_milestonesolutionarea': 'Solution Area',
        'msp_milestonecategory': 'Category',
        'msp_milestonestatusreason': 'Status Reason',
        'msp_helpneeded': 'Help Needed',
        'msp_completedon': 'Completed On',
        'msp_completedby': 'Completed By',
        'msp_riskorblockeddate': 'Risk/Blocked Date',
    }

    @property
    def field_label(self) -> str:
        """Return human-readable label for the changed field."""
        return self.FIELD_LABELS.get(self.field_name, self.field_name)

    def __repr__(self) -> str:
        return f'<MilestoneAudit {self.audit_id[:8]} milestone={self.milestone_id} field={self.field_name}>'


# =============================================================================
# User Preferences Model
# =============================================================================

class UserPreference(db.Model):
    """User preferences including dark mode and customer view settings."""
    __tablename__ = 'user_preferences'
    
    id = db.Column(db.Integer, primary_key=True)
    dark_mode = db.Column(db.Boolean, default=None, nullable=True)
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
    msx_auto_writeback = db.Column(db.Boolean, default=False, nullable=False, server_default='0')  # Auto-sync note summaries and engagement stories to MSX milestones
    copilot_actions_enabled = db.Column(db.Boolean, default=True, nullable=False, server_default='1')  # Show Copilot daily action items on dashboard
    show_stale_milestones = db.Column(db.Boolean, default=True, nullable=False, server_default='1')  # Show stale milestones in Action Items card
    show_hygiene_tasks = db.Column(db.Boolean, default=True, nullable=False, server_default='1')  # Show data hygiene tasks in Action Items card
    last_copilot_sync = db.Column(db.DateTime, nullable=True)  # Last time Copilot daily action items were synced
    milestone_auto_sync = db.Column(db.Boolean, default=True, nullable=False, server_default='1')  # Auto-sync milestones from MSX
    milestone_sync_hour = db.Column(db.Integer, nullable=True)  # Random hour (5-8) for daily milestone sync
    milestone_sync_minute = db.Column(db.Integer, nullable=True)  # Random minute (0-59) for sync time
    last_milestone_sync = db.Column(db.DateTime, nullable=True)  # Last time milestone sync ran (UTC)
    compensated_buckets = db.Column(db.Text, nullable=True)  # JSON array of selected ServiceCompGrouping buckets (fallback for localStorage)
    revenue_import_reminder = db.Column(db.Boolean, default=True, nullable=False, server_default='1')
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

class PartnerRecommendation(db.Model):
    """Persisted AI partner recommendation for an engagement."""
    __tablename__ = 'partner_recommendations'

    id = db.Column(db.Integer, primary_key=True)
    engagement_id = db.Column(
        db.Integer, db.ForeignKey('engagements.id', ondelete='CASCADE'),
        nullable=False,
    )
    partner_id = db.Column(
        db.Integer, db.ForeignKey('partners.id', ondelete='CASCADE'),
        nullable=False,
    )
    rank = db.Column(db.Integer, nullable=False)  # 1, 2, 3
    fit_score = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.Text, nullable=False)
    generated_at = db.Column(db.DateTime, default=utc_now, nullable=False)

    engagement = db.relationship('Engagement', backref=db.backref(
        'partner_recommendations', cascade='all, delete-orphan',
        order_by='PartnerRecommendation.rank',
    ))
    partner = db.relationship('Partner')

    def __repr__(self) -> str:
        return (
            f'<PartnerRecommendation eng={self.engagement_id} '
            f'partner={self.partner_id} rank={self.rank}>'
        )


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
    category = db.Column(db.String(50), nullable=False)  # DECLINING, RECENT_DIP, etc.
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
    review_notes_list = db.relationship('RevenueReviewNote', back_populates='analysis',
                                        lazy='select', order_by='RevenueReviewNote.created_at.desc()')
    
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


class RevenueReviewNote(db.Model):
    """Immutable history of review actions on a revenue analysis.

    Each time a user sets/changes the review status on a RevenueAnalysis, a new
    row is appended here so the full review timeline is preserved.
    """
    __tablename__ = 'revenue_review_notes'

    id = db.Column(db.Integer, primary_key=True)
    analysis_id = db.Column(db.Integer, db.ForeignKey('revenue_analyses.id'), nullable=False)
    review_status = db.Column(db.String(20), nullable=False)
    review_notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)

    # Relationships
    analysis = db.relationship('RevenueAnalysis', back_populates='review_notes_list')

    def __repr__(self) -> str:
        return f'<RevenueReviewNote {self.id} analysis={self.analysis_id} status={self.review_status}>'


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

    # Entity tracking -- populated when the request targets a specific entity
    entity_type = db.Column(db.String(50), nullable=True, index=True)  # e.g. 'milestone', 'customer'
    entity_id = db.Column(db.Integer, nullable=True, index=True)       # PK of the entity

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


class Favorite(db.Model):
    """Favorited items - polymorphic table covering milestones, engagements, and opportunities.

    Uses (object_type, object_id) as a composite unique key. No columns are
    added to the source models - toggling favorite just inserts/deletes a row here.
    """
    __tablename__ = 'favorites'
    __table_args__ = (
        db.UniqueConstraint('object_type', 'object_id', name='uq_favorite'),
    )

    id = db.Column(db.Integer, primary_key=True)
    object_type = db.Column(db.String(50), nullable=False)   # 'milestone', 'engagement', 'opportunity'
    object_id = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)

    @staticmethod
    def is_favorited(object_type: str, object_id: int) -> bool:
        """Return True if the given object is currently favorited."""
        return Favorite.query.filter_by(
            object_type=object_type, object_id=object_id
        ).first() is not None

    @staticmethod
    def toggle(object_type: str, object_id: int) -> bool:
        """Toggle favorite state. Returns the new is_favorited value."""
        existing = Favorite.query.filter_by(
            object_type=object_type, object_id=object_id
        ).first()
        if existing:
            db.session.delete(existing)
            db.session.commit()
            return False
        else:
            db.session.add(Favorite(object_type=object_type, object_id=object_id))
            db.session.commit()
            return True

    def __repr__(self) -> str:
        return f'<Favorite {self.object_type}:{self.object_id}>'


class HygieneNote(db.Model):
    """Free-text reason note for hygiene report items.

    Tracks why an engagement has no milestones or a milestone has no
    engagement. Stored in a separate table to avoid muddying existing models.
    """
    __tablename__ = 'hygiene_notes'
    ENTITY_TYPES = ['engagement', 'milestone']

    id = db.Column(db.Integer, primary_key=True)
    entity_type = db.Column(db.String(20), nullable=False)  # 'engagement' or 'milestone'
    entity_id = db.Column(db.Integer, nullable=False)  # FK to engagement.id or milestone.id
    note = db.Column(db.Text, nullable=False, default='')
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    __table_args__ = (
        db.UniqueConstraint('entity_type', 'entity_id', name='uq_hygiene_note_entity'),
    )

    def __repr__(self) -> str:
        return f'<HygieneNote {self.entity_type}:{self.entity_id}>'


class MarketingSummary(db.Model):
    """Account-level marketing engagement summary from MSX.

    One row per customer (keyed by TPID). Sourced from the
    msp_marketingengagements entity in Dynamics 365.
    """
    __tablename__ = 'marketing_summaries'

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False)
    tpid = db.Column(db.String(50), nullable=False)
    total_interactions = db.Column(db.Integer, default=0)
    content_downloads = db.Column(db.Integer, default=0)
    trials = db.Column(db.Integer, default=0)
    engaged_contacts = db.Column(db.Integer, default=0)
    unique_decision_makers = db.Column(db.Integer, default=0)
    last_interaction_date = db.Column(db.DateTime, nullable=True)
    synced_at = db.Column(db.DateTime, default=utc_now)

    customer = db.relationship('Customer', backref=db.backref(
        'marketing_summary', uselist=False, cascade='all, delete-orphan'))

    __table_args__ = (
        db.UniqueConstraint('customer_id', name='uq_marketing_summary_customer'),
    )

    def __repr__(self) -> str:
        return f'<MarketingSummary customer={self.customer_id} interactions={self.total_interactions}>'


class MarketingInteraction(db.Model):
    """Per-sales-play marketing interaction breakdown from MSX.

    One row per TPID + solution area + sales play combination. Sourced from
    the msp_marketinginteractions entity in Dynamics 365.
    """
    __tablename__ = 'marketing_interactions'

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False)
    tpid = db.Column(db.String(50), nullable=False)
    composite_key = db.Column(db.String(200), nullable=False)  # msp_tpidsolutionareasalesplay
    solution_area = db.Column(db.String(200), nullable=True)
    sales_play = db.Column(db.String(200), nullable=True)
    all_interactions = db.Column(db.Integer, default=0)
    contact_me = db.Column(db.Integer, default=0)
    trial_signups = db.Column(db.Integer, default=0)
    content_downloads = db.Column(db.Integer, default=0)
    events = db.Column(db.Integer, default=0)
    unique_decision_makers = db.Column(db.Integer, default=0)
    high_interaction_contacts = db.Column(db.Integer, default=0)
    high_interaction_count = db.Column(db.Integer, default=0)
    last_interaction_date = db.Column(db.DateTime, nullable=True)
    last_high_interaction_date = db.Column(db.DateTime, nullable=True)
    synced_at = db.Column(db.DateTime, default=utc_now)

    customer = db.relationship('Customer', backref=db.backref(
        'marketing_interactions', cascade='all, delete-orphan', lazy='dynamic'))

    __table_args__ = (
        db.UniqueConstraint('composite_key', name='uq_marketing_interaction_key'),
    )

    def __repr__(self) -> str:
        return f'<MarketingInteraction {self.sales_play} interactions={self.all_interactions}>'


class MarketingContact(db.Model):
    """Per-contact marketing engagement aggregates from MSX.

    One row per CRM-linked contact per customer. Sourced from contact entity
    msp_ fields in Dynamics 365. Requires two-step query (account lookup
    then contacts).
    """
    __tablename__ = 'marketing_contacts'

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False)
    contact_guid = db.Column(db.String(50), nullable=False)  # Dynamics contact GUID
    contact_name = db.Column(db.String(200), nullable=True)
    job_title = db.Column(db.String(200), nullable=True)
    email = db.Column(db.String(255), nullable=True)
    mail_interactions = db.Column(db.Integer, default=0)
    meeting_interactions = db.Column(db.Integer, default=0)
    audience_type = db.Column(db.String(200), nullable=True)
    engagement_level = db.Column(db.String(100), nullable=True)
    last_interaction_date = db.Column(db.DateTime, nullable=True)
    last_solution_area = db.Column(db.String(200), nullable=True)
    synced_at = db.Column(db.DateTime, default=utc_now)

    customer = db.relationship('Customer', backref=db.backref(
        'marketing_contacts', cascade='all, delete-orphan', lazy='dynamic'))

    __table_args__ = (
        db.UniqueConstraint('customer_id', 'contact_guid', name='uq_marketing_contact'),
    )

    def __repr__(self) -> str:
        return f'<MarketingContact {self.contact_name} mail={self.mail_interactions}>'


# =============================================================================
# U2C Snapshot Models
# =============================================================================

class U2CSnapshot(db.Model):
    """Header for a fiscal quarter U2C milestone snapshot.

    Captured on the 5th of each fiscal quarter's first month (or manually).
    Records the set of uncommitted milestones on open opportunities at that
    point in time so attainment can be tracked against a fixed baseline.
    """
    __tablename__ = 'u2c_snapshots'

    id = db.Column(db.Integer, primary_key=True)
    fiscal_quarter = db.Column(db.String(10), nullable=False, unique=True)  # e.g. "FY26 Q4"
    snapshot_date = db.Column(db.DateTime, nullable=False, default=utc_now)
    total_items = db.Column(db.Integer, default=0, nullable=False)
    total_monthly_acr = db.Column(db.Float, default=0.0, nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now, nullable=False)

    items = db.relationship(
        'U2CSnapshotItem', back_populates='snapshot',
        cascade='all, delete-orphan', lazy='dynamic',
    )

    def __repr__(self) -> str:
        return f'<U2CSnapshot {self.fiscal_quarter} items={self.total_items}>'


class U2CSnapshotItem(db.Model):
    """Individual milestone captured in a U2C snapshot.

    Stores a frozen copy of milestone data at snapshot time so the baseline
    never changes even if the live milestone is later edited or deleted.
    """
    __tablename__ = 'u2c_snapshot_items'

    id = db.Column(db.Integer, primary_key=True)
    snapshot_id = db.Column(db.Integer, db.ForeignKey('u2c_snapshots.id'), nullable=False)
    milestone_id = db.Column(db.Integer, db.ForeignKey('milestones.id'), nullable=True)  # nullable if milestone deleted later
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=True)

    # Frozen milestone data at snapshot time
    customer_name = db.Column(db.String(500), nullable=False)
    milestone_title = db.Column(db.String(500), nullable=False)
    milestone_number = db.Column(db.String(50), nullable=True)
    workload = db.Column(db.String(200), nullable=True)
    due_date = db.Column(db.DateTime, nullable=True)
    monthly_acr = db.Column(db.Float, default=0.0, nullable=False)
    opportunity_name = db.Column(db.String(500), nullable=True)
    msx_status = db.Column(db.String(50), nullable=True)  # Status at snapshot time

    snapshot = db.relationship('U2CSnapshot', back_populates='items')
    milestone = db.relationship('Milestone', lazy='select')
    customer = db.relationship('Customer', lazy='select')

    def __repr__(self) -> str:
        return f'<U2CSnapshotItem {self.milestone_title} ${self.monthly_acr}>'