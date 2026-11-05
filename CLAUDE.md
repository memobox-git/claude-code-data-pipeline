# Olist Lightning Pipeline

A data pipeline built live in front of an audience. Kaggle's Olist e-commerce
dataset -> S3 raw zone -> RDS PostgreSQL (bronze, silver, gold) -> a dashboard.

## The situation

Finance reports **R$16.0M** revenue from the payments export. Marketplace
reports **R$15.8M** from the items export. Nobody can explain the difference.

**Business question:** which products and sellers drive sales, and does late
delivery hurt reviews?

**Goal:** one number everyone agrees on, with a bridge from each team's number
to it.

## How to work

- Before each step, say in one sentence what you'll do.
- Keep answers short. Prefer tables.
- Commit after each completed step with a clear message.

## AWS

| Setting | Value |
|---|---|
| Profile | `lightning-demo` |
| Region | `us-east-1` |
| Tag on every resource | `project=olist-lightning` |

- **Ask before creating, changing or deleting anything in AWS.**
- Never print, log or save passwords or AWS keys.
- The database password lives in Secrets Manager and is fetched at runtime.
  The secret name goes in `config.env` as `DB_SECRET_NAME`.
- In a Claude cloud session the keys arrive as environment variables and no
  profile exists; leave `AWS_PROFILE` unset there. On a laptop use the profile.

Already created (no need to create again):

| Resource | Value |
|---|---|
| RDS instance | `lightning-demo-rds`, PostgreSQL 18, db.t4g.micro, 20 GB |
| Host | `lightning-demo-rds.c2bksoesyxw3.us-east-1.rds.amazonaws.com:5432` |
| Database / admin user | `olist` / `postgres` |
| Public access | Port 5432 open to one IP only, via security group `lightning-demo-rds-access` |

## Settings

- All settings go in `config.env`, copied from `config.env.example`.
- `config.env` is git-ignored. **Never commit it.**
- **Never commit the bucket name or the account number anywhere**: not in
  code, docs, comments, notebooks or commit messages. Read them from
  `config.env` at runtime.

## Layers

| Layer | Contract |
|---|---|
| **bronze** | Files exactly as landed. All columns `text`. Plus `batch_id`, `source_file`, `loaded_at`. |
| **silver** | Correct types. Deduplicated. Bad rows moved to a quarantine table with a `reason`. Never deleted silently. |
| **gold** | Star schema. Every table states its grain in a comment: one row = one what? |
| **meta** | `dq_results`, `lineage`, batch bookkeeping. |

## Business rules

- **GMV = price + freight.** GMV is **0** for canceled or unavailable orders.
- **Never join order items to payments directly.** Roll each up to one row per
  order first, then join on `order_id`.
- The two team numbers are both "right" for what they count. The bridge
  explains each adjustment from a team's number to the agreed number.

## Data quality

- A check is a SQL query that returns the **failing rows**.
- Every check has a severity: `block` or `warn`.
- Results are recorded in `meta.dq_results` (check name, layer, severity,
  failing row count, batch id, run time).
- Build and check each layer in **one transaction**. A failed `block` check
  rolls the whole layer back. `warn` is recorded and the build continues.

## Lineage

Every step records source -> target with row counts in `meta.lineage`
(batch id, source, target, rows in, rows out, rows quarantined, run time).

## Dashboard

- Reads **gold only**.
- Connects as the read-only role **`bi_reader`** (SELECT on gold, nothing else).
