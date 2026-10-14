# AgentX runtime patches

Recipes opt into a patch using `benchmark.env.AIPERF_RUNTIME_PATCH`. The AgentX bootstrap applies it to the isolated PyPI installation before starting the client. Source paths use the upstream `src/aiperf` layout (`git apply -p2`). Unselected recipes use the unmodified release.

| Patch | Source | Fix |
| --- | --- | --- |
| `52-spawn-worker-pinning.patch` | [agentx-harness #52](https://github.com/SemiAnalysisAI/agentx-harness/pull/52), commit `b5d7652b50aceb9e0c4ca77daf9ae8f472ac77fd` | Pin each SPAWN child to one client worker to prevent stale session copies accumulating across workers. Includes the runtime routing change; documentation-only changes and upstream tests are omitted. |
