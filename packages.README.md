# packages.txt is intentionally disabled → renamed to `packages.txt.disabled`

**Why:** Streamlit Community Cloud runs `apt-get` whenever a `packages.txt` is present.
On 2026-09-08 the upstream Debian mirror's `bullseye-security` **release file expired**
(`E: Release file … is expired (invalid since 15h…)`), so `apt-get update` returned a
non-zero exit and Streamlit aborted the whole deploy — "Oh no. Error running app."

The listed packages are only the **headless-Chromium system libraries for PDF export**,
and PDF export already falls back to HTML when Chromium isn't available (#242). So skipping
apt keeps the app running; only native PDF export degrades to the HTML fallback.

**To re-enable** (once the Debian mirror's release metadata is fresh again, or Streamlit
updates its base image): `git mv packages.txt.disabled packages.txt` and push.
