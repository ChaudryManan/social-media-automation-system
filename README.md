# Social Media Automation Platform

## Setup

1. Install Python 3.13 and GNU Make.
2. Copy `.env.example` to `.env` and set development values as needed.
3. Run `make test`. This creates the project-local `.venv`, installs pinned dependencies, and runs the offline tests.
4. Run `make lint` to check formatting and imports.
5. Run `make dev` to start the Docker Compose development stack. The API is then
   available at `http://localhost:8000`; use `/health` for liveness and
   `/health/ready` for database and Redis readiness.

Docker Desktop with WSL2 is required for the development stack. Database migrations
and admin seeding are introduced by their respective later tasks.
