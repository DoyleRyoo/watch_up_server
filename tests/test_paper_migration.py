import re
from pathlib import Path


MIGRATION = (
    Path(__file__).parents[1]
    / "supabase"
    / "migrations"
    / "20260901000000_create_paper_trading.sql"
)


def _sql() -> str:
    return " ".join(MIGRATION.read_text().lower().split())


def test_creates_exact_paper_tables_and_enum_values() -> None:
    sql = _sql()

    assert re.findall(r"create table public\.(paper_\w+)", sql) == [
        "paper_accounts",
        "paper_transactions",
        "paper_positions",
    ]
    assert (
        "paper_transaction_type as enum ('initial_grant','top_up','buy','sell')" in sql
    )
    assert "paper_asset_class as enum ('crypto')" in sql
    assert "create table public.coin_" not in sql
    assert "create table public.instruments" not in sql


def test_preserves_money_types_checks_and_foreign_key_cascades() -> None:
    sql = _sql()

    for expression in (
        "user_id uuid primary key references auth.users(id) on delete cascade",
        "cash_balance_krw bigint not null check (cash_balance_krw >= 0)",
        "lifetime_top_up_krw bigint not null default 0 check (lifetime_top_up_krw >= 0)",
        "account_id uuid not null references public.paper_accounts(user_id) on delete cascade",
        "execution_price numeric(38,18)",
        "quantity numeric(38,18)",
        "disposed_cost_basis_krw numeric(38,18)",
        "realized_pnl_krw numeric(38,18)",
        "quantity numeric(38,18) not null default 0 check (quantity >= 0)",
        "cost_basis_krw numeric(38,18) not null default 0 check (cost_basis_krw >= 0)",
    ):
        assert expression in sql

    assert sql.count("on delete cascade") == 3
    assert "paper_accounts ( id " not in sql
    assert "check (realized_pnl_krw" not in sql


def test_creates_history_uniqueness_and_positive_position_indexes() -> None:
    sql = _sql()

    for expression in (
        "on public.paper_transactions (account_id, id desc)",
        "on public.paper_transactions (account_id, idempotency_key) where idempotency_key is not null",
        "on public.paper_transactions (account_id) where type = 'initial_grant'",
        "unique (account_id, asset_class, market_code)",
        "on public.paper_positions (account_id) where quantity > 0",
    ):
        assert expression in sql


def test_enables_rls_and_limits_authenticated_grants() -> None:
    sql = _sql()

    for table in ("paper_accounts", "paper_transactions", "paper_positions"):
        assert f"alter table public.{table} enable row level security" in sql

    for policy in (
        "for select using (auth.uid() = user_id)",
        "for insert with check (auth.uid() = user_id)",
        "for update using (auth.uid() = user_id)",
        "for select using (auth.uid() = account_id)",
        "for insert with check (auth.uid() = account_id)",
        "for update using (auth.uid() = account_id)",
    ):
        assert policy in sql

    assert "revoke delete on public.paper_accounts from authenticated" in sql
    assert (
        "grant select, insert, update on public.paper_accounts to authenticated" in sql
    )
    assert (
        "revoke update, delete on public.paper_transactions from authenticated" in sql
    )
    assert "grant select, insert on public.paper_transactions to authenticated" in sql
    assert "grant select, insert, update on public.paper_transactions" not in sql
    assert "revoke delete on public.paper_positions from authenticated" in sql
    assert (
        "grant select, insert, update on public.paper_positions to authenticated" in sql
    )


def test_does_not_duplicate_or_modify_watchlist_migration() -> None:
    sql = _sql()

    assert "watchlist" not in sql
    assert "drop table" not in sql
    assert "delete from" not in sql
    assert "truncate" not in sql
    assert "insert into" not in sql
