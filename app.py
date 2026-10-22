
import os
import json
import sqlite3
import hashlib
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="A2A Invoice Agent", version="1.0.0")

BASE_URL = os.getenv("A2A_BASE_URL", "https://YOUR-SERVICE.onrender.com/a2a/")
API_TOKEN = os.getenv("A2A_API_TOKEN", "local-dev-token")
DB_PATH = os.getenv("TASK_DB", "tasks.db")

INPUT_MODE = "application/vnd.ga5.invoice-claim-batch+json"
PROPOSALS_MODE = "application/vnd.ga5.invoice-action-proposals+json"
RECEIPTS_MODE = "application/vnd.ga5.invoice-action-receipts+json"
RESULTS_MODE = "application/vnd.ga5.invoice-action-results+json"
A2A_MEDIA = "application/a2a+json"


def connect_db():
    db = sqlite3.connect(DB_PATH, timeout=20)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    with connect_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                principal TEXT NOT NULL,
                context_id TEXT NOT NULL,
                state TEXT NOT NULL,
                task_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                principal TEXT NOT NULL,
                message_id TEXT NOT NULL,
                digest TEXT NOT NULL,
                task_id TEXT NOT NULL,
                PRIMARY KEY (principal, message_id)
            )
        """)


init_db()


def error(status, code):
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code}}
    )


def get_principal(request):
    auth = request.headers.get("Authorization", "")
    expected = f"Bearer {API_TOKEN}"
    if not API_TOKEN or auth != expected:
        return None
    return auth[len("Bearer "):]


def save_task(task):
    with connect_db() as db:
        db.execute(
            """INSERT INTO tasks
            (id, principal, context_id, state, task_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (
                task["id"],
                task["_principal"],
                task["contextId"],
                task["status"]["state"],
                json.dumps(task),
                datetime.now(timezone.utc).isoformat()
            )
        )


def public_task(task):
    return {k: v for k, v in task.items() if k != "_principal"}

def a2a_response(data, status_code=200):
    return JSONResponse(
        status_code=status_code,
        content=data,
        media_type="application/a2a+json"
    )

@app.get("/")
def home():
    return {"status": "running", "service": "A2A Invoice Agent"}


@app.get("/.well-known/agent-card.json")
def agent_card():
    return {
        "name": "A2A Invoice Agent",
        "description": "Evidence-based invoice action agent",
        "version": "1.0.0",
        "supportedInterfaces": [{
            "url": BASE_URL,
            "protocolBinding": "HTTP+JSON",
            "protocolVersion": "1.0"
        }],
        "capabilities": {},
        "defaultInputModes": [INPUT_MODE],
        "defaultOutputModes": [PROPOSALS_MODE, RECEIPTS_MODE],
        "skills": [{
            "id": "invoice_action_agent",
            "name": "Invoice Action Agent",
            "description": "Evaluates invoices and manages approved actions",
            "tags": ["invoice", "approval", "reconciliation"]
        }]
    }


@app.middleware("http")
async def protect_a2a(request: Request, call_next):
    if request.url.path.startswith("/a2a/"):
        principal = get_principal(request)

        if principal is None:
            return error(401, "UNAUTHENTICATED")

        if request.headers.get("A2A-Version") != "1.0":
            return error(400, "UNSUPPORTED_A2A_VERSION")

        # GET requests normally don't have a Content-Type header.
        if request.method in ("POST", "PUT", "PATCH"):
            content_type = request.headers.get("Content-Type", "")
            content_type = content_type.split(";")[0].strip()

            if content_type != A2A_MEDIA:
                return error(415, "UNSUPPORTED_MEDIA_TYPE")

    return await call_next(request)


@app.post("/a2a/message:send")
async def send_message(request: Request):
    principal = get_principal(request)

    body = await request.json()
    message = body["message"]
    message_id = message["messageId"]

    digest = hashlib.sha256(
        json.dumps(message, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()

    with connect_db() as db:
        old = db.execute(
            "SELECT digest, task_id FROM messages WHERE principal=? AND message_id=?",
            (principal, message_id)
        ).fetchone()

        if old:
            if old["digest"] != digest:
                return error(409, "IDEMPOTENCY_CONFLICT")

            row = db.execute(
                "SELECT task_json FROM tasks WHERE id=? AND principal=?",
                (old["task_id"], principal)
            ).fetchone()

            return a2a_response({"task": public_task(json.loads(row["task_json"]))})

        part = message["parts"][0]
        data = part["data"]
        batch_id = data["batchId"]
        packages = data["packages"]

        proposals = []

        for p in packages:
            proposals.append({
                "packageId": p["packageId"],
                "actionId": uuid4().hex,
                "action": "hold_invoice",
                "facts": {
                    "vendorName": "",
                    "invoiceNumber": "",
                    "amountMinor": 0,
                    "currency": "INR"
                },
                "evidenceRefs": [],
                "rationale": "Temporary placeholder proposal."
            })

        task = {
            "id": uuid4().hex,
            "contextId": uuid4().hex,
            "status": {"state": "TASK_STATE_INPUT_REQUIRED"},
            "history": [message],
            "artifacts": [{
                "parts": [{
                    "mediaType": PROPOSALS_MODE,
                    "data": {
                        "batchId": batch_id,
                        "proposals": proposals
                    }
                }]
            }],
            "_principal": principal
        }

        db.execute(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?)",
            (
                task["id"],
                principal,
                task["contextId"],
                task["status"]["state"],
                json.dumps(task),
                datetime.now(timezone.utc).isoformat()
            )
        )

        db.execute(
            "INSERT INTO messages VALUES (?,?,?,?)",
            (principal, message_id, digest, task["id"])
        )

    return a2a_response({"task": public_task(task)})


@app.get("/a2a/tasks")
async def list_tasks(request: Request):
    principal = get_principal(request)
    with connect_db() as db:
        rows = db.execute(
            "SELECT task_json FROM tasks WHERE principal=? ORDER BY created_at DESC",
            (principal,)
        ).fetchall()
    return a2a_response({
    "tasks": [
        public_task(json.loads(r["task_json"]))
        for r in rows
    ]
})


@app.get("/a2a/tasks/{task_id}")
async def get_task(task_id: str, request: Request):
    principal = get_principal(request)
    with connect_db() as db:
        row = db.execute(
            "SELECT task_json FROM tasks WHERE id=? AND principal=?",
            (task_id, principal)
        ).fetchone()
    if not row:
        return error(404, "TASK_NOT_FOUND")
    return a2a_response({
        "task": public_task(json.loads(row["task_json"]))
    })


@app.post("/a2a/tasks/{task_id}:cancel")
async def cancel_task(task_id: str, request: Request):
    principal = get_principal(request)
    with connect_db() as db:
        row = db.execute(
            "SELECT task_json FROM tasks WHERE id=? AND principal=?",
            (task_id, principal)
        ).fetchone()

        if not row:
            return error(404, "TASK_NOT_FOUND")

        task = json.loads(row["task_json"])
        state = task["status"]["state"]

        if state in ("TASK_STATE_COMPLETED", "TASK_STATE_CANCELED",
                     "TASK_STATE_REJECTED", "TASK_STATE_FAILED"):
            return error(409, "TASK_TERMINAL")

        task["status"]["state"] = "TASK_STATE_CANCELED"
        db.execute(
            "UPDATE tasks SET state=?, task_json=? WHERE id=? AND principal=?",
            (
                "TASK_STATE_CANCELED",
                json.dumps(task),
                task_id,
                principal
            )
        )

    return a2a_response({
        "task": public_task(task)
    })


@app.post("/a2a/tasks/{task_id}/message:send")
async def continue_task(task_id: str, request: Request):
    principal = get_principal(request)
    body = await request.json()
    message = body.get("message", {})

    parts = message.get("parts", [])
    if not parts or not isinstance(parts[0].get("data"), dict):
        return error(400, "INVALID_RESULTS")

    results_data = parts[0]["data"]

    with connect_db() as db:
        row = db.execute(
            "SELECT task_json FROM tasks WHERE id=? AND principal=?",
            (task_id, principal)
        ).fetchone()

        if not row:
            return error(404, "TASK_NOT_FOUND")

        task = json.loads(row["task_json"])

        if task["status"]["state"] != "TASK_STATE_INPUT_REQUIRED":
            return error(409, "TASK_NOT_CONTINUABLE")

        artifacts = task.get("artifacts", [])
        proposal_data = None

        for artifact in artifacts:
            for part in artifact.get("parts", []):
                if part.get("mediaType") == PROPOSALS_MODE:
                    proposal_data = part.get("data", {})
                    break

        if not proposal_data:
            return error(409, "PROPOSALS_NOT_FOUND")

        if results_data.get("batchId") != proposal_data.get("batchId"):
            return error(409, "BATCH_MISMATCH")

        proposals = proposal_data.get("proposals", [])
        results = results_data.get("results", [])
        proposal_map = {p["packageId"]: p for p in proposals}

        receipts = []
        seen = set()

        for result in results:
            package_id = result.get("packageId")
            proposal = proposal_map.get(package_id)

            if not proposal or package_id in seen:
                return error(400, "INVALID_PACKAGE")
            seen.add(package_id)

            if (
                result.get("actionId") != proposal.get("actionId")
                or result.get("action") != proposal.get("action")
            ):
                return error(409, "ACTION_MISMATCH")

            if result.get("outcome") not in ("ACCEPTED", "REJECTED"):
                return error(400, "INVALID_OUTCOME")

            if result["outcome"] == "ACCEPTED":
                receipts.append({
                    "packageId": package_id,
                    "actionId": proposal["actionId"],
                    "action": proposal["action"],
                    "receiptNonce": result.get("receiptNonce"),
                    "facts": proposal["facts"],
                    "evidenceRefs": proposal["evidenceRefs"]
                })

        task["status"] = {"state": "TASK_STATE_COMPLETED"}
        task["artifacts"].append({
            "parts": [{
                "mediaType": RECEIPTS_MODE,
                "data": {
                    "batchId": proposal_data["batchId"],
                    "receipts": receipts
                }
            }]
        })

        db.execute(
            "UPDATE tasks SET state=?, task_json=? WHERE id=? AND principal=?",
            (
                "TASK_STATE_COMPLETED",
                json.dumps(task),
                task_id,
                principal
            )
        )

    return a2a_response(public_task(task))
