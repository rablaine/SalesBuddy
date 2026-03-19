"""
Seller mode helpers for Sales Buddy.
Provides shared utility for resolving the active seller in seller mode.
"""
from flask import session

from app.models import UserPreference


def get_seller_mode_seller_id():
    """Return the active seller ID if in seller mode, else None.

    For DSS users, the seller ID is permanently stored in preferences.
    For SE users, the seller ID comes from the session (temporary).
    """
    pref = UserPreference.query.first()
    if pref and pref.user_role == 'dss' and pref.my_seller_id:
        return pref.my_seller_id
    return session.get('seller_mode_seller_id')
