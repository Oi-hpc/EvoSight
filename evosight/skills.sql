CREATE TABLE IF NOT EXISTS research_skills(
    id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL REFERENCES events,
    lesson_id INTEGER NOT NULL UNIQUE REFERENCES observations, job_id INTEGER REFERENCES jobs,
    status TEXT NOT NULL CHECK(status IN ('generating','pending','approved','rejected','unsuitable')),
    method TEXT, reason TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL, reviewed_at TEXT,
    CHECK(status IN ('generating','unsuitable') OR method IS NOT NULL)
);
