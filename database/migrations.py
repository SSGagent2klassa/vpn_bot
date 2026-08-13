"""Database schema baseline and incremental migrations.

Fresh installations are created directly at the committed v97 compatibility
boundary. Older databases must pass through the ordered blocking releases that
materialize v97 before this code can run. Migrations v98-v99 remain incremental
so already installed v97 databases and fresh databases use the same transitions.
"""
from __future__ import annotations

import json
import logging
import sqlite3

from .connection import get_db
from .db_user_ui_texts import update_user_ui_text_defaults
from .user_ui_text_catalog import USER_UI_TEXT_DEFINITIONS


logger = logging.getLogger(__name__)


# The latest schema guaranteed by the preceding ordered blocking releases.
INITIAL_VERSION = 97

# Current schema version; post-v97 changes stay outside the compressed baseline.
LATEST_VERSION = 99


DEFAULT_BROADCAST_STYLE_PROFILE = {
    "schema_version": 1,
    "tone": "friendly_professional",
    "address": "polite_you",
    "emoji_level": "medium",
    "length": "compact",
    "headline": "emoji_bold",
    "paragraphs": "short",
    "cta": "direct_calm",
    "use_lists": True,
    "custom_instructions": "",
}


def _my_keys_item_template() -> str:
    """Hidden default of one key format on the “My Keys” page."""
    return (
        "🔑 <b>%key(field=name)%</b>\n"
        "%key(field=status)% · %key(field=traffic)%\n"
        "📅 До %key(field=expires_at)%\n"
        "📍 %key(field=server)%"
    )


def _referral_new_ref_notification_text() -> str:
    """Hidden default notification to the referral provider about a new referral."""
    return (
        "👥 <b>Новый реферал</b>\n\n"
        "По вашей ссылке зарегистрировался пользователь.\n\n"
        "👤 Имя: <b>%реферал_имя%</b>\n"
        "🔗 Логин: %реферал_логин%\n"
        "📊 Уровень: <b>%реферальный_уровень%</b>"
    )


def _referral_purchase_notification_text() -> str:
    """Hidden default notification to the referral provider about the purchase of a referral."""
    return (
        "💳 <b>Покупка реферала</b>\n\n"
        "Пользователь <b>%покупатель_имя%</b> (%покупатель_логин%) оплатил тариф.\n\n"
        "🎫 Тариф: <b>%платеж_тариф%</b>\n"
        "💵 Сумма: <b>%платеж_сумма%</b>\n"
        "⏳ Срок: <b>%платеж_срок%</b>\n"
        "🎁 Ваш бонус: <b>%реферальное_вознаграждение%</b>\n"
        "📊 Уровень: <b>%реферальный_уровень%</b>"
    )


def get_current_version() -> int:
    """
    Gets the current version of the database schema.
    
    Returns:
        int: Version number (0 if version table does not exist)
    """
    with get_db() as conn:
        # Checking the existence of the schema_version table
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        )
        if not cursor.fetchone():
            return 0
        
        cursor = conn.execute("SELECT version FROM schema_version LIMIT 1")
        row = cursor.fetchone()
        return row["version"] if row else 0


def set_version(conn: sqlite3.Connection, version: int) -> None:
    """
    Sets the database schema version.
    
    Args:
        conn: Connection to the database
        version: Version number
    """
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))


_BASELINE_SCHEMA_V97 = r"""
CREATE TABLE balance_operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            operation_type TEXT NOT NULL,
            delta_cents INTEGER NOT NULL,
            balance_before INTEGER NOT NULL,
            balance_after INTEGER NOT NULL,
            source TEXT NOT NULL,
            reason TEXT,
            reference_type TEXT,
            reference_id TEXT,
            performed_by INTEGER,
            metadata TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        , currency TEXT NOT NULL DEFAULT 'RUB', delta_minor INTEGER NOT NULL DEFAULT 0);

CREATE TABLE base_currency_switches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_currency TEXT NOT NULL,
            to_currency TEXT NOT NULL,
            to_units_per_from TEXT NOT NULL,
            admin_telegram_id INTEGER NOT NULL,
            backup_path TEXT NOT NULL,
            converted_tariffs INTEGER NOT NULL DEFAULT 0,
            converted_balances INTEGER NOT NULL DEFAULT 0,
            converted_referral_rows INTEGER NOT NULL DEFAULT 0,
            canceled_intents INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE currency_rates (
            base_currency TEXT NOT NULL,
            target_currency TEXT NOT NULL,
            units_per_base TEXT NOT NULL,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (base_currency, target_currency)
        );

CREATE TABLE extension_core_operations (
            extension_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            operation TEXT NOT NULL,
            target_user_id INTEGER,
            amount INTEGER,
            reason TEXT,
            status TEXT NOT NULL,
            metadata TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (extension_id, idempotency_key)
        );

CREATE TABLE extension_schema_versions (
            extension_id TEXT PRIMARY KEY,
            version INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE extension_storage (
            extension_id TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (extension_id, key)
        );

CREATE TABLE key_lifecycle_event_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vpn_key_id INTEGER NOT NULL REFERENCES vpn_keys(id) ON DELETE CASCADE,
            event_name TEXT NOT NULL,
            event_token TEXT NOT NULL,
            metadata_json TEXT,
            emitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (vpn_key_id, event_name, event_token)
        );

CREATE TABLE key_operation_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vpn_key_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            operation_type TEXT NOT NULL,
            delta_days INTEGER DEFAULT 0,
            source TEXT NOT NULL,
            reason TEXT,
            reference_type TEXT,
            reference_id TEXT,
            expires_before TEXT,
            expires_after TEXT,
            metadata TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE lapsed_coupon_deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            lapse_token TEXT NOT NULL,
            lapsed_at TIMESTAMP NOT NULL,
            coupon_id INTEGER,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'sent', 'failed', 'canceled')),
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            sent_at TIMESTAMP,
            UNIQUE (user_id, lapse_token),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (coupon_id) REFERENCES promo_codes(id) ON DELETE SET NULL
        );

CREATE TABLE notification_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vpn_key_id INTEGER NOT NULL,
            sent_at DATE NOT NULL,
            FOREIGN KEY (vpn_key_id) REFERENCES vpn_keys(id)
        );

CREATE TABLE page_routes (
            route_key TEXT PRIMARY KEY,
            page_key TEXT NOT NULL,
            guard_names TEXT NOT NULL DEFAULT '[]',
            hook_names TEXT NOT NULL DEFAULT '[]',
            is_enabled INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (page_key) REFERENCES pages(page_key)
        );

CREATE TABLE pages (
            page_key         TEXT PRIMARY KEY,
            text_default     TEXT NOT NULL DEFAULT '',
            image_default    TEXT,
            media_type_default TEXT,
            buttons_default  TEXT NOT NULL DEFAULT '[]',
            text_custom      TEXT,
            image_custom     TEXT,
            media_type_custom TEXT,
            updated_at       TIMESTAMP,
            buttons_custom   TEXT,
            guard_names      TEXT NOT NULL DEFAULT '[]',
            hook_names       TEXT NOT NULL DEFAULT '[]'
        );

CREATE TABLE payment_auto_checks (
            order_id TEXT PRIMARY KEY,
            provider_id TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'active'
                CHECK (state IN (
                    'active', 'provider_succeeded', 'completed',
                    'canceled', 'exhausted', 'completion_failed'
                )),
            started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            next_check_at TIMESTAMP,
            last_check_at TIMESTAMP,
            check_attempts INTEGER NOT NULL DEFAULT 0,
            completion_attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (order_id) REFERENCES payments(order_id) ON DELETE CASCADE
        );

CREATE TABLE payment_effects (
            order_id TEXT NOT NULL,
            effect_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'started'
                CHECK (status IN ('started', 'completed', 'failed')),
            metadata_json TEXT NOT NULL DEFAULT '{}',
            attempts INTEGER NOT NULL DEFAULT 1,
            last_error TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            PRIMARY KEY (order_id, effect_name),
            FOREIGN KEY (order_id) REFERENCES payments(order_id) ON DELETE CASCADE
        );

CREATE TABLE payment_provider_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT NOT NULL UNIQUE,
            provider_id TEXT NOT NULL,
            payment_type TEXT NOT NULL,
            provider_payment_id TEXT,
            payment_url TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            metadata_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, purpose TEXT, charge_amount TEXT, charge_currency TEXT,
            FOREIGN KEY (order_id) REFERENCES payments(order_id) ON DELETE CASCADE
        );

CREATE TABLE payment_referral_effects (
            order_id TEXT NOT NULL,
            level INTEGER NOT NULL,
            referrer_id INTEGER NOT NULL,
            payer_id INTEGER NOT NULL,
            reward_cents INTEGER NOT NULL DEFAULT 0,
            reward_days INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, reward_currency TEXT NOT NULL DEFAULT 'RUB', reward_minor INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (order_id, level),
            FOREIGN KEY (order_id) REFERENCES payments(order_id) ON DELETE CASCADE
        );

CREATE TABLE payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vpn_key_id INTEGER,
            user_id INTEGER NOT NULL,
            tariff_id INTEGER,
            order_id TEXT NOT NULL UNIQUE,
            payment_type TEXT,
            amount_cents INTEGER,
            amount_stars INTEGER,
            period_days INTEGER,
            status TEXT DEFAULT 'paid',
            paid_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            yookassa_payment_id TEXT,
            wata_link_id TEXT,
            platega_transaction_id TEXT,
            cardlink_bill_id TEXT,
            promo_code_id INTEGER,
            promo_code TEXT,
            discount_percent INTEGER DEFAULT 0,
            original_amount_cents INTEGER,
            discount_amount_cents INTEGER DEFAULT 0,
            final_amount_cents INTEGER,
            original_amount_stars INTEGER,
            discount_amount_stars INTEGER DEFAULT 0,
            final_amount_stars INTEGER,
            is_promo_free INTEGER DEFAULT 0, balance_deduct_cents INTEGER NOT NULL DEFAULT 0, intent_version INTEGER NOT NULL DEFAULT 0, purpose TEXT NOT NULL DEFAULT 'legacy_key_payment', purpose_data_json TEXT NOT NULL DEFAULT '{}', nominal_amount_cents INTEGER NOT NULL DEFAULT 0, payable_amount_cents INTEGER NOT NULL DEFAULT 0, charge_amount TEXT, charge_currency TEXT, rate_snapshot_json TEXT NOT NULL DEFAULT '{}', description TEXT, success_target_json TEXT NOT NULL DEFAULT '{}', cancel_target_json TEXT NOT NULL DEFAULT '{}', fulfillment_status TEXT NOT NULL DEFAULT 'pending', fulfillment_attempts INTEGER NOT NULL DEFAULT 0, fulfillment_started_at TIMESTAMP, fulfillment_last_error TEXT, provider_confirmed_at TIMESTAMP, fulfilled_at TIMESTAMP, created_at TIMESTAMP, base_currency TEXT NOT NULL DEFAULT 'RUB', nominal_amount_minor INTEGER NOT NULL DEFAULT 0, payable_amount_minor INTEGER NOT NULL DEFAULT 0, balance_deduct_minor INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (vpn_key_id) REFERENCES vpn_keys(id),
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (tariff_id) REFERENCES tariffs(id)
        );

CREATE TABLE promo_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL CHECK (type IN ('promo', 'coupon')),
            code TEXT NOT NULL UNIQUE,
            discount_percent INTEGER NOT NULL DEFAULT 0
                CHECK (discount_percent >= 0 AND discount_percent <= 100),
            expires_at TIMESTAMP,
            is_active INTEGER NOT NULL DEFAULT 1,
            activation_limit INTEGER,
            usage_count INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'manual',
            issued_to_user_id INTEGER,
            created_by_admin_id INTEGER,
            snapshot_discount_percent INTEGER,
            snapshot_lifetime_days INTEGER,
            snapshot_generated_at TIMESTAMP,
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (issued_to_user_id) REFERENCES users(id) ON DELETE SET NULL
        );

CREATE TABLE promo_link_visits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            promo_code_id INTEGER NOT NULL,
            code TEXT NOT NULL,
            user_id INTEGER,
            telegram_id INTEGER NOT NULL,
            start_param TEXT NOT NULL,
            converted_order_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            converted_at TIMESTAMP,
            FOREIGN KEY (promo_code_id) REFERENCES promo_codes(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
        );

CREATE TABLE promo_redemptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            promo_code_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            order_id TEXT NOT NULL,
            code TEXT NOT NULL,
            discount_percent INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'reserved'
                CHECK (status IN ('reserved', 'applied', 'canceled')),
            payment_type TEXT,
            action TEXT,
            original_amount INTEGER NOT NULL DEFAULT 0,
            discount_amount INTEGER NOT NULL DEFAULT 0,
            final_amount INTEGER NOT NULL DEFAULT 0,
            amount_unit TEXT NOT NULL DEFAULT 'cents',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            applied_at TIMESTAMP,
            FOREIGN KEY (promo_code_id) REFERENCES promo_codes(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

CREATE TABLE referral_levels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            level_number INTEGER NOT NULL UNIQUE,
            percent INTEGER NOT NULL,
            enabled INTEGER DEFAULT 1
        );

CREATE TABLE referral_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER NOT NULL,
            referral_id INTEGER NOT NULL,
            level INTEGER NOT NULL,
            total_payments_count INTEGER DEFAULT 0,
            total_reward_cents INTEGER DEFAULT 0,
            total_reward_days INTEGER DEFAULT 0, reward_currency TEXT NOT NULL DEFAULT 'RUB', total_reward_minor INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (referrer_id) REFERENCES users(id),
            FOREIGN KEY (referral_id) REFERENCES users(id),
            UNIQUE (referrer_id, referral_id, level)
        );

CREATE TABLE schema_version (
            version INTEGER NOT NULL
        );

CREATE TABLE server_groups (
            server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
            group_id  INTEGER NOT NULL REFERENCES tariff_groups(id) ON DELETE CASCADE,
            PRIMARY KEY (server_id, group_id)
        );

CREATE TABLE servers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            host TEXT NOT NULL,
            port INTEGER NOT NULL,
            web_base_path TEXT NOT NULL,
            login TEXT NOT NULL,
            password TEXT NOT NULL,
            is_active INTEGER DEFAULT 1,
            protocol TEXT DEFAULT 'https',
            api_token TEXT,
            panel_version TEXT,
            panel_api_profile TEXT,
            panel_checked_at TEXT
        );

CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );

CREATE TABLE support_admin_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id INTEGER NOT NULL,
            admin_telegram_id INTEGER NOT NULL,
            card_message_id INTEGER,
            copy_message_id INTEGER,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (thread_id) REFERENCES support_threads(id) ON DELETE CASCADE
        );

CREATE TABLE support_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id INTEGER NOT NULL,
            sender_type TEXT NOT NULL CHECK (sender_type IN ('user', 'admin')),
            sender_telegram_id INTEGER NOT NULL,
            recipient_telegram_id INTEGER,
            text_html TEXT NOT NULL DEFAULT '',
            media_type TEXT,
            media_file_id TEXT,
            source_chat_id INTEGER NOT NULL,
            source_message_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (thread_id) REFERENCES support_threads(id) ON DELETE CASCADE
        );

CREATE TABLE support_threads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            user_telegram_id INTEGER NOT NULL,
            initiator_type TEXT NOT NULL CHECK (initiator_type IN ('user', 'admin')),
            initiator_admin_id INTEGER,
            assigned_admin_id INTEGER,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_message_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

CREATE TABLE tariff_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            sort_order INTEGER DEFAULT 1,
            monthly_traffic_reset_enabled INTEGER NOT NULL DEFAULT 0
                CHECK (monthly_traffic_reset_enabled IN (0, 1)),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE tariffs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            duration_days INTEGER NOT NULL,
            display_order INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            price_rub INTEGER DEFAULT 0,
            traffic_limit_gb INTEGER DEFAULT 0,
            group_id INTEGER DEFAULT 1,
            max_ips INTEGER DEFAULT 1,
            system_type TEXT
        , price_minor INTEGER NOT NULL DEFAULT 0);

CREATE TABLE trial_activations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            offer_id INTEGER,
            tariff_id INTEGER,
            group_id INTEGER,
            vpn_key_id INTEGER REFERENCES vpn_keys(id) ON DELETE SET NULL,
            payment_id INTEGER REFERENCES payments(id) ON DELETE SET NULL,
            legacy_global_block INTEGER NOT NULL DEFAULT 0
                CHECK (legacy_global_block IN (0, 1)),
            activated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE trial_offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tariff_id INTEGER REFERENCES tariffs(id) ON DELETE RESTRICT,
            is_primary INTEGER NOT NULL DEFAULT 0
                CHECK (is_primary IN (0, 1)),
            is_enabled INTEGER NOT NULL DEFAULT 1
                CHECK (is_enabled IN (0, 1)),
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (is_primary = 1 OR tariff_id IS NOT NULL)
        );

CREATE TABLE user_ui_texts (
            text_key TEXT PRIMARY KEY,
            text_default TEXT NOT NULL,
            text_custom TEXT,
            text_format TEXT NOT NULL CHECK (text_format IN ('html', 'plain', 'button')),
            description TEXT NOT NULL,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL UNIQUE,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            is_banned INTEGER DEFAULT 0,
            is_bot_blocked INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            used_trial INTEGER DEFAULT 0,
            referral_code TEXT,
            referred_by INTEGER REFERENCES users(id),
            personal_balance INTEGER DEFAULT 0,
            referral_coefficient REAL DEFAULT 1.0,
            active_promo_code_id INTEGER
        , last_key_number INTEGER NOT NULL DEFAULT 0 CHECK (last_key_number >= 0));

CREATE TABLE vpn_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            server_id INTEGER,
            tariff_id INTEGER NOT NULL,
            panel_inbound_id INTEGER,
            client_uuid TEXT,
            panel_email TEXT,
            custom_name TEXT,
            expires_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            traffic_used INTEGER DEFAULT 0,
            traffic_limit INTEGER DEFAULT 0,
            traffic_updated_at DATETIME,
            traffic_notified_pct INTEGER DEFAULT 100,
            sub_id TEXT,
            traffic_limit_override INTEGER
                CHECK (traffic_limit_override IS NULL OR traffic_limit_override >= 0),
            max_ips_override INTEGER
                CHECK (max_ips_override IS NULL OR max_ips_override BETWEEN 1 AND 999),
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (server_id) REFERENCES servers(id),
            FOREIGN KEY (tariff_id) REFERENCES tariffs(id)
        );

CREATE UNIQUE INDEX idx_balance_operations_payment_referral ON balance_operations(user_id, operation_type, source, reference_type, reference_id) WHERE reference_type = 'payment_referral' AND reference_id IS NOT NULL;

CREATE UNIQUE INDEX idx_balance_operations_payment_topup
        ON balance_operations(reference_type, reference_id)
        WHERE reference_type = 'payment_topup' AND reference_id IS NOT NULL;

CREATE INDEX idx_balance_operations_reference ON balance_operations(reference_type, reference_id);

CREATE INDEX idx_balance_operations_user_created ON balance_operations(user_id, created_at);

CREATE INDEX idx_key_lifecycle_event_emitted ON key_lifecycle_event_log(emitted_at);

CREATE INDEX idx_key_lifecycle_event_lookup ON key_lifecycle_event_log(event_name, vpn_key_id, event_token);

CREATE INDEX idx_key_operation_log_key_created ON key_operation_log(vpn_key_id, created_at);

CREATE INDEX idx_key_operation_log_user_created ON key_operation_log(user_id, created_at);

CREATE UNIQUE INDEX idx_key_operations_payment_reward ON key_operation_log(user_id, source, reference_type, reference_id) WHERE reference_type IN ('payment_referral', 'payment_promo_reward') AND reference_id IS NOT NULL;

CREATE INDEX idx_lapsed_coupon_deliveries_coupon
        ON lapsed_coupon_deliveries(coupon_id);

CREATE INDEX idx_lapsed_coupon_deliveries_due
        ON lapsed_coupon_deliveries(status, lapsed_at, id);

CREATE UNIQUE INDEX idx_notification_log_unique ON notification_log(vpn_key_id, sent_at);

CREATE INDEX idx_notification_log_vpn_key ON notification_log(vpn_key_id);

CREATE INDEX idx_page_routes_page_key ON page_routes(page_key);

CREATE INDEX idx_payment_auto_checks_due
        ON payment_auto_checks(state, next_check_at);

CREATE INDEX idx_payment_provider_orders_external ON payment_provider_orders(provider_id, provider_payment_id);

CREATE INDEX idx_payment_provider_orders_provider ON payment_provider_orders(provider_id, status);

CREATE INDEX idx_payments_cardlink_bill_id ON payments(cardlink_bill_id);

CREATE INDEX idx_payments_fulfillment ON payments(fulfillment_status, provider_confirmed_at);

CREATE INDEX idx_payments_key_status_paid_at ON payments(vpn_key_id, status, paid_at DESC);

CREATE INDEX idx_payments_order_id ON payments(order_id);

CREATE INDEX idx_payments_paid_at ON payments(paid_at);

CREATE INDEX idx_payments_platega_transaction_id ON payments(platega_transaction_id);

CREATE INDEX idx_payments_promo_code_id ON payments(promo_code_id);

CREATE INDEX idx_payments_status_paid_at ON payments(status, paid_at);

CREATE INDEX idx_payments_user_id ON payments(user_id);

CREATE INDEX idx_payments_wata_link_id ON payments(wata_link_id);

CREATE INDEX idx_payments_yookassa_payment_id ON payments(yookassa_payment_id);

CREATE INDEX idx_promo_codes_expires ON promo_codes(expires_at);

CREATE UNIQUE INDEX idx_promo_codes_lapsed_coupon
        ON promo_codes(source) WHERE source LIKE 'auto_lapsed:%';

CREATE UNIQUE INDEX idx_promo_codes_payment_coupon ON promo_codes(source) WHERE source LIKE 'auto_payment:%';

CREATE INDEX idx_promo_codes_source ON promo_codes(source);

CREATE INDEX idx_promo_codes_type ON promo_codes(type, is_active);

CREATE INDEX idx_promo_link_visits_code ON promo_link_visits(promo_code_id, created_at);

CREATE INDEX idx_promo_link_visits_user ON promo_link_visits(user_id, created_at);

CREATE INDEX idx_promo_redemptions_code_status ON promo_redemptions(promo_code_id, status);

CREATE UNIQUE INDEX idx_promo_redemptions_order ON promo_redemptions(order_id) WHERE status != 'canceled';

CREATE INDEX idx_promo_redemptions_user ON promo_redemptions(user_id, created_at);

CREATE INDEX idx_server_groups_group ON server_groups(group_id);

CREATE INDEX idx_support_admin_notifications_thread ON support_admin_notifications(thread_id, is_active);

CREATE INDEX idx_support_messages_thread ON support_messages(thread_id, created_at);

CREATE INDEX idx_support_threads_assigned ON support_threads(assigned_admin_id, updated_at);

CREATE INDEX idx_support_threads_user ON support_threads(user_telegram_id, created_at);

CREATE UNIQUE INDEX idx_tariffs_admin_custom_group ON tariffs(group_id) WHERE system_type = 'admin_custom';

CREATE UNIQUE INDEX idx_trial_activations_legacy_user ON trial_activations(user_id) WHERE legacy_global_block = 1;

CREATE INDEX idx_trial_activations_offer ON trial_activations(offer_id);

CREATE UNIQUE INDEX idx_trial_activations_user_group ON trial_activations(user_id, group_id) WHERE legacy_global_block = 0 AND group_id IS NOT NULL;

CREATE UNIQUE INDEX idx_trial_offers_primary ON trial_offers(is_primary) WHERE is_primary = 1;

CREATE INDEX idx_users_created_at ON users(created_at);

CREATE INDEX idx_users_is_bot_blocked ON users(is_bot_blocked);

CREATE INDEX idx_users_referral_code ON users(referral_code);

CREATE INDEX idx_users_referred_by ON users(referred_by);

CREATE INDEX idx_users_telegram_id ON users(telegram_id);

CREATE INDEX idx_users_username_lower ON users(LOWER(username));

CREATE INDEX idx_vpn_keys_expires_at ON vpn_keys(expires_at);

CREATE INDEX idx_vpn_keys_panel_email_lower ON vpn_keys(LOWER(panel_email));

CREATE INDEX idx_vpn_keys_server_email ON vpn_keys(server_id, panel_email);

CREATE INDEX idx_vpn_keys_server_id ON vpn_keys(server_id);

CREATE INDEX idx_vpn_keys_user_expires ON vpn_keys(user_id, expires_at DESC);

CREATE INDEX idx_vpn_keys_user_id ON vpn_keys(user_id);

CREATE TRIGGER trg_trial_offers_protect_primary_delete
        BEFORE DELETE ON trial_offers
        WHEN OLD.is_primary = 1
        BEGIN
            SELECT RAISE(ABORT, 'primary trial offer cannot be deleted');
        END;

CREATE TRIGGER trg_trial_offers_protect_primary_marker
        BEFORE UPDATE OF is_primary ON trial_offers
        WHEN NEW.is_primary <> OLD.is_primary
        BEGIN
            SELECT RAISE(ABORT, 'trial offer primary marker is immutable');
        END;

CREATE TRIGGER trg_trial_offers_reject_missing_insert
        BEFORE INSERT ON trial_offers
        WHEN NEW.tariff_id IS NOT NULL
         AND NOT EXISTS (
                SELECT 1 FROM tariffs WHERE id = NEW.tariff_id
             )
        BEGIN
            SELECT RAISE(ABORT, 'trial tariff does not exist');
        END;

CREATE TRIGGER trg_trial_offers_reject_missing_update
        BEFORE UPDATE OF tariff_id ON trial_offers
        WHEN NEW.tariff_id IS NOT NULL
         AND NOT EXISTS (
                SELECT 1 FROM tariffs WHERE id = NEW.tariff_id
             )
        BEGIN
            SELECT RAISE(ABORT, 'trial tariff does not exist');
        END;

CREATE TRIGGER trg_trial_offers_reject_system_insert
        BEFORE INSERT ON trial_offers
        WHEN NEW.tariff_id IS NOT NULL
         AND EXISTS (
                SELECT 1 FROM tariffs
                WHERE id = NEW.tariff_id AND system_type IS NOT NULL
             )
        BEGIN
            SELECT RAISE(ABORT, 'system tariff cannot be used by a trial offer');
        END;

CREATE TRIGGER trg_trial_offers_reject_system_update
        BEFORE UPDATE OF tariff_id ON trial_offers
        WHEN NEW.tariff_id IS NOT NULL
         AND EXISTS (
                SELECT 1 FROM tariffs
                WHERE id = NEW.tariff_id AND system_type IS NOT NULL
             )
        BEGIN
            SELECT RAISE(ABORT, 'system tariff cannot be used by a trial offer');
        END;

CREATE TRIGGER trg_trial_offers_touch_updated_at
        AFTER UPDATE OF tariff_id, is_enabled ON trial_offers
        BEGIN
            UPDATE trial_offers
            SET updated_at = CURRENT_TIMESTAMP
            WHERE id = NEW.id;
        END;

CREATE TRIGGER trg_trial_usage_scope_insert
        BEFORE INSERT ON settings
        WHEN NEW.key = 'trial_usage_scope'
         AND NEW.value NOT IN ('once_per_user', 'once_per_group')
        BEGIN
            SELECT RAISE(ABORT, 'invalid trial usage scope');
        END;

CREATE TRIGGER trg_trial_usage_scope_update
        BEFORE UPDATE OF value ON settings
        WHEN NEW.key = 'trial_usage_scope'
         AND NEW.value NOT IN ('once_per_user', 'once_per_group')
        BEGIN
            SELECT RAISE(ABORT, 'invalid trial usage scope');
        END;
"""


_BASELINE_SETTINGS_V97 = (('base_currency', 'RUB'),
 ('bot_mode', 'subscription'),
 ('broadcast_config_revision', '0'),
 ('broadcast_filter', '[]'),
 ('broadcast_filter_contract_version', '2'),
 ('broadcast_in_progress', '0'),
 ('broadcast_style_profile',
  '{"schema_version":1,"tone":"friendly_professional","address":"polite_you","emoji_level":"medium","length":"compact","headline":"emoji_bold","paragraphs":"short","cta":"direct_calm","use_lists":true,"custom_instructions":""}'),
 ('cardlink_api_token', ''),
 ('cardlink_enabled', '0'),
 ('cardlink_shop_id', ''),
 ('cards_enabled', '0'),
 ('cards_provider_token', ''),
 ('coupon_auto_discount_percent', '10'),
 ('coupon_auto_enabled', '0'),
 ('coupon_auto_lifetime_days', '90'),
 ('coupon_lapsed_delay_days', '7'),
 ('coupon_lapsed_discount_percent', '10'),
 ('coupon_lapsed_enabled', '0'),
 ('coupon_lapsed_enabled_since', ''),
 ('coupon_lapsed_lifetime_days', '90'),
 ('crypto_enabled', '0'),
 ('crypto_item_url', ''),
 ('crypto_secret_key', ''),
 ('custom_extensions_enabled', '0'),
 ('custom_payment_webhooks_enabled', '0'),
 ('custom_payment_webhooks_host', '127.0.0.1'),
 ('custom_payment_webhooks_path_prefix', '/custom-payment-webhook'),
 ('custom_payment_webhooks_port', '8088'),
 ('daily_tasks_time', '03:00'),
 ('demo_payment_enabled', '0'),
 ('display_timezone', 'Europe/Moscow'),
 ('expired_key_deletion_notifications_enabled', '1'),
 ('expired_key_retention_days', '30'),
 ('key_name_prefix', 'Ключ'),
 ('my_keys_item_template',
  '🔑 <b>%key(field=name)%</b>\n'
  '%key(field=status)% · %key(field=traffic)%\n'
  '📅 До %key(field=expires_at)%\n'
  '📍 %key(field=server)%'),
 ('notification_days', '3'),
 ('notification_text',
  '⚠️ <b>Ваш VPN-ключ %ключ_имя% скоро истекает!</b>\n'
  '\n'
  'Через %ключ_дней_до_окончания% дней закончится срок действия вашего ключа.\n'
  '\n'
  'Продлите подписку, чтобы сохранить доступ к VPN без перерыва!'),
 ('platega_enabled', '0'),
 ('platega_merchant_id', ''),
 ('platega_secret', ''),
 ('referral_enabled', '0'),
 ('referral_new_ref_notification_text',
  '👥 <b>Новый реферал</b>\n'
  '\n'
  'По вашей ссылке зарегистрировался пользователь.\n'
  '\n'
  '👤 Имя: <b>%реферал_имя%</b>\n'
  '🔗 Логин: %реферал_логин%\n'
  '📊 Уровень: <b>%реферальный_уровень%</b>'),
 ('referral_new_ref_notifications_enabled', '0'),
 ('referral_notification_levels', '1'),
 ('referral_purchase_notification_text',
  '💳 <b>Покупка реферала</b>\n'
  '\n'
  'Пользователь <b>%покупатель_имя%</b> (%покупатель_логин%) оплатил тариф.\n'
  '\n'
  '🎫 Тариф: <b>%платеж_тариф%</b>\n'
  '💵 Сумма: <b>%платеж_сумма%</b>\n'
  '⏳ Срок: <b>%платеж_срок%</b>\n'
  '🎁 Ваш бонус: <b>%реферальное_вознаграждение%</b>\n'
  '📊 Уровень: <b>%реферальный_уровень%</b>'),
 ('referral_purchase_notifications_enabled', '0'),
 ('referral_reward_type', 'days'),
 ('stablecoin_rub_rate', '95'),
 ('star_rub_rate', '1.235'),
 ('stars_enabled', '0'),
 ('support_claim_cleanup_mode', 'remove_button'),
 ('telegram_link_domain', 't.me'),
 ('traffic_notification_text',
  '⚠️ По ключу <b>%ключ_имя%</b> осталось %ключ_трафик_процент_остатка%% трафика '
  '(%ключ_трафик_использовано% из %ключ_трафик_лимит%)'),
 ('trial_usage_scope', 'once_per_user'),
 ('update_blocked', '0'),
 ('update_check_time', '12:00'),
 ('update_notifications_enabled', '1'),
 ('usd_rub_rate', '9500'),
 ('wata_enabled', '0'),
 ('wata_jwt_token', ''),
 ('yadreno_admin_core_changes_enabled', '0'),
 ('yookassa_qr_enabled', '0'),
 ('yookassa_secret_key', ''),
 ('yookassa_shop_id', ''))


_BASELINE_TARIFF_GROUPS_V97 = ((1, 'Основная', 1, 0),)


_BASELINE_TARIFFS_V97 = ((1, 'Admin Tariff', 0, 999, 0, 0, 0, 1, 1, 'admin_custom', 0),)


_BASELINE_REFERRAL_LEVELS_V97 = ((1, 1, 10, 1), (2, 2, 5, 0), (3, 3, 2, 0))


_BASELINE_CURRENCY_RATES_V97 = (('RUB', 'USDT', '0.01052631578947368421052631579'),
 ('RUB', 'XTR', '0.8097165991902834008097165992'))


_BASELINE_TRIAL_OFFERS_V97 = ((1, None, 1, 0),)


_BASELINE_PAGES_V97 = (('access_blocked',
  '⛔ <b>Доступ заблокирован</b>\n\nВаш аккаунт заблокирован. Обратитесь в поддержку.',
  '[{"id": "btn_back_main", "label": "🈴 На главную", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('action_unavailable',
  '⚠️ <b>Действие недоступно</b>\n\nОткройте нужный раздел заново и повторите попытку.',
  '[{"id": "btn_back_main", "label": "🈴 На главную", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('balance_insufficient',
  '💎 <b>Недостаточно средств</b>\n'
  '\n'
  'Ваш баланс: <b>%payment_balance%</b>\n'
  'К оплате: <b>%payment_amount%</b>',
  '[{"id": "btn_intent_methods", "label": "🔄 Сменить способ", "color": "secondary", "row": 0, '
  '"col": 0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_cancel", "label": "⬅️ Назад", "color": "secondary", "row": 1, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": "btn_back_main", '
  '"label": "🈴 На главную", "color": "secondary", "row": 1, "col": 1, "is_hidden": false, '
  '"action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('balance_payment',
  '💎 <b>Оплата с баланса</b>\n'
  '\n'
  'Тариф: <b>%payment_tariff%</b>\n'
  'Стоимость: <b>%payment_amount%</b>\n'
  '%payment_discount_line%\n'
  'Ваш баланс: <b>%payment_balance%</b>\n'
  '\n'
  'С баланса будет списано: <b>%payment_balance_deduct%</b>\n'
  'Останется оплатить: <b>%payment_remaining%</b>',
  '[{"id": "btn_intent_balance", "label": "💎 Использовать баланс", "color": "secondary", "row": 0, '
  '"col": 0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_cancel", "label": "⬅️ Назад", "color": "secondary", "row": 1, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": "btn_back_main", '
  '"label": "🈴 На главную", "color": "secondary", "row": 1, "col": 1, "is_hidden": false, '
  '"action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('balance_topup_amount',
  '💰 <b>Пополнение баланса</b>\n'
  '\n'
  'Введите сумму в базовой валюте (%payment_base_currency%), которую хотите зачислить на баланс.\n'
  '\n'
  'Например: <code>500</code>',
  '[{"id": "btn_back_main", "label": "❌ Отмена", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('balance_topup_amount_invalid',
  '⚠️ <b>Некорректная сумма</b>\n\nВведите положительное число без дополнительных символов.',
  '[{"id": "btn_back_main", "label": "🈴 На главную", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('balance_topup_result',
  '✅ <b>Баланс пополнен</b>\n'
  '\n'
  'На баланс зачислено: <b>%платеж_номинал%</b>\n'
  'Оплачено: <b>%платеж_сумма%</b>\n'
  '\n'
  '%payment_coupon%',
  '[{"id": "btn_back_main", "label": "🈴 На главную", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('crypto_payment',
  '🪙 <b>Оплата криптовалютой</b>\n'
  '\n'
  '🎫 Тариф: <b>%payment_tariff%</b>\n'
  '💰 Сумма: <b>%payment_amount%</b>\n'
  '%payment_discount_line%\n'
  'Перейдите к оплате по кнопке ниже.',
  '[{"id": "btn_intent_open", "label": "💳 Перейти к оплате", "color": "secondary", "row": 0, '
  '"col": 0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_check", "label": "✅ Я оплатил", "color": "secondary", "row": 1, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_methods", "label": "🔄 Сменить способ", "color": "secondary", "row": 2, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": "btn_intent_cancel", '
  '"label": "⬅️ Назад", "color": "secondary", "row": 3, "col": 0, "is_hidden": false, '
  '"action_type": "system", "action_value": null}, {"id": "btn_back_main", "label": "🈴 На '
  'главную", "color": "secondary", "row": 3, "col": 1, "is_hidden": false, "action_type": '
  '"internal", "action_value": "cmd_back_main"}]'),
 ('custom_profile',
  '👤 <b>Личный кабинет</b>\n'
  '\n'
  'Имя: <b>%user_name%</b>\n'
  'Telegram ID: <code>%telegram_id%</code>\n'
  'Username: %user_username%\n'
  'Дата регистрации: %user_registered_at%\n'
  'Баланс: <b>%user_balance%</b>\n'
  '\n'
  '━━━━━━━━━━━━━━━\n'
  '🔑 <b>Ключи</b>\n'
  'Всего: <b>%keys_total%</b>\n'
  'Активных: <b>%keys_active%</b>\n'
  'Истёкших: <b>%keys_expired%</b>',
  '[{"id": "btn_profile_my_keys", "label": "🔑 Мои ключи", "color": "secondary", "row": 0, "col": '
  '0, "is_hidden": false, "action_type": "internal", "action_value": "cmd_my_keys"}, {"id": '
  '"btn_profile_buy", "label": "💳 Купить ключ", "color": "secondary", "row": 0, "col": 1, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_buy"}, {"id": '
  '"btn_profile_referral", "label": "🔗 Реферальная система", "color": "secondary", "row": 1, '
  '"col": 0, "is_hidden": false, "action_type": "internal", "action_value": "cmd_referral"}, '
  '{"id": "btn_profile_show_id", "label": "🆔 Мой ID", "color": "secondary", "row": 1, "col": 1, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_show_id"}, {"id": '
  '"btn_profile_help", "label": "❓ Справка", "color": "secondary", "row": 2, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_help"}, {"id": '
  '"btn_profile_back_main", "label": "🈴 На главную", "color": "secondary", "row": 3, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('demo_payment',
  '🏦 <b>Демонстрационная оплата</b>\n'
  '\n'
  'Это демо-режим. Реального списания не происходит.\n'
  '\n'
  '🎫 Тариф: <b>%payment_tariff%</b>\n'
  '📅 Срок: <b>%payment_term%</b>\n'
  '💰 Сумма: <b>%payment_amount%</b>',
  '[{"id": "btn_intent_methods", "label": "🔄 Сменить способ", "color": "secondary", "row": 0, '
  '"col": 0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_cancel", "label": "⬅️ Назад", "color": "secondary", "row": 1, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": "btn_back_main", '
  '"label": "🈴 На главную", "color": "secondary", "row": 1, "col": 1, "is_hidden": false, '
  '"action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('expired_keys_deleted',
  '🗑️ <b>Неактивные ключи удалены</b>\n'
  '\n'
  'Срок действия этих VPN-ключей закончился не менее %retention_days% дней назад, поэтому мы '
  'удалили их из бота:\n'
  '\n'
  '%deleted_keys%\n'
  '\n'
  'Если VPN снова понадобится, нажмите «Купить ключ» — новый доступ можно оформить в любое время.',
  '[{"id": "btn_buy_key", "label": "💳 Купить ключ", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_buy"}, {"id": '
  '"btn_back_main", "label": "🈴 На главную", "color": "secondary", "row": 1, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('expiry_notification_actions',
  '',
  '[{"id": "btn_my_keys", "label": "🔑 Мои ключи", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_my_keys"}, {"id": '
  '"btn_back_main", "label": "🈴 На главную", "color": "secondary", "row": 1, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('help',
  '🔐 Этот бот предоставляет доступ к VPN-сервису.\n'
  '\n'
  '<b>Как это работает:</b>\n'
  '1. Купите ключ через раздел «Купить ключ»\n'
  '\n'
  '2. Установите VPN-клиент для вашего устройства:\n'
  '\n'
  'Hiddify или v2rayNG или V2Box\n'
  'Подробная инструкция по настройке VPN👇 '
  'https://telegra.ph/Kak-nastroit-VPN-Gajd-za-2-minuty-01-23\n'
  '\n'
  '3. Импортируйте ключ в приложение\n'
  '\n'
  '4. Подключайтесь и наслаждайтесь! 🚀\n'
  '\n'
  '---\n'
  'Разработчик @plushkin_blog\n'
  '---',
  '[{"id": "btn_news", "label": "📢 Новости", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "url", "action_value": '
  '"https://%telegram_link_domain%/plushkin_blog"}, {"id": "btn_support", "label": "💬 Поддержка", '
  '"color": "secondary", "row": 0, "col": 1, "is_hidden": false, "action_type": "url", '
  '"action_value": "https://%telegram_link_domain%/plushkin_chat"}, {"id": "btn_back_main", '
  '"label": "🈴 На главную", "color": "secondary", "row": 1, "col": 0, "is_hidden": false, '
  '"action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('key_delivery',
  '✅ <b>Ваш VPN-ключ!</b>\n'
  '\n'
  '%ключ_для_копирования%\n'
  '☝️ Нажмите, чтобы скопировать.\n'
  '\n'
  '📱 <b>Инструкция:</b>\n'
  '1. Скопируйте ссылку или отсканируйте QR-код.\n'
  '2. Импортируйте в свой клиент. Какой именно клиент подходит, смотри в инструкции по кнопке '
  'ниже.\n'
  '3. Нажмите подключиться!',
  '[{"id": "btn_help", "label": "📄 Инструкция", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_help"}, {"id": '
  '"btn_my_keys", "label": "🔑 Мои ключи", "color": "secondary", "row": 0, "col": 1, "is_hidden": '
  'false, "action_type": "internal", "action_value": "cmd_my_keys"}, {"id": "btn_back_main", '
  '"label": "🈴 На главную", "color": "secondary", "row": 1, "col": 0, "is_hidden": false, '
  '"action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('key_delivery_failed',
  '❌ <b>Ошибка выдачи ключа</b>\n\nПопробуйте позже или обратитесь в поддержку.',
  '[{"id": "btn_my_keys", "label": "🔑 Мои ключи", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_my_keys"}, {"id": '
  '"btn_back_main", "label": "🈴 На главную", "color": "secondary", "row": 1, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('key_delivery_partial',
  '📋 <b>Ваш VPN-ключ</b>\n'
  '\n'
  '%ключ_для_копирования%\n'
  '\n'
  '⚠️ Полную конфигурацию получить не удалось. Попробуйте позже.',
  '[{"id": "btn_my_keys", "label": "🔑 Мои ключи", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_my_keys"}, {"id": '
  '"btn_back_main", "label": "🈴 На главную", "color": "secondary", "row": 1, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('key_details',
  '🔑 <b>%key(field=name)%</b>\n'
  '\n'
  '<b>Статус:</b> %key(field=status)%\n'
  '<b>Сервер:</b> %key(field=server)%\n'
  '<b>Тариф:</b> %key(field=tariff)%\n'
  '<b>Устройств:</b> %key(field=device_limit)%\n'
  '<b>Трафик:</b> %key(field=traffic)%\n'
  '<b>Действует до:</b> %key(field=expires_at)%\n'
  '\n'
  '📜 <b>История операций:</b>\n'
  '%key_history%',
  '[{"id": "btn_key_show_key", "label": "📋 Показать ключ", "color": "secondary", "row": 0, "col": '
  '0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_key_show_subscription", "label": "📋 Показать подписку", "color": "secondary", "row": 0, '
  '"col": 0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_key_configure", "label": "⚙️ Настроить", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": "btn_key_renew", '
  '"label": "📈 Продлить", "color": "secondary", "row": 0, "col": 1, "is_hidden": false, '
  '"action_type": "system", "action_value": null}, {"id": "btn_key_replace", "label": "🔄 '
  'Заменить", "color": "secondary", "row": 1, "col": 0, "is_hidden": false, "action_type": '
  '"system", "action_value": null}, {"id": "btn_key_delete", "label": "🗑 Удалить", "color": '
  '"secondary", "row": 1, "col": 0, "is_hidden": false, "action_type": "system", "action_value": '
  'null}, {"id": "btn_key_rename", "label": "✏️ Переименовать", "color": "secondary", "row": 1, '
  '"col": 1, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_my_keys", "label": "🔑 Мои ключи", "color": "secondary", "row": 2, "col": 0, "is_hidden": '
  'false, "action_type": "internal", "action_value": "cmd_my_keys"}, {"id": "btn_back_main", '
  '"label": "🈴 На главную", "color": "secondary", "row": 2, "col": 1, "is_hidden": false, '
  '"action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('key_not_found',
  '❌ <b>Ключ не найден</b>\n\nКлюч удалён, устарел или принадлежит другому пользователю.',
  '[{"id": "btn_my_keys", "label": "🔑 Мои ключи", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_my_keys"}, {"id": '
  '"btn_back_main", "label": "🈴 На главную", "color": "secondary", "row": 1, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('key_operation_failed',
  '❌ <b>Не удалось выполнить операцию</b>\n\nПопробуйте позже или обратитесь в поддержку.',
  '[{"id": "btn_my_keys", "label": "🔑 Мои ключи", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_my_keys"}, {"id": '
  '"btn_back_main", "label": "🈴 На главную", "color": "secondary", "row": 1, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('key_operation_unavailable',
  '⚠️ <b>Действие с ключом недоступно</b>\n\nОткройте карточку ключа заново и повторите попытку.',
  '[{"id": "btn_my_keys", "label": "🔑 Мои ключи", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_my_keys"}, {"id": '
  '"btn_back_main", "label": "🈴 На главную", "color": "secondary", "row": 1, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('key_progress',
  '⏳ <b>Выполняем операцию с ключом</b>\n\nПодождите немного.',
  '[{"id": "btn_my_keys", "label": "🔑 Мои ключи", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_my_keys"}, {"id": '
  '"btn_back_main", "label": "🈴 На главную", "color": "secondary", "row": 1, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('key_rename_invalid',
  '⚠️ <b>Некорректное имя</b>\n\nВведите непустое имя длиной не более 30 символов.',
  '[{"id": "btn_my_keys", "label": "🔑 Мои ключи", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_my_keys"}, {"id": '
  '"btn_back_main", "label": "🈴 На главную", "color": "secondary", "row": 1, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('key_rename_prompt',
  '✏️ <b>Переименование ключа</b>\n'
  '\n'
  'Текущее имя: <b>%key(field=name)%</b>\n'
  '\n'
  'Введите новое название для ключа (макс. 30 символов):\n'
  '<i>(Отправьте любой текст)</i>',
  '[{"id": "btn_key_flow_back", "label": "❌ Отмена", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}]'),
 ('key_renewed',
  '✅ <b>Ключ продлён</b>\n'
  '\n'
  '🔑 <b>%key(field=name)%</b>\n'
  'Новый срок: <b>%payment_term%</b>.\n'
  '\n'
  '%payment_coupon%',
  '[{"id": "btn_my_keys", "label": "🔑 Мои ключи", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_my_keys"}, {"id": '
  '"btn_back_main", "label": "🈴 На главную", "color": "secondary", "row": 1, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('key_replace_confirm',
  '⚠️ <b>Подтверждение замены</b>\n'
  '\n'
  'Ключ: <b>%key(field=name)%</b>\n'
  'Новый сервер: <b>%selected_server%</b>\n'
  '\n'
  'Старый ключ или ссылка перестанет работать. Обновите настройки в приложении.\n'
  '\n'
  'Вы уверены?',
  '[{"id": "btn_key_flow_confirm", "label": "✅ Да, заменить", "color": "secondary", "row": 0, '
  '"col": 0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_key_flow_back", "label": "❌ Отмена", "color": "secondary", "row": 1, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}]'),
 ('key_replace_inbound_select',
  '🖥️ <b>Выбор протокола</b>\n\nСервер: <b>%selected_server%</b>\n\nВыберите протокол:',
  '[{"id": "btn_protocol_items", "label": "🔌 %item_name% (%item_protocol%)", "color": "secondary", '
  '"row": 0, "col": 0, "is_hidden": false, "action_type": "system_collection", "action_value": '
  'null}, {"id": "btn_key_flow_back", "label": "⬅️ Назад", "color": "secondary", "row": 1000, '
  '"col": 0, "is_hidden": false, "action_type": "system", "action_value": null}]'),
 ('key_replace_server_select',
  '🔄 <b>Замена ключа</b>\n'
  '\n'
  'Вы можете пересоздать ключ на другом или том же сервере.\n'
  'Старый ключ будет удалён, но срок действия сохранится.\n'
  '\n'
  'Выберите сервер:',
  '[{"id": "btn_server_items", "label": "🌐 %item_name%", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "system_collection", "action_value": null}, {"id": '
  '"btn_key_flow_back", "label": "❌ Отмена", "color": "secondary", "row": 1000, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}]'),
 ('key_show_unconfigured',
  '📋 <b>Показать ключ</b>\n\n⚠️ Ключ ещё не создан на сервере.\nОбратитесь в поддержку.',
  '[{"id": "btn_help", "label": "📄 Инструкция", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_help"}, {"id": '
  '"btn_my_keys", "label": "🔑 Мои ключи", "color": "secondary", "row": 0, "col": 1, "is_hidden": '
  'false, "action_type": "internal", "action_value": "cmd_my_keys"}, {"id": "btn_back_main", '
  '"label": "🈴 На главную", "color": "secondary", "row": 1, "col": 0, "is_hidden": false, '
  '"action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('key_status',
  '%ключ_статус_заголовок%\n\n%ключ_статус_текст%',
  '[{"id": "btn_back_main", "label": "🈴 На главную", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('lapsed_key_coupon',
  '🎁 <b>Купон для вас</b>\n'
  '\n'
  'Мы заметили, что вы не продлили VPN-ключ, и хотим помочь вам вернуться.\n'
  '\n'
  'Ваш купон на скидку <b>%promo_discount%%</b>:\n'
  '<pre>%promo_code%</pre>\n'
  'Купон действует до <b>%promo_expires_at%</b>.\n'
  '\n'
  'Введите его в поле промокода при следующей покупке или продлении.',
  '[{"id": "btn_my_keys", "label": "🔑 Мои ключи", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_my_keys"}, {"id": '
  '"btn_back_main", "label": "🈴 На главную", "color": "secondary", "row": 1, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('main',
  '🔐 <b>Добро пожаловать в VPN-бот!</b>\n'
  '\n'
  'Быстрый, безопасный и анонимный доступ к интернету.\n'
  'Без логов, без ограничений, без проблем! 🚀\n'
  '\n'
  '📋 <b>Тарифы:</b>\n'
  '%tariffs%',
  '[{"id": "btn_my_keys", "label": "🔑 Мои ключи", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_my_keys"}, {"id": '
  '"btn_buy_key", "label": "💳 Купить ключ", "color": "secondary", "row": 0, "col": 1, "is_hidden": '
  'false, "action_type": "internal", "action_value": "cmd_buy"}, {"id": "btn_trial", "label": "🎁 '
  'Пробная подписка", "color": "secondary", "row": 1, "col": 0, "is_hidden": true, "action_type": '
  '"internal", "action_value": "cmd_trial"}, {"id": "btn_referral", "label": "🔗 Реферальная '
  'ссылка", "color": "secondary", "row": 2, "col": 0, "is_hidden": true, "action_type": '
  '"internal", "action_value": "cmd_referral"}, {"id": "btn_help", "label": "❓ Справка", "color": '
  '"secondary", "row": 2, "col": 1, "is_hidden": false, "action_type": "internal", "action_value": '
  '"cmd_help"}, {"id": "btn_support", "label": "💬 Написать в поддержку", "color": "secondary", '
  '"row": 3, "col": 0, "is_hidden": true, "action_type": "internal", "action_value": '
  '"cmd_support"}]'),
 ('my_keys',
  '🔑 <b>Мои ключи</b>\n\n%список_ключей%\n\nВыберите ключ для управления:',
  '[{"id": "btn_key_items", "label": "%item_status_indicator% %item_name%", "color": "secondary", '
  '"row": 0, "col": 0, "is_hidden": false, "action_type": "system_collection", "action_value": '
  'null}, {"id": "btn_back_main", "label": "🈴 На главную", "color": "secondary", "row": 1000, '
  '"col": 0, "is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('my_keys_empty',
  '🔑 <b>Мои ключи</b>\n'
  '\n'
  'У вас пока нет VPN-ключей.\n'
  '\n'
  'Нажмите «Купить ключ», чтобы приобрести доступ! 🚀',
  '[{"id": "btn_buy_key", "label": "💳 Купить ключ", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_buy"}, {"id": '
  '"btn_back_main", "label": "🈴 На главную", "color": "secondary", "row": 1, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('my_keys_key_deleted',
  '✅ <b>Ключ удалён</b>\n\nКлюч <b>%key(field=name)%</b> успешно удалён.',
  '[{"id": "btn_my_keys", "label": "🔑 Мои ключи", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_my_keys"}, {"id": '
  '"btn_back_main", "label": "🈴 На главную", "color": "secondary", "row": 1, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('new_key_inbound_select',
  '🖥️ <b>Выбор протокола</b>\n\nСервер: <b>%selected_server%</b>\n\nВыберите протокол:',
  '[{"id": "btn_protocol_items", "label": "🔌 %item_name% (%item_protocol%)", "color": "secondary", '
  '"row": 0, "col": 0, "is_hidden": false, "action_type": "system_collection", "action_value": '
  'null}, {"id": "btn_key_flow_back", "label": "⬅️ Назад", "color": "secondary", "row": 1000, '
  '"col": 0, "is_hidden": false, "action_type": "system", "action_value": null}]'),
 ('new_key_no_servers',
  '⚠️ <b>Нет доступных серверов</b>\n'
  '\n'
  'К сожалению, сейчас нет доступных серверов.\n'
  'Пожалуйста, свяжитесь с поддержкой.',
  '[{"id": "btn_back_main", "label": "🈴 На главную", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('new_key_server_select',
  '🌐 <b>Выбор сервера</b>\n\n%экран_данные%',
  '[{"id": "btn_server_items", "label": "🌐 %item_name%", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "system_collection", "action_value": null}, {"id": '
  '"btn_back_main", "label": "🈴 На главную", "color": "secondary", "row": 1000, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('payment_auto_completed',
  '✅ <b>Платёж подтверждён</b>\n\nОперация завершена автоматически.\n\n%payment_coupon%',
  '[{"id": "btn_my_keys", "label": "🔑 Мои ключи", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_my_keys"}, {"id": '
  '"btn_back_main", "label": "🈴 На главную", "color": "secondary", "row": 1, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('payment_canceled',
  '⚪ <b>Платёж отменён</b>\n\nВыберите другой способ оплаты или вернитесь позже.',
  '[{"id": "btn_intent_methods", "label": "🔄 Сменить способ", "color": "secondary", "row": 0, '
  '"col": 0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_cancel", "label": "⬅️ Назад", "color": "secondary", "row": 1, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": "btn_back_main", '
  '"label": "🈴 На главную", "color": "secondary", "row": 1, "col": 1, "is_hidden": false, '
  '"action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('payment_check_wait',
  '⏳ <b>Проверка пока недоступна</b>\n\nПовторите через %payment_wait_seconds% сек.',
  '[{"id": "btn_intent_methods", "label": "🔄 Сменить способ", "color": "secondary", "row": 0, '
  '"col": 0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_cancel", "label": "⬅️ Назад", "color": "secondary", "row": 1, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": "btn_back_main", '
  '"label": "🈴 На главную", "color": "secondary", "row": 1, "col": 1, "is_hidden": false, '
  '"action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('payment_completed', '✅ <b>Оплата прошла успешно!</b>\n\n%payment_coupon%', '[]'),
 ('payment_coupon_message', '', '[]'),
 ('payment_creating',
  '⏳ <b>Создаём платёж</b>\n\nПодождите немного.',
  '[{"id": "btn_back_main", "label": "🈴 На главную", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('payment_failed',
  '❌ <b>Не удалось обработать платёж</b>\n\nПопробуйте позже или выберите другой способ оплаты.',
  '[{"id": "btn_intent_methods", "label": "🔄 Сменить способ", "color": "secondary", "row": 0, '
  '"col": 0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_cancel", "label": "⬅️ Назад", "color": "secondary", "row": 1, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": "btn_back_main", '
  '"label": "🈴 На главную", "color": "secondary", "row": 1, "col": 1, "is_hidden": false, '
  '"action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('payment_link_renewal',
  '💳 <b>Оплата продления</b>\n'
  '\n'
  '🔑 <b>%key(field=name)%</b>\n'
  '💰 Сумма: <b>%payment_amount%</b>\n'
  '%payment_discount_line%\n'
  'Перейдите к оплате по кнопке ниже.\n'
  '\n'
  '<i>Статус обновится автоматически; если доступна ручная проверка, используйте кнопку ниже.</i>',
  '[{"id": "btn_intent_open", "label": "💳 Перейти к оплате", "color": "secondary", "row": 0, '
  '"col": 0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_check", "label": "✅ Я оплатил", "color": "secondary", "row": 1, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_methods", "label": "🔄 Сменить способ", "color": "secondary", "row": 2, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": "btn_intent_cancel", '
  '"label": "⬅️ Назад", "color": "secondary", "row": 3, "col": 0, "is_hidden": false, '
  '"action_type": "system", "action_value": null}, {"id": "btn_back_main", "label": "🈴 На '
  'главную", "color": "secondary", "row": 3, "col": 1, "is_hidden": false, "action_type": '
  '"internal", "action_value": "cmd_back_main"}]'),
 ('payment_link_topup',
  '💰 <b>Пополнение баланса</b>\n'
  '\n'
  'На баланс: <b>%payment_nominal%</b>\n'
  'К оплате: <b>%payment_amount%</b>\n'
  'Перейдите к оплате по кнопке ниже.\n'
  '\n'
  '<i>Статус обновится автоматически; если доступна ручная проверка, используйте кнопку ниже.</i>',
  '[{"id": "btn_intent_open", "label": "💳 Перейти к оплате", "color": "secondary", "row": 0, '
  '"col": 0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_check", "label": "✅ Я оплатил", "color": "secondary", "row": 1, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_methods", "label": "🔄 Сменить способ", "color": "secondary", "row": 2, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": "btn_intent_cancel", '
  '"label": "⬅️ Назад", "color": "secondary", "row": 3, "col": 0, "is_hidden": false, '
  '"action_type": "system", "action_value": null}, {"id": "btn_back_main", "label": "🈴 На '
  'главную", "color": "secondary", "row": 3, "col": 1, "is_hidden": false, "action_type": '
  '"internal", "action_value": "cmd_back_main"}]'),
 ('payment_method_select',
  '💳 <b>Выбор способа оплаты</b>\n'
  '\n'
  '%payment_tariff%\n'
  '💰 К оплате: <b>%payment_amount%</b>\n'
  '%payment_discount_line%\n'
  'Выберите способ оплаты:',
  '[{"id": "btn_intent_provider_crypto", "label": "🪙 Оплатить USDT", "color": "secondary", "row": '
  '0, "col": 0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_provider_stars", "label": "⭐ Оплатить звёздами", "color": "secondary", "row": 1, '
  '"col": 0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_provider_cards", "label": "💳 TG payments", "color": "secondary", "row": 2, "col": '
  '0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_provider_yookassa_qr", "label": "📱 ЮКасса", "color": "secondary", "row": 3, "col": '
  '0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_provider_wata", "label": "🌊 WATA", "color": "secondary", "row": 4, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_provider_platega", "label": "💸 Platega", "color": "secondary", "row": 5, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_provider_cardlink", "label": "🔗 Cardlink", "color": "secondary", "row": 6, "col": '
  '0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_provider_demo", "label": "🏦 Демо оплата", "color": "secondary", "row": 7, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_balance", "label": "💎 Использовать баланс", "color": "secondary", "row": 8, "col": '
  '0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_cancel", "label": "⬅️ Назад", "color": "secondary", "row": 9, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": "btn_back_main", '
  '"label": "🈴 На главную", "color": "secondary", "row": 9, "col": 1, "is_hidden": false, '
  '"action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('payment_method_select_renewal',
  '💳 <b>Продление ключа</b>\n'
  '\n'
  '🔑 <b>%key(field=name)%</b>\n'
  '💰 К оплате: <b>%payment_amount%</b>\n'
  '%payment_discount_line%\n'
  'Выберите способ оплаты:',
  '[{"id": "btn_intent_provider_crypto", "label": "🪙 Оплатить USDT", "color": "secondary", "row": '
  '0, "col": 0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_provider_stars", "label": "⭐ Оплатить звёздами", "color": "secondary", "row": 1, '
  '"col": 0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_provider_cards", "label": "💳 TG payments", "color": "secondary", "row": 2, "col": '
  '0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_provider_yookassa_qr", "label": "📱 ЮКасса", "color": "secondary", "row": 3, "col": '
  '0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_provider_wata", "label": "🌊 WATA", "color": "secondary", "row": 4, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_provider_platega", "label": "💸 Platega", "color": "secondary", "row": 5, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_provider_cardlink", "label": "🔗 Cardlink", "color": "secondary", "row": 6, "col": '
  '0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_provider_demo", "label": "🏦 Демо оплата", "color": "secondary", "row": 7, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_balance", "label": "💎 Использовать баланс", "color": "secondary", "row": 8, "col": '
  '0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_cancel", "label": "⬅️ Назад", "color": "secondary", "row": 9, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": "btn_back_main", '
  '"label": "🈴 На главную", "color": "secondary", "row": 9, "col": 1, "is_hidden": false, '
  '"action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('payment_method_select_surcharge',
  '💎 <b>Доплата после списания баланса</b>\n'
  '\n'
  'С баланса: <b>%payment_balance_deduct%</b>\n'
  'Осталось оплатить: <b>%payment_remaining%</b>\n'
  'Выберите способ доплаты:',
  '[{"id": "btn_intent_provider_crypto", "label": "🪙 Оплатить USDT", "color": "secondary", "row": '
  '0, "col": 0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_provider_stars", "label": "⭐ Оплатить звёздами", "color": "secondary", "row": 1, '
  '"col": 0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_provider_cards", "label": "💳 TG payments", "color": "secondary", "row": 2, "col": '
  '0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_provider_yookassa_qr", "label": "📱 ЮКасса", "color": "secondary", "row": 3, "col": '
  '0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_provider_wata", "label": "🌊 WATA", "color": "secondary", "row": 4, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_provider_platega", "label": "💸 Platega", "color": "secondary", "row": 5, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_provider_cardlink", "label": "🔗 Cardlink", "color": "secondary", "row": 6, "col": '
  '0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_provider_demo", "label": "🏦 Демо оплата", "color": "secondary", "row": 7, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_balance", "label": "💎 Использовать баланс", "color": "secondary", "row": 8, "col": '
  '0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_cancel", "label": "⬅️ Назад", "color": "secondary", "row": 9, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": "btn_back_main", '
  '"label": "🈴 На главную", "color": "secondary", "row": 9, "col": 1, "is_hidden": false, '
  '"action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('payment_method_select_topup',
  '💰 <b>Пополнение баланса</b>\n'
  '\n'
  'На баланс: <b>%payment_nominal%</b>\n'
  'К оплате: <b>%payment_amount%</b>\n'
  'Выберите способ оплаты:',
  '[{"id": "btn_intent_provider_crypto", "label": "🪙 Оплатить USDT", "color": "secondary", "row": '
  '0, "col": 0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_provider_stars", "label": "⭐ Оплатить звёздами", "color": "secondary", "row": 1, '
  '"col": 0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_provider_cards", "label": "💳 TG payments", "color": "secondary", "row": 2, "col": '
  '0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_provider_yookassa_qr", "label": "📱 ЮКасса", "color": "secondary", "row": 3, "col": '
  '0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_provider_wata", "label": "🌊 WATA", "color": "secondary", "row": 4, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_provider_platega", "label": "💸 Platega", "color": "secondary", "row": 5, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_provider_cardlink", "label": "🔗 Cardlink", "color": "secondary", "row": 6, "col": '
  '0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_provider_demo", "label": "🏦 Демо оплата", "color": "secondary", "row": 7, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_balance", "label": "💎 Использовать баланс", "color": "secondary", "row": 8, "col": '
  '0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_promo", "label": "🎟 Ввести промокод", "color": "secondary", "row": 9, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": "btn_intent_cancel", '
  '"label": "⬅️ Назад", "color": "secondary", "row": 10, "col": 0, "is_hidden": false, '
  '"action_type": "system", "action_value": null}, {"id": "btn_back_main", "label": "🈴 На '
  'главную", "color": "secondary", "row": 10, "col": 1, "is_hidden": false, "action_type": '
  '"internal", "action_value": "cmd_back_main"}]'),
 ('payment_minimum_unavailable',
  '⚠️ <b>Сумма слишком мала</b>\n'
  '\n'
  'Минимальная сумма для выбранного способа: <b>%payment_minimum%</b>.',
  '[{"id": "btn_intent_methods", "label": "🔄 Сменить способ", "color": "secondary", "row": 0, '
  '"col": 0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_cancel", "label": "⬅️ Назад", "color": "secondary", "row": 1, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": "btn_back_main", '
  '"label": "🈴 На главную", "color": "secondary", "row": 1, "col": 1, "is_hidden": false, '
  '"action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('payment_order_unavailable',
  '⚠️ <b>Платёж не найден</b>\n\nОткройте оплату заново — прежний счёт мог устареть.',
  '[{"id": "btn_back_main", "label": "🈴 На главную", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('payment_pending',
  '⏳ <b>Платёж ещё не поступил</b>\n\nЗавершите оплату и повторите проверку немного позже.',
  '[{"id": "btn_intent_methods", "label": "🔄 Сменить способ", "color": "secondary", "row": 0, '
  '"col": 0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_cancel", "label": "⬅️ Назад", "color": "secondary", "row": 1, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": "btn_back_main", '
  '"label": "🈴 На главную", "color": "secondary", "row": 1, "col": 1, "is_hidden": false, '
  '"action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('payment_status', '%платеж_провайдер%\n\n%платеж_инструкция%%платеж_подсказка%', '[]'),
 ('payment_tariff_select',
  '💳 <b>Выбор тарифа</b>\n\nВыберите подходящий тариф:',
  '[{"id": "btn_tariff_items", "label": "💳 %item_name% — %item_price%", "color": "secondary", '
  '"row": 0, "col": 0, "is_hidden": false, "action_type": "system_collection", "action_value": '
  'null}, {"id": "btn_tariff_back", "label": "⬅️ Назад", "color": "secondary", "row": 1000, "col": '
  '0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": "btn_back_main", '
  '"label": "🈴 На главную", "color": "secondary", "row": 1000, "col": 1, "is_hidden": false, '
  '"action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('payment_unavailable',
  '⚠️ <b>Оплата недоступна</b>\n\nВыберите другой способ оплаты или попробуйте позже.',
  '[{"id": "btn_intent_methods", "label": "🔄 Сменить способ", "color": "secondary", "row": 0, '
  '"col": 0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_cancel", "label": "⬅️ Назад", "color": "secondary", "row": 1, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": "btn_back_main", '
  '"label": "🈴 На главную", "color": "secondary", "row": 1, "col": 1, "is_hidden": false, '
  '"action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('prepayment',
  '💳 <b>Купить ключ</b>\n'
  '\n'
  '🔐 <b>Что вы получаете:</b>\n'
  '• Доступ к нескольким серверам и протоколам\n'
  '• 1 ключ = 1 устройство (одновременное подключение)\n'
  '• Лимит трафика: до 1 ТБ в месяц (сброс каждые 30 дней)\n'
  '\n'
  '⚠️ <b>Важно знать:</b>\n'
  '• Средства не возвращаются — услуга считается оказанной в момент получения ключа\n'
  '• Мы не даём никаких гарантий бесперебойной работы сервиса в будущем\n'
  '• Мы не можем гарантировать, что данная технология останется рабочей\n'
  '\n'
  '<i>Приобретая ключ, вы соглашаетесь с этими условиями.</i>',
  '[{"id": "btn_tariff_items", "label": "💳 %item_name% — %item_price%", "color": "secondary", '
  '"row": 0, "col": 0, "is_hidden": false, "action_type": "system_collection", "action_value": '
  'null}, {"id": "btn_enter_promo", "label": "🎟 Ввести промокод", "color": "secondary", "row": '
  '999, "col": 0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_back_main", "label": "🈴 На главную", "color": "secondary", "row": 1000, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('prepayment_unavailable',
  '💳 <b>Купить ключ</b>\n'
  '\n'
  '😔 К сожалению, сейчас оплата недоступна.\n'
  '\n'
  'Попробуйте позже или обратитесь в поддержку.',
  '[{"id": "btn_back_main", "label": "🈴 На главную", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('promo_applied',
  '✅ <b>Промокод применён</b>\n\nКод: <code>%promo_code%</code>\nСкидка: <b>%promo_discount%%</b>',
  '[{"id": "btn_promo_return", "label": "💳 К оплате", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": "btn_back_main", '
  '"label": "🈴 На главную", "color": "secondary", "row": 1, "col": 0, "is_hidden": false, '
  '"action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('promo_enter',
  '🎟 <b>Промокод</b>\n'
  '\n'
  'Отправьте промокод или одноразовый купон одним сообщением.\n'
  '\n'
  'Ручной ввод заменит промокод, который мог быть сохранён по промо-ссылке.',
  '[{"id": "btn_promo_return", "label": "⬅️ Назад", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": "btn_back_main", '
  '"label": "🈴 На главную", "color": "secondary", "row": 1, "col": 0, "is_hidden": false, '
  '"action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('promo_exhausted',
  '⚪ <b>Промокод уже использован</b>\n\nВернитесь к оплате и выберите другой вариант.',
  '[{"id": "btn_promo_return", "label": "💳 К оплате", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": "btn_back_main", '
  '"label": "🈴 На главную", "color": "secondary", "row": 1, "col": 0, "is_hidden": false, '
  '"action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('promo_expired',
  '⌛ <b>Срок промокода истёк</b>\n\nВернитесь к оплате и выберите другой вариант.',
  '[{"id": "btn_promo_return", "label": "💳 К оплате", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": "btn_back_main", '
  '"label": "🈴 На главную", "color": "secondary", "row": 1, "col": 0, "is_hidden": false, '
  '"action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('promo_inactive',
  '⚪ <b>Промокод неактивен</b>\n\nВернитесь к оплате и выберите другой вариант.',
  '[{"id": "btn_promo_return", "label": "💳 К оплате", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": "btn_back_main", '
  '"label": "🈴 На главную", "color": "secondary", "row": 1, "col": 0, "is_hidden": false, '
  '"action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('promo_invalid',
  '⚠️ <b>Некорректный промокод</b>\n\nПроверьте введённое значение и попробуйте снова.',
  '[{"id": "btn_promo_return", "label": "⬅️ Назад", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": "btn_back_main", '
  '"label": "🈴 На главную", "color": "secondary", "row": 1, "col": 0, "is_hidden": false, '
  '"action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('promo_link_saved',
  '🎟 <b>Промокод сохранён</b>\n\nКод <code>%promo_code%</code> будет применён при оплате.',
  '[{"id": "btn_promo_return", "label": "💳 Перейти к оплате", "color": "secondary", "row": 0, '
  '"col": 0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_back_main", "label": "🈴 На главную", "color": "secondary", "row": 1, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('promo_not_found',
  '❌ <b>Промокод не найден</b>\n\nПроверьте код или вернитесь к оплате.',
  '[{"id": "btn_promo_return", "label": "⬅️ Назад", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": "btn_back_main", '
  '"label": "🈴 На главную", "color": "secondary", "row": 1, "col": 0, "is_hidden": false, '
  '"action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('promo_status', '%промо_статус_заголовок%\n\n%промо_статус_текст%', '[]'),
 ('promo_unavailable',
  '⚠️ <b>Промокоды недоступны</b>\n\nВернитесь к оплате и выберите другой вариант.',
  '[{"id": "btn_promo_return", "label": "💳 К оплате", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": "btn_back_main", '
  '"label": "🈴 На главную", "color": "secondary", "row": 1, "col": 0, "is_hidden": false, '
  '"action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('qr_payment',
  '💳 <b>Оплата</b>\n'
  '\n'
  '%payment_tariff%\n'
  '💰 Сумма: <b>%payment_amount%</b>\n'
  '%payment_discount_line%\n'
  'Перейдите по ссылке или отсканируйте QR-код.\n'
  '\n'
  '<i>Статус обновится автоматически; если доступна ручная проверка, используйте кнопку ниже.</i>',
  '[{"id": "btn_intent_open", "label": "💳 Перейти к оплате", "color": "secondary", "row": 0, '
  '"col": 0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_check", "label": "✅ Я оплатил", "color": "secondary", "row": 1, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": '
  '"btn_intent_methods", "label": "🔄 Сменить способ", "color": "secondary", "row": 2, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": "btn_intent_cancel", '
  '"label": "⬅️ Назад", "color": "secondary", "row": 3, "col": 0, "is_hidden": false, '
  '"action_type": "system", "action_value": null}, {"id": "btn_back_main", "label": "🈴 На '
  'главную", "color": "secondary", "row": 3, "col": 1, "is_hidden": false, "action_type": '
  '"internal", "action_value": "cmd_back_main"}]'),
 ('referral',
  '👥 <b>Реферальная система</b>\n'
  '\n'
  '📎 Ваша реферальная ссылка:\n'
  '<code>%referral_link%</code>\n'
  '\n'
  '━━━━━━━━━━━━━━━\n'
  '📝 <b>Условия:</b>\n'
  'Приглашённые пользователи регистрируются по вашей ссылке. Когда они оплачивают подписку, вы '
  'получаете реферальное вознаграждение.\n'
  '\n'
  '━━━━━━━━━━━━━━━\n'
  '📊 <b>Ваша статистика:</b>\n'
  '\n'
  '%referral_stats%',
  '[{"id": "btn_back_main", "label": "🈴 На главную", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('renew_payment',
  '💳 <b>Продление ключа</b>\n\n🔑 <b>%key(field=name)%</b>\nВыберите тариф:',
  '[{"id": "btn_tariff_items", "label": "💳 %item_name% — %item_price%", "color": "secondary", '
  '"row": 0, "col": 0, "is_hidden": false, "action_type": "system_collection", "action_value": '
  'null}, {"id": "btn_renew_enter_promo", "label": "🎟 Ввести промокод", "color": "secondary", '
  '"row": 999, "col": 0, "is_hidden": false, "action_type": "system", "action_value": null}, '
  '{"id": "btn_tariff_back", "label": "⬅️ Назад", "color": "secondary", "row": 1000, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": "btn_back_main", '
  '"label": "🈴 На главную", "color": "secondary", "row": 1000, "col": 1, "is_hidden": false, '
  '"action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('renew_payment_unavailable',
  '💳 <b>Продление ключа</b>\n\n😔 Способы оплаты временно недоступны.\nПопробуйте позже.',
  '[{"id": "btn_renew_back", "label": "⬅️ Назад", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": "btn_back_main", '
  '"label": "🈴 На главную", "color": "secondary", "row": 0, "col": 1, "is_hidden": false, '
  '"action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('screen_unavailable',
  '⚠️ <b>Экран недоступен</b>\n\nВернитесь на главную и попробуйте ещё раз.',
  '[{"id": "btn_back_main", "label": "🈴 На главную", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('show_id',
  '🆔 <b>Ваш Telegram ID</b>\n\n<code>%telegram_id%</code>',
  '[{"id": "btn_back_main", "label": "🈴 На главную", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('support_failed',
  '⚠️ <b>Сообщение не отправлено</b>\n\nПопробуйте позже.',
  '[{"id": "btn_back_main", "label": "🈴 На главную", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('support_format_unsupported',
  '❌ <b>Формат не поддерживается</b>\n\nОтправьте текст, фото, видео или GIF.',
  '[{"id": "btn_back_main", "label": "🈴 На главную", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('support_reply',
  '',
  '[{"id": "btn_support_reply", "label": "💬 Ответить", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "system", "action_value": null}, {"id": "btn_back_main", '
  '"label": "🈴 На главную", "color": "secondary", "row": 1, "col": 0, "is_hidden": false, '
  '"action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('support_reply_start',
  '💬 <b>Ответ в поддержку</b>\n\nОтправьте текст, фото, видео или GIF одним сообщением.',
  '[{"id": "btn_back_main", "label": "❌ Отмена", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('support_sent',
  '✅ <b>Сообщение отправлено</b>\n\nОтвет придёт сюда, в бот.',
  '[{"id": "btn_back_main", "label": "🈴 На главную", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('support_start',
  '💬 <b>Поддержка</b>\n\nОтправьте текст, фото, видео или GIF одним сообщением.',
  '[{"id": "btn_back_main", "label": "❌ Отмена", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('support_status',
  '%поддержка_статус_заголовок%\n\n%поддержка_статус_текст%',
  '[{"id": "btn_back_main", "label": "🈴 На главную", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('support_thread_unavailable',
  '❌ <b>Диалог не найден</b>\n\nНачните новое обращение в поддержку.',
  '[{"id": "btn_back_main", "label": "🈴 На главную", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('trial',
  '🎁 <b>Пробная подписка</b>\n'
  '\n'
  'Попробуйте VPN бесплатно и оцените качество соединения.\n'
  '\n'
  '%trial_offer%\n'
  '\n'
  'Нажмите кнопку ниже, чтобы активировать пробный доступ.\n'
  '\n'
  '%trial_eligibility%',
  '[{"id": "btn_activate_trial", "label": "✅ Активировать", "color": "primary", "row": 0, "col": '
  '0, "is_hidden": false, "action_type": "system", "action_value": null}, {"id": "btn_back_main", '
  '"label": "🈴 На главную", "color": "secondary", "row": 1, "col": 0, "is_hidden": false, '
  '"action_type": "internal", "action_value": "cmd_back_main"}]'),
 ('trial_already_used',
  '🎁 <b>Пробный период недоступен</b>\n'
  '\n'
  'Это пробное предложение уже недоступно для вашего аккаунта.',
  '[{"id": "btn_back_main", "label": "🈴 На главную", "color": "secondary", "row": 0, "col": 0, '
  '"is_hidden": false, "action_type": "internal", "action_value": "cmd_back_main"}]'))


_BASELINE_PAGE_UPDATED_KEYS_V97 = (
    'balance_topup_result',
    'key_delivery',
    'key_delivery_failed',
    'key_delivery_partial',
    'key_operation_failed',
    'key_operation_unavailable',
    'key_progress',
    'key_renewed',
    'new_key_inbound_select',
    'new_key_no_servers',
    'new_key_server_select',
    'payment_auto_completed',
    'payment_method_select',
    'payment_method_select_renewal',
    'payment_method_select_surcharge',
    'payment_method_select_topup',
    'prepayment',
    'renew_payment',
)


_BASELINE_PAGE_ROUTES_V97 = (('balance_topup_result', 'balance_topup_result', '["not_banned"]', '[]', 1),
 ('profile', 'custom_profile', '["not_banned"]', '[]', 1))


def migration_initial(conn: sqlite3.Connection) -> None:
    """Create the complete v97 baseline for a new empty database."""
    logger.info("Создание БД (сжатая базовая схема v97)...")

    # executescript commits any prior transaction. Opening an explicit
    # transaction inside the script keeps the complete baseline rollback-safe;
    # the caller or v98 commits it only after all seed data is present.
    conn.executescript("BEGIN IMMEDIATE;\n" + _BASELINE_SCHEMA_V97)
    conn.executemany(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        _BASELINE_SETTINGS_V97,
    )
    conn.executemany(
        """
        INSERT INTO tariff_groups (
            id, name, sort_order, monthly_traffic_reset_enabled
        ) VALUES (?, ?, ?, ?)
        """,
        _BASELINE_TARIFF_GROUPS_V97,
    )
    conn.executemany(
        """
        INSERT INTO tariffs (
            id, name, duration_days, display_order, is_active, price_rub,
            traffic_limit_gb, group_id, max_ips, system_type, price_minor
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        _BASELINE_TARIFFS_V97,
    )
    conn.executemany(
        """
        INSERT INTO referral_levels (id, level_number, percent, enabled)
        VALUES (?, ?, ?, ?)
        """,
        _BASELINE_REFERRAL_LEVELS_V97,
    )
    conn.executemany(
        """
        INSERT INTO currency_rates (
            base_currency, target_currency, units_per_base
        ) VALUES (?, ?, ?)
        """,
        _BASELINE_CURRENCY_RATES_V97,
    )
    conn.executemany(
        """
        INSERT INTO pages (page_key, text_default, buttons_default)
        VALUES (?, ?, ?)
        """,
        _BASELINE_PAGES_V97,
    )
    conn.executemany(
        "UPDATE pages SET updated_at = CURRENT_TIMESTAMP WHERE page_key = ?",
        ((page_key,) for page_key in _BASELINE_PAGE_UPDATED_KEYS_V97),
    )
    update_user_ui_text_defaults(USER_UI_TEXT_DEFINITIONS, conn=conn)
    conn.executemany(
        """
        INSERT INTO trial_offers (id, tariff_id, is_primary, is_enabled)
        VALUES (?, ?, ?, ?)
        """,
        _BASELINE_TRIAL_OFFERS_V97,
    )
    conn.executemany(
        """
        INSERT INTO page_routes (
            route_key, page_key, guard_names, hook_names, is_enabled
        ) VALUES (?, ?, ?, ?, ?)
        """,
        _BASELINE_PAGE_ROUTES_V97,
    )

    logger.info("БД создана (сжатая базовая схема v97)")


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return column names for one SQLite table."""
    return {
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _remove_direct_key_buttons_v98(conn: sqlite3.Connection) -> int:
    """Remove the retired direct-key action from default and custom layouts."""
    row = conn.execute(
        """
        SELECT buttons_default, buttons_custom
        FROM pages
        WHERE page_key = 'key_details'
        """
    ).fetchone()
    if row is None:
        return 0

    changed = 0
    updates: dict[str, str] = {}
    for column, raw_value in (
        ('buttons_default', row[0]),
        ('buttons_custom', row[1]),
    ):
        if raw_value is None:
            continue
        try:
            buttons = json.loads(raw_value)
        except (TypeError, json.JSONDecodeError):
            logger.warning(
                "Migration v98 kept malformed %s on key_details unchanged",
                column,
            )
            continue
        if not isinstance(buttons, list):
            continue

        migrated = [
            button
            for button in buttons
            if not (
                isinstance(button, dict)
                and button.get('id') == 'btn_key_show_key'
            )
        ]
        column_changed = len(migrated) != len(buttons)
        if column_changed:
            updates[column] = json.dumps(migrated, ensure_ascii=False)
            changed += 1

    if updates:
        assignments = ', '.join(f'{column} = ?' for column in updates)
        conn.execute(
            f"UPDATE pages SET {assignments} WHERE page_key = 'key_details'",
            tuple(updates.values()),
        )
    return changed


def _rebuild_servers_for_v98(conn: sqlite3.Connection) -> None:
    """Drop the retired panel API profile cache from servers."""
    conn.execute("DROP TABLE IF EXISTS servers_v98")
    conn.execute(
        """
        CREATE TABLE servers_v98 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            host TEXT NOT NULL,
            port INTEGER NOT NULL,
            web_base_path TEXT NOT NULL,
            login TEXT NOT NULL,
            password TEXT NOT NULL,
            is_active INTEGER DEFAULT 1,
            protocol TEXT DEFAULT 'https',
            api_token TEXT,
            panel_version TEXT,
            panel_checked_at TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO servers_v98 (
            id, name, host, port, web_base_path, login, password, is_active,
            protocol, api_token, panel_version, panel_checked_at
        )
        SELECT
            id, name, host, port, web_base_path, login, password, is_active,
            protocol, api_token, panel_version, panel_checked_at
        FROM servers
        """
    )
    conn.execute("DROP TABLE servers")
    conn.execute("ALTER TABLE servers_v98 RENAME TO servers")


def _rebuild_vpn_keys_for_v98(conn: sqlite3.Connection) -> int:
    """Keep only complete subscription bindings or fully unbound drafts."""
    complete_binding = (
        "server_id IS NOT NULL "
        "AND panel_email IS NOT NULL AND LENGTH(TRIM(panel_email)) > 0 "
        "AND sub_id IS NOT NULL AND LENGTH(TRIM(sub_id)) > 0"
    )
    incomplete_count = int(conn.execute(
        f"""
        SELECT COUNT(*)
        FROM vpn_keys
        WHERE NOT ({complete_binding})
          AND NOT (
              server_id IS NULL
              AND panel_email IS NULL
              AND sub_id IS NULL
          )
        """
    ).fetchone()[0])

    conn.execute("DROP TABLE IF EXISTS vpn_keys_v98")
    conn.execute(
        """
        CREATE TABLE vpn_keys_v98 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            server_id INTEGER,
            tariff_id INTEGER NOT NULL,
            panel_email TEXT,
            custom_name TEXT,
            expires_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            traffic_used INTEGER DEFAULT 0,
            traffic_limit INTEGER DEFAULT 0,
            traffic_updated_at DATETIME,
            traffic_notified_pct INTEGER DEFAULT 100,
            sub_id TEXT,
            traffic_limit_override INTEGER
                CHECK (traffic_limit_override IS NULL OR traffic_limit_override >= 0),
            max_ips_override INTEGER
                CHECK (max_ips_override IS NULL OR max_ips_override BETWEEN 1 AND 999),
            CHECK (
                (server_id IS NULL AND panel_email IS NULL AND sub_id IS NULL)
                OR
                (server_id IS NOT NULL
                 AND panel_email IS NOT NULL
                 AND sub_id IS NOT NULL
                 AND LENGTH(TRIM(panel_email)) > 0
                 AND LENGTH(TRIM(sub_id)) > 0)
            ),
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (server_id) REFERENCES servers(id),
            FOREIGN KEY (tariff_id) REFERENCES tariffs(id)
        )
        """
    )
    conn.execute(
        f"""
        INSERT INTO vpn_keys_v98 (
            id, user_id, server_id, tariff_id, panel_email, custom_name,
            expires_at, created_at, traffic_used, traffic_limit,
            traffic_updated_at, traffic_notified_pct, sub_id,
            traffic_limit_override, max_ips_override
        )
        SELECT
            id,
            user_id,
            CASE WHEN {complete_binding} THEN server_id ELSE NULL END,
            tariff_id,
            CASE WHEN {complete_binding} THEN TRIM(panel_email) ELSE NULL END,
            custom_name,
            expires_at,
            created_at,
            traffic_used,
            traffic_limit,
            traffic_updated_at,
            traffic_notified_pct,
            CASE WHEN {complete_binding} THEN TRIM(sub_id) ELSE NULL END,
            traffic_limit_override,
            max_ips_override
        FROM vpn_keys
        """
    )
    conn.execute("DROP TABLE vpn_keys")
    conn.execute("ALTER TABLE vpn_keys_v98 RENAME TO vpn_keys")
    conn.execute("CREATE INDEX idx_vpn_keys_user_id ON vpn_keys(user_id)")
    conn.execute("CREATE INDEX idx_vpn_keys_expires_at ON vpn_keys(expires_at)")
    conn.execute(
        "CREATE INDEX idx_vpn_keys_user_expires "
        "ON vpn_keys(user_id, expires_at DESC)"
    )
    conn.execute(
        "CREATE INDEX idx_vpn_keys_server_email "
        "ON vpn_keys(server_id, panel_email)"
    )
    conn.execute(
        "CREATE INDEX idx_vpn_keys_panel_email_lower "
        "ON vpn_keys(LOWER(panel_email))"
    )
    conn.execute("CREATE INDEX idx_vpn_keys_server_id ON vpn_keys(server_id)")
    return incomplete_count


def migration_98(conn: sqlite3.Connection) -> None:
    """Migration v98: enforce subscription-only keys and 3X-UI clients API."""
    rebuild_servers = 'panel_api_profile' in _table_columns(conn, 'servers')
    key_columns = _table_columns(conn, 'vpn_keys')
    rebuild_keys = bool({'panel_inbound_id', 'client_uuid'} & key_columns)
    retired_pages = 0
    removed_button_sets = 0
    incomplete_keys = 0

    # SQLite can change foreign_keys only outside a transaction. Commit earlier
    # migrations first, then keep every v98 mutation in one explicit unit so a
    # failed table rebuild cannot leave customization or schema half-updated.
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM settings WHERE key = 'bot_mode'")
        conn.execute(
            "DELETE FROM page_routes WHERE page_key IN (?, ?)",
            ('new_key_inbound_select', 'key_replace_inbound_select'),
        )
        retired_pages = conn.execute(
            "DELETE FROM pages WHERE page_key IN (?, ?)",
            ('new_key_inbound_select', 'key_replace_inbound_select'),
        ).rowcount
        removed_button_sets = _remove_direct_key_buttons_v98(conn)

        if rebuild_servers:
            _rebuild_servers_for_v98(conn)
        if rebuild_keys:
            incomplete_keys = _rebuild_vpn_keys_for_v98(conn)

        foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise RuntimeError(
                "foreign_key_check failed after subscription-only rebuild: "
                f"{foreign_key_errors[:5]}"
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")

    logger.info(
        "Migration v98 applied: retired_pages=%s, removed_button_sets=%s, "
        "normalized_incomplete_keys=%s",
        retired_pages,
        removed_button_sets,
        incomplete_keys,
    )


_CRYPTOBOT_METHOD_PAGE_KEYS_V99 = (
    'payment_method_select',
    'payment_method_select_renewal',
    'payment_method_select_surcharge',
    'payment_method_select_topup',
)


def migration_99(conn: sqlite3.Connection) -> None:
    """Migration v99: add Crypto Pay settings and stock method buttons."""
    conn.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
        ('cryptobot_enabled', '0'),
    )
    conn.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
        ('cryptobot_api_token', ''),
    )

    updated_pages = 0
    for page_key in _CRYPTOBOT_METHOD_PAGE_KEYS_V99:
        row = conn.execute(
            "SELECT buttons_default FROM pages WHERE page_key = ?",
            (page_key,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Required payment page is missing: {page_key}")
        try:
            buttons = json.loads(row[0] or '[]')
        except (TypeError, json.JSONDecodeError) as error:
            raise RuntimeError(
                f"Payment page has malformed default buttons: {page_key}"
            ) from error
        if not isinstance(buttons, list):
            raise RuntimeError(
                f"Payment page default buttons are not an array: {page_key}"
            )
        if any(
            isinstance(button, dict)
            and button.get('id') == 'btn_intent_provider_cryptobot'
            for button in buttons
        ):
            continue

        legacy_crypto_index = next(
            (
                index
                for index, button in enumerate(buttons)
                if isinstance(button, dict)
                and button.get('id') == 'btn_intent_provider_crypto'
            ),
            0,
        )
        legacy_crypto = (
            buttons[legacy_crypto_index]
            if 0 <= legacy_crypto_index < len(buttons)
            else None
        )
        if (
            isinstance(legacy_crypto, dict)
            and legacy_crypto.get('row') == 0
            and legacy_crypto.get('col') == 0
        ):
            legacy_crypto['col'] = 1

        buttons.insert(legacy_crypto_index, {
            'id': 'btn_intent_provider_cryptobot',
            'label': '💎 CryptoBot',
            'color': 'secondary',
            'row': 0,
            'col': 0,
            'is_hidden': False,
            'action_type': 'system',
            'action_value': None,
        })
        conn.execute(
            """
            UPDATE pages
            SET buttons_default = ?, updated_at = CURRENT_TIMESTAMP
            WHERE page_key = ?
            """,
            (
                json.dumps(buttons, ensure_ascii=False, separators=(',', ':')),
                page_key,
            ),
        )
        updated_pages += 1

    logger.info(
        "Migration v99 applied: Crypto Pay settings seeded, payment_pages=%s",
        updated_pages,
    )


MIGRATIONS = {
    98: migration_98,
    99: migration_99,
}


def _assert_migration_database_integrity(
    conn: sqlite3.Connection,
    *,
    stage: str,
) -> None:
    """Fail a migration boundary on structural or foreign-key corruption."""
    quick_rows = conn.execute('PRAGMA quick_check').fetchall()
    if len(quick_rows) != 1 or quick_rows[0][0] != 'ok':
        raise RuntimeError(
            f"quick_check failed {stage}: {quick_rows[:5]}"
        )
    foreign_key_rows = conn.execute('PRAGMA foreign_key_check').fetchall()
    if foreign_key_rows:
        raise RuntimeError(
            f"foreign_key_check failed {stage}: {foreign_key_rows[:5]}"
        )


def run_migrations() -> None:
    """
    Runs all necessary migrations.
    
    Logic:
    - version = 0 (new install): calls migration_initial → sets INITIAL_VERSION → applies incremental migrations up to LATEST_VERSION
    - version = LATEST_VERSION: does nothing
    - version < INITIAL_VERSION: error (need to update via intermediate version)
    - version >= INITIAL_VERSION: applies incremental migrations from MIGRATIONS
    """
    try:
        current = get_current_version()
        
        if current >= LATEST_VERSION:
            logger.info(f"✅ БД соответствует версии {LATEST_VERSION}. Миграция не требуется.")
            return
        
        # Protection: Database on an intermediate version that cannot be updated with compressed migrations
        if 0 < current < INITIAL_VERSION:
            raise RuntimeError(
                f"Версия БД ({current}) ниже минимально поддерживаемой ({INITIAL_VERSION}). "
                f"Сначала обновите бот до промежуточной версии, чтобы БД мигрировала до v{INITIAL_VERSION}."
            )

        with get_db() as validation_conn:
            _assert_migration_database_integrity(
                validation_conn,
                stage="before migrations",
            )
        
        logger.info(f"🔄 Требуется миграция БД с версии {current} до {LATEST_VERSION}")
        
        with get_db() as conn:
            # New installation - creating a database from scratch
            if current == 0:
                migration_initial(conn)
                set_version(conn, INITIAL_VERSION)
                current = INITIAL_VERSION
            
            # Incremental migrations after the compressed baseline.
            for version in range(current + 1, LATEST_VERSION + 1):
                if version in MIGRATIONS:
                    logger.info(f"🚀 Применяю миграцию v{version}...")
                    MIGRATIONS[version](conn)
                    set_version(conn, version)

        with get_db() as validation_conn:
            _assert_migration_database_integrity(
                validation_conn,
                stage="after migrations",
            )
            version_row = validation_conn.execute(
                "SELECT version FROM schema_version LIMIT 1"
            ).fetchone()
            final_version = int(version_row[0]) if version_row else 0
            if final_version != LATEST_VERSION:
                raise RuntimeError(
                    f"schema version mismatch after migrations: "
                    f"expected {LATEST_VERSION}, got {final_version}"
                )
        
        logger.info(f"✅ Миграция успешная: БД обновлена до версии {LATEST_VERSION}")
        
    except Exception as e:
        logger.error(f"❌ Неуспешная миграция: {e}")
        raise
