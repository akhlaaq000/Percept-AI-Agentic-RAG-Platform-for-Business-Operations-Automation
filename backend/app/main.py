import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.background import BackgroundScheduler

from app.api import agent_runs, escalations, notifications, admin, evaluation, submissions, meeting_action_items
from app.api.admin import VERTICAL_SOURCE_TYPES
from app.core.ingestion import ingest_staging_folder
from app.verticals.meeting_action_items.followup import run_followup_check

import app.verticals.dummy.tools
import app.verticals.dummy.graph
import app.verticals.meeting_action_items.tools
import app.verticals.meeting_action_items.graph

import app.verticals.contract_tracking.tools
import app.verticals.contract_tracking.graph
from app.verticals.contract_tracking.scheduler import run_scheduled_contract_ingestion

import app.verticals.internal_mobility.tools
import app.verticals.internal_mobility.graph

import app.verticals.post_incident.tools
import app.verticals.post_incident.graph

# Section 6.3: "A shared function, called on a timer via APScheduler,
# scans each vertical's staging folder..." Interval is configurable
# since this is a dev/demo project, not production — default kept
# short (5 min) so the mechanism is easy to observe while testing.
SCHEDULED_INGESTION_INTERVAL_MINUTES = int(
    os.getenv("SCHEDULED_INGESTION_INTERVAL_MINUTES", "5")
)

# Section 8.4's Trigger 2: "a daily scheduled job re-checks every
# open, overdue action item." Deliberately NOT a literal 24-hour
# cadence here — same reasoning as the ingestion interval above: a
# short, configurable default makes this observable during dev/demo
# without waiting a full day to see it fire.
SCHEDULED_FOLLOWUP_INTERVAL_MINUTES = int(
    os.getenv("SCHEDULED_FOLLOWUP_INTERVAL_MINUTES", "10")
)

scheduler = BackgroundScheduler()


def run_scheduled_ingestion() -> None:
    """
    Scans every real vertical's staging folder and ingests anything
    new or changed — the automatic counterpart to the manual
    POST /admin/resync/{vertical} button, reusing the exact same
    ingest_staging_folder() function and the same vertical ->
    source_type mapping admin.py already defines.

    Only "dummy" is excluded here (not a real vertical with real
    scheduled ingestion needs, per its own docstrings elsewhere) —
    every real vertical listed in VERTICAL_SOURCE_TYPES is scanned,
    EXCEPT "contract_tracking" (Section 6.4's one deliberate
    exception): its scheduled ingestion IS the analysis trigger, so
    it needs clause-level chunking + the full extraction workflow,
    not the generic embed-only path every other vertical uses here.
    See app.verticals.contract_tracking.scheduler for why it can't
    just reuse ingest_staging_folder with a different chunk_fn.

    Failures for one vertical are caught and logged, not allowed to
    stop the other verticals' ingestion in the same run.
    """
    for vertical, source_type in VERTICAL_SOURCE_TYPES.items():
        if vertical == "contract_tracking":
            continue
        try:
            summary = ingest_staging_folder(vertical=vertical, source_type=source_type)
            if summary["processed"] or summary["errors"]:
                print(f"[scheduled ingestion] {vertical}: {summary}")
        except Exception as e:
            print(f"[scheduled ingestion] ERROR for vertical '{vertical}': {e}")

    try:
        summary = run_scheduled_contract_ingestion()
        if summary["processed"] or summary["errors"]:
            print(f"[scheduled ingestion] contract_tracking: {summary}")
    except Exception as e:
        print(f"[scheduled ingestion] ERROR for vertical 'contract_tracking': {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.getenv("AUTO_SETUP", "").lower() in {"1", "true", "yes"}:
        from app.core.db import get_connection
        from app.verticals.internal_mobility.seed_local import seed_internal_mobility

        # Bootstrap: a fresh database lacks the `vector` extension, and
        # get_connection()'s register_vector() needs it to merely connect.
        # Create it on a bare connection first; schema.sql (which repeats
        # CREATE EXTENSION IF NOT EXISTS) then applies cleanly below.
        conn = get_connection(register_pgvector_types=False)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            print("[AUTO_SETUP] Ensured pgvector extension.")
        finally:
            conn.close()

        conn = get_connection()
        try:
            # Set autocommit BEFORE any query — psycopg2 refuses to flip
            # autocommit while a transaction is open, and plain connects
            # lazily start one on the first statement (incl. the probe).
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'documents');"
                )
                row = cur.fetchone()
                schema_exists = bool(row and row["exists"])
                if not schema_exists:
                    schema_path = Path(__file__).resolve().parents[1] / "db" / "schema.sql"
                    cur.execute(schema_path.read_text(encoding="utf-8"))
                    print("[AUTO_SETUP] Applied database schema.")
        finally:
            conn.close()
        print("[AUTO_SETUP] Seeding internal_mobility (V2)...")
        seed_internal_mobility()

    scheduler.add_job(
        run_scheduled_ingestion,
        "interval",
        minutes=SCHEDULED_INGESTION_INTERVAL_MINUTES,
        id="scheduled_ingestion",
    )
    scheduler.add_job(
        run_followup_check,
        "interval",
        minutes=SCHEDULED_FOLLOWUP_INTERVAL_MINUTES,
        id="scheduled_meeting_action_items_followup",
    )
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="Agentic RAG Platform - Backend", lifespan=lifespan)

CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(agent_runs.router)
app.include_router(escalations.router)
app.include_router(notifications.router)
app.include_router(admin.router)
app.include_router(evaluation.router)
app.include_router(submissions.router)
app.include_router(meeting_action_items.router)

# Built frontend (dist/), baked into app/static at container build time by
# Dockerfile.web. Absent in dev/source-tree runs, so local and docker-compose
# behavior is unchanged — the SPA catch-all is only added when the files exist.
# Registered LAST so it can never shadow the /health or API routes above.
STATIC_DIR = Path(__file__).resolve().parent / "static"

@app.get("/health")
def health_check():
    return {"status": "ok"}

if STATIC_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        base = STATIC_DIR.resolve()
        resolved = (base / full_path).resolve()
        if full_path and resolved.is_file() and resolved.is_relative_to(base):
            return FileResponse(resolved)
        return FileResponse(base / "index.html")