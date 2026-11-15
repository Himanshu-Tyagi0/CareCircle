"""Production repository wiring failing closed unless valid database settings exist."""

from __future__ import annotations

from core.config import AppConfig
from core.errors import ConfigurationError

from repositories.interfaces import Repositories
from repositories.supabase_repo import SupabaseRpcClient


def create_postgres_repositories(config: AppConfig) -> Repositories:
    """Create production repositories; fail closed when production DB config is incomplete."""
    config.require_production_ready()
    if config.has_supabase:
        # Validate Supabase service-role client initialization cleanly
        SupabaseRpcClient(config)
    raise ConfigurationError(
        "errors.config_missing",
        params={"missing": "PRODUCTION_RPC_ADAPTER_CONFIG"},
        detail="Production Supabase RPC adapter requires live Supabase project & schema provisioning.",
    )
