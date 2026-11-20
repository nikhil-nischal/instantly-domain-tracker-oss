# Contributing

Thanks for your interest in improving the Instantly Domain Burn Detector.

## Development setup

```bash
git clone <your-fork-url>
cd instantly-domain-tracker

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements-dev.txt

cp .env.example .env
# Set INSTANTLY_API_KEY in .env if you want to run the live ETL.

pytest tests/ -v
```

You can run the dashboard locally with:

```bash
cd dashboard
python3 app.py
```

## Guidelines

- Keep the pipeline idempotent — every write should use an `UPSERT`.
- Add or update tests under `tests/` for any behavior change.
- Do not commit `.env`, `data/`, `*.db`, or any real API keys or mailbox data.
  These paths are already covered by `.gitignore`.
- Match the existing code style (standard library + `requests`, `click`, `rich`,
  Flask). Keep changes focused.

## Reporting bugs

Open an issue with steps to reproduce, expected vs. actual behavior, and any
relevant log output. Redact API keys and mailbox addresses before posting.
