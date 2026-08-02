---
title: Postgres connection pool exhausted
service: data
doc_type: runbook
last_reviewed: 2026-07-08
---

## Symptom

Requests begin timing out under load while the database itself looks healthy — CPU
is low, disk is idle, and queries that do run return quickly. Application logs show
`FATAL: sorry, too many clients already`, or a pool timeout such as
`TimeoutError: QueuePool limit of size 20 overflow 10 reached`.

The distinction between those two messages matters. The first means Postgres refused
a new connection; the second means the application's own pool refused to hand one
out and never reached the database at all.

## How to confirm

Look at where the connections are and what they are doing:

    SELECT state, count(*) FROM pg_stat_activity GROUP BY state;
    SELECT count(*), usename, application_name FROM pg_stat_activity
      GROUP BY usename, application_name ORDER BY 1 DESC;
    SHOW max_connections;

A large `idle in transaction` count is the most informative result you can get.
Those connections are held open by a client that opened a transaction and never
committed or rolled back — they consume a slot and can hold locks indefinitely.
That is a leak in the application, not a capacity problem.

Find the oldest offenders:

    SELECT pid, state, now() - state_change AS age, left(query, 80)
      FROM pg_stat_activity
      WHERE state = 'idle in transaction'
      ORDER BY age DESC LIMIT 10;

## Likely causes

- **Pool size times replica count exceeds `max_connections`.** Twenty replicas with a
  pool of 20 each want 400 connections against a default limit of 100. This appears
  the moment an HPA scales up, which is why it often coincides with a traffic spike
  and gets misdiagnosed as load.
- **Connections leaked by the application** — an exception path that never returns the
  connection, or a transaction left open.
- **Long-running analytical queries** holding slots.
- **Migrations or a batch job** opening their own connections during peak.
- **No pooler in front of the database**, so every application process holds a real
  backend connection.

## Resolution

Raising `max_connections` is the tempting move and usually the wrong one — each
backend costs memory, and a few hundred is where Postgres starts spending more time
on context switching than on work.

1. Do the arithmetic first: `replicas × pool_size + headroom` must fit inside
   `max_connections`. If it does not, shrink the pool, not the database.
2. Put pgbouncer in transaction pooling mode between the application and Postgres.
   This is the structural fix — it decouples application concurrency from backend
   connection count.
3. Fix `idle in transaction` leaks at the source. As a guardrail, set
   `idle_in_transaction_session_timeout` so a leaked transaction is reaped rather
   than held forever.
4. Give batch jobs and migrations a separate role with its own connection allowance,
   so they cannot starve serving traffic.

## Escalation

If connection count tracks replica count exactly, this is a capacity planning problem
and the HPA maximum needs reconciling with the database limit. Involve whoever owns
the autoscaling policy, not just the database.
