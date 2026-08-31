from pathlib import Path


MIGRATIONS = Path(__file__).parents[1] / "supabase" / "migrations"
CREATE_MIGRATION = MIGRATIONS / "20260805000000_create_watchlist.sql"
DEACTIVATE_MIGRATION = MIGRATIONS / "20260831000000_deactivate_watchlist.sql"


def test_deactivation_matches_existing_policy_names_and_revokes_data_api_access() -> (
    None
):
    created = CREATE_MIGRATION.read_text()
    deactivated = DEACTIVATE_MIGRATION.read_text()

    for policy in (
        "Users can read own watchlist",
        "Users can insert own watchlist",
        "Users can delete own watchlist",
    ):
        assert f'create policy "{policy}"' in created
        assert f'drop policy if exists "{policy}" on public.watchlist;' in deactivated

    assert (
        "revoke select, insert, update, delete on public.watchlist "
        "from anon, authenticated;"
    ) in deactivated


def test_deactivation_preserves_table_rows_columns_and_rls_state() -> None:
    sql = DEACTIVATE_MIGRATION.read_text().lower()

    assert "drop table" not in sql
    assert "delete from" not in sql
    assert "truncate" not in sql
    assert "alter table" not in sql
    assert "insert into" not in sql
