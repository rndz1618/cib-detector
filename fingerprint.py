# CIB Detector - Layer 1: Content Fingerprinting + Layer 2: Temporal Anomaly
"""
Detects coordinated inauthentic behavior signals:

  Layer 1 — Content Fingerprinting
    * exact SHA256 (byte match)
    * robust audio fingerprint (energy envelope, robust to re-encode)
    * robust video frame signature (region-luma, robust to small crops)

  Layer 2 — Temporal Anomaly (resurrection)
    * finds aged content that went dormant then suddenly re-shared/spiked

Pure stdlib + SQLite → tiny footprint for 2GB ARM host.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "fingerprints.db"

# ------------------------------------------------------------------ hashing


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def audio_fingerprint(path: Path, sample_rate: int = 8000, max_seconds: int = 30) -> str:
    """Energy-envelope audio fingerprint, robust to re-encode/bitrate."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(path),
             "-t", str(max_seconds), "-ac", "1", "-ar", str(sample_rate),
             "-f", "wav", tmp_path],
            check=True, capture_output=True, timeout=60,
        )
        with wave.open(tmp_path, "rb") as w:
            raw = w.readframes(w.getnframes())
    except Exception as _e:
        # TODO: log _e
        return ""
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    if not raw:
        return ""

    buckets = 256
    if len(raw) >= buckets * 2:
        step = len(raw) // buckets
    else:
        step = 1
        buckets = max(1, len(raw) // 2)
    feats = []
    for i in range(buckets):
        chunk = raw[i * step:(i + 1) * step]
        if not chunk:
            feats.append("0")
            continue
        total = 0
        crossings = 0
        prev = 0
        for j in range(0, len(chunk) - 1, 2):
            s = int.from_bytes(chunk[j:j+2], "little", signed=True)
            total += abs(s)
            if prev and (s >= 0) != (prev >= 0):
                crossings += 1
            prev = s
        n = len(chunk) // 2
        avg = total / max(1, n)
        zcr = crossings / max(1, n)  # zero-crossing rate ~ pitch proxy
        # 2-bit energy + 3-bit zcr → 5 bits per bucket, more discriminative
        e = min(3, int(avg / 4000)) if avg > 80 else 0
        z = min(7, int(zcr * 64))
        feats.append(f"{e}{z:02d}")

    packed = "".join(feats)
    return hmac.new(b"plan365-cib-audio-v1", packed.encode(), hashlib.sha256).hexdigest()


def video_signature(path: Path, samples: int = 8) -> str:
    """Frame region-luma signature; robust to re-encode and small crops."""
    sig_parts: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        pattern = td + "/f%03d.png"
        try:
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-i", str(path),
                 "-vf", "fps=1,scale=16:9",
                 "-frames:v", str(samples), pattern],
                check=True, capture_output=True, timeout=60,
            )
        except subprocess.CalledProcessError:
            return ""
        frames = sorted(Path(td).glob("f*.png"))
        for i, p in enumerate(frames):
            if i >= samples:
                break
            sig = _frame_luma(p, 4, 4)
            if sig:
                sig_parts.append(sig)
    if not sig_parts:
        return ""
    joined = "\n".join(sig_parts)
    return hmac.new(b"plan365-cib-video-v1", joined.encode(), hashlib.sha256).hexdigest()


def _frame_luma(png: Path, cols: int = 4, rows: int = 4) -> str | None:
    with tempfile.NamedTemporaryFile(suffix=".raw") as tmp:
        try:
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-i", str(png),
                 "-vf", f"scale={cols}:{rows},format=gray",
                 "-f", "rawvideo", tmp.name],
                check=True, capture_output=True, timeout=30,
            )
            tmp.seek(0)
            data = tmp.read(cols * rows)
        except (subprocess.CalledProcessError, OSError):
            return None
    if not data:
        return None
    # quantize to coarse levels for robustness
    q = "".join(chr(48 + (b >> 4)) for b in data)
    return q


# ------------------------------------------------------------------ database

SCHEMA = """
CREATE TABLE IF NOT EXISTS content (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL,
    audio_fp TEXT NOT NULL DEFAULT '',
    video_fp TEXT NOT NULL DEFAULT '',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    upload_count INTEGER NOT NULL DEFAULT 1,
    UNIQUE(sha256)
);
CREATE INDEX IF NOT EXISTS idx_content_audio ON content(audio_fp);
CREATE INDEX IF NOT EXISTS idx_content_video ON content(video_fp);

CREATE TABLE IF NOT EXISTS appearances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_id INTEGER NOT NULL REFERENCES content(id) ON DELETE CASCADE,
    seen_at TEXT NOT NULL,
    engagement INTEGER NOT NULL DEFAULT 0,
    url TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_appearances_content ON appearances(content_id);
CREATE INDEX IF NOT EXISTS idx_appearances_seen ON appearances(seen_at);

CREATE TABLE IF NOT EXISTS engagement_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_id INTEGER NOT NULL,
    age_days INTEGER NOT NULL,
    spike_ratio REAL NOT NULL,
    detected_at TEXT NOT NULL,
    score REAL NOT NULL
);
"""


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace(" ", "T"))


def _now() -> str:
    return datetime.utcnow().isoformat(sep=" ", timespec="seconds")


@dataclass
class IngestResult:
    is_new: bool
    content_id: int
    same_as: int | None
    is_reupload: bool
    age_days: int
    fp_score: float


class FingerprintStore:
    def __init__(self, db: Path = DB_PATH):
        self.conn = sqlite3.connect(str(db), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def ingest(self, path: Path, source: str = "", engagement: int = 0,
               seen_at: str | None = None, url: str = "") -> IngestResult:
        seen_at = seen_at or _now()
        sha = sha256_file(path)
        afp = audio_fingerprint(path)
        vfp = video_signature(path)

        cur = self.conn.execute(
            """SELECT * FROM content
               WHERE sha256=?
                  OR (audio_fp != '' AND audio_fp=?)
                  OR (video_fp != '' AND video_fp=?)""",
            (sha, afp, vfp),
        )
        rows = cur.fetchall()

        if rows:
            row = rows[0]
            cid = row["id"]
            try:
                age_days = max(0, (datetime.utcnow() - _parse(row["first_seen"])).days)
            except Exception:
                age_days = 0
            self.conn.execute(
                "UPDATE content SET last_seen=?, upload_count=upload_count+1 WHERE id=?",
                (seen_at, cid),
            )
            self.conn.execute(
                "INSERT INTO appearances(content_id, seen_at, engagement, url) VALUES(?,?,?,?)",
                (cid, seen_at, engagement, url),
            )
            self.conn.commit()
            return IngestResult(False, cid, row["id"], True, age_days, 1.0)

        cur = self.conn.execute(
            "INSERT INTO content(sha256, audio_fp, video_fp, first_seen, last_seen, source, upload_count) "
            "VALUES(?,?,?,?,?,?,1)",
            (sha, afp, vfp, seen_at, seen_at, source),
        )
        cid = cur.lastrowid
        self.conn.execute(
            "INSERT INTO appearances(content_id, seen_at, engagement, url) VALUES(?,?,?,?)",
            (cid, seen_at, engagement, url),
        )
        self.conn.commit()
        return IngestResult(True, cid, None, False, 0, 0.0)

    def detect_resurrections(self, min_age_days: int = 30, spike_ratio: float = 5.0,
                             recent_window_days: int = 7) -> list[dict]:
        """Layer 2: content that was idle long ago then re-shared recently with spike."""
        now = datetime.utcnow()
        flags = []
        cur = self.conn.execute("SELECT * FROM content")
        for r in cur.fetchall():
            try:
                first = _parse(r["first_seen"])
            except Exception:
                continue
            age = (now - first).days
            if age < min_age_days:
                continue

            hist = self.conn.execute(
                "SELECT engagement, seen_at FROM appearances WHERE content_id=? ORDER BY seen_at",
                (r["id"],),
            ).fetchall()
            if len(hist) < 2:
                continue

            # baseline = first half engagement mean
            half = max(1, len(hist) // 2)
            base_eng = [h["engagement"] for h in hist[:half]]
            recent_eng = [h["engagement"] for h in hist[half:]]
            base_avg = sum(base_eng) / max(1, len(base_eng))
            recent_avg = sum(recent_eng) / max(1, len(recent_eng))
            ratio = recent_avg / (base_avg or 1)
            if ratio >= spike_ratio:
                score = min(1.0, ratio / 20)
                self.conn.execute(
                    "INSERT INTO engagement_events(content_id, age_days, spike_ratio, detected_at, score) "
                    "VALUES(?,?,?,?,?)",
                    (r["id"], age, ratio, _now(), score),
                )
                self.conn.commit()
                flags.append({
                    "content_id": r["id"],
                    "sha256": r["sha256"],
                    "age_days": age,
                    "spike_ratio": round(ratio, 2),
                    "score": round(score, 2),
                    "upload_count": r["upload_count"],
                })
        flags.sort(key=lambda x: x["score"], reverse=True)
        return flags

    def stats(self) -> dict:
        return {
            "content": self.conn.execute("SELECT COUNT(*) FROM content").fetchone()[0],
            "appearances": self.conn.execute("SELECT COUNT(*) FROM appearances").fetchone()[0],
            "events": self.conn.execute("SELECT COUNT(*) FROM engagement_events").fetchone()[0],
        }


if __name__ == "__main__":
    print("CIB Detector Layer 1+2 ready.")