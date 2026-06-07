-- Migration 024 — scan_blacklist: user-managed hard-reject list.
-- Each `pattern` is matched as a case-insensitive SUBSTRING against a
-- candidate's opportunity_link during scanning (core/auto_scorer.is_eligible).
-- Any match → the candidate is rejected before scoring and never becomes a
-- record. Use a bare domain to block a whole site (e.g. 'cdc.gov') or a path
-- fragment to block a section (e.g. 'comicrelief.com/sportrelief', '/donate').
-- Managed from Admin → Blacklist and the 🚫 button on the Records table.

create table if not exists scan_blacklist (
    id          bigserial primary key,
    pattern     text not null unique,
    reason      text,
    created_by  text,
    created_at  timestamptz not null default now()
);

-- Seed the "usual suspects" — off-topic donor-site sections + sites with no
-- calls page. Safe, low-false-positive substrings (sections that are never a
-- funding call). Re-runnable: ON CONFLICT DO NOTHING.
insert into scan_blacklist (pattern, reason, created_by) values
    ('cdc.gov',            'CDC has no calls page; NOFOs come via grants.gov', 'seed'),
    ('/sportrelief',       'Comic Relief fundraising challenges, not calls',   'seed'),
    ('/rednoseday',        'Comic Relief fundraising, not calls',              'seed'),
    ('/fundraise',         'fundraising page',                                 'seed'),
    ('/fundraising',       'fundraising page',                                 'seed'),
    ('/donate',            'donation page',                                    'seed'),
    ('/donation',          'donation page',                                    'seed'),
    ('/give-now',          'donation page',                                    'seed'),
    ('/shop',              'merchandise / shop',                               'seed'),
    ('/store/',            'merchandise / store',                              'seed'),
    ('/merch',             'merchandise',                                      'seed'),
    ('/jobs',              'careers / jobs',                                   'seed'),
    ('/careers',           'careers / jobs',                                   'seed'),
    ('/vacanc',            'careers / vacancies',                              'seed'),
    ('/podcast',           'media / podcast',                                  'seed'),
    ('facebook.com',       'social media',                                     'seed'),
    ('twitter.com',        'social media',                                     'seed'),
    ('://x.com',           'social media',                                     'seed'),
    ('linkedin.com',       'social media',                                     'seed'),
    ('youtube.com',        'social media / video',                             'seed'),
    ('instagram.com',      'social media',                                     'seed'),
    ('tiktok.com',         'social media',                                     'seed')
on conflict (pattern) do nothing;
