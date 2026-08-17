# Agent ops brain latency proof

- generated_at_utc: `2026-08-16T20:26:28+00:00`
- python: `3.12.13`
- nodes: `500`
- edges: `498`
- status: `PASS`

```
surface                    p50_ms   p95_ms   budget status
----------------------------------------------------------
capabilities                0.303    0.396     50.0   PASS
graph_critical_path        31.220   83.990    150.0   PASS
next_no_adapters           11.211   35.390    150.0   PASS
explain_service            18.286   46.164    100.0   PASS
search_graph                6.667   56.409    100.0   PASS
adapter_collection_stub     5.522   76.737   1500.0   PASS
adapter_parallel_fixture  408.264  413.578   1000.0   PASS
model_build                24.298   34.288   5000.0   PASS
capabilities_cli_import  1444.995 1444.995   6000.0   PASS
```
