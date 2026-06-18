"""Compatibility wrapper for older imports.

New code should use analysis.part_analyser.analyse_part.
"""

from analysis.part_analyser import analyse_part

__all__ = ["analyse_part"]

