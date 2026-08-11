from __future__ import annotations
from ...plugins import Migration


def linear_migrations(storage_kind: str) -> tuple[Migration, ...]:
    if storage_kind not in {"sqlite", "postgresql"}: raise ValueError(f"unsupported storage kind: {storage_kind}")
    boolean = "BOOLEAN" if storage_kind == "postgresql" else "INTEGER"; timestamp = "TIMESTAMPTZ" if storage_kind == "postgresql" else "TEXT"
    return (Migration(1, (
        f"CREATE TABLE linear_users (id TEXT PRIMARY KEY,email TEXT NOT NULL UNIQUE,name TEXT NOT NULL,display_name TEXT NOT NULL,active {boolean} NOT NULL,admin {boolean} NOT NULL,created_at {timestamp} NOT NULL)",
        "CREATE TABLE linear_teams (id TEXT PRIMARY KEY,key TEXT NOT NULL UNIQUE,name TEXT NOT NULL,description TEXT,next_issue_number INTEGER NOT NULL)",
        "CREATE TABLE linear_team_members (team_id TEXT NOT NULL REFERENCES linear_teams(id),user_id TEXT NOT NULL REFERENCES linear_users(id),role TEXT NOT NULL,PRIMARY KEY(team_id,user_id))",
        "CREATE TABLE linear_workflow_states (id TEXT PRIMARY KEY,team_id TEXT NOT NULL REFERENCES linear_teams(id),name TEXT NOT NULL,type TEXT NOT NULL,position INTEGER NOT NULL,UNIQUE(team_id,name))",
        "CREATE TABLE linear_labels (id TEXT PRIMARY KEY,team_id TEXT NOT NULL REFERENCES linear_teams(id),name TEXT NOT NULL,color TEXT NOT NULL,UNIQUE(team_id,name))",
        f"CREATE TABLE linear_projects (id TEXT PRIMARY KEY,team_id TEXT NOT NULL REFERENCES linear_teams(id),name TEXT NOT NULL,description TEXT,state TEXT NOT NULL,lead_id TEXT REFERENCES linear_users(id),created_at {timestamp} NOT NULL,updated_at {timestamp} NOT NULL)",
        f"CREATE TABLE linear_cycles (id TEXT PRIMARY KEY,team_id TEXT NOT NULL REFERENCES linear_teams(id),number INTEGER NOT NULL,name TEXT NOT NULL,description TEXT,starts_at {timestamp},ends_at {timestamp},completed_at {timestamp},UNIQUE(team_id,number))",
        f"CREATE TABLE linear_issues (id TEXT PRIMARY KEY,team_id TEXT NOT NULL REFERENCES linear_teams(id),number INTEGER NOT NULL,identifier TEXT NOT NULL UNIQUE,title TEXT NOT NULL,description TEXT,priority INTEGER NOT NULL,state_id TEXT NOT NULL REFERENCES linear_workflow_states(id),assignee_id TEXT REFERENCES linear_users(id),creator_id TEXT NOT NULL REFERENCES linear_users(id),project_id TEXT REFERENCES linear_projects(id),cycle_id TEXT REFERENCES linear_cycles(id),archived {boolean} NOT NULL,created_at {timestamp} NOT NULL,updated_at {timestamp} NOT NULL,UNIQUE(team_id,number))",
        "CREATE TABLE linear_issue_labels (issue_id TEXT NOT NULL REFERENCES linear_issues(id),label_id TEXT NOT NULL REFERENCES linear_labels(id),PRIMARY KEY(issue_id,label_id))",
        f"CREATE TABLE linear_comments (id TEXT PRIMARY KEY,issue_id TEXT NOT NULL REFERENCES linear_issues(id),user_id TEXT NOT NULL REFERENCES linear_users(id),body TEXT NOT NULL,created_at {timestamp} NOT NULL,updated_at {timestamp} NOT NULL)",
        f"CREATE TABLE linear_issue_relations (id TEXT PRIMARY KEY,issue_id TEXT NOT NULL REFERENCES linear_issues(id),related_issue_id TEXT NOT NULL REFERENCES linear_issues(id),type TEXT NOT NULL,created_at {timestamp} NOT NULL,UNIQUE(issue_id,related_issue_id,type))",
        "CREATE INDEX linear_issues_team_state ON linear_issues(team_id,state_id,updated_at)",
    )),)
