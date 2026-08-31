drop policy if exists "Users can read own watchlist" on public.watchlist;
drop policy if exists "Users can insert own watchlist" on public.watchlist;
drop policy if exists "Users can delete own watchlist" on public.watchlist;
revoke select, insert, update, delete on public.watchlist from anon, authenticated;
