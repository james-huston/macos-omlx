# Coding eval — idempotent ledger

Mid-level coding challenge for story 002. Models implement `ledger.py` from
[`PROMPT.md`](PROMPT.md); we score them with **our** hidden pytest suite
([`tests/test_ledger.py`](tests/test_ledger.py)), not tests they invent.

```bash
# All four shortlist models (unloads/restores gpt-oss around brain-sized ones)
python3 tests/coding_eval/run_eval.py

# Subset
python3 tests/coding_eval/run_eval.py qwen3-coder-next glm47-flash
```

Artifacts land in `work/<model>/` (gitignored): `ledger.py`, `raw_content.md`, `pytest.out`.

GLM models are prompted with thinking disabled (`enable_thinking: false` + `/nothink`)
so reasoning does not exhaust `max_tokens` without emitting code.
