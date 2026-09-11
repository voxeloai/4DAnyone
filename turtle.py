"""Shim: GVHMR's hmr4d/utils/body_model/body_model.py has a stray `from turtle import forward`,
which imports tkinter (absent from this venv's Python). This module shadows the stdlib turtle only
when the repo root is first on PYTHONPATH, which is how 4DAnyone launches its GVHMR workers
(see fdanyone/pipeline.py::_worker_environment). Nothing else here imports turtle."""

forward = None
