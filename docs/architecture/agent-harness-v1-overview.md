# Agent Harness v1

mca's Agent Harness v1 centers on a `TaskState` for each user request. The task state records attempts, tool steps, the latest tool, stop reason, final answer, checkpoint id, and resume status.

The harness persists task state separately from session history so a run can be audited without overloading future prompts. Trace events capture the timeline, while reports summarize usage and outcomes.
