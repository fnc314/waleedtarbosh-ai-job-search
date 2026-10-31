# Archive

Frozen artifacts from the CSV era, kept for provenance, not for use.

- `job_search_tracker.csv` — the last pre-migration snapshot (84 rows). It does
  not update. Firestore is the record; regenerate a CSV with
  `tracker.py export --csv` if one is ever needed.
- `job_search_tracker.schema.json` — the retired CSV contract, superseded by
  `tracker.schema.json` (v4).
- `export_tracker_json.py` — the retired CSV validator/exporter. Its job is now
  `tracker.py check` and `tracker.py export`.

Nothing in the repo should read these files as a source. If a workflow needs
tracker data, read it through `scripts/reader.py` or `scripts/tracker.py`.
