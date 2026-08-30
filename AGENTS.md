This is the prompt I'd give OpenCode as a single update request. It tells it to only modify the Streamlit frontend, preserve the backend, and turn the dashboard into an interactive testing environment rather than a read-only artifact viewer.

# OpenCode Update Prompt — Streamlit Interactive Playground

You are updating the Meridian Freight project.

The backend (LangGraph, RAG, entity resolution, rule engine, audit system, Qdrant, LangSmith, and artifacts generation) is already working.

Do not rewrite the backend unless absolutely necessary. This task is primarily a Streamlit UX overhaul that makes the system easy to test live during judging.

The goal is to transform the dashboard from an artifact viewer into an interactive operations console where a judge can upload files, inject failures, inspect reasoning, replay executions, and understand every page without prior knowledge.

Whenever a change affects architecture, performance, or UX, stop and ask me for a tradeoff analysis before implementing it.

# Primary Goal

Every page should satisfy three things:

1. Explain what this page does (plain English).

2. Allow interaction, not just inspection.

3. Expose underlying JSON and artifacts for technical judges.

Think of this like a mix of LangSmith + Kibana + Streamlit Playground.

# Global UX Improvements

Every page must start with:

* Title

* One-line description

* "Why this matters" section

* Small help tooltip wherever terminology is technical.

Example:

> Rule Explorer

> See every dispatch rule the AI extracted from the retiring dispatcher's knowledge. Test how different vehicles, clients, and routes affect decisions before running the full pipeline.

Every JSON viewer should include:

* Copy button

* Download JSON

* Collapse/Expand All

* Search inside JSON

Every table should support:

* Search

* Sorting

* Filtering

* CSV export

# New Landing Page

Current executive overview stays.

Add:

### Pipeline Status

Show:

* Last run

* Current model

* Embedding model

* Vector database status

* LangSmith tracing status

* Collection size

* Average retrieval latency

### Quick Actions

Large buttons:

* Upload Ticket

* Upload Ticket Batch

* Run Sample Ticket

* Chaos Test

* Open Retrieval Playground

# 1. Ticket Sandbox (Highest Priority)

This is the biggest missing feature.

Currently users can only inspect existing tickets.

Instead build a live testing page.

## Input methods

### A. Upload JSON

Upload a ticket.

### B. Upload CSV

Batch process.

### C. Manual Form

Fields:

* ticket_id

* client

* vehicle

* issue

* origin

* destination

* driver

* timestamp

### D. Paste Raw JSON

Editable code editor.

## Run Pipeline

When clicked:

* create new LangGraph run

* stream logs live

* automatically open results

Show:

* execution time

* tokens

* retrieval time

* selected vehicle

* applied rules

* work order

* drafted communication

* final communication

# 2. Upload Your Own Knowledge Base

This makes the pipeline genuinely reusable.

Create a new page:

# Knowledge Upload

Allow uploading:

* TXT

* PDF

* DOCX

* XLSX

* CSV

* JSON

Users should be able to upload:

* maintenance logs

* email threads

* interview transcripts

* vehicle roster

* driver roster

After upload:

Show:

## Preview

* detected document type

* detected language

* extracted text

## Chunk Preview

Show every chunk before indexing.

Example

Chunk 17

Source: dispatcher_interview.txt

Tokens: 247

Rule Candidate: Yes

Confidence: 0.91

Text:

...

Buttons:

* Edit chunk

* Delete chunk

* Merge

* Split

## Index

After confirmation:

* generate embeddings

* insert into Qdrant

* show progress

* show chunk count

* show estimated embedding cost

This makes the project reusable beyond Meridian Freight.

# 3. Live Pipeline Visualization

Current page shows JSON.

Replace with animated execution.

Nodes:

* Validate

* Deduplicate

* Enrich

* Retrieve

* Rule Engine

* Select Vehicle

* Draft Communication

* HITL

* Send

* Complete

Nodes should animate while running.

Clicking a node opens:

* Input State

* Output State

* Prompt

* Retrieved Context

* Rule IDs

* Execution time

This should feel similar to LangSmith.

# 4. Ticket Explorer

Keep existing functionality.

Add:

Timeline scrubber.

As the slider moves:

* state updates

* work order appears

* communication draft appears

* final message appears

Also add:

Compare two tickets.

# 5. Knowledge Explorer

Current version only displays chunks.

Upgrade it.

Add filters:

* document type

* source

* language

* rule candidate

* entity

* client

* vehicle

Each chunk should display:

* embedding score

* token count

* neighboring chunks

* provenance

* edit history

Buttons:

* Open source

* Download chunk

* View adjacent chunks

# 6. Rule Explorer

Current rules are static.

Add:

# Rule Simulator

Inputs:

* client

* route

* season

* vehicle year

* BS stage

* engine heater

* hub

Output:

Eligible?

Applied rules.

Rejected rules.

Hard constraints.

Heuristics.

Show reasoning as a decision tree.

# 7. Entity Explorer

Current version is read-only.

Turn it into an interactive resolver.

## Vehicle Resolver

Input:

CH81AQ4130

Output:

Canonical vehicle.

Aliases.

Provenance.

Conflicts.

## Client Resolver

Input:

shakti cement hold

Output:

Shakti Cement

Matched alias.

Confidence.

## Conflict Inspector

Show both values.

Highlight winner.

Show why.

# 8. Retrieval Playground

Current retrieval page is too limited.

Upgrade it into a full RAG debugger.

Input:

Any question.

Show:

Query embedding.

Retrieved chunks.

Similarity score.

Rerank score.

Why selected.

Why rejected.

Neighbor chunks.

Source document.

Download retrieved context.

Optional:

Toggle:

* Top K

* Similarity threshold

* Reranker

This demonstrates RAG quality live.

# 9. Chaos Mode

Add a completely new page.

Purpose:

Break the pipeline intentionally.

Toggles:

* Duplicate ticket

* Missing vehicle

* Invalid date

* Missing hub

* Broken JSON

* Wrong schema

* Conflicting maintenance records

* Duplicate communication event

Run Chaos Test.

Output:

Exactly-once maintained?

Quarantined?

Safe degradation?

Recovered?

PII leaked?

This directly demonstrates the biggest scoring criterion.

# 10. Human Approval Console

Current HITL exists.

Make it interactive.

Inbox:

Pending messages.

Actions:

* Approve

* Reject

* Edit

* Compare revisions

Show:

Approval history.

Approver.

Timestamp.

# 11. Audit Explorer

Current logs are JSON.

Upgrade.

Timeline view.

Filter by:

* ticket

* node

* severity

* run

Clicking an event opens:

* state

* artifacts

* LangSmith trace

* rule citations

# 12. Analytics Dashboard

Add charts.

Use Plotly.

Show:

* Work orders by client

* Quarantine reasons

* Rule usage frequency

* Retrieval latency

* Duplicate rate

* Processing time distribution

* Embedding collection growth

# Prompt & State Inspector (Important)

For every LangGraph node add a side panel.

Tabs:

### Input

State entering node.

### Prompt

Exact prompt sent to Qwen.

### LLM Output

Raw model response.

### Parsed Output

Validated JSON.

### Timing

Latency.

Token usage.

This becomes the equivalent of LangSmith debugging.

# File Upload Testing Flow

A judge should be able to:

1. Upload a brand-new interview transcript.

2. Preview chunks.

3. Edit one chunk.

4. Index it.

5. Ask a question in Retrieval Playground.

6. See the newly uploaded knowledge retrieved.

No code changes should be required.

# UI Quality Requirements

* Keep dark theme.

* Preserve existing aesthetic.

* Improve spacing.

* Add icons.

* Add hover tooltips.

* Add loading indicators.

* Add success/error banners.

* Add progress bars.

* Make layouts responsive.

# Preserve Existing Features

Do not remove:

* Executive Overview

* Live Pipeline

* Ticket Explorer

* Knowledge Explorer

* Rule Explorer

* Entity Explorer

* Retrieval Inspector

* HITL Console

* Audit Explorer

Instead, evolve them.

# Tradeoff Protocol (Mandatory)

Before implementing any change involving:

* backend architecture

* LangGraph state

* artifact formats

* Qdrant schema

* chunking strategy

* caching

* retrieval logic

* performance optimization

* storage structure

stop and ask me.

Use this format:

> Tradeoff Decision Needed

> Proposed change:
>
> Benefits:
>
> Drawbacks:
>
> Alternative options:
>
> My recommendation:

Wait for my approval before proceeding.

# Success Criteria

By the end of this update, a judge should be able to:

* Upload their own ticket.

* Upload their own knowledge base.

* Watch the pipeline execute live.

* Inspect every prompt and state transition.

* Test retrieval with arbitrary questions.

* Simulate dispatch rules.

* Resolve messy entities.

* Inject failures through Chaos Mode.

* Approve or reject communications.

* Replay executions.

* Export every artifact.

* Understand every page without prior knowledge.

The Streamlit app should feel like a production AI Operations Console, not just a JSON viewer.
