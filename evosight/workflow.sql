CREATE TABLE IF NOT EXISTS event_goals (
    event_id INTEGER NOT NULL REFERENCES events, version INTEGER NOT NULL,
    title TEXT NOT NULL, scope TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 2,
    related TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL,
    PRIMARY KEY(event_id,version)
);
CREATE TABLE IF NOT EXISTS tracking_rounds (
    id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL REFERENCES events, goal_version INTEGER NOT NULL,
    request_key TEXT NOT NULL UNIQUE, reasons TEXT NOT NULL, window_start TEXT NOT NULL, window_end TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued', created_at TEXT NOT NULL, finished_at TEXT, detail TEXT NOT NULL DEFAULT '',
    input_docs TEXT, context TEXT, plan TEXT, job_id INTEGER REFERENCES jobs, parent_snapshot INTEGER,
    world_seed_only INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS rounds_by_event ON tracking_rounds(event_id,id);
CREATE TABLE IF NOT EXISTS research_tasks (
    id INTEGER PRIMARY KEY, round_id INTEGER NOT NULL REFERENCES tracking_rounds, role TEXT NOT NULL,
    question TEXT NOT NULL, target TEXT NOT NULL, time_scope TEXT NOT NULL, done_when TEXT NOT NULL,
    priority INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'queued', detail TEXT NOT NULL DEFAULT '',
    result TEXT, input_ids TEXT, UNIQUE(round_id,role,question)
);
CREATE TABLE IF NOT EXISTS model_receipts (
    job_id INTEGER NOT NULL REFERENCES jobs, role TEXT NOT NULL, input_hash TEXT NOT NULL,
    response TEXT NOT NULL, model TEXT NOT NULL, run_id INTEGER REFERENCES runs,
    PRIMARY KEY(job_id,role,input_hash)
);
CREATE TABLE IF NOT EXISTS platforms (
    url TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, next_poll TEXT NOT NULL,
    resource TEXT, etag TEXT, modified TEXT, checked_at TEXT, success_at TEXT, failures INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '', lease_until TEXT, lease_owner TEXT
);
CREATE TABLE IF NOT EXISTS event_platforms (
    event_id INTEGER NOT NULL REFERENCES events, url TEXT NOT NULL REFERENCES platforms,
    reason TEXT NOT NULL, origin TEXT NOT NULL, watching INTEGER NOT NULL, pinned INTEGER NOT NULL DEFAULT 0,
    consumed INTEGER NOT NULL DEFAULT 0, watch_cursor INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(event_id,url)
);
CREATE TABLE IF NOT EXISTS platform_entries (
    id INTEGER PRIMARY KEY, platform TEXT NOT NULL REFERENCES platforms, document_id INTEGER NOT NULL REFERENCES documents,
    data TEXT NOT NULL, seen_at TEXT NOT NULL, UNIQUE(platform,document_id)
);
CREATE INDEX IF NOT EXISTS stream_for_platform ON platform_entries(platform,id);
CREATE TABLE IF NOT EXISTS discovery_log (
    id INTEGER PRIMARY KEY,event_id INTEGER REFERENCES events, round_id INTEGER REFERENCES tracking_rounds,
    url TEXT NOT NULL, reason TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watch_signals (
    id INTEGER PRIMARY KEY,event_id INTEGER NOT NULL REFERENCES events, document_id INTEGER NOT NULL REFERENCES documents,
    signal_key TEXT NOT NULL,action TEXT NOT NULL,reason TEXT NOT NULL,seen_at TEXT NOT NULL,
    round_id INTEGER REFERENCES tracking_rounds, UNIQUE(event_id,signal_key)
);
CREATE TABLE IF NOT EXISTS evidence_blobs (
    digest TEXT PRIMARY KEY, mime TEXT NOT NULL, content BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_assets (
    id INTEGER PRIMARY KEY,document_id INTEGER NOT NULL REFERENCES documents, url TEXT NOT NULL,
    kind TEXT NOT NULL, state TEXT NOT NULL, detail TEXT NOT NULL, digest TEXT REFERENCES evidence_blobs,
    extractor TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(document_id,url,extractor,digest)
);
CREATE INDEX IF NOT EXISTS assets_by_document ON evidence_assets(document_id,id);
CREATE TABLE IF NOT EXISTS evidence_children (
    parent_id INTEGER NOT NULL REFERENCES evidence_assets,url TEXT NOT NULL,kind TEXT NOT NULL,playback TEXT NOT NULL DEFAULT '',
    asset_id INTEGER REFERENCES evidence_assets,last_attempt TEXT,PRIMARY KEY(parent_id,url)
);
CREATE TABLE IF NOT EXISTS fragments (
    id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL REFERENCES evidence_assets, locator TEXT NOT NULL,
    text TEXT NOT NULL, link TEXT NOT NULL, UNIQUE(asset_id,locator)
);
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, identity TEXT NOT NULL,
    UNIQUE(kind,name,identity)
);
CREATE TABLE IF NOT EXISTS entity_aliases (
    entity_id INTEGER NOT NULL REFERENCES entities, alias TEXT NOT NULL COLLATE NOCASE, PRIMARY KEY(entity_id,alias)
);
CREATE INDEX IF NOT EXISTS aliases_by_name ON entity_aliases(alias);
CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY,event_id INTEGER NOT NULL REFERENCES events, role TEXT NOT NULL, section TEXT NOT NULL,
    kind TEXT NOT NULL, subject TEXT NOT NULL, text TEXT NOT NULL, occurred_at TEXT, precision TEXT NOT NULL,
    reason TEXT NOT NULL, refs TEXT NOT NULL, citations TEXT NOT NULL, entity_id INTEGER REFERENCES entities,
    replaces INTEGER REFERENCES claims, identity TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(event_id,identity)
);
CREATE INDEX IF NOT EXISTS claims_by_object ON claims(event_id,entity_id,section);
CREATE INDEX IF NOT EXISTS shared_claims_by_entity ON claims(entity_id,id) WHERE entity_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS knowledge_snapshots (
    id INTEGER PRIMARY KEY,event_id INTEGER NOT NULL REFERENCES events, round_id INTEGER REFERENCES tracking_rounds,
    generation INTEGER NOT NULL DEFAULT 1,parent_id INTEGER REFERENCES knowledge_snapshots,
    goal_version INTEGER NOT NULL, created_at TEXT NOT NULL, gaps TEXT NOT NULL, summary TEXT NOT NULL, changes TEXT NOT NULL,
    node_upper INTEGER NOT NULL DEFAULT 0, UNIQUE(round_id,generation)
);
CREATE TABLE IF NOT EXISTS snapshot_claims (
    snapshot_id INTEGER NOT NULL REFERENCES knowledge_snapshots,claim_id INTEGER NOT NULL REFERENCES claims,
    index_section TEXT NOT NULL DEFAULT '',index_reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(snapshot_id,claim_id)
);
CREATE INDEX IF NOT EXISTS index_by_version ON snapshot_claims(claim_id,snapshot_id);
CREATE INDEX IF NOT EXISTS indexes_by_event ON knowledge_snapshots(event_id,id);
CREATE TABLE IF NOT EXISTS world_entries (
    id INTEGER PRIMARY KEY, demo INTEGER NOT NULL CHECK(demo IN (0,1)), category TEXT NOT NULL,
    head_id INTEGER NOT NULL REFERENCES claims
);
CREATE INDEX IF NOT EXISTS world_by_category ON world_entries(demo,category,id);
CREATE UNIQUE INDEX IF NOT EXISTS world_current_version ON world_entries(head_id);
CREATE TABLE IF NOT EXISTS world_versions (
    claim_id INTEGER PRIMARY KEY REFERENCES claims, entry_id INTEGER NOT NULL REFERENCES world_entries,
    version INTEGER NOT NULL CHECK(version>=1), canonical_id INTEGER NOT NULL REFERENCES claims,
    demo INTEGER NOT NULL, signature TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS world_content_key ON world_versions(demo,signature) WHERE claim_id=canonical_id;
CREATE UNIQUE INDEX IF NOT EXISTS world_version_number ON world_versions(entry_id,version) WHERE claim_id=canonical_id;
CREATE INDEX IF NOT EXISTS world_version_entry ON world_versions(entry_id,claim_id);
CREATE TABLE IF NOT EXISTS world_terms (
    term TEXT NOT NULL, entry_id INTEGER NOT NULL REFERENCES world_entries, weight INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(term,entry_id)
);
CREATE INDEX IF NOT EXISTS world_terms_by_entry ON world_terms(entry_id);
CREATE TABLE IF NOT EXISTS world_aliases (
    demo INTEGER NOT NULL,entity_id INTEGER NOT NULL REFERENCES entities,alias TEXT NOT NULL COLLATE NOCASE,
    PRIMARY KEY(demo,entity_id,alias)
);
CREATE TABLE IF NOT EXISTS world_link_choices (
    event_id INTEGER NOT NULL REFERENCES events,entry_id INTEGER NOT NULL REFERENCES world_entries,
    choice TEXT NOT NULL CHECK(choice IN ('pin','exclude')),reason TEXT NOT NULL,
    PRIMARY KEY(event_id,entry_id)
);
CREATE TABLE IF NOT EXISTS world_directories (
    code TEXT PRIMARY KEY,parent_code TEXT REFERENCES world_directories(code),label TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS directory_children ON world_directories(parent_code,code);
CREATE TABLE IF NOT EXISTS world_record_data (
    version_id INTEGER PRIMARY KEY REFERENCES claims, schema_id TEXT NOT NULL, schema_version INTEGER NOT NULL,
    path TEXT NOT NULL REFERENCES world_directories(code), attributes TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS world_catalog_members (
    entry_id INTEGER PRIMARY KEY REFERENCES world_entries, head_id INTEGER NOT NULL REFERENCES claims,
    demo INTEGER NOT NULL, path TEXT NOT NULL REFERENCES world_directories(code),
    entity_id INTEGER REFERENCES entities, kind TEXT NOT NULL, occurred_at TEXT, recorded_at TEXT NOT NULL,
    index_format INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS catalog_by_path ON world_catalog_members(demo,path,recorded_at DESC,entry_id DESC);
CREATE INDEX IF NOT EXISTS catalog_by_recent ON world_catalog_members(demo,recorded_at DESC,entry_id DESC);
CREATE INDEX IF NOT EXISTS catalog_by_entity ON world_catalog_members(demo,entity_id,recorded_at DESC,entry_id DESC) WHERE entity_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS catalog_by_occurrence ON world_catalog_members(demo,occurred_at,entry_id) WHERE occurred_at IS NOT NULL;
CREATE TABLE IF NOT EXISTS world_task_reads (
    task_id INTEGER PRIMARY KEY REFERENCES research_tasks, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,event_id INTEGER NOT NULL REFERENCES events, prediction_id INTEGER REFERENCES predictions,
    origin TEXT NOT NULL, question TEXT NOT NULL, condition TEXT NOT NULL, due_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open', priority INTEGER NOT NULL DEFAULT 2, refs TEXT NOT NULL, answer TEXT NOT NULL DEFAULT '',
    resolution_id INTEGER REFERENCES resolutions, fingerprint TEXT NOT NULL, UNIQUE(event_id,fingerprint)
);
CREATE TABLE IF NOT EXISTS end_proposals (
    id INTEGER PRIMARY KEY,event_id INTEGER NOT NULL REFERENCES events, reason TEXT NOT NULL, refs TEXT NOT NULL,
    goal_version INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
    UNIQUE(event_id,goal_version,reason)
);
CREATE TABLE IF NOT EXISTS prediction_checks (
    prediction_id INTEGER NOT NULL REFERENCES predictions, snapshot_id INTEGER NOT NULL REFERENCES knowledge_snapshots,
    state TEXT NOT NULL, detail TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(prediction_id,snapshot_id)
);
CREATE TABLE IF NOT EXISTS revision_contexts (
    revision_id INTEGER PRIMARY KEY REFERENCES revisions, snapshot_id INTEGER NOT NULL REFERENCES knowledge_snapshots,
    dependencies TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS round_publications (
    round_id INTEGER NOT NULL REFERENCES tracking_rounds, signature TEXT NOT NULL,
    snapshot_id INTEGER NOT NULL REFERENCES knowledge_snapshots, PRIMARY KEY(round_id,signature)
);
CREATE TABLE IF NOT EXISTS round_prediction_gates (
    round_id INTEGER NOT NULL REFERENCES tracking_rounds,prediction_id INTEGER NOT NULL REFERENCES predictions,
    state TEXT NOT NULL,PRIMARY KEY(round_id,prediction_id)
);
CREATE TABLE IF NOT EXISTS claim_details (
    claim_id INTEGER PRIMARY KEY REFERENCES claims,facets TEXT NOT NULL,aliases TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot_contexts (
    snapshot_id INTEGER PRIMARY KEY REFERENCES knowledge_snapshots,payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot_documents (
    snapshot_id INTEGER NOT NULL REFERENCES knowledge_snapshots, document_id INTEGER NOT NULL REFERENCES documents,
    PRIMARY KEY(snapshot_id,document_id)
);
CREATE TABLE IF NOT EXISTS node_fragments (
    node_id INTEGER NOT NULL REFERENCES nodes,fragment_id INTEGER NOT NULL REFERENCES fragments,
    PRIMARY KEY(node_id,fragment_id)
);
CREATE TABLE IF NOT EXISTS run_inputs (
    run_id INTEGER PRIMARY KEY REFERENCES runs,input_hash TEXT NOT NULL,payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revision_runs (
    revision_id INTEGER PRIMARY KEY REFERENCES revisions, run_id INTEGER NOT NULL UNIQUE REFERENCES run_inputs
);
CREATE INDEX IF NOT EXISTS receipts_by_run ON model_receipts(run_id) WHERE run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS revisions_by_prediction ON revisions(prediction_id,id);
CREATE TABLE IF NOT EXISTS forecast_scales (
    prediction_id INTEGER PRIMARY KEY REFERENCES predictions, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS forecast_outlooks (
    revision_id INTEGER PRIMARY KEY REFERENCES revisions, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fixed_forecast_requests (
    fixed_id INTEGER PRIMARY KEY REFERENCES fixed_questions, kind TEXT NOT NULL,
    window_start TEXT, scale TEXT
);
CREATE TABLE IF NOT EXISTS discussion_turns (
    id INTEGER PRIMARY KEY, event_id INTEGER REFERENCES events, revision_id INTEGER REFERENCES revisions,
    basis TEXT NOT NULL CHECK(basis IN ('original','latest')), nonce TEXT NOT NULL, provider TEXT NOT NULL,
    question TEXT NOT NULL, context TEXT NOT NULL, snapshot_id INTEGER REFERENCES knowledge_snapshots,
    created_at TEXT NOT NULL, job_id INTEGER UNIQUE REFERENCES jobs, run_id INTEGER UNIQUE REFERENCES runs,
    answer TEXT, citations TEXT, answered_at TEXT, UNIQUE(event_id,nonce)
);
CREATE INDEX IF NOT EXISTS discussion_by_scope ON discussion_turns(event_id,revision_id,basis,id);
CREATE UNIQUE INDEX IF NOT EXISTS workspace_discussion_nonce ON discussion_turns(nonce) WHERE event_id IS NULL;
CREATE TABLE IF NOT EXISTS workflow_actions (
    turn_id INTEGER NOT NULL REFERENCES discussion_turns, position INTEGER NOT NULL,
    action TEXT NOT NULL, event_id INTEGER NOT NULL REFERENCES events, summary TEXT NOT NULL,
    before_state TEXT NOT NULL, after_state TEXT NOT NULL, round_id INTEGER REFERENCES tracking_rounds,
    job_id INTEGER REFERENCES jobs, created_at TEXT NOT NULL, PRIMARY KEY(turn_id,position)
);
CREATE TABLE IF NOT EXISTS event_focus (
    id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL REFERENCES events, kind TEXT NOT NULL CHECK(kind IN ('person','topic')),
    target TEXT NOT NULL, target_key TEXT NOT NULL, angle TEXT NOT NULL, angle_key TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1, first_round_id INTEGER REFERENCES tracking_rounds,
    person_target_id INTEGER REFERENCES person_targets, created_at TEXT NOT NULL,
    UNIQUE(event_id,kind,target_key,angle_key)
);
CREATE INDEX IF NOT EXISTS focus_by_event ON event_focus(event_id,active,id);
CREATE TABLE IF NOT EXISTS discussion_focus (
    turn_id INTEGER NOT NULL REFERENCES discussion_turns, focus_id INTEGER NOT NULL REFERENCES event_focus,
    round_id INTEGER NOT NULL REFERENCES tracking_rounds, added INTEGER NOT NULL,
    PRIMARY KEY(turn_id,focus_id)
);
CREATE TABLE IF NOT EXISTS focus_checks (
    focus_id INTEGER NOT NULL REFERENCES event_focus, round_id INTEGER NOT NULL REFERENCES tracking_rounds,
    checked_at TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL, documents TEXT NOT NULL,
    PRIMARY KEY(focus_id,round_id)
);
CREATE TABLE IF NOT EXISTS person_targets (
    id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL REFERENCES events, entity_id INTEGER NOT NULL REFERENCES entities,
    goal_version INTEGER NOT NULL, role TEXT NOT NULL, relevance TEXT NOT NULL, fragment_id INTEGER NOT NULL REFERENCES fragments,
    quote TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(event_id,entity_id)
);
CREATE TABLE IF NOT EXISTS person_accounts (
    id INTEGER PRIMARY KEY, demo INTEGER NOT NULL, entity_id INTEGER NOT NULL REFERENCES entities, platform TEXT NOT NULL,
    account_key TEXT NOT NULL, url TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('candidate','confirmed','rejected')),
    proof_doc INTEGER NOT NULL REFERENCES documents, proof_text TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(demo,entity_id,platform,account_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS person_verified_account ON person_accounts(demo,platform,account_key) WHERE state='confirmed';
CREATE TABLE IF NOT EXISTS person_checks (
    round_id INTEGER NOT NULL REFERENCES tracking_rounds,target_id INTEGER NOT NULL REFERENCES person_targets,
    channel TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL, checked_at TEXT NOT NULL,
    document_ids TEXT NOT NULL, PRIMARY KEY(round_id,target_id,channel)
);
CREATE INDEX IF NOT EXISTS person_last_checks ON person_checks(target_id,round_id DESC);
CREATE TABLE IF NOT EXISTS person_materials (
    target_id INTEGER NOT NULL REFERENCES person_targets, document_id INTEGER NOT NULL REFERENCES documents,
    channel TEXT NOT NULL, source_url TEXT NOT NULL, fragment_id INTEGER NOT NULL REFERENCES fragments,
    attribution TEXT NOT NULL, author_key TEXT NOT NULL, source_at TEXT,
    period TEXT NOT NULL, round_id INTEGER NOT NULL REFERENCES tracking_rounds, PRIMARY KEY(target_id,source_url,document_id)
);
CREATE INDEX IF NOT EXISTS person_material_by_round ON person_materials(round_id,target_id);
CREATE TABLE IF NOT EXISTS person_post_links (
    target_id INTEGER NOT NULL REFERENCES person_targets,platform TEXT NOT NULL,url TEXT NOT NULL,
    proof_doc INTEGER NOT NULL REFERENCES documents,PRIMARY KEY(target_id,platform,url)
);
CREATE TABLE IF NOT EXISTS person_account_reviews (
    id INTEGER PRIMARY KEY,account_id INTEGER NOT NULL REFERENCES person_accounts,previous TEXT NOT NULL,
    state TEXT NOT NULL,reviewed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS people_seed_checks (
    round_id INTEGER PRIMARY KEY REFERENCES tracking_rounds, status TEXT NOT NULL, document_ids TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lesson_reviews (
    id INTEGER PRIMARY KEY, lesson_id INTEGER NOT NULL REFERENCES observations,
    event_id INTEGER NOT NULL REFERENCES events, fragment_id INTEGER NOT NULL REFERENCES fragments,
    outcome TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(lesson_id,event_id,fragment_id,outcome,reason)
);
CREATE TABLE IF NOT EXISTS revision_lessons (
    revision_id INTEGER NOT NULL REFERENCES revisions, lesson_id INTEGER NOT NULL REFERENCES observations,
    PRIMARY KEY(revision_id,lesson_id)
);
CREATE TABLE IF NOT EXISTS round_sources (
    round_id INTEGER NOT NULL REFERENCES tracking_rounds,url TEXT NOT NULL REFERENCES platforms,
    window_start TEXT NOT NULL,window_end TEXT NOT NULL,state TEXT NOT NULL,checked_at TEXT NOT NULL,
    recovery_round INTEGER REFERENCES tracking_rounds,PRIMARY KEY(round_id,url)
);
CREATE INDEX IF NOT EXISTS missing_source_windows ON round_sources(url,round_id)
    WHERE state='missing' AND recovery_round IS NULL;
CREATE TABLE IF NOT EXISTS round_monitors (
    round_id INTEGER PRIMARY KEY REFERENCES tracking_rounds,added INTEGER NOT NULL,applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_guides (
    id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL REFERENCES events, goal_version INTEGER NOT NULL,
    question TEXT NOT NULL, reason TEXT NOT NULL, wake TEXT NOT NULL, query TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
    UNIQUE(event_id,goal_version,question,query)
);
CREATE TABLE IF NOT EXISTS guide_candidates (
    guide_id INTEGER NOT NULL REFERENCES event_guides, version_id INTEGER NOT NULL REFERENCES claims,
    found_at TEXT NOT NULL, PRIMARY KEY(guide_id,version_id)
);
CREATE TABLE IF NOT EXISTS guide_lines (
    event_id INTEGER NOT NULL REFERENCES events,goal_version INTEGER NOT NULL,line TEXT NOT NULL,
    guide_id INTEGER NOT NULL REFERENCES event_guides,PRIMARY KEY(event_id,goal_version,line)
);
CREATE TABLE IF NOT EXISTS event_notes (
    id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL REFERENCES events, goal_version INTEGER NOT NULL,
    round_id INTEGER REFERENCES tracking_rounds, task_id INTEGER REFERENCES research_tasks,
    question TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL, kind TEXT NOT NULL,
    reason TEXT NOT NULL, citations TEXT NOT NULL, recorded_at TEXT NOT NULL, note_key TEXT NOT NULL,
    UNIQUE(event_id,note_key)
);
CREATE INDEX IF NOT EXISTS event_notes_by_goal ON event_notes(event_id,goal_version,id DESC);
CREATE TABLE IF NOT EXISTS handoff_inbox (
    note_id INTEGER PRIMARY KEY REFERENCES event_notes, documents TEXT NOT NULL, received_run INTEGER REFERENCES runs
);
CREATE TABLE IF NOT EXISTS world_dispositions (
    entry_id INTEGER PRIMARY KEY REFERENCES world_entries, state TEXT NOT NULL CHECK(state IN ('event','withdrawn','merged')),
    target INTEGER REFERENCES world_entries, reason TEXT NOT NULL, recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS world_disputes (
    id INTEGER PRIMARY KEY, version_id INTEGER NOT NULL REFERENCES claims, event_id INTEGER NOT NULL REFERENCES events,
    field TEXT NOT NULL, proposed TEXT NOT NULL, citations TEXT NOT NULL, reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open', UNIQUE(version_id,event_id,field,proposed,citations)
);
CREATE TABLE IF NOT EXISTS agent_sessions (
    lane TEXT PRIMARY KEY, thread_id TEXT, last_run INTEGER REFERENCES runs, checkpoint TEXT NOT NULL DEFAULT '{}',
    busy_run INTEGER REFERENCES runs, backend TEXT CHECK(backend IN ('codex','dsh')), dsh_session_id TEXT
);
CREATE TABLE IF NOT EXISTS research_tool_steps (
    task_id INTEGER NOT NULL REFERENCES research_tasks, step INTEGER NOT NULL, request TEXT NOT NULL, result TEXT NOT NULL,
    PRIMARY KEY(task_id,step)
);
CREATE TABLE IF NOT EXISTS role_retries (
    task_id INTEGER PRIMARY KEY REFERENCES research_tasks, attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS role_retry_sources (
    task_id INTEGER NOT NULL REFERENCES research_tasks, job_id INTEGER NOT NULL REFERENCES jobs,
    PRIMARY KEY(task_id,job_id)
);
CREATE TABLE IF NOT EXISTS forecast_reviews (
    job_id INTEGER PRIMARY KEY REFERENCES jobs, author_run INTEGER NOT NULL REFERENCES runs,
    reviewer_run INTEGER NOT NULL REFERENCES runs, final_run INTEGER NOT NULL REFERENCES runs, decision TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS investigation_lines (
    id INTEGER PRIMARY KEY,event_id INTEGER NOT NULL REFERENCES events,line_key TEXT NOT NULL,
    goal_version INTEGER NOT NULL,question TEXT NOT NULL,role TEXT NOT NULL,last_task INTEGER REFERENCES research_tasks,
    UNIQUE(event_id,line_key)
);
CREATE TABLE IF NOT EXISTS field_evidence (
    version_id INTEGER NOT NULL REFERENCES claims,field TEXT NOT NULL,fragment_id INTEGER NOT NULL REFERENCES fragments,
    quote TEXT NOT NULL,PRIMARY KEY(version_id,field,fragment_id)
);
CREATE TABLE IF NOT EXISTS dispute_resolutions (
    dispute_id INTEGER PRIMARY KEY REFERENCES world_disputes,version_id INTEGER NOT NULL REFERENCES claims,
    fragment_id INTEGER NOT NULL REFERENCES fragments,reason TEXT NOT NULL,recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot_checklist (
    snapshot_id INTEGER NOT NULL REFERENCES knowledge_snapshots, position INTEGER NOT NULL,
    question TEXT NOT NULL, rejection TEXT NOT NULL DEFAULT '', PRIMARY KEY(snapshot_id,position)
);
CREATE TABLE IF NOT EXISTS delivery_answers (
    snapshot_id INTEGER NOT NULL, position INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('confirmed','unknown')),
    answer TEXT NOT NULL,citations TEXT NOT NULL,
    PRIMARY KEY(snapshot_id,position), FOREIGN KEY(snapshot_id,position) REFERENCES snapshot_checklist
);
CREATE TABLE IF NOT EXISTS card_provenance (
    version_id INTEGER PRIMARY KEY REFERENCES claims, fields TEXT NOT NULL, evidence TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS archive_publishers (
    asset_id INTEGER PRIMARY KEY REFERENCES evidence_assets,name TEXT NOT NULL,locator TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_sessions (
    run_id INTEGER PRIMARY KEY REFERENCES runs,lane TEXT,previous_run INTEGER,requested_thread TEXT,observed_thread TEXT
);
CREATE TABLE IF NOT EXISTS task_rejections (
    task_id INTEGER NOT NULL REFERENCES research_tasks,subject TEXT NOT NULL,reason TEXT NOT NULL,
    PRIMARY KEY(task_id,subject,reason)
);
CREATE TABLE IF NOT EXISTS snapshot_rejections (
    snapshot_id INTEGER NOT NULL REFERENCES knowledge_snapshots,task_id INTEGER NOT NULL REFERENCES research_tasks,
    subject TEXT NOT NULL,reason TEXT NOT NULL, PRIMARY KEY(snapshot_id,task_id,subject,reason)
);
