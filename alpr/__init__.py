"""Video ALPR for Malaysian plates: vehicle tracking + plate OCR with multi-frame voting.

Pure-logic modules (plates, voting, quality, lead, evaluate) have no model dependency and are
unit-tested in alpr/tests. Model-dependent code lives in ocr.py and pipeline.py.
"""
