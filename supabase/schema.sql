-- =============================================================================
-- JARVIS — Schéma Supabase (Step 1 : Data Layer)
-- =============================================================================
-- À exécuter dans l'éditeur SQL Supabase (ou via `supabase db push` en migration).
-- Le script est ré-exécutable : objets créés avec IF NOT EXISTS / OR REPLACE,
-- politiques supprimées puis recréées.
--
-- Principes :
--   * Chaque ligne appartient à un utilisateur ; RLS filtre via auth.uid().
--   * Le client Flutter (rôle `authenticated`) ne peut PAS modifier son palier
--     ni ses limites de quota, ni écrire des messages `assistant`.
--   * Le backend FastAPI utilise la clé `service_role` (bypass RLS) pour écrire
--     les réponses IA et synchroniser la consommation des quotas.
--   * Redis = compteur temps réel ; table `quota_usage` = trace persistante.
--   * Jour de quota = date UTC (reset à minuit UTC).
-- =============================================================================

create extension if not exists pgcrypto;

-- -----------------------------------------------------------------------------
-- Types
-- -----------------------------------------------------------------------------
do $$
begin
  if not exists (select 1 from pg_type where typname = 'plan_tier') then
    create type public.plan_tier as enum ('free', 'pro');
  end if;
  if not exists (select 1 from pg_type where typname = 'message_role') then
    create type public.message_role as enum ('user', 'assistant', 'system');
  end if;
end$$;

-- -----------------------------------------------------------------------------
-- Fonction utilitaire : updated_at automatique
-- -----------------------------------------------------------------------------
create or replace function public.set_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

-- =============================================================================
-- Table : users (extension de profil de auth.users + quotas)
-- =============================================================================
-- Limites à NULL = illimité (palier Pro « illimité »).
create table if not exists public.users (
  id                   uuid primary key references auth.users (id) on delete cascade,
  email                text,
  display_name         text check (char_length(display_name) <= 80),
  avatar_url           text,
  preferences          jsonb not null default '{}'::jsonb,
  plan                 public.plan_tier not null default 'free',
  daily_message_limit  integer default 30    check (daily_message_limit is null or daily_message_limit >= 0),
  daily_token_limit    integer default 50000 check (daily_token_limit  is null or daily_token_limit  >= 0),
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now()
);

drop trigger if exists users_set_updated_at on public.users;
create trigger users_set_updated_at
  before update on public.users
  for each row execute function public.set_updated_at();

-- Création automatique du profil à l'inscription (auth.users -> public.users)
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.users (id, email, display_name, avatar_url)
  values (
    new.id,
    new.email,
    coalesce(new.raw_user_meta_data ->> 'display_name', new.raw_user_meta_data ->> 'full_name'),
    new.raw_user_meta_data ->> 'avatar_url'
  )
  on conflict (id) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- =============================================================================
-- Table : conversations
-- =============================================================================
create table if not exists public.conversations (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid not null references public.users (id) on delete cascade,
  title            text not null default 'Nouvelle conversation' check (char_length(title) <= 200),
  persona          text,              -- identifiant de persona (table dédiée dans une étape ultérieure)
  archived         boolean not null default false,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  last_message_at  timestamptz not null default now(),
  -- Permet la FK composite depuis messages (garantit messages.user_id = propriétaire)
  unique (id, user_id)
);

create index if not exists conversations_user_recent_idx
  on public.conversations (user_id, archived, last_message_at desc);

drop trigger if exists conversations_set_updated_at on public.conversations;
create trigger conversations_set_updated_at
  before update on public.conversations
  for each row execute function public.set_updated_at();

-- =============================================================================
-- Table : messages
-- =============================================================================
-- user_id est dénormalisé pour des politiques RLS simples et rapides ;
-- la FK composite empêche d'insérer un message dans la conversation d'un autre.
-- attachments : tableau JSON de chemins Storage, ex.
--   [{"path": "<user_id>/<conv_id>/rapport.pdf", "mime": "application/pdf", "size": 12345}]
create table if not exists public.messages (
  id               uuid primary key default gen_random_uuid(),
  conversation_id  uuid not null,
  user_id          uuid not null,
  role             public.message_role not null,
  content          text not null default '',
  attachments      jsonb not null default '[]'::jsonb check (jsonb_typeof(attachments) = 'array'),
  model            text,               -- ex. 'gpt-4o-mini' (messages assistant)
  tokens_input     integer check (tokens_input  is null or tokens_input  >= 0),
  tokens_output    integer check (tokens_output is null or tokens_output >= 0),
  created_at       timestamptz not null default now(),
  foreign key (conversation_id, user_id)
    references public.conversations (id, user_id) on delete cascade
);

create index if not exists messages_conversation_created_idx
  on public.messages (conversation_id, created_at);
create index if not exists messages_user_idx
  on public.messages (user_id);

-- Met à jour conversations.last_message_at à chaque nouveau message
create or replace function public.touch_conversation()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  update public.conversations
     set last_message_at = new.created_at
   where id = new.conversation_id;
  return new;
end;
$$;

drop trigger if exists messages_touch_conversation on public.messages;
create trigger messages_touch_conversation
  after insert on public.messages
  for each row execute function public.touch_conversation();

-- =============================================================================
-- Table : quota_usage (trace persistante de la consommation journalière)
-- =============================================================================
-- Redis tient le compteur temps réel (quota:{user_id}:{YYYY-MM-DD}) ;
-- le backend reporte ici la consommation après chaque réponse.
create table if not exists public.quota_usage (
  user_id        uuid not null references public.users (id) on delete cascade,
  usage_date     date not null default ((now() at time zone 'utc')::date),
  messages_used  integer not null default 0 check (messages_used >= 0),
  tokens_used    integer not null default 0 check (tokens_used  >= 0),
  updated_at     timestamptz not null default now(),
  primary key (user_id, usage_date)
);

-- Incrément atomique (appelé par le backend avec la clé service_role)
create or replace function public.increment_quota_usage(
  p_user_id  uuid,
  p_messages integer,
  p_tokens   integer
)
returns public.quota_usage
language sql
security definer
set search_path = ''
as $$
  insert into public.quota_usage as q (user_id, usage_date, messages_used, tokens_used)
  values (p_user_id, (now() at time zone 'utc')::date, greatest(p_messages, 0), greatest(p_tokens, 0))
  on conflict (user_id, usage_date) do update
     set messages_used = q.messages_used + excluded.messages_used,
         tokens_used   = q.tokens_used   + excluded.tokens_used,
         updated_at    = now()
  returning q.*;
$$;

revoke all on function public.increment_quota_usage(uuid, integer, integer) from public, anon, authenticated;
grant execute on function public.increment_quota_usage(uuid, integer, integer) to service_role;

-- Lecture du quota du jour pour l'utilisateur connecté (rpc côté Flutter).
-- NB : peut avoir un léger retard sur Redis ; la valeur temps réel est
-- renvoyée par FastAPI dans les en-têtes / événements SSE.
create or replace function public.get_my_quota()
returns table (
  plan                public.plan_tier,
  daily_message_limit integer,
  daily_token_limit   integer,
  messages_used       integer,
  tokens_used         integer,
  usage_date          date
)
language sql
stable
security definer
set search_path = ''
as $$
  select u.plan,
         u.daily_message_limit,
         u.daily_token_limit,
         coalesce(q.messages_used, 0),
         coalesce(q.tokens_used, 0),
         (now() at time zone 'utc')::date
    from public.users u
    left join public.quota_usage q
      on q.user_id = u.id
     and q.usage_date = (now() at time zone 'utc')::date
   where u.id = auth.uid();
$$;

revoke all on function public.get_my_quota() from public, anon;
grant execute on function public.get_my_quota() to authenticated;

-- =============================================================================
-- Privilèges : le client ne touche jamais au palier / aux limites
-- =============================================================================
revoke all on public.users, public.conversations, public.messages, public.quota_usage from anon;

revoke insert, update, delete on public.users from authenticated;
grant select on public.users to authenticated;
grant update (display_name, avatar_url, preferences) on public.users to authenticated;

grant select, insert, update, delete on public.conversations to authenticated;

revoke update on public.messages from authenticated;           -- messages immuables
grant select, insert, delete on public.messages to authenticated;

revoke insert, update, delete on public.quota_usage from authenticated;
grant select on public.quota_usage to authenticated;

-- =============================================================================
-- Row Level Security
-- =============================================================================
alter table public.users         enable row level security;
alter table public.conversations enable row level security;
alter table public.messages      enable row level security;
alter table public.quota_usage   enable row level security;

-- (select auth.uid()) : évalué une seule fois par requête (recommandation Supabase)

-- users -----------------------------------------------------------------------
drop policy if exists "users_select_own" on public.users;
create policy "users_select_own" on public.users
  for select to authenticated
  using (id = (select auth.uid()));

drop policy if exists "users_update_own" on public.users;
create policy "users_update_own" on public.users
  for update to authenticated
  using (id = (select auth.uid()))
  with check (id = (select auth.uid()));

-- conversations ---------------------------------------------------------------
drop policy if exists "conversations_select_own" on public.conversations;
create policy "conversations_select_own" on public.conversations
  for select to authenticated
  using (user_id = (select auth.uid()));

drop policy if exists "conversations_insert_own" on public.conversations;
create policy "conversations_insert_own" on public.conversations
  for insert to authenticated
  with check (user_id = (select auth.uid()));

drop policy if exists "conversations_update_own" on public.conversations;
create policy "conversations_update_own" on public.conversations
  for update to authenticated
  using (user_id = (select auth.uid()))
  with check (user_id = (select auth.uid()));

drop policy if exists "conversations_delete_own" on public.conversations;
create policy "conversations_delete_own" on public.conversations
  for delete to authenticated
  using (user_id = (select auth.uid()));

-- messages --------------------------------------------------------------------
drop policy if exists "messages_select_own" on public.messages;
create policy "messages_select_own" on public.messages
  for select to authenticated
  using (user_id = (select auth.uid()));

-- Le client n'insère que ses propres messages `user` ; les messages `assistant`
-- sont écrits par le backend (service_role), ce qui empêche toute falsification
-- de réponses IA ou de comptage de tokens.
drop policy if exists "messages_insert_own_user_role" on public.messages;
create policy "messages_insert_own_user_role" on public.messages
  for insert to authenticated
  with check (
    user_id = (select auth.uid())
    and role = 'user'
    and tokens_input is null
    and tokens_output is null
  );

drop policy if exists "messages_delete_own" on public.messages;
create policy "messages_delete_own" on public.messages
  for delete to authenticated
  using (user_id = (select auth.uid()));

-- quota_usage -----------------------------------------------------------------
drop policy if exists "quota_usage_select_own" on public.quota_usage;
create policy "quota_usage_select_own" on public.quota_usage
  for select to authenticated
  using (user_id = (select auth.uid()));

-- =============================================================================
-- Storage : bucket privé `documents`
-- =============================================================================
-- Convention de chemin : {user_id}/{conversation_id}/{nom_fichier}
-- Le premier segment DOIT être l'uid de l'utilisateur connecté.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
  'documents',
  'documents',
  false,
  20971520,  -- 20 Mo
  array[
    'application/pdf',
    'text/plain',
    'text/markdown',
    'text/csv',
    'application/json',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'image/png',
    'image/jpeg',
    'image/webp',
    'audio/mpeg',
    'audio/wav',
    'audio/webm'
  ]
)
on conflict (id) do update
  set public             = excluded.public,
      file_size_limit    = excluded.file_size_limit,
      allowed_mime_types = excluded.allowed_mime_types;

drop policy if exists "documents_select_own" on storage.objects;
create policy "documents_select_own" on storage.objects
  for select to authenticated
  using (
    bucket_id = 'documents'
    and (storage.foldername(name))[1] = (select auth.uid())::text
  );

drop policy if exists "documents_insert_own" on storage.objects;
create policy "documents_insert_own" on storage.objects
  for insert to authenticated
  with check (
    bucket_id = 'documents'
    and (storage.foldername(name))[1] = (select auth.uid())::text
  );

drop policy if exists "documents_update_own" on storage.objects;
create policy "documents_update_own" on storage.objects
  for update to authenticated
  using (
    bucket_id = 'documents'
    and (storage.foldername(name))[1] = (select auth.uid())::text
  )
  with check (
    bucket_id = 'documents'
    and (storage.foldername(name))[1] = (select auth.uid())::text
  );

drop policy if exists "documents_delete_own" on storage.objects;
create policy "documents_delete_own" on storage.objects
  for delete to authenticated
  using (
    bucket_id = 'documents'
    and (storage.foldername(name))[1] = (select auth.uid())::text
  );

-- =============================================================================
-- Administration (exemples, à exécuter manuellement avec un rôle admin)
-- =============================================================================
-- Passer un utilisateur en Pro illimité :
--   update public.users set plan = 'pro', daily_message_limit = null, daily_token_limit = null
--    where id = '<uuid>';
-- Pro sur-mesure :
--   update public.users set plan = 'pro', daily_message_limit = 500, daily_token_limit = 1000000
--    where id = '<uuid>';
