# Social Media Automation Platform

## Setup

1. Install Python 3.13 and GNU Make.
2. Copy `.env.example` to `.env` and set development values as needed.
3. Run `make test`. This creates the project-local `.venv`, installs pinned dependencies, and runs the offline tests.
4. Run `make lint` to check formatting and imports.

Docker Compose, database migrations, API startup, and admin seeding are introduced by their respective later tasks. Docker Desktop with WSL2 will be required from B1-02.
