"""eufinreg — keep, watch and evidence the European public licence registers.

Three layers, each usable on its own:

* :mod:`eufinreg.sources` reads the registers (EU-level plus DE/AT/CH).
* :mod:`eufinreg.store` and :mod:`eufinreg.pipeline` keep dated, checksummed
  snapshots and turn them into change events, because no register here
  publishes a changelog and every one of them overwrites its own file.
* :mod:`eufinreg.watchlist` and :mod:`eufinreg.evidence` answer the questions
  people actually have: did anything change for the entities I care about,
  and can I prove what the register said on a given day.
"""

from __future__ import annotations

__version__ = "0.5.0"

__all__ = ["__version__"]
