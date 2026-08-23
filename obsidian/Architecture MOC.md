---
tags: [moc, fpl-decision-engine]
---

# Architecture — Map of Content

Entry point for the autonomous multi-agent FPL decision engine's design reasoning. See [[../docs/architecture.md]] for the full system diagram; these notes capture the *why*, not just the *what*.

## Components
- [[ML Model]]
- [[Transfer Debate Engine]]
- [[Captain and Lineup]]
- [[Reliability and Escalation]]
- [[Feedback Loop]]
- [[Hosting and Scheduling]]
- [[Observability]]
- [[Known Gaps]]

## Timeline
- [[Decisions Log]] — chronological record of major design decisions and why they were made

## Origin
Built in response to a request to automate: ML-weighted player selection, multi-agent debate over transfers/captain/lineup with extra scrutiny on point hits, autonomous (semi-autonomous, human-approved) execution against the real FPL team, and full logging of every discussion and decision.
