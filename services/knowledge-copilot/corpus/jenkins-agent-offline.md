---
title: Jenkins agent offline, builds queued
service: ci
doc_type: runbook
last_reviewed: 2026-05-30
---

## Symptom

Builds sit in the queue and never start. The queue tooltip says "there are no nodes
with the label X" or "all nodes of label X are offline". On the Nodes page one or
more agents show as disconnected, or show as connected but with zero executors
available.

## How to confirm

Distinguish the three states before doing anything, because they have different
causes:

- **Agent disconnected** — the agent process is gone or cannot reach the controller.
  The Nodes page shows it greyed out with a launch log.
- **Agent online but marked temporarily offline** — someone, or the built-in disk
  space monitor, took it out of rotation. The reason is shown on the node page.
- **Agent online with all executors busy** — nothing is broken; you are out of
  capacity.

Read the agent launch log from the node page first. For a disconnect, the last lines
usually state the cause plainly. On the agent host:

    systemctl status jenkins-agent
    journalctl -u jenkins-agent --since "1 hour ago"
    df -h /var/lib/jenkins

## Likely causes

- **Workspace disk full.** Jenkins takes a node offline when free space drops below a
  threshold. This is the single most common cause, and it presents as "offline"
  rather than "disk full", which sends people looking in the wrong place.
- **Agent JVM killed** — out of memory on the host, or an OOM kill if the agent runs
  in a container.
- **Network partition or expired credentials** between agent and controller. JNLP
  agents reconnect automatically; SSH-launched agents do not always.
- **Controller restarted** and did not re-launch SSH agents.
- **Label mismatch** after a job or node was edited — no agent actually carries the
  label the job requests. Nothing is offline at all in this case.

## Resolution

1. Check free space on the agent before anything else, and clean workspaces:
   configure the Workspace Cleanup plugin, or delete stale `workspace/*` directories
   for jobs that no longer exist.
2. Bring the node back online from the node page once space is reclaimed. Jenkins
   will not do it automatically until the threshold clears.
3. For repeated disconnects, prefer ephemeral agents — Kubernetes or Docker agents
   provisioned per build — over long-lived hosts. A fresh workspace every build makes
   disk exhaustion structurally impossible.
4. For label mismatches, compare the job's label expression against the node's labels
   character by character. A trailing space is a real cause of this.

## Escalation

If agents disconnect across multiple hosts at the same time, the controller is the
suspect, not the agents. Check controller heap usage and GC pauses — a controller in
a long GC pause drops agent connections, and they all reconnect in a thundering herd.
