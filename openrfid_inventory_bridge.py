#!/usr/bin/env python3
"""
OpenRFID -> Moonraker inventory bridge for Snapmaker U1

This version ignores RFID tags that OpenRFID can detect but cannot decrypt/read.
That prevents generic or unsupported tags, such as unreadable Bambu Lab tags, from
showing up as new spools in the Filament Inventory.

It only posts to Moonraker after OpenRFID logs a successful tag read:
  Successfully read tag with UID XXXXXXXX on reader slot_N_reader

Moonraker database output:
  namespace: fluidd
  key:       u1_last_rfid_tag
"""

import json
import re
import time
import urllib.request
from pathlib import Path

LOG_PATH = Path("/oem/printer_data/logs/openrfid.log")
MOONRAKER_URL = "http://localhost/server/database/item"
DATABASE_NAMESPACE = "fluidd"
DATABASE_KEY = "u1_last_rfid_tag"

# Optional: clear the last tag at startup so a stale unknown/old tag banner does not remain.
CLEAR_LAST_TAG_ON_START = True

last_sent_key = None
current_slot = None
current_uid = None
current_vendor = ""
current_manufacturer = ""
current_type = ""
current_subtype = ""
current_color = ""
current_weight = None
current_diameter = None
current_drying_temp = None
current_drying_time = None
current_hotend_min = None
current_hotend_max = None
current_bed_temp = None
current_first_layer_temp = None
current_other_layer_temp = None
current_sku = ""
current_manufactured_date = ""

# Successful decode/read patterns
RE_PROCESSING_SLOT = re.compile(r"Processing reader slot_(\d+)_reader", re.IGNORECASE)
RE_SUCCESS_UID_WITH_SLOT = re.compile(r"Successfully read tag with UID ([0-9A-Fa-f]+) on reader slot_(\d+)_reader", re.IGNORECASE)

# UID-only / failed read patterns. These are intentionally NOT posted to inventory.
RE_DETECTED_TYPE_UID = re.compile(r"Detected tag type .* with UID ([0-9A-Fa-f]+)", re.IGNORECASE)
RE_FAILED_UID_WITH_SLOT = re.compile(r"Detected tag with UID ([0-9A-Fa-f]+) on reader slot_(\d+)_reader", re.IGNORECASE)
RE_FAILED_READ = re.compile(r"Failed to read data from tag|Failed to read MIFARE Classic card data|M1 AUTH ERROR|Mifare Classic read error", re.IGNORECASE)

# Metadata patterns from OpenRFID processors
RE_VENDOR = re.compile(r"Vendor:\s*(.+)$", re.IGNORECASE)
RE_MANUFACTURER = re.compile(r"Manufacturer:\s*(.+)$", re.IGNORECASE)
RE_MAIN_TYPE = re.compile(r"Main Type:\s*(.+)$", re.IGNORECASE)
RE_SUB_TYPE = re.compile(r"Sub Type:\s*(.+)$", re.IGNORECASE)
RE_ARGB_COLOR = re.compile(r"ARGB Color:\s*0x([0-9A-Fa-f]{8})", re.IGNORECASE)
RE_RGB1_COLOR = re.compile(r"RGB1:\s*0x([0-9A-Fa-f]{8})", re.IGNORECASE)
RE_WEIGHT = re.compile(r"Weight \(grams\):\s*(\d+)", re.IGNORECASE)
RE_DIAMETER = re.compile(r"Diameter \(mm\):\s*([0-9.]+)", re.IGNORECASE)
RE_DRYING_TEMP = re.compile(r"Drying Temp \(C\):\s*([0-9.]+)", re.IGNORECASE)
RE_DRYING_TIME = re.compile(r"Drying Time \(hours\):\s*([0-9.]+)", re.IGNORECASE)
RE_HOTEND_MAX = re.compile(r"Hotend Max Temp \(C\):\s*([0-9.]+)", re.IGNORECASE)
RE_HOTEND_MIN = re.compile(r"Hotend Min Temp \(C\):\s*([0-9.]+)", re.IGNORECASE)
RE_BED_TEMP = re.compile(r"Bed Temp \(C\):\s*([0-9.]+)", re.IGNORECASE)
RE_FIRST_LAYER_TEMP = re.compile(r"First Layer Temp \(C\):\s*([0-9.]+)", re.IGNORECASE)
RE_OTHER_LAYER_TEMP = re.compile(r"Other Layer Temp \(C\):\s*([0-9.]+)", re.IGNORECASE)
RE_SKU = re.compile(r"SKU:\s*(.+)$", re.IGNORECASE)
RE_CARD_UID = re.compile(r"Card UID:\s*([0-9A-Fa-f:]+)", re.IGNORECASE)
RE_MANUFACTURED_ON = re.compile(r"Manufactured on:\s*(.+)$", re.IGNORECASE)


def normalize_uid(uid: str) -> str:
    return str(uid).replace(":", "").strip().upper()


def normalize_color(argb_or_rgb: str) -> str:
    value = str(argb_or_rgb).replace("#", "").replace("0x", "").strip().upper()
    if len(value) == 8:
        return value[2:]  # drop alpha channel
    if len(value) == 6:
        return value
    return ""


def to_number(value):
    try:
        f = float(value)
        return int(f) if f.is_integer() else f
    except Exception:
        return None


def reset_current_tag_context():
    global current_uid, current_vendor, current_manufacturer, current_type, current_subtype, current_color
    global current_weight, current_diameter, current_drying_temp, current_drying_time
    global current_hotend_min, current_hotend_max, current_bed_temp, current_first_layer_temp
    global current_other_layer_temp, current_sku, current_manufactured_date

    current_uid = None
    current_vendor = ""
    current_manufacturer = ""
    current_type = ""
    current_subtype = ""
    current_color = ""
    current_weight = None
    current_diameter = None
    current_drying_temp = None
    current_drying_time = None
    current_hotend_min = None
    current_hotend_max = None
    current_bed_temp = None
    current_first_layer_temp = None
    current_other_layer_temp = None
    current_sku = ""
    current_manufactured_date = ""


def post_database_value(value):
    payload = {
        "namespace": DATABASE_NAMESPACE,
        "key": DATABASE_KEY,
        "value": value,
    }
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        MOONRAKER_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        response.read()


def clear_last_tag():
    try:
        post_database_value({"cleared": True, "card_uid": "", "slot": None})
        print("[inventory-bridge] Cleared stale u1_last_rfid_tag", flush=True)
    except Exception as exc:
        print(f"[inventory-bridge] Could not clear stale tag: {exc}", flush=True)


def post_successful_tag(slot, uid):
    global last_sent_key

    if slot is None or uid is None:
        return

    slot = int(slot)
    uid = normalize_uid(uid)

    value = {
        "slot": slot,
        "card_uid": uid,
        "vendor": current_vendor or "",
        "manufacturer": current_manufacturer or current_vendor or "",
        "type": current_type or "",
        "subtype": current_subtype or "",
        "color": current_color or "",
        "read_status": "decoded",
    }

    optional_fields = {
        "weight": current_weight,
        "diameter": current_diameter,
        "drying_temp": current_drying_temp,
        "drying_time": current_drying_time,
        "hotend_min": current_hotend_min,
        "hotend_max": current_hotend_max,
        "bed_temp": current_bed_temp,
        "first_layer_temp": current_first_layer_temp,
        "other_layer_temp": current_other_layer_temp,
        "sku": current_sku,
        "manufactured_date": current_manufactured_date,
    }

    for key, val in optional_fields.items():
        if val not in (None, ""):
            value[key] = val

    # De-dupe the same completed read.
    event_key = json.dumps(value, sort_keys=True)
    if event_key == last_sent_key:
        return

    try:
        post_database_value(value)
        last_sent_key = event_key
        print(
            f"[inventory-bridge] Sent decoded UID={uid} slot={slot} "
            f"manufacturer='{value.get('manufacturer', '')}' type='{value.get('type', '')}' "
            f"subtype='{value.get('subtype', '')}' color='{value.get('color', '')}' "
            f"weight='{value.get('weight', '')}'",
            flush=True,
        )
    except Exception as exc:
        print(f"[inventory-bridge] Failed to send decoded UID {uid}: {exc}", flush=True)


def handle_line(line: str):
    global current_slot, current_uid, current_vendor, current_manufacturer, current_type, current_subtype, current_color
    global current_weight, current_diameter, current_drying_temp, current_drying_time
    global current_hotend_min, current_hotend_max, current_bed_temp, current_first_layer_temp
    global current_other_layer_temp, current_sku, current_manufactured_date

    match = RE_PROCESSING_SLOT.search(line)
    if match:
        current_slot = int(match.group(1))
        reset_current_tag_context()
        return

    # Record UID, but do not post it yet. UID-only tags are ignored unless a successful read follows.
    match = RE_DETECTED_TYPE_UID.search(line)
    if match:
        current_uid = normalize_uid(match.group(1))
        return

    # Explicitly ignore failed/unsupported tags. This is what filters out unreadable Bambu/generic tags.
    if RE_FAILED_READ.search(line):
        return

    match = RE_FAILED_UID_WITH_SLOT.search(line)
    if match:
        uid = normalize_uid(match.group(1))
        slot = int(match.group(2))
        print(f"[inventory-bridge] Ignored unreadable UID={uid} slot={slot}", flush=True)
        return

    match = RE_VENDOR.search(line)
    if match:
        current_vendor = match.group(1).strip()
        return

    match = RE_MANUFACTURER.search(line)
    if match:
        current_manufacturer = match.group(1).strip()
        return

    match = RE_MAIN_TYPE.search(line)
    if match:
        current_type = match.group(1).strip()
        return

    match = RE_SUB_TYPE.search(line)
    if match:
        current_subtype = match.group(1).strip()
        return

    match = RE_ARGB_COLOR.search(line) or RE_RGB1_COLOR.search(line)
    if match:
        current_color = normalize_color(match.group(1))
        return

    match = RE_WEIGHT.search(line)
    if match:
        current_weight = to_number(match.group(1))
        return

    match = RE_DIAMETER.search(line)
    if match:
        current_diameter = to_number(match.group(1))
        return

    match = RE_DRYING_TEMP.search(line)
    if match:
        current_drying_temp = to_number(match.group(1))
        return

    match = RE_DRYING_TIME.search(line)
    if match:
        current_drying_time = to_number(match.group(1))
        return

    match = RE_HOTEND_MAX.search(line)
    if match:
        current_hotend_max = to_number(match.group(1))
        return

    match = RE_HOTEND_MIN.search(line)
    if match:
        current_hotend_min = to_number(match.group(1))
        return

    match = RE_BED_TEMP.search(line)
    if match:
        current_bed_temp = to_number(match.group(1))
        return

    match = RE_FIRST_LAYER_TEMP.search(line)
    if match:
        current_first_layer_temp = to_number(match.group(1))
        return

    match = RE_OTHER_LAYER_TEMP.search(line)
    if match:
        current_other_layer_temp = to_number(match.group(1))
        return

    match = RE_SKU.search(line)
    if match:
        current_sku = match.group(1).strip()
        return

    match = RE_CARD_UID.search(line)
    if match:
        current_uid = normalize_uid(match.group(1))
        return

    match = RE_MANUFACTURED_ON.search(line)
    if match:
        current_manufactured_date = match.group(1).strip()
        return

    # Only successful reads are sent to inventory.
    match = RE_SUCCESS_UID_WITH_SLOT.search(line)
    if match:
        success_uid = normalize_uid(match.group(1))
        slot = int(match.group(2))
        uid = current_uid or success_uid
        post_successful_tag(slot=slot, uid=uid)
        return


def follow_log(path: Path):
    while not path.exists():
        print(f"[inventory-bridge] Waiting for log file: {path}", flush=True)
        time.sleep(2)

    print(f"[inventory-bridge] Watching {path}", flush=True)

    with path.open("r", errors="ignore") as log_file:
        # Start at end of current log. New scans after startup are processed.
        log_file.seek(0, 2)

        while True:
            line = log_file.readline()
            if not line:
                time.sleep(0.2)
                continue
            handle_line(line)


if __name__ == "__main__":
    print("[inventory-bridge] Starting OpenRFID inventory bridge - decoded tags only", flush=True)

    if CLEAR_LAST_TAG_ON_START:
        clear_last_tag()

    try:
        follow_log(LOG_PATH)
    except KeyboardInterrupt:
        print("[inventory-bridge] Stopped by user", flush=True)
