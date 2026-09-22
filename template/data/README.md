# Data

- `raw/`: immutable source data; not synced or published by default.
- `processed/`: stable transformed data; sync only when explicitly needed.
- `external/`: third-party downloads; not synced or published by default.

Do not store credentials, access tokens, or private-data exports in the repository.
