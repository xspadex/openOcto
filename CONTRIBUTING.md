# Contributing

## Development Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,qr,nearby,metrics]"
```

If you only need the core CLI and tests:

```bash
pip install -e ".[dev]"
```

## Common Commands

```bash
pytest -q
python -m build
python -m twine check dist/*
```

## Notes

- Optional extras keep the base install small. Use `qr`, `nearby`, and `metrics` when working on those features.
- Keep machine-local files such as `.mcp.json` and `.jit/` out of commits.
- Update both `README.md` and `README_CN.md` when changing user-facing setup or CLI behavior.
