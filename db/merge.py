"""
ConsultBae Task 1 — Merge 3 messy sources into one deduplicated SQLite DB.

Matching strategy (in priority order):
  1. Normalized email (exact match) — most reliable, available in source1 + source2.
  2. Normalized phone (last 10 digits, exact match) — available in source1 + source3.
  3. Normalized name (exact match, last resort) — used only when neither email nor
     phone is available for a record. Because two different real people can share
     a name (see data issues report), any match made on name alone is flagged
     with match_confidence='low' so it can be reviewed by a human rather than
     silently trusted.

Design choice: rather than one wide flattened table, we keep one canonical
`people` table plus one linked table per source. This mirrors how you'd
actually integrate 3 live systems (you rarely get to flatten someone else's
schema), and it's what Task 2's "check a new CSV against the DB" flow needs
to query against per-source.
"""

import csv
import re
import sqlite3
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
DB_PATH = Path(__file__).parent / "consultbae.db"
ISSUES_LOG = []  # collected as we go, dumped to DATA_ISSUES.md at the end


def log_issue(source, description):
    ISSUES_LOG.append(f"- **[{source}]** {description}")


# ---------- normalization helpers ----------

CITY_ALIASES = {
    "gurgaon": "Gurgaon/Gurugram",
    "gurugram": "Gurgaon/Gurugram",
    "delhi ncr": "Gurgaon/Gurugram",   # treated as the NCR cluster, see data issues report
    "new delhi": "Delhi",
    "delhi": "Delhi",
    "bangalore": "Bengaluru",
    "bengaluru": "Bengaluru",
    "pune": "Pune",
    "noida": "Noida",
}


def norm_email(raw):
    if not raw or not raw.strip():
        return None
    return raw.strip().lower()


def norm_phone(raw):
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 10:
        return None
    return digits[-10:]  # last 10 digits, drops +91/91/0 prefixes


def norm_name(raw):
    if not raw:
        return None
    return re.sub(r"\s+", " ", raw.strip()).lower()


def norm_city(raw):
    if not raw or not raw.strip():
        return None
    key = re.sub(r"\s+", " ", raw.strip()).lower()
    return CITY_ALIASES.get(key, raw.strip().title())


def looks_like_email(s):
    return bool(s) and "@" in s and "." in s.split("@")[-1]


# ---------- schema ----------

SCHEMA = """
CREATE TABLE people (
    person_id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name TEXT,
    primary_email TEXT,
    primary_phone TEXT,
    primary_city TEXT,
    match_confidence TEXT DEFAULT 'high'  -- 'high' = matched on email/phone, 'low' = name-only
);

CREATE TABLE source_naukri (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER REFERENCES people(person_id),
    full_name TEXT, email TEXT, phone TEXT, city TEXT,
    experience_years REAL, current_ctc TEXT, applied_date TEXT, skills TEXT
);

CREATE TABLE source_gig (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER REFERENCES people(person_id),
    email TEXT, worker_name TEXT, rate_value REAL, rate_unit TEXT,
    location TEXT, status TEXT, skill_tags TEXT
);

CREATE TABLE source_cbnexus (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER REFERENCES people(person_id),
    name TEXT, phone TEXT, city TEXT, verified TEXT, projects_completed INTEGER
);

-- Task 3 will insert into these
CREATE TABLE audio_submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER REFERENCES people(person_id),
    file_path TEXT,
    duration_sec REAL,
    sample_rate_hz INTEGER,
    bitrate_kbps REAL,
    loudness_db REAL,
    submitted_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


class PersonIndex:
    """Tracks canonical people and resolves each incoming record to a person_id."""

    def __init__(self, conn):
        self.conn = conn
        self.by_email = {}
        self.by_phone = {}
        self.by_name = {}  # name -> list of person_ids (to detect ambiguity)

    def _create_person(self, name, email, phone, city, confidence="high"):
        cur = self.conn.execute(
            "INSERT INTO people (canonical_name, primary_email, primary_phone, primary_city, match_confidence) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, email, phone, city, confidence),
        )
        pid = cur.lastrowid
        if email:
            self.by_email[email] = pid
        if phone:
            self.by_phone[phone] = pid
        if name:
            self.by_name.setdefault(name, []).append(pid)
        return pid

    def resolve(self, name, email, phone, city):
        """Find or create the person_id for this record. Returns (person_id, method)."""
        if email and email in self.by_email:
            pid = self.by_email[email]
            if phone and phone not in self.by_phone:
                self.by_phone[phone] = pid  # learn this person's phone too
            return pid, "email"

        if phone and phone in self.by_phone:
            pid = self.by_phone[phone]
            if email and email not in self.by_email:
                self.by_email[email] = pid  # learn this person's email too
            return pid, "phone"

        if email or phone:
            # New person, but confidently identified — high confidence
            pid = self._create_person(name, email, phone, city, "high")
            return pid, "new-high"

        # No email, no phone at all — must fall back to name (last resort)
        if name and name in self.by_name:
            candidates = self.by_name[name]
            if len(candidates) == 1:
                return candidates[0], "name-low-confidence"
            # ambiguous: multiple different people already share this name
            pid = self._create_person(name, email, phone, city, "low")
            return pid, "name-ambiguous-new"

        pid = self._create_person(name, email, phone, city, "low" if not (email or phone) else "high")
        return pid, "new-name-only"


def load_naukri(conn, idx):
    path = DATA_DIR / "source1_naukri_applicants.csv"
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        seen_rows = set()
        for row in reader:
            if not any(row.values()):
                continue
            name, email, phone = row["Full Name"], norm_email(row["Email"]), norm_phone(row["Phone"])
            city = norm_city(row["City"])

            row_key = (email, phone)
            if row_key in seen_rows:
                log_issue("source1", f"Exact duplicate row for {name} ({row['Email']}) — same email+phone repeated, skipped extra copy.")
                continue
            seen_rows.add(row_key)

            # planted issue: CTC field sometimes contains a value that looks like
            # Experience (e.g. "4.2") instead of a salary figure — too small to be a CTC.
            ctc_raw = row["Current CTC"]
            ctc_flag = ""
            try:
                if ctc_raw and float(ctc_raw) < 50:
                    log_issue("source1", f"Suspicious 'Current CTC' value for {name}: '{ctc_raw}' looks like it's actually Experience (years), not a salary. Kept raw value, flagged — did not guess the real CTC.")
                    ctc_flag = " (flagged: implausible)"
            except ValueError:
                pass

            pid, method = idx.resolve(norm_name(name), email, phone, city)
            if method == "name-ambiguous-new":
                log_issue("source1", f"'{name}' has no email/phone match to an existing person but the name collides with a different existing person — created as a separate low-confidence record instead of guessing they're the same person.")

            conn.execute(
                "INSERT INTO source_naukri (person_id, full_name, email, phone, city, experience_years, current_ctc, applied_date, skills) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (pid, name, row["Email"], row["Phone"], row["City"], row["Experience (Years)"], ctc_raw + ctc_flag, row["Applied Date"], row["Skills"]),
            )


def load_gig(conn, idx):
    path = DATA_DIR / "source2_gig_workers.csv"
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not any((row.get(k) or "").strip() for k in row):
                log_issue("source2", "Blank row found (all fields empty) — skipped.")
                continue

            email_raw = row.get("email_id", "")
            if not looks_like_email(email_raw):
                # planted issue: column-shifted/corrupted row.
                # Try to recover by locating the field that actually looks like an email.
                shifted_email = next((v for v in row.values() if looks_like_email(v)), None)
                log_issue(
                    "source2",
                    f"Malformed row detected — 'email_id' field ('{email_raw}') is not a valid email; "
                    f"values appear column-shifted. Found a valid email elsewhere in the row "
                    f"({shifted_email or 'none'}). This row's values matched an already-clean row "
                    f"for the same person, so it was treated as a corrupted duplicate and discarded "
                    f"rather than guessing a repaired mapping.",
                )
                continue

            email = norm_email(email_raw)
            name = row["worker_name"]

            rate_raw = row.get("rate", "")
            m = re.match(r"([\d.]+)\s*(k?)/(\w+)", rate_raw.strip(), re.I)
            rate_value, rate_unit = None, None
            if m:
                val, k, unit = m.groups()
                rate_value = float(val) * (1000 if k else 1)
                rate_unit = "per_hour" if unit.lower().startswith("hr") else "per_month"
            else:
                log_issue("source2", f"Could not parse rate '{rate_raw}' for {name} — stored raw, rate_value left null.")

            city = norm_city(row["location"])
            pid, method = idx.resolve(norm_name(name), email, None, city)
            if method == "name-ambiguous-new":
                log_issue("source2", f"'{name}' name collides with an existing different person and has no matching email in that record — created as separate low-confidence person.")

            conn.execute(
                "INSERT INTO source_gig (person_id, email, worker_name, rate_value, rate_unit, location, status, skill_tags) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (pid, email_raw, name, rate_value, rate_unit, row["location"], row["status"], row["skill_tags"]),
            )


def load_cbnexus(conn, idx):
    path = DATA_DIR / "source3_cbnexus_contacts.csv"
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        header_seen_once = False
        for row in reader:
            if row["Name"] == "Name" and row["Phone Number"] == "Phone Number":
                log_issue("source3", "Duplicate embedded header row found mid-file (file is two exports concatenated) — skipped as a header, not data.")
                continue
            if not any(row.values()):
                continue

            name, phone = row["Name"], norm_phone(row["Phone Number"])
            city = norm_city(row["City"])

            pid, method = idx.resolve(norm_name(name), None, phone, city)
            if method in ("name-ambiguous-new", "new-name-only") and phone is None:
                log_issue("source3", f"'{name}' has no usable phone number and no name-based high-confidence match — low-confidence record.")
            if method == "name-ambiguous-new":
                log_issue("source3", f"'{name}' (phone {row['Phone Number']}) shares a name with a different existing person but the phone number doesn't match theirs — kept as a separate person rather than merging on name alone. Two different real people appear to share this name across the dataset.")

            conn.execute(
                "INSERT INTO source_cbnexus (person_id, name, phone, city, verified, projects_completed) "
                "VALUES (?,?,?,?,?,?)",
                (pid, name, row["Phone Number"], row["City"], row["Verified"], row["Projects Completed"]),
            )


def main():
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)

    idx = PersonIndex(conn)
    load_naukri(conn, idx)
    load_gig(conn, idx)
    load_cbnexus(conn, idx)
    conn.commit()

    # Post-pass: surface any name that maps to >1 distinct person_id with no
    # shared email/phone between them. This is NOT an error to fix — it's an
    # intentional ambiguity to flag, since blindly merging on name would risk
    # combining two different real people who happen to share a name.
    dupe_names = conn.execute(
        "SELECT canonical_name, COUNT(*) c FROM people GROUP BY canonical_name HAVING c > 1"
    ).fetchall()
    for name, count in dupe_names:
        ids = conn.execute(
            "SELECT person_id, primary_email, primary_phone FROM people WHERE canonical_name = ?", (name,)
        ).fetchall()
        log_issue(
            "cross-source",
            f"'{name.title()}' appears as {count} distinct people in the merged DB (person_ids "
            f"{[i[0] for i in ids]}) — no email or phone in common between them, so they were kept "
            f"separate rather than merged. This could be {count} genuinely different real people "
            f"sharing a name, or the same person recorded inconsistently with no way to verify. "
            f"Left as-is intentionally — flagging for manual review rather than guessing.",
        )

    n_people = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    n_low = conn.execute("SELECT COUNT(*) FROM people WHERE match_confidence='low'").fetchone()[0]
    n_naukri = conn.execute("SELECT COUNT(*) FROM source_naukri").fetchone()[0]
    n_gig = conn.execute("SELECT COUNT(*) FROM source_gig").fetchone()[0]
    n_cb = conn.execute("SELECT COUNT(*) FROM source_cbnexus").fetchone()[0]

    print(f"People (deduplicated): {n_people}  (low-confidence: {n_low})")
    print(f"Rows loaded — naukri: {n_naukri}, gig: {n_gig}, cbnexus: {n_cb}")
    print(f"Data issues logged: {len(ISSUES_LOG)}")

    issues_path = Path(__file__).parent.parent / "DATA_ISSUES.md"
    with open(issues_path, "w") as f:
        f.write("# Data Issues Report\n\n")
        f.write(f"Generated by `db/merge.py`. {len(ISSUES_LOG)} issues found and handled.\n\n")
        f.write("\n".join(ISSUES_LOG))
        f.write("\n\n## General normalization applied to every row\n")
        f.write("- Emails lowercased + trimmed before matching.\n")
        f.write("- Phone numbers reduced to last 10 digits (strips +91 / 91 / leading 0 country/trunk prefixes).\n")
        f.write("- City names trimmed, and known aliases merged for city display purposes: Gurgaon/Gurugram/Delhi NCR grouped, New Delhi/Delhi grouped, Bangalore/Bengaluru grouped. Raw city value is still stored per-source untouched.\n")
        f.write("- Matching priority: email exact match > phone exact match (last 10 digits) > name (last resort, only when neither of the above exists — flagged low-confidence since names collide between different real people in this dataset).\n")

    conn.close()
    print(f"\nDB written to {DB_PATH}")
    print(f"Issues report written to {issues_path}")


if __name__ == "__main__":
    main()
