# CRAP Harden Result

Status: done

Summary:
Added targeted additive tests in `tests/distribution/test_sync_pipeline.py` to cover previously under-tested branches in `sync_distributor_sources` and `_carry_forward` without changing production code. The new tests exercise skip/continue paths, invalid lock parsing, unchanged/error carry-forward flows, existing-manifest version propagation, merge/write behavior, and lockfile preservation semantics for repo/path/distributor combinations (including legacy distributor entries with missing `distributor_id`).

Tests Added:
- `TestSyncDistributorSourcesCoverageEdges::test_skips_non_skill_repo_sets_wrong_modes_and_bad_config_paths`
- `TestSyncDistributorSourcesCoverageEdges::test_skips_when_distribution_sources_are_not_declared`
- `TestSyncDistributorSourcesCoverageEdges::test_invalid_existing_lock_and_sync_error_do_not_write_lock`
- `TestSyncDistributorSourcesCoverageEdges::test_manifest_unchanged_without_existing_lock_skips_merge_write`
- `TestSyncDistributorSourcesCoverageEdges::test_manifest_unchanged_carries_forward_matching_dist_and_preserves_non_dist_entries`

Coverage Delta (before/after per function):
- `sync_distributor_sources`
  - Before: 74.24% (49/66 covered, 17 missing)
  - After: 100.00% (66/66 covered, 0 missing)
- `_carry_forward`
  - Before: 11.11% (1/9 covered, 8 missing)
  - After: 100.00% (9/9 covered, 0 missing)

Validation:
- `cd <repo> && python3 -m pytest tests/distribution/test_sync_pipeline.py -v`
  - PASS (25 passed)
- `cd <repo> && python3 -m pytest tests/distribution/ tests/test_config_schema.py tests/test_lockfile_schema.py tests/test_skill_repos.py tests/test_doctor_distribution.py tests/test_auth_surface.py`
  - PASS (176 passed)
- `cd <repo> && python3 -m pytest tests/distribution/ tests/test_config_schema.py tests/test_lockfile_schema.py tests/test_doctor_distribution.py tests/test_auth_surface.py --cov=.env-manager/runtime_manager/distribution --cov-report=xml:coverage.xml 2>&1 | tail -15`
  - PASS (150 passed, coverage XML written)
- Focused missing-line check used for branch targeting:
  - `cd <repo> && PYTHONPATH=.env-manager python3 -m pytest tests/distribution/test_sync_pipeline.py --cov=runtime_manager.distribution.sync --cov-report=term-missing`
  - Result: `distribution/sync.py` at 98% overall (remaining uncovered lines are outside the targeted functions: `_http_get` URLError path + bundle SHA mismatch branch)

Blockers:
- None.
