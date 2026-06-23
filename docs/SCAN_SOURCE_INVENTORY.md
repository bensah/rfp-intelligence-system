# Scan Source Inventory — the three pools behind the old "133"

_Generated 2026-06-21. Historical: before the catalogue-only change, the scan unioned these three pools (deduped) = 133 non-manual sources. **Now the scan runs POOL 2 only** (active `donor_sources`). Pools 1 and 3 are documented here so you can decide whether any belong in the catalogue._


## Migrated to the catalogue (2026-06-23)

Verified yaml-only sources moved into `donor_sources` + flagged `in_catalogue` in
`source_registry` (script: `scripts/migrate_verified_yaml_sources.py`):
**TED (EU)**, **UK Find a Tender**, **UK Contracts Finder** (all `rest_json`, real
handlers), **ResearchNet/CIHR** (`rss`; items stamped default `Canada` scope so
the geo gate keeps only beyond-Canada calls). Catalogue 75 → 79 (active 48 → 52).

NOT migrated: **World Bank Procurement Notices** (duplicate of the `worldbank.org`
catalogue row), **Pierre Fabre — ODESS** (covered by the active Pierre Fabre row),
**Global South Opportunities** (aggregator blog, not a primary source).

## POOL 1 — `config/sources.yaml` (legacy keyword list, file-based)

54 entries (35 non-manual). NOT scanned anymore.

| # | name | method | url |
|---|---|---|---|
| 1 | AFD (Agence Française de Développement) | html | https://www.afd.fr/en/calls-for-projects |
| 2 | African Development Bank (AfDB) | html | https://www.afdb.org/en/projects-and-operations/procurement |
| 3 | Biswas Family Foundation — Fast Grants | html | https://www.biswasfamilyfoundation.org/science/fast-grants |
| 4 | BMGF Grand Challenges | html | https://gcgh.grandchallenges.org/challenges |
| 5 | Center for Global Development (CGD) | manual | https://www.cgdev.org/section/publications |
| 6 | CEPI | html | https://cepi.net/calls-for-proposals |
| 7 | CEPI calls portal (Salesforce) | manual | https://cepi.my.site.com/ |
| 8 | Chan Zuckerberg Initiative | manual | https://chanzuckerberg.com/grants-ventures/grants/ |
| 9 | DevelopmentAid Grants Aggregator | html_js | https://www.developmentaid.org/grants/search?hiddenAdvancedFilters=0&locations=16,41&sectors=11,87&applicantNationalities=16,41&languages=92 |
| 10 | ELMA Philanthropies | html | https://www.elmaphilanthropies.org/ |
| 11 | EU Funding & Tenders Portal | rest_json | https://api.tech.ec.europa.eu/search-api/prod/rest/search |
| 12 | Fondation Pierre Fabre | html | https://www.fondationpierrefabre.org/en/current-initiatives/call-for-projects/ |
| 13 | Fondation Pierre Fabre — ODESS detail | html | https://www.odess.io/en/call-for-projects-2026-applications-open-2/ |
| 14 | Fund for Innovation in Development (FID) | manual | https://fund-innovation-development.org/en/calls/ |
| 15 | FundsForNGOs | rss | https://www2.fundsforngos.org/feed/ |
| 16 | Gavi | manual | https://www.gavi.org/business-opportunities |
| 17 | GiveWell | manual | https://www.givewell.org/research/grants |
| 18 | Global Fund | manual | https://www.theglobalfund.org/en/sourcing-management/ |
| 19 | Global South Opportunities | rss | https://globalsouthopportunities.com/feed/ |
| 20 | Google Alert — Gov/Org RFPs (Global Health, no PDF) | rss | https://www.google.com/alerts/feeds/11375502939470857620/16113984356459273159 |
| 21 | Google Alert — RFPs / CFPs / EOIs (Global Health) | rss | https://www.google.com/alerts/feeds/11375502939470857620/4042177278441426697 |
| 22 | Google.org Impact Challenges | manual | https://www.google.org/our-work/ |
| 23 | Grand Challenges Canada | rss | https://www.grandchallenges.ca/feed/ |
| 24 | Grants.gov | rest_json | https://api.grants.gov/v1/api/search2 |
| 25 | Health Research Inc. (HRI) | html | https://www.healthresearch.org/funding-opportunities/ |
| 26 | Hewlett Foundation | manual | https://hewlett.org/grants/ |
| 27 | IDRC (International Development Research Centre) | html | https://idrc-crdi.ca/en/funding |
| 28 | JICA Africa Hiroba | html | https://www.jica.go.jp/english/africahiroba/index.html |
| 29 | KfW Development Bank | manual | https://www.kfw-entwicklungsbank.de/Service/Vergaberegularien/ |
| 30 | Mastercard Foundation | manual | https://mastercardfdn.org/en/partners/ |
| 31 | MIT Solve | manual | https://solve.mit.edu/challenges |
| 32 | NIH Guide | rss | https://grants.nih.gov/grants/guide/rss/ |
| 33 | Open Society Foundations | html | https://www.opensocietyfoundations.org/grants |
| 34 | Packard Foundation | manual | https://www.packard.org/grantees/search-our-grants/ |
| 35 | ReliefWeb | rss | https://reliefweb.int/jobs/funding/rss.xml |
| 36 | ResearchNet (CIHR) | rss | https://www.researchnet-recherchenet.ca/rnr16/fodRss.do?type=ALL&chanTyp=ALL&lang=E |
| 37 | Rockefeller Foundation | manual | https://www.rockefellerfoundation.org/grants/ |
| 38 | Sida (Swedish International Development Cooperation Agency) | html | https://www.sida.se/en/for-partners/calls-and-announcements |
| 39 | Sida calls portal | html | https://calls.sida.se/course/index.php?lang=en |
| 40 | TED (EU procurement) | rest_json | https://api.ted.europa.eu/v3/notices/search |
| 41 | UK Contracts Finder | rest_json | https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search?order=desc |
| 42 | UK Find a Tender | rest_json | https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages |
| 43 | UNFPA Procurement | html | https://www.unfpa.org/procurement |
| 44 | UNGM | html | https://www.ungm.org/Public/Notice |
| 45 | UNICEF Supply Division | manual | https://www.unicef.org/supply/ |
| 46 | Unitaid | html | https://unitaid.org/call-for-proposals/ |
| 47 | Wellcome Trust | html | https://wellcome.org/grant-funding |
| 48 | WHO AHPSR (Alliance for Health Policy and Systems Research) | html | https://ahpsr.who.int/call-for-proposals |
| 49 | WHO AHPSR — Funding Opportunities | html | https://ahpsr.who.int/funding-opportunities |
| 50 | WHO ETDR Solicitations | manual | https://who.my.site.com/etdr/s/ |
| 51 | WHO Procurement | manual | https://www.who.int/about/accountability/procurement |
| 52 | WHO TDR Grants (deprecated portal) | manual | https://who.my.site.com/etdr/s/tdr-grant/TDR_Grant__c/Default |
| 53 | World Bank Procurement Notices | rest_json | https://search.worldbank.org/api/v2/procnotices |
| 54 | World Bank Projects | manual | https://projects.worldbank.org/en/projects-operations/projects-home |

## POOL 3 — `donor_source_seeds` table (donor-matrix research seeds)

65 scannable seeds folded in (of 229 raw rows; rest failed the opportunity-page filter or duplicated a catalogue/yaml URL). NOT scanned anymore.

| # | donor (seed) | url |
|---|---|---|
| 1 | African Development Bank (AfDB) | https://www.afdb.org/en/sectors/private-sector/how-work-us/funding-request |
| 2 | Agence Française de Développement (AFD) | https://www.afd.fr/en/grants-stand-alone-tool-or-complement-loans |
| 3 | AmplifyChange | https://amplifychange.org/apply |
| 4 | AmplifyChange | https://amplifychange.org/grant-type/opportunity-grant |
| 5 | Barr Foundation | https://www.barrfoundation.org/grantmaking/grants-database |
| 6 | Bill & Melinda Gates Foundation | https://www.gatesfoundation.org/about/how-we-work/grant-opportunities |
| 7 | Bill & Melinda Gates Foundation | https://www.gatesfoundation.org/about/our-funding |
| 8 | Centers for Disease Control and Prevention (CDC) | https://www.cdc.gov/grants/index.html |
| 9 | Children’s Investment Fund Foundation (CIFF) | https://ciff.org/grant-portfolio |
| 10 | Children’s Investment Fund Foundation (CIFF) | https://ciff.org/grant-portfolio/our-grant-making-process |
| 11 | Comic Relief | https://www.comicrelief.com/funding |
| 12 | Conrad N. Hilton Foundation | https://www.hiltonfoundation.org/grants/overview/ |
| 13 | CRI Foundation | https://crifoundation.org/how-we-fund/grants/ |
| 14 | David and Lucile Packard Foundation | https://www.packard.org/grant |
| 15 | Elton John AIDS Foundation (EJAF) | https://www.eltonjohnaidsfoundation.org/funding |
| 16 | EU Civil Protection and Humanitarian Aid | https://civil-protection-humanitarian-aid.ec.europa.eu/funding-evaluations/funding-humanitarian-aid_en |
| 17 | European Commission (INTPA) | https://international-partnerships.ec.europa.eu/funding-and-technical-assistance/funding-opportunities_en |
| 18 | European Union – Team Europe Initiatives | https://eufundingportal.eu/eu-calls-for-proposals |
| 19 | FIND (Foundation for Innovative New Diagnostics) | https://www.finddx.org/funding/ |
| 20 | Fondation Botnar | https://www.fondationbotnar.org/funding/ |
| 21 | Gavi, the Vaccine Alliance | https://www.gavi.org/programmes-impact/types-support/gavi-funding-civil-society-organisations/application-guidance |
| 22 | Gavi, the Vaccine Alliance | https://www.gavi.org/our-work/funding-civil-society-organisations |
| 23 | Gavi, the Vaccine Alliance | https://www.gavi.org/our-work/funding-civil-society-organisations/application-guidance |
| 24 | GiveWell | https://www.givewell.org/apply-for-consideration |
| 25 | GiveWell | https://www.givewell.org/all-grants-fund |
| 26 | Global Financing Facility (GFF) | https://www.globalfinancingfacility.org/sites/gff_new/files/documents/GFF-CSO-Host-Organziation-Call-for-Proposals.pdf |
| 27 | Global Health EDCTP3 (Horizon Europe) | https://www.global-health-edctp3.europa.eu/funding/how-apply-funding_en |
| 28 | Global Innovation Fund | https://www.globalinnovation.fund/apply-for-funding |
| 29 | Government of the Netherlands | https://www.government.nl/topics/grant-programmes |
| 30 | Grand Challenges Canada | https://www.grandchallenges.ca/apply-for-funding/?utm |
| 31 | Health Resources and Services Administration (HRSA) | https://www.grants.gov/learn-grants/grant-eligibility |
| 32 | IKEA Foundation | https://ikeafoundation.org/grants/ |
| 33 | Italian Agency for Development Cooperation | https://www.aics.gov.it/home-eng/opportunities/noprofit-development/ |
| 34 | Japan International Cooperation Agency (JICA) | https://www.jica.go.jp/english/activities/schemes/grant_aid/index.html |
| 35 | KfW Development Bank | https://www.giz.de/en/partner/funding |
| 36 | MacArthur Foundation (100&Change) | https://www.macfound.org/grants |
| 37 | Norwegian Agency for Development Cooperation (NORAD) | https://www.norad.no/en/front/funding |
| 38 | Novo Nordisk Foundation | https://novonordiskfonden.dk/en/how-we-work/what-are-grants/applying-for-a-grant |
| 39 | Novo Nordisk Foundation | https://novonordiskfonden.dk/en/grant |
| 40 | Open Philanthropy | https://www.openphilanthropy.org/grants/ |
| 41 | Pure Earth Opportunity Fund | https://www.pureearth.org/opportunity-fund |
| 42 | Pure Earth Opportunity Fund | https://leadelimination.org/opportunity-fund/ |
| 43 | Robert Carr Fund | https://robertcarrfund.org/request-for-proposals |
| 44 | Robert Wood Johnson Foundation | https://www.rwjf.org/en/grants.html |
| 45 | Robert Wood Johnson Foundation | https://www.rwjf.org/en/how-we-work/grants-explorer.html |
| 46 | Swedish International Development Cooperation Agency (Sida) | https://www.sida.se/en/how-we-work/funding |
| 47 | Swedish International Development Cooperation Agency (Sida) | https://www.sida.se/en/for-partners/apply-for-financial-support-from-sida |
| 48 | The Global Fund to Fight AIDS, Tuberculosis and Malaria | https://www.theglobalfund.org/en/funding-model/ |
| 49 | The Global Fund to Fight AIDS, Tuberculosis and Malaria | https://www.theglobalfund.org/en/applying-for-funding |
| 50 | The Global Fund to Fight AIDS, Tuberculosis and Malaria | https://resources.theglobalfund.org/en/grant-life-cycle/applying-for-funding/funding-request-documents |
| 51 | The Global Fund to Fight AIDS, Tuberculosis and Malaria | https://resources.theglobalfund.org/en/grant-life-cycle/applying-for-funding |
| 52 | The Rockefeller Foundation | https://www.rockefellerfoundation.org/our-grants |
| 53 | UK Foreign, Commonwealth & Development Office (FCDO) | https://www.gov.uk/international-development-funding |
| 54 | Unitaid | https://unitaid.org/apply-for-funding |
| 55 | Unitaid | https://unitaid.org/apply-for-funding/calls-for-proposals-frequently-asked-questions |
| 56 | United Nations Development Programme (UNDP) | https://www.undp.org/funding |
| 57 | United States Agency for International Development (USAID) | https://apply07.grants.gov/apply/opportunities/instructions/PKG00276629-instructions.pdf |
| 58 | United States Agency for International Development (USAID) | https://www.usaid.gov/work-usaid/get-grant-or-contract |
| 59 | United States Agency for International Development (USAID) | https://www.grants.gov/learn-grants/grant-making-agencies/u-s-agency-for-international-development-usaid |
| 60 | US Department of State | https://cm.usembassy.gov/embassy-grants-opportunities |
| 61 | Wellcome Trust | https://wellcome.org/research-funding/guidance/prepare-to-apply/how-to-write-wellcome-grant-application |
| 62 | Wellcome Trust | https://wellcome.org/research-funding/guidance/prepare-to-apply/eligibility-information-grant-applicants |
| 63 | Wellcome Trust | https://wellcome.org/research-funding/guidance/prepare-to-apply |
| 64 | Wellcome Trust | https://wellcome.org/grant-funding/schemes |
| 65 | World Health Organization (WHO) | https://www.who.int/about/accountability/procurement/contract-awards |
