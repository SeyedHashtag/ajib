"""Persistent selection-or-none, shared by the catalog and admin screens."""

import os
from utils.atomic_store import read_json, locked_json

DEFAULT_PLANS_FILE = '/etc/ajib/core/scripts/telegrambot/plans.json'


def preferences_path(plans_file):
    return os.path.join(os.path.dirname(plans_file), 'plan_preferences.json')


def set_recommendation(plan_id, *, plans_file=DEFAULT_PLANS_FILE):
    with locked_json(preferences_path(plans_file), {}) as settings:
        settings['recommended_plan_id'] = str(plan_id) if plan_id is not None else None


def get_recommendation(plans, configured_plan_id=None, *, plans_file=DEFAULT_PLANS_FILE):
    eligible = {str(key): value for key, value in (plans or {}).items()
                if str(key).isdigit() and isinstance(value, dict) and value.get('target', 'both') != 'reseller'}
    settings = read_json(preferences_path(plans_file), {})
    if 'recommended_plan_id' in settings:
        selected = settings['recommended_plan_id']
        return selected if selected in eligible else None
    stored = next((key for key in sorted(eligible, key=int) if eligible[key].get('recommended') is True), None)
    if stored is not None:
        return stored
    fallback = str(configured_plan_id if configured_plan_id is not None
                   else os.getenv('AJIB_RECOMMENDED_PLAN_ID') or '').strip()
    return fallback if fallback in eligible else None
