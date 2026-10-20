# Engineering decisions

## 1. Work items and append-only activity

The current item row stores the latest state, including a named next action, for fast queue rendering. A separate activity table records creation, assignment, status, priority, next-action changes, claims, and comments. This gives a useful history without repeatedly reconstructing current state from events. Activity is appended in the same transaction as the state change, so a visible update always has its explanation. History reads are scoped through the owning item and capped to the latest 100 events for this demo.

## 2. Optimistic concurrency with a version number

Every item has an integer `version`. A client must send the version it read; the API compares it inside a write transaction and increments it on success. A stale editor receives HTTP 409 and refreshes instead of silently overwriting a newer change. This is simple to explain and works well when updates are relatively infrequent. A richer client could merge fields or show a side-by-side conflict.

## 3. Atomic ownership and idempotent retries

SQLite `BEGIN IMMEDIATE` serializes write transactions, and claims check the current owner before changing it. Therefore two distinct users cannot both claim an unassigned item successfully. An optional idempotency key stores the successful response in the same transaction. A retry with the same key gets the original response instead of repeating the action. Keys are currently retained indefinitely; a production service should scope them to the actor and expire them after a documented window.

## 4. Authorization at the resource boundary

Memberships are stored per user and team, so one person can be a member in one team and a read-only viewer in another. Admins can view and change all teams; other users can only access teams they belong to. Managers and admins can reassign work, members can claim and update it, and viewers can read only. Team checks run in the API before returning details or accepting writes, and assignment validates membership in the work item's team. The browser selector is a demo identity switch only; deployed identity must come from a trusted authentication layer, never from a user-controlled header.

## 5. Server-side queue, SQLite, and deliberately small scope

The API filters, searches, sorts, and paginates in SQL, with indexes for team/status and activity lookup. This avoids shipping all records into browser memory. SQLite WAL and a short write transaction keep this one-process demo straightforward. For multiple application replicas or much heavier writes, move to PostgreSQL, use database-backed authentication, and consider an outbox plus worker for notifications. This assessment prioritizes correctness in the core queue and collaboration path over building secondary infrastructure.
