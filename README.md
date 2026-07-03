# PyExtract

PyExtract is a lead-generation and cold email outreach tool. It scrapes leads, verifies email deliverability, writes personalized outreach emails with AI, and sends tracked email campaigns — all from a single dashboard.

## Features

- **Lead scraping** — pull lead data into the app for outreach
- **Email verification** — checks whether an email address is deliverable before you send to it
- **AI email writer** — generates personalized cold emails per lead using Groq's LLM API, based on tone and goal (e.g. professional / offer services)
- **Campaign sending** — send email campaigns with randomized delays between sends, using each user's own connected Gmail account
- **Open tracking** — tracks when sent emails are opened
- **Sent log & export** — view send history and export it to CSV
- **Per-user email accounts** — each user connects their own Gmail (address + App Password), encrypted at rest, rather than sharing one sending identity across all users
- **Auth** — registration/login with hashed passwords (Flask-Login + Bcrypt)

## Tech stack

- **Backend:** Flask, Flask-Login, Flask-Bcrypt
- **Database:** SQLite (see [Deployment notes](#deployment-notes) below)
- **AI:** [Groq](https://groq.com) API for email generation
- **Email:** SMTP (Gmail) — see deployment notes on hosting limitations
- **Encryption:** `cryptography` (Fernet) for encrypting stored Gmail App Passwords
- **Server:** Gunicorn

## Setup (local development)

1. Clone the repo and install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and fill in real values:
   ```bash
   cp .env.example .env
   ```

3. Required environment variables:

   | Variable | Purpose |
   |---|---|
   | `FLASK_SECRET_KEY` | Signs Flask session cookies. Generate with `python3 -c "import secrets; print(secrets.token_hex(32))"` |
   | `ENCRYPTION_KEY` | Encrypts users' stored Gmail App Passwords. Generate with `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
   | `GROQ_API_KEY` | API key from [console.groq.com](https://console.groq.com/keys), used by the AI email writer |
   | `BASE_URL` | Public URL of your deployment (used for the email open-tracking pixel) |
   | `FLASK_DEBUG` | Set to `false` in production |

   Each user connects their own Gmail address + [App Password](https://myaccount.google.com/apppasswords) from the Account page in-app — this is **not** an environment variable.

4. Run locally:
   ```bash
   python3 app.py
   ```

## Deployment notes

This app is currently designed to run on any standard Python host (Render, Railway, etc.), but two platform-level constraints matter:

- **SQLite + ephemeral disks:** most free-tier PaaS hosts (Render, Railway) wipe the local filesystem on every restart/redeploy, which resets the database. For persistent data on a free tier, use a managed database (e.g. Render's free Postgres) instead of local SQLite. A paid plan with a persistent disk keeps SQLite working as-is.
- **Outbound SMTP ports blocked on free tiers:** Render, Railway, and most free PaaS hosts block outbound SMTP (ports 25/465/587) to prevent spam abuse. Sending email via Gmail SMTP therefore requires either a paid hosting plan, or switching to an HTTP-based transactional email API (e.g. [Resend](https://resend.com)).

## Security

- Passwords are hashed with Bcrypt, never stored in plaintext.
- Each user's Gmail App Password is encrypted at rest (Fernet/AES) using `ENCRYPTION_KEY`, and only decrypted in-memory when sending a campaign.
- No secrets are committed to this repository — all keys/passwords are supplied via environment variables at runtime.
