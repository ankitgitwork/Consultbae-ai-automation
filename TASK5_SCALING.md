# Task 5 — Scaling to 5,000 Gig Workers Over a Weekend

This is grounded in what was actually built for Task 1–3 (SQLite, local file
storage, synchronous ffmpeg processing, a Streamlit dev server) — not a
generic scaling essay. These are the specific things in *this* architecture
that would break first.

## What breaks first

**1. SQLite write locking.** The whole system — the merge DB, the Flask
duplicate-check API, and the Streamlit audio app — all write to one SQLite
file. SQLite allows only one writer at a time; the rest queue or fail with
"database is locked." With 5,000 workers submitting over a single weekend
(almost certainly clustered around a few peak hours, not spread evenly),
concurrent submissions would start failing well before 5,000 — probably in
the low hundreds of simultaneous writers, since each submission does two
writes (person lookup/insert, then the audio_submissions insert).

**2. Synchronous audio processing blocks the request.** Right now, converting
audio through ffmpeg happens inline, in the same request that's supposed to
tell the worker "submission received." A worker on a slow connection
uploading a large file would sit there waiting for ffmpeg to finish before
getting any confirmation — and if enough submissions land at once, the
single dev server thread queues them one behind another, so wait times climb
sharply as load increases, not gracefully.

**3. Local disk storage doesn't scale or persist safely.** Audio files are
currently saved to a folder on whatever machine is running the app. That
doesn't survive a redeploy, doesn't work if you ever need more than one
server instance to handle load, and has no backup — a disk failure loses
every recording.

**4. No protection against duplicate/accidental submissions.** A worker who
double-clicks Submit, or whose request times out and retries, currently
creates two separate audio_submissions rows with no way to tell they're the
same event. At small scale this is a minor annoyance; at 5,000 submissions
in a weekend it's a real chunk of duplicate storage and confusing data.

**5. The ngrok tunnel (used for the n8n automation in Task 2) is a
single point of failure and not meant for production** — it's a free dev
tunnel to one laptop. It would need to be replaced entirely before any real
traffic, not just scaled up.

## What I'd change before launch

- **Swap SQLite for Postgres** — same schema, but built for concurrent
  writers instead of locking the whole file.
- **Make audio processing asynchronous** — the submit endpoint should just
  save the raw file and return "received" immediately, then a background
  worker (a queue like Celery/RQ, or a cloud function trigger) does the
  ffmpeg conversion and property extraction afterward. This decouples "did
  the worker's submission succeed" from "how long does audio processing
  take," which matters a lot under load.
- **Move audio files to object storage** (S3 or equivalent) instead of local
  disk — durable, scales independently of the app server, and cheap for
  this volume.
- **Add an idempotency key** from the client on submit (e.g. a UUID generated
  in the browser before the request fires) so a retry or double-click
  updates the same record instead of creating a duplicate.
- **Deploy behind a real host with autoscaling** (Render/Railway with more
  than one instance) instead of a single ngrok-tunneled dev server, so a
  traffic spike adds capacity instead of a queue.
- **Add basic rate limiting** on the public submission form — a launch to
  5,000 real workers is also an obvious target for accidental spam (impatient
  retries) or automated abuse, and there's currently nothing to slow that down.

## Cost

The main new costs at this scale are object storage for audio files (small
per-file, but adds up linearly with submissions and needs a retention
policy — do we keep every recording forever, or expire old ones?) and
background-worker compute for the ffmpeg processing, which is currently free
because it happens inline. Both are proportional to submission volume, so
the right first move is just measuring actual per-file cost from a small
pilot before assuming what 5,000 submissions costs.
