# Flock Review Against `whatsapp.pdf`

## Overall assessment
Flock is aligned with the core idea of the project order: clients exchange messages peer-to-peer, while servers act as distributed identity managers rather than centralized relays. The implementation already includes ring-based partitioning, replicated user records, and client-side offline retry behavior, which are strong matches for the distributed-systems goals in the official order.

## What is covered well
- **P2P messaging**: clients resolve user addresses through the distributed identity layer and then send messages directly over UDP.
- **Distributed identity management**: server state is split by hash range and replicated to additional servers, avoiding a single central registry node.
- **Fault tolerance direction**: servers maintain predecessor/successor links, detect failures, and attempt ring repair.
- **Replication**: user registration data is replicated to backup nodes, and the replication factor is now configurable through `FLOCK_FAIL_TOLERANCE`.
- **Offline-first behavior at the client**: messages that cannot be delivered immediately are queued locally for retry instead of being stored on a central service.

## Gaps and risks to keep in mind
- **Testing coverage was missing** before this pass, so regressions in DB and client/server local logic were hard to detect automatically.
- **Operational observability was uneven**: the server had partial logging, but the client relied heavily on `print`, which made distributed debugging difficult.
- **Manual validation through the console was weak**: invalid inputs could break the flow, and the console did not expose enough state to validate distributed behavior comfortably.
- **Fault tolerance is present but still lightweight**: the current repair logic is practical for the course project, but it is not yet a production-grade membership or consensus system.

## Outcome of this hardening pass
- Added structured logging to client, server, and console with both terminal and file output.
- Added a `tests/` suite for the most important local guarantees.
- Improved the console UI so it can be used as a practical validation tool for distributed behavior.
- Fixed storage paths so local client/server state is stable regardless of the directory from which the process is launched.
