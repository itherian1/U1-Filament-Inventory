#!/usr/bin/env python3
"""
OpenRFID -> Moonraker inventory bridge for Snapmaker U1

Debounced / insertion-only version.

Purpose:
- Watches /oem/printer_data/logs/openrfid.log
- Posts decoded tag data when OpenRFID can decrypt it
- Posts UID-only tag data when OpenRFID sees a tag but cannot decrypt it, such as Bambu Lab tags
- Posts data only when a slot receives a NEW tag
- Suppresses repeated "same tag still sitting in the same slot" events
- Persists slot state so bridge restarts do not re-post the same already-loaded tags

Moonraker database output:
  namespace: fluidd
  key:       u1_last_rfid_tag

Inventory page polls:
  /server/database/item?namespace=fluidd&key=u1_last_rfid_tag
"""

import json
import re
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

LOG_PATH = Path("/oem/printer_data/logs/openrfid.log")
STATE_PATH = Path("/oem/printer_data/config/filament_inventory/rfid_slot_state.json")
MOONRAKER_URL = "http://localhost/server/database/item"
DATABASE_NAMESPACE = "fluidd"
DATABASE_KEY = "u1_last_rfid_tag"

# Keep this False for normal use. If True, the last tag banner in the UI clears when the bridge starts.
CLEAR_LAST_TAG_ON_START = False

# If OpenRFID repeatedly re-reads the same tag in the same slot, ignore it indefinitely until
# the slot is seen empty or a different UID appears in that slot.
slot_uid_state: Dict[str, str] = {}
last_sent_key: Optional[str] = None

current_slot: Optional[int] = None
current_uid: Optional[str] = None
current_vendor = ""
current_manufacturer = ""
current_type = ""
current_subtype = ""
current_color = ""
current_weight: Optional[float] = None
current_diameter: Optional[float] = None
current_drying_temp: Optional[float] = None
current_drying_time: Optional[float] = None
current_hotend_min: Optional[float] = None
current_hotend_max: Optional[float] = None
current_bed_temp: Optional[float] = None
current_first_layer_temp: Optional[float] = None
current_other_layer_temp: Optional[float] = None
current_sku = ""
current_manufactured_date = ""

# OpenRFID log patterns
RE_PROCESSING_SLOT = re.compile(r"Processing reader slot_(\d+)_reader", re.IGNORECASE)
RE_SUCCESS_UID_WITH_SLOT = re.compile(r"Successfully read tag with UID ([0-9A-Fa-f]+) on reader slot_(\d+)_reader", re.IGNORECASE)
RE_DETECTED_TYPE_UID = re.compile(r"Detected tag type .* with UID ([0-9A-Fa-f]+)", re.IGNORECASE)
RE_FAILED_UID_WITH_SLOT = re.compile(r"Detected tag with UID ([0-9A-Fa-f]+) on reader slot_(\d+)_reader", re.IGNORECASE)
RE_FAILED_READ = re.compile(r"Failed to read data from tag|Failed to read MIFARE Classic card data|M1 AUTH ERROR|Mifare Classic read error", re.IGNORECASE)
RE_STATUS_UPDATE = re.compile(r"(?:Initial value for tracked field set to|Received status update):\s*\[([^\]]+)\]", re.IGNORECASE)

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


def to_number(value: str) -> Optional[float]:
    try:
        f = float(value)
        return int(f) if f.is_integer() else f
    except Exception:
        return None


def load_state() -> None:
    global slot_uid_state
    try:
        if STATE_PATH.exists():
            data = json.loads(STATE_PATH.read_text())
            if isinstance(data, dict):
                slot_uid_state = {str(k): normalize_uid(v) for k, v in data.get("slot_uid_state", {}).items() if v}
        print(f"[inventory-bridge] Loaded slot state: {slot_uid_state}", flush=True)
    except Exception as exc:
        print(f"[inventory-bridge] Could not load slot state: {exc}", flush=True)
        slot_uid_state = {}


def save_state() -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps({"slot_uid_state": slot_uid_state}, indent=2, sort_keys=True))
    except Exception as exc:
        print(f"[inventory-bridge] Could not save slot state: {exc}", flush=True)


def reset_current_tag_context() -> None:
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


def post_database_value(value: Dict[str, Any]) -> None:
    payload = {"namespace": DATABASE_NAMESPACE, "key": DATABASE_KEY, "value": value}
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        MOONRAKER_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        response.read()


def clear_last_tag() -> None:
    try:
        post_database_value({"cleared": True, "card_uid": "", "slot": None})
        print("[inventory-bridge] Cleared stale u1_last_rfid_tag", flush=True)
    except Exception as exc:
        print(f"[inventory-bridge] Could not clear stale tag: {exc}", flush=True)


def handle_slot_status_update(line: str) -> bool:
    """
    Parse OpenRFID's status update list. A 0 means the slot is empty/cleared.
    This allows the same UID to be processed again after it is physically removed
    and later reinserted.
    """
    match = RE_STATUS_UPDATE.search(line)
    if not match:
        return False

    try:
        values = [int(x.strip()) for x in match.group(1).split(",")]
    except Exception:
        return True

    changed = False
    for slot, present in enumerate(values):
        key = str(slot)
        if present == 0 and key in slot_uid_state:
            old_uid = slot_uid_state.pop(key)
            changed = True
            print(f"[inventory-bridge] Slot {slot} now empty; cleared remembered UID {old_uid}", flush=True)

    if changed:
        save_state()

    return True


def build_current_value(slot: int, uid: str, read_status: str = "decoded") -> Dict[str, Any]:
    value: Dict[str, Any] = {
        "slot": int(slot),
        "card_uid": normalize_uid(uid),
        "vendor": current_vendor or "",
        "manufacturer": current_manufacturer or current_vendor or "",
        "type": current_type or "",
        "subtype": current_subtype or "",
        "color": current_color or "",
        "read_status": read_status,
    }

    # UID-only tags are useful for inventory identity/location tracking, but do not
    # have trustworthy material/color/settings metadata. Leave those fields blank
    # so the Web UI prompts the user to fill them in manually.
    if read_status == "uid_only":
        value.update({
            "vendor": "UID Only",
            "manufacturer": "",
            "type": "",
            "subtype": "",
            "color": "",
        })
        return value

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

    return value


def post_uid_only_tag(slot: int, uid: str) -> None:
    """
    Post a UID-only tag event for tags OpenRFID can see but cannot decrypt.
    This is intended for Bambu Lab and other built-in RFID tags where the UID is
    enough to identify the spool in the inventory, but metadata must be entered
    manually once.
    """
    global last_sent_key

    slot = int(slot)
    uid = normalize_uid(uid)
    slot_key = str(slot)

    remembered_uid = slot_uid_state.get(slot_key)
    if remembered_uid == uid:
        print(f"[inventory-bridge] Ignored repeated UID-only tag UID={uid} still in slot={slot}", flush=True)
        return

    value = build_current_value(slot, uid, read_status="uid_only")
    event_key = json.dumps(value, sort_keys=True)
    if event_key == last_sent_key:
        print(f"[inventory-bridge] Ignored duplicate UID-only event UID={uid} slot={slot}", flush=True)
        return

    try:
        post_database_value(value)
        last_sent_key = event_key
        slot_uid_state[slot_key] = uid
        save_state()
        print(f"[inventory-bridge] Sent NEW UID-only tag UID={uid} slot={slot}", flush=True)
    except Exception as exc:
        print(f"[inventory-bridge] Failed to send UID-only tag UID={uid}: {exc}", flush=True)


def post_successful_tag(slot: int, uid: str) -> None:
    global last_sent_key

    slot = int(slot)
    uid = normalize_uid(uid)
    slot_key = str(slot)

    remembered_uid = slot_uid_state.get(slot_key)
    if remembered_uid == uid:
        print(f"[inventory-bridge] Ignored repeated UID={uid} still in slot={slot}", flush=True)
        return

    value = build_current_value(slot, uid)
    event_key = json.dumps(value, sort_keys=True)
    if event_key == last_sent_key:
        print(f"[inventory-bridge] Ignored duplicate event UID={uid} slot={slot}", flush=True)
        return

    try:
        post_database_value(value)
        last_sent_key = event_key
        slot_uid_state[slot_key] = uid
        save_state()
        print(
            f"[inventory-bridge] Sent NEW decoded UID={uid} slot={slot} "
            f"manufacturer='{value.get('manufacturer', '')}' type='{value.get('type', '')}' "
            f"subtype='{value.get('subtype', '')}' color='{value.get('color', '')}' "
            f"weight='{value.get('weight', '')}'",
            flush=True,
        )
    except Exception as exc:
        print(f"[inventory-bridge] Failed to send decoded UID {uid}: {exc}", flush=True)


def handle_line(line: str) -> None:
    global current_slot, current_uid, current_vendor, current_manufacturer, current_type, current_subtype, current_color
    global current_weight, current_diameter, current_drying_temp, current_drying_time
    global current_hotend_min, current_hotend_max, current_bed_temp, current_first_layer_temp
    global current_other_layer_temp, current_sku, current_manufactured_date

    if handle_slot_status_update(line):
        return

    match = RE_PROCESSING_SLOT.search(line)
    if match:
        current_slot = int(match.group(1))
        reset_current_tag_context()
        return

    match = RE_DETECTED_TYPE_UID.search(line)
    if match:
        current_uid = normalize_uid(match.group(1))
        return

    if RE_FAILED_READ.search(line):
        return

    match = RE_FAILED_UID_WITH_SLOT.search(line)
    if match:
        uid = normalize_uid(match.group(1))
        slot = int(match.group(2))
        post_uid_only_tag(slot=slot, uid=uid)
        return

    for regex, attr in [
        (RE_VENDOR, "current_vendor"),
        (RE_MANUFACTURER, "current_manufacturer"),
        (RE_MAIN_TYPE, "current_type"),
        (RE_SUB_TYPE, "current_subtype"),
        (RE_SKU, "current_sku"),
        (RE_MANUFACTURED_ON, "current_manufactured_date"),
    ]:
        match = regex.search(line)
        if match:
            globals()[attr] = match.group(1).strip()
            return

    match = RE_ARGB_COLOR.search(line) or RE_RGB1_COLOR.search(line)
    if match:
        current_color = normalize_color(match.group(1))
        return

    for regex, attr in [
        (RE_WEIGHT, "current_weight"),
        (RE_DIAMETER, "current_diameter"),
        (RE_DRYING_TEMP, "current_drying_temp"),
        (RE_DRYING_TIME, "current_drying_time"),
        (RE_HOTEND_MAX, "current_hotend_max"),
        (RE_HOTEND_MIN, "current_hotend_min"),
        (RE_BED_TEMP, "current_bed_temp"),
        (RE_FIRST_LAYER_TEMP, "current_first_layer_temp"),
        (RE_OTHER_LAYER_TEMP, "current_other_layer_temp"),
    ]:
        match = regex.search(line)
        if match:
            globals()[attr] = to_number(match.group(1))
            return

    match = RE_CARD_UID.search(line)
    if match:
        current_uid = normalize_uid(match.group(1))
        return

    match = RE_SUCCESS_UID_WITH_SLOT.search(line)
    if match:
        success_uid = normalize_uid(match.group(1))
        slot = int(match.group(2))
        uid = current_uid or success_uid
        post_successful_tag(slot=slot, uid=uid)
        return


def follow_log(path: Path) -> None:
    while not path.exists():
        print(f"[inventory-bridge] Waiting for log file: {path}", flush=True)
        time.sleep(2)

    print(f"[inventory-bridge] Watching {path}", flush=True)

    with path.open("r", errors="ignore") as log_file:
        log_file.seek(0, 2)
        while True:
            line = log_file.readline()
            if not line:
                time.sleep(0.2)
                continue
            handle_line(line)


if __name__ == "__main__":
    print("[inventory-bridge] Starting OpenRFID inventory bridge - insertion-only hybrid decoded + UID-only tags", flush=True)
    load_state()

    if CLEAR_LAST_TAG_ON_START:
        clear_last_tag()

    try:
        follow_log(LOG_PATH)
    except KeyboardInterrupt:
        print("[inventory-bridge] Stopped by user", flush=True)
