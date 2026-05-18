"""
vision_ingest.py — Farm Pod vision ingest server

Receives JPEG images from ESP32-CAM greenhouse nodes via HTTP POST,
runs EfficientNet B0 + ViT plant diagnosis model, stores results in
farm_data.db alongside sensor telemetry.

Usage:
    python vision_ingest.py

Dependencies:
    pip install fastapi uvicorn python-multipart pillow torch torchvision

The model (efficientnet_vit_plant_diag) is expected at:
    ./models/plant_diag.pt

If model is not found, vision_ingest runs in capture-only mode:
images are saved to ./captures/ and flagged for manual review.

Integrates with existing farm_data.db schema — adds vision_observations
table (see schema below). Results feed into sensor_aggregator.py alert
pipeline via the same sensor_alerts table.
"""

import io
import json
import logging
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from PIL import Image

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
DB_PATH       = "./farm_data.db"
MODELS_DIR    = "./models"
CAPTURES_DIR  = "./captures"
MODEL_PATH    = os.path.join(MODELS_DIR, "plant_diag.pt")
HOST          = "0.0.0.0"
PORT          = 8765
MAX_IMAGE_MB  = 5
LOG_LEVEL     = logging.INFO

# EfficientNet B0 input size
MODEL_INPUT_SIZE = (224, 224)

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("vision_ingest")

# ---------------------------------------------------------------------------
# MODEL LOADER
# ---------------------------------------------------------------------------
model      = None
model_mode = "capture_only"

def load_model():
    global model, model_mode
    if not os.path.exists(MODEL_PATH):
        log.warning(f"Model not found at {MODEL_PATH} — running in capture-only mode")
        model_mode = "capture_only"
        return

    try:
        import torch
        device = "mps" if torch.backends.mps.is_available() else "cpu"
        log.info(f"Loading model on {device}...")
        model = torch.load(MODEL_PATH, map_location=device)
        model.eval()
        model_mode = "inference"
        log.info(f"Model loaded: {MODEL_PATH}")
    except Exception as e:
        log.error(f"Model load failed: {e} — capture-only mode")
        model_mode = "capture_only"


def run_inference(img: Image.Image) -> dict:
    """
    Run plant diagnosis inference. Returns dict with:
      - labels: list of {label, confidence} sorted by confidence desc
      - top_label: str
      - top_confidence: float
      - alert: bool (confidence > threshold for a disease class)
    """
    import torch
    import torchvision.transforms as T

    transform = T.Compose([
        T.Resize(MODEL_INPUT_SIZE),
        T.ToTensor(),
        T.Normalize(mean=[0.485, 0.456, 0.406],
                    std=[0.229, 0.224, 0.225]),
    ])

    tensor = transform(img.convert("RGB")).unsqueeze(0)

    device = next(model.parameters()).device
    tensor = tensor.to(device)

    with torch.no_grad():
        logits = model(tensor)
        probs  = torch.softmax(logits, dim=1)[0].cpu().tolist()

    # Class labels — update to match your model's actual output classes
    # These are placeholders; replace with your EfficientNet+ViT label list
    class_labels = [
        "healthy",
        "botrytis_grey_mold",
        "powdery_mildew",
        "aphid_infestation",
        "leaf_chlorosis",
        "overwatering",
        "underwatering",
        "nutrient_deficiency",
    ]

    # Pad or truncate labels to match model output size
    num_classes = len(probs)
    while len(class_labels) < num_classes:
        class_labels.append(f"class_{len(class_labels)}")
    class_labels = class_labels[:num_classes]

    labels = sorted(
        [{"label": class_labels[i], "confidence": round(probs[i], 4)}
         for i in range(num_classes)],
        key=lambda x: x["confidence"],
        reverse=True
    )

    top        = labels[0]
    is_alert   = top["label"] != "healthy" and top["confidence"] > 0.65

    return {
        "top_label":      top["label"],
        "top_confidence": top["confidence"],
        "alert":          is_alert,
        "labels":         labels[:5],   # top 5 only
    }


# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS vision_observations (
    obs_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id    TEXT NOT NULL,
    recorded_at  TEXT NOT NULL,
    image_path   TEXT,
    width        INTEGER,
    height       INTEGER,
    size_bytes   INTEGER,
    model_mode   TEXT,
    top_label    TEXT,
    top_conf     REAL,
    alert        INTEGER DEFAULT 0,
    labels_json  TEXT,
    wifi_rssi    INTEGER,
    boot_count   INTEGER
);

CREATE TABLE IF NOT EXISTS sensor_alerts (
    alert_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id      TEXT,
    alert_type   TEXT,
    message      TEXT,
    severity     TEXT DEFAULT 'warning',
    created_at   TEXT,
    acknowledged INTEGER DEFAULT 0
);
"""

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL)
    return conn


def save_observation(
    device_id:    str,
    image_path:   Optional[str],
    meta:         dict,
    result:       Optional[dict],
    image_size:   int,
):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        conn.execute(
            """INSERT INTO vision_observations
               (device_id, recorded_at, image_path, width, height, size_bytes,
                model_mode, top_label, top_conf, alert, labels_json, wifi_rssi, boot_count)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                device_id,
                now,
                image_path,
                meta.get("width"),
                meta.get("height"),
                image_size,
                model_mode,
                result.get("top_label")      if result else None,
                result.get("top_confidence") if result else None,
                int(result.get("alert", False)) if result else 0,
                json.dumps(result.get("labels", [])) if result else None,
                meta.get("wifi_rssi"),
                meta.get("boot"),
            )
        )

        # If alert, write to sensor_alerts for aggregator pickup
        if result and result.get("alert"):
            label = result["top_label"]
            conf  = result["top_confidence"]
            msg   = (f"[{device_id}] Plant diagnosis alert: {label} "
                     f"({conf:.0%} confidence)")
            conn.execute(
                """INSERT INTO sensor_alerts (node_id, alert_type, message, severity, created_at)
                   VALUES (?, 'vision_alert', ?, 'warning', ?)""",
                (device_id, msg, now)
            )
            log.warning(f"ALERT: {msg}")

        conn.commit()


# ---------------------------------------------------------------------------
# IMAGE STORAGE
# ---------------------------------------------------------------------------
def save_image(device_id: str, jpeg_bytes: bytes) -> str:
    """Save JPEG to captures directory, return relative path."""
    Path(CAPTURES_DIR).mkdir(parents=True, exist_ok=True)
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{device_id}_{ts}.jpg"
    filepath = os.path.join(CAPTURES_DIR, filename)
    with open(filepath, "wb") as f:
        f.write(jpeg_bytes)
    return filepath


# ---------------------------------------------------------------------------
# FASTAPI APP
# ---------------------------------------------------------------------------
app = FastAPI(title="Farm Pod Vision Ingest", version="1.0.0")


@app.on_event("startup")
async def startup():
    Path(CAPTURES_DIR).mkdir(parents=True, exist_ok=True)
    Path(MODELS_DIR).mkdir(parents=True, exist_ok=True)
    load_model()
    # Ensure schema exists
    get_db().close()
    log.info(f"Vision ingest ready on {HOST}:{PORT} — mode: {model_mode}")


@app.post("/api/vision/ingest")
async def ingest(request: Request):
    # Parse metadata from header
    meta_str = request.headers.get("X-Meta", "{}")
    device_id = request.headers.get("X-Device-ID", "unknown")
    try:
        meta = json.loads(meta_str)
    except json.JSONDecodeError:
        meta = {}

    # Read raw body (JPEG bytes)
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Empty image body")

    size_bytes = len(body)
    if size_bytes > MAX_IMAGE_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Image too large")

    log.info(f"[{device_id}] Received {size_bytes} bytes")

    # Validate it's actually a JPEG
    try:
        img = Image.open(io.BytesIO(body))
        img.verify()
        img = Image.open(io.BytesIO(body))  # reopen after verify
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid image: {e}")

    # Save image
    image_path = save_image(device_id, body)

    # Run inference if model available
    result = None
    if model_mode == "inference":
        try:
            t0     = time.time()
            result = run_inference(img)
            elapsed = time.time() - t0
            log.info(
                f"[{device_id}] Inference: {result['top_label']} "
                f"({result['top_confidence']:.0%}) in {elapsed:.2f}s"
            )
        except Exception as e:
            log.error(f"[{device_id}] Inference failed: {e}")

    # Persist
    save_observation(device_id, image_path, meta, result, size_bytes)

    response = {
        "status":      "ok",
        "device_id":   device_id,
        "model_mode":  model_mode,
        "image_saved": image_path,
        "result":      result,
    }
    return JSONResponse(content=response, status_code=201)


@app.get("/api/vision/recent")
async def recent(limit: int = 20):
    """Return recent observations for dashboard/debugging."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT obs_id, device_id, recorded_at, top_label, top_conf,
                      alert, wifi_rssi, model_mode
               FROM vision_observations
               ORDER BY recorded_at DESC
               LIMIT ?""",
            (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/health")
async def health():
    return {"status": "ok", "model_mode": model_mode, "db": DB_PATH}


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run(
        "vision_ingest:app",
        host=HOST,
        port=PORT,
        log_level="info",
        reload=False,
    )
