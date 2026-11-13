# Upgrading oMLX

1. Note the current version and a recent benchmark baseline:
   ```bash
   omlx --version || brew info omlx
   ls benchmarks/results/ | tail
   ```
2. Upgrade via Homebrew:
   ```bash
   brew update && brew upgrade omlx
   omlx restart   # or: brew services restart omlx
   ```
3. Re-apply model settings (pins/TTLs/aliases) and smoke-test:
   ```bash
   make models-apply NO_DOWNLOAD=1
   make test-models
   ./tests/tool_use/run.sh
   ```
4. Re-benchmark and compare against the previous jsonl:
   ```bash
   make bench
   make bench-report
   # diff metrics vs benchmarks/results/<previous-date>-*.jsonl
   ```
5. If you previously skipped custom kernels and have since installed Xcode:
   ```bash
   brew reinstall jundot/omlx/omlx --with-custom-kernel
   omlx restart
   ```

Record notable regressions or wins under [`investigations/`](investigations/) or
in the active BMAD story's Completion Notes.
