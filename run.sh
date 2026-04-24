#!/bin/bash
# Run the Trip Planner with the venv isolated from system google packages.
cd "$(dirname "$0")"
PYTHONPATH=".venv/lib/python3.10/site-packages" .venv/bin/streamlit run app.py "$@"
