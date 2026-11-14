# Social Media Automation Platform

## Setup

1. Install Python 3.13 and GNU Make.
2. Copy `.env.example` to `.env` and set development values as needed.
3. Run `make test`. This creates the project-local `.venv`, installs pinned dependencies, and runs the offline tests.
4. Run `make lint` to check formatting and imports.
5. Run `make dev` to start the Docker Compose development stack. The API is then
   available at `http://localhost:8000`; use `/health` for liveness and
   `/health/ready` for database and Redis readiness.

Docker Desktop with WSL2 is required for the development stack.

### Database Connectivity & Migrations
- **Host machine (local development / Alembic / tests):** PostgreSQL is published on `127.0.0.1:15432` (host port 15432 is used because port 5432 is reserved/in-use on this machine). Use `DATABASE_URL=postgresql+psycopg://sma:sma@127.0.0.1:15432/sma` and `TEST_DATABASE_URL=postgresql+psycopg://sma:sma@127.0.0.1:15432/sma_test`.
- **Inside Docker Compose:** services communicate via the Docker network where the database host is `postgres` (`postgresql+psycopg://sma:sma@postgres:5432/sma`).
- Run `make migrate` to apply Alembic migrations against the database.
- Run `make downgrade` to roll back one migration step.
- Run `make migration m="message"` to create a new revision.
