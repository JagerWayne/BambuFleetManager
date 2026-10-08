"""Catalogue of Bambu Lab filament materials and their system filament ids.

The ``id`` of every entry is the RFID tag's **material id** (a value like
``GFA00`` read straight off a genuine Bambu spool by the community project
https://github.com/queengooborg/Bambu-Lab-RFID-Library). That is exactly the
string Bambu firmware expects in ``tray_info_idx`` / ``setting_id`` when writing
an AMS tray (``ams_filament_setting``), so writing a known id makes the printer
show the proper material name instead of "?".

``type`` is the short Bambu ``tray_type`` (``PLA``, ``PETG``, ``PLA-CF``,
``PA-S`` ...) - *not* the friendly label. ``min``/``max`` are the RFID tag's
nozzle temperature range.

The list is intentionally data-only; :mod:`backend.commands` derives lookups
from it and ``GET /api/filaments`` serves it to the dashboard so the dropdown
and the payload builder can never drift apart.
"""

from typing import Dict, List

#: (key, label, category, id, tray_type, nozzle_min, nozzle_max)
_DEFS = [
    # -- PLA ---------------------------------------------------------------
    ("PLA Basic", "PLA Basic", "PLA", "GFA00", "PLA", 190, 230),
    ("PLA Matte", "PLA Matte", "PLA", "GFA01", "PLA", 190, 230),
    ("PLA Silk", "PLA Silk", "PLA", "GFA05", "PLA", 210, 230),
    ("PLA Silk Multi-Color", "PLA Silk Multi-Color", "PLA", "GFA05", "PLA", 210, 230),
    ("PLA Silk+", "PLA Silk+", "PLA", "GFA06", "PLA", 190, 240),
    ("PLA-CF", "PLA-CF", "PLA", "GFA50", "PLA-CF", 210, 240),
    ("PLA Tough", "PLA Tough", "PLA", "GFA09", "PLA", 190, 230),
    ("PLA Tough+", "PLA Tough+", "PLA", "GFA10", "PLA", 220, 250),
    ("PLA Aero", "PLA Aero", "PLA", "GFA11", "PLA", 210, 260),
    ("PLA Glow", "PLA Glow", "PLA", "GFA12", "PLA", 190, 230),
    ("PLA Galaxy", "PLA Galaxy", "PLA", "GFA15", "PLA", 190, 230),
    ("PLA Wood", "PLA Wood", "PLA", "GFA16", "PLA", 190, 230),
    ("PLA Translucent", "PLA Translucent", "PLA", "GFA17", "PLA", 200, 240),
    ("PLA Lite", "PLA Lite", "PLA", "GFA18", "PLA", 190, 240),
    ("PLA Pure", "PLA Pure", "PLA", "GFA19", "PLA", 190, 230),
    ("PLA Marble", "PLA Marble", "PLA", "GFA07", "PLA", 190, 230),
    ("PLA Metal", "PLA Metal", "PLA", "GFA02", "PLA", 190, 230),
    ("PLA Sparkle", "PLA Sparkle", "PLA", "GFA08", "PLA", 190, 230),
    ("PLA Basic Gradient", "PLA Basic Gradient", "PLA", "GFA00", "PLA", 190, 230),
    # -- PETG --------------------------------------------------------------
    ("PETG Basic", "PETG Basic", "PETG", "GFG00", "PETG", 230, 260),
    ("PETG HF", "PETG HF", "PETG", "GFG02", "PETG", 230, 260),
    ("PETG Matte", "PETG Matte", "PETG", "GFG03", "PETG", 230, 260),
    ("PETG Translucent", "PETG Translucent", "PETG", "GFG01", "PETG", 230, 260),
    ("PETG-CF", "PETG-CF", "PETG", "GFG50", "PETG-CF", 240, 270),
    # -- ABS ---------------------------------------------------------------
    ("ABS", "ABS", "ABS", "GFB00", "ABS", 240, 270),
    ("ABS-GF", "ABS-GF", "ABS", "GFB50", "ABS-GF", 240, 270),
    # -- ASA ---------------------------------------------------------------
    ("ASA", "ASA", "ASA", "GFB01", "ASA", 240, 270),
    ("ASA Aero", "ASA Aero", "ASA", "GFB02", "ASA-AERO", 240, 280),
    ("ASA-CF", "ASA-CF", "ASA", "GFB51", "ASA-CF", 250, 280),
    # -- PC ----------------------------------------------------------------
    ("PC", "PC", "PC", "GFC00", "PC", 260, 280),
    ("PC FR", "PC FR", "PC", "GFC01", "PC", 260, 280),
    # -- PA ----------------------------------------------------------------
    ("PA6-GF", "PA6-GF", "PA", "GFN08", "PA-GF", 260, 290),
    ("PAHT-CF", "PAHT-CF", "PA", "GFN04", "PA-CF", 260, 290),
    # -- TPU ---------------------------------------------------------------
    ("TPU for AMS", "TPU for AMS", "TPU", "GFU02", "TPU-AMS", 220, 240),
    # -- Support -----------------------------------------------------------
    ("Support for PLA", "Support for PLA", "Support", "GFS02", "PLA-S", 190, 240),
    ("Support for PLA/PETG", "Support for PLA/PETG", "Support", "GFS05", "PLA-S", 190, 220),
    ("Support for ABS", "Support for ABS", "Support", "GFS06", "ABS-S", 240, 270),
    ("Support for PA/PET", "Support for PA/PET", "Support", "GFS03", "PA-S", 280, 300),
    ("PVA", "PVA", "Support", "GFS04", "PVA", 220, 250),
]

_FIELDS = ("key", "label", "category", "id", "type", "min", "max")

#: Ordered material catalogue grouped by category (order is the UI order).
FILAMENTS: List[Dict[str, object]] = [dict(zip(_FIELDS, d)) for d in _DEFS]

#: Preferred Bambu category ordering for the UI.
FILAMENT_CATEGORIES: List[str] = ["PLA", "PETG", "ABS", "ASA", "PC", "PA", "TPU", "Support"]
