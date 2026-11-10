"""Feedback ORM model (CT-64, OBS-02). Orchestrator skeleton; TASK-135 implements it.

`Feedback(id, message_id UNIQUE FK messages.id ON DELETE CASCADE, customer_id FK customers.id,
rating TEXT CHECK (rating IN ('up','down')), trace_id TEXT NOT NULL, created_at timestamptz
default now())`, table `feedback`, migration `0010_feedback` (down_revision `0009`).
"""
