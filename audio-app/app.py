"""
ConsultBae Task 3 — mini audio collection app.

Two views (pick from the sidebar):
  1. Submit — name + phone + record/upload audio. On submit, the audio file
     is saved, its properties (duration, sample rate, bitrate, loudness) are
     extracted, and a record is written into the SAME database Task 1 built
     (people + audio_submissions tables) — matched against existing people
     using the same phone-normalization logic as merge.py, so a gig worker
     who already exists in the DB gets linked rather than duplicated.
  2. All Submissions — lists every submission with a play button and its
     extracted properties.

Run with:
    pip install streamlit pydub
    (ffmpeg must be installed and on PATH — pydub shells out to it for any
    format that isn't plain WAV, which covers browser-recorded audio)
    streamlit run app.py
"""

import re
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

import streamlit as st
from pydub import AudioSegment

DB_PATH = Path(__file__).parent.parent / "db" / "consultbae.db"
UPLOADS_DIR = Path(__file__).parent / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)

st.set_page_config(page_title="ConsultBae Audio Collector", layout="centered")


# ---------- shared normalization (same rules as db/merge.py) ----------

def norm_phone(raw):
    digits = re.sub(r"\D", "", raw or "")
    return digits[-10:] if len(digits) >= 10 else None


def norm_name(raw):
    return re.sub(r"\s+", " ", (raw or "").strip()).lower()


def get_or_create_person(conn, name, phone_raw):
    phone = norm_phone(phone_raw)
    if phone:
        row = conn.execute("SELECT person_id FROM people WHERE primary_phone = ?", (phone,)).fetchone()
        if row:
            return row[0]
    cur = conn.execute(
        "INSERT INTO people (canonical_name, primary_phone, match_confidence) VALUES (?, ?, ?)",
        (norm_name(name), phone, "high" if phone else "low"),
    )
    return cur.lastrowid


# ---------- audio analysis ----------

def analyze_audio(file_path):
    """Returns (duration_sec, sample_rate_hz, bitrate_kbps, loudness_db, quality_note).

    Browser mic recordings are typically Opus-in-WebM with no fixed bit depth and
    often no duration in the container header (it's a live stream, not a file).
    pydub's AudioSegment.from_file() tries to text-parse ffmpeg's probe output in
    that case and can throw a raw KeyError('sample_width') because Opus has no
    bit-depth field to parse. Rather than fight that, we first force a clean
    conversion to standard 16-bit PCM WAV via a direct ffmpeg subprocess call —
    that format is unambiguous, so pydub (and everything downstream) reads it
    without any of the fragile text-parsing.
    """
    original_size_bytes = Path(file_path).stat().st_size

    clean_wav_path = Path(file_path).with_suffix(".clean.wav")
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", str(file_path), "-ar", "44100", "-ac", "1", str(clean_wav_path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not clean_wav_path.exists():
        raise RuntimeError(f"ffmpeg could not decode this audio: {result.stderr[-300:]}")

    audio = AudioSegment.from_wav(clean_wav_path)
    clean_wav_path.unlink()

    duration_sec = len(audio) / 1000.0
    sample_rate_hz = audio.frame_rate
    loudness_db = audio.dBFS  # average loudness relative to max possible (0 dBFS = loudest)

    # Bitrate reflects the ORIGINAL submitted file (e.g. compressed Opus), not
    # the intermediate WAV we generated just for analysis — that's what the
    # gig worker actually uploaded/recorded.
    bitrate_kbps = (original_size_bytes * 8 / duration_sec / 1000) if duration_sec > 0 else 0

    # Bonus: rough quality/noise estimate from peak vs average loudness (crest factor).
    # A very small gap between peak and average suggests either heavy compression/clipping
    # or a flat, noisy signal; a very large gap suggests a quiet, clean recording with
    # occasional peaks (normal speech). This is a heuristic, not a lab-grade SNR measurement.
    crest_factor = audio.max_dBFS - audio.dBFS
    if loudness_db < -40:
        quality_note = "Very quiet — likely low input volume or silence"
    elif crest_factor < 6:
        quality_note = "Low dynamic range — possible clipping or heavy background noise"
    elif crest_factor > 20:
        quality_note = "Good dynamic range — likely a clean recording"
    else:
        quality_note = "Normal range"

    return duration_sec, sample_rate_hz, round(bitrate_kbps, 1), round(loudness_db, 1), quality_note


# ---------- pages ----------

def submit_page():
    st.title("Submit Audio")
    st.caption("Gig worker audio submission — Task 3 demo")

    name = st.text_input("Name")
    phone = st.text_input("Phone number")

    st.write("Record audio, or upload a file (either is fine):")
    recorded = st.audio_input("Record")
    uploaded = st.file_uploader("...or upload", type=["wav", "mp3", "m4a", "ogg", "webm"])

    audio_source = recorded or uploaded

    if st.button("Submit", type="primary"):
        if not name or not phone:
            st.error("Name and phone number are required.")
            return
        if not audio_source:
            st.error("Record or upload an audio file first.")
            return

        # Save the raw upload, then re-encode a clean copy so pydub/ffmpeg can
        # reliably read the container format regardless of source.
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        raw_path = UPLOADS_DIR / f"{timestamp}_{norm_name(name).replace(' ', '_')}.raw"
        raw_path.write_bytes(audio_source.getvalue())

        try:
            duration, sample_rate, bitrate, loudness, quality = analyze_audio(raw_path)
        except Exception as e:
            st.error(f"Could not process this audio file: {e}")
            return

        final_path = raw_path.with_suffix(".wav")
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(raw_path), "-ar", "44100", "-ac", "1", str(final_path)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            st.error(f"Could not save final audio copy: {result.stderr[-300:]}")
            return
        raw_path.unlink()

        conn = sqlite3.connect(DB_PATH)
        person_id = get_or_create_person(conn, name, phone)
        conn.execute(
            "INSERT INTO audio_submissions (person_id, file_path, duration_sec, sample_rate_hz, bitrate_kbps, loudness_db) "
            "VALUES (?,?,?,?,?,?)",
            (person_id, str(final_path.name), duration, sample_rate, bitrate, loudness),
        )
        conn.commit()
        conn.close()

        st.success(f"Submitted! Duration: {duration:.1f}s · {sample_rate} Hz · {bitrate} kbps · {loudness} dB")
        st.info(f"Quality estimate: {quality}")


def list_page():
    st.title("All Submissions")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT a.*, p.canonical_name, p.primary_phone FROM audio_submissions a "
        "JOIN people p ON a.person_id = p.person_id ORDER BY a.submitted_at DESC"
    ).fetchall()
    conn.close()

    if not rows:
        st.write("No submissions yet.")
        return

    for row in rows:
        with st.container(border=True):
            st.subheader(row["canonical_name"].title() if row["canonical_name"] else "Unknown")
            st.caption(row["primary_phone"] or "no phone on file")
            file_path = UPLOADS_DIR / row["file_path"]
            if file_path.exists():
                st.audio(str(file_path))
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Duration", f"{row['duration_sec']:.1f}s")
            col2.metric("Sample rate", f"{row['sample_rate_hz']} Hz")
            col3.metric("Bitrate", f"{row['bitrate_kbps']} kbps")
            col4.metric("Loudness", f"{row['loudness_db']} dB")


# ---------- nav ----------

page = st.sidebar.radio("View", ["Submit Audio", "All Submissions"])
if page == "Submit Audio":
    submit_page()
else:
    list_page()
