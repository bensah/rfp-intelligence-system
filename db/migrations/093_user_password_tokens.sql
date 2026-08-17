-- 093 — one-time links for account setup and password reset.
--
-- Replaces emailing a temporary password in plaintext. A password sent by
-- email stays valid in that inbox, in the sender's outbox, and in every
-- mail server that relayed it, for as long as nobody changes it. A token
-- expires, can be used once, and proves nothing after that.
--
-- The token itself is NEVER stored. Only its SHA-256 digest is, so this
-- table leaking gives an attacker nothing usable — the same reason
-- password_hash exists rather than a password column.
--
-- The link authorises ONE thing: setting a password. It does not sign the
-- user in. If an invite email is forwarded or an inbox is compromised, the
-- attacker can set a password but cannot do so invisibly — the legitimate
-- user's own login stops working, which is a loud failure rather than a
-- silent one.

create table if not exists user_password_tokens (
    id           uuid primary key default gen_random_uuid(),
    user_id      uuid not null references users(id) on delete cascade,

    -- SHA-256 of the token that was emailed. Unique so a digest collision
    -- or a duplicate issue is a database error rather than an ambiguity
    -- resolved by whichever row happened to be returned first.
    token_hash   text not null unique,

    -- 'invite'  — new account, 7 days: a new joiner may not read email today
    -- 'reset'   — deliberate request, 2 hours: acted on immediately, so a
    --             short window limits how long a forwarded copy is live
    purpose      text not null check (purpose in ('invite', 'reset')),

    expires_at   timestamptz not null,
    used_at      timestamptz,
    created_at   timestamptz not null default now(),
    created_by   text
);

-- The lookup every redemption performs.
create index if not exists idx_user_password_tokens_hash
    on user_password_tokens (token_hash);

-- "Does this user have a live token?" — used to avoid issuing a second
-- link while the first is still valid, and to invalidate outstanding
-- tokens when a password is set.
create index if not exists idx_user_password_tokens_user_live
    on user_password_tokens (user_id)
    where used_at is null;

comment on table user_password_tokens is
    'One-time, expiring links for account setup and password reset. Stores '
    'only the SHA-256 of each token; the token itself exists solely in the '
    'email that was sent.';
