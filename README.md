# Memory Squeeze Monitor

Public dashboard for SNDK, SKHY, MU and NBIS:

- 30-day interpolated 25-delta skew
- Largest upside call open-interest walls
- IBorrowDesk borrow fee and available shares

GitHub Actions refreshes the data and redeploys GitHub Pages every 30 minutes.

## Run locally

```bash
python -m pip install -r requirements.txt
python scripts/update_data.py
python -m http.server 8000
```

Data is indicative and delayed. It does not constitute investment advice.
