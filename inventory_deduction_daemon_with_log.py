#!/usr/bin/env python3
"""
Snapmaker U1 Filament Inventory Deduction Daemon

Runs on the printer and performs inventory weight deduction even when the web UI
is closed.

Inputs:
- Klipper/Moonraker save_variables:
  pending_filament_slot1_mm
  pending_filament_slot2_mm
  pending_filament_slot3_mm
  pending_filament_slot4_mm

Inventory database:
- Moonraker database namespace: fluidd
- Inventory key: custom_filament_spools

Behavior:
- While print_stats.state == "printing", it does not deduct.
- When the printer is no longer printing and pending slot usage exists, it:
  1. Reads inventory from Moonraker DB
  2. Finds spools assigned to Snapmaker U1 - Slot 1..4
  3. Converts mm of 1.75mm filament to grams using material density
  4. Deducts from the correct spool(s)
  5. Saves inventory back to Moonraker DB
  6. Clears only the slot variables that were successfully processed
  7. Writes a summary to custom_filament_last_deduction

This intentionally does NOT touch OpenRFID configuration.
"""

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

MOONRAKER_BASE = "http://localhost"
NAMESPACE = "fluidd"
INVENTORY_KEY = "custom_filament_spools"
LAST_DEDUCTION_KEY = "custom_filament_last_deduction"
DEDUCTION_LOG_KEY = "custom_filament_deduction_log"
MAX_DEDUCTION_LOG_ENTRIES = 50
POLL_SECONDS = 10

# 1.75mm filament radius = 0.875mm. Volume mm^3 -> cm^3, then density g/cm^3.
FILAMENT_RADIUS_MM = 0.875

MATERIAL_DENSITIES = {
    "PLA": 1.24,
    "PLA-CF": 1.27,
    "PLA SILK": 1.24,
    "PETG": 1.27,
    "PETG-CF": 1.29,
    "ABS": 1.04,
    "ASA": 1.07,
    "TPU": 1.21,
    "PC": 1.20,
    "NYLON (PA)": 1.14,
    "PA": 1.14,
    "PA-CF": 1.14,
    "WOOD": 1.20,
    "PVA": 1.23,
    "HIPS": 1.04,
}

SLOT_VARIABLES = [
    (1, "pending_filament_slot1_mm"),
    (2, "pending_filament_slot2_mm"),
    (3, "pending_filament_slot3_mm"),
    (4, "pending_filament_slot4_mm"),
]

last_processed_signature = None
last_missing_signature = None


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def log(message):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [deduction-daemon] {message}", flush=True)


def http_json(method, path, payload=None, timeout=5):
    url = f"{MOONRAKER_BASE}{path}"
    data = None
    headers = {}

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
        if not raw:
            return None
        return json.loads(raw)


def db_get(key, default=None):
    query = urllib.parse.urlencode({"namespace": NAMESPACE, "key": key})
    try:
        data = http_json("GET", f"/server/database/item?{query}")
        return data.get("result", {}).get("value", default)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return default
        raise


def db_set(key, value):
    payload = {"namespace": NAMESPACE, "key": key, "value": value}
    return http_json("POST", "/server/database/item", payload)


def append_deduction_log(entry):
    """Append one deduction/missing-assignment event to a persistent Moonraker DB log."""
    try:
        existing = db_get(DEDUCTION_LOG_KEY, default=[])
        if not isinstance(existing, list):
            existing = []

        existing.append(entry)
        existing = existing[-MAX_DEDUCTION_LOG_ENTRIES:]
        db_set(DEDUCTION_LOG_KEY, existing)
    except Exception as exc:
        # Do not let log-writing failures block actual inventory deduction.
        log(f"Warning: failed to append deduction log: {exc}")


def gcode(script):
    return http_json("POST", "/printer/gcode/script", {"script": script})


def get_printer_status():
    return http_json("GET", "/printer/objects/query?save_variables&print_stats")


def material_density(material):
    key = str(material or "PLA").strip().upper()
    return MATERIAL_DENSITIES.get(key, 1.24)


def mm_to_grams(mm_used, density):
    volume_cm3 = (math.pi * (FILAMENT_RADIUS_MM ** 2) * float(mm_used)) / 1000.0
    return volume_cm3 * float(density)


def round_tenth(value):
    return round(float(value), 1)


def format_grams(value):
    value = float(value)
    if abs(value - round(value)) < 0.05:
        return f"{round(value)}g"
    return f"{value:.1f}g"


def find_spool_for_slot(spools, slot):
    location = f"Snapmaker U1 - Slot {slot}"
    for spool in spools:
        if str(spool.get("location", "")).strip() == location:
            return spool
    return None


def clear_processed_slots(processed_slots):
    if not processed_slots:
        return

    lines = [f"SAVE_VARIABLE VARIABLE=pending_filament_slot{slot}_mm VALUE=0" for slot in sorted(processed_slots)]
    # Keep legacy variable clear too; harmless and avoids stale old-version deductions.
    lines.append("SAVE_VARIABLE VARIABLE=pending_filament_mm VALUE=0")
    gcode("\n".join(lines))


def build_usage_signature(print_state, slot_usage):
    return json.dumps({"state": print_state, "slots": slot_usage}, sort_keys=True)


def process_pending_usage(print_state, variables):
    global last_processed_signature, last_missing_signature

    slot_usage = []
    for slot, key in SLOT_VARIABLES:
        try:
            mm = float(variables.get(key, 0) or 0)
        except (TypeError, ValueError):
            mm = 0.0
        slot_usage.append({"slot": slot, "key": key, "mm": mm})

    if not any(item["mm"] > 0 for item in slot_usage):
        return

    signature = build_usage_signature(print_state, slot_usage)
    if signature == last_processed_signature:
        return

    inventory = db_get(INVENTORY_KEY, default=[])
    if not isinstance(inventory, list):
        log("Inventory DB value is not a list; skipping deduction.")
        return

    deductions = []
    missing = []
    processed_slots = []

    for item in slot_usage:
        slot = item["slot"]
        mm = float(item["mm"])
        if mm <= 0:
            continue

        spool = find_spool_for_slot(inventory, slot)
        if not spool:
            missing.append({
                "slot": slot,
                "location": f"Snapmaker U1 - Slot {slot}",
                "mm": round(mm, 1),
                "message": f"Slot {slot}: {round(mm)}mm used, but no spool is assigned."
            })
            continue

        density = material_density(spool.get("material", "PLA"))
        grams = round_tenth(mm_to_grams(mm, density))

        # Deduct even small values, but avoid saving weird negative/NaN data.
        if grams <= 0:
            continue

        before = float(spool.get("weight", 0) or 0)
        after = max(0.0, round_tenth(before - grams))
        spool["weight"] = after

        deduction = {
            "slot": slot,
            "location": f"Snapmaker U1 - Slot {slot}",
            "spool_id": str(spool.get("id", "")),
            "brand": spool.get("brand", ""),
            "color": spool.get("colorName") or spool.get("color") or spool.get("colorHex") or "",
            "material": spool.get("material", ""),
            "mm": round(mm, 1),
            "grams": grams,
            "weight_before": round_tenth(before),
            "weight_after": after,
            "density": density,
        }
        deductions.append(deduction)
        processed_slots.append(slot)

    if not deductions:
        missing_signature = json.dumps(missing, sort_keys=True)
        if missing and missing_signature != last_missing_signature:
            last_missing_signature = missing_signature
            log("Pending usage exists but no matching spool was assigned: " + "; ".join(m["message"] for m in missing))
            missing_summary = {
                "timestamp": now_iso(),
                "status": "missing_assignment",
                "deductions": [],
                "missing": missing,
                "print_state": print_state,
            }
            db_set(LAST_DEDUCTION_KEY, missing_summary)
            append_deduction_log(missing_summary)
        return

    db_set(INVENTORY_KEY, inventory)
    clear_processed_slots(processed_slots)

    summary = {
        "timestamp": now_iso(),
        "status": "deducted",
        "print_state": print_state,
        "deductions": deductions,
        "missing": missing,
    }
    db_set(LAST_DEDUCTION_KEY, summary)
    append_deduction_log(summary)
    last_processed_signature = signature

    human = "; ".join(
        f"Slot {d['slot']}: -{format_grams(d['grams'])} from {d['brand']} {d['color']} ({round(d['mm'])}mm)"
        for d in deductions
    )
    log(f"Deducted filament usage: {human}")

    if missing:
        log("Some pending usage was not cleared because no spool was assigned: " + "; ".join(m["message"] for m in missing))


def main():
    log("Starting Snapmaker U1 inventory deduction daemon")
    last_state = None

    while True:
        try:
            data = get_printer_status()
            status = data.get("result", {}).get("status", {})
            variables = status.get("save_variables", {}).get("variables", {}) or {}
            print_state = str(status.get("print_stats", {}).get("state", "standby") or "standby").lower()

            if print_state != last_state:
                log(f"Printer state: {last_state} -> {print_state}")
                last_state = print_state

            # Never deduct during active printing. Klipper accumulates pending slot usage during print.
            if print_state != "printing":
                process_pending_usage(print_state, variables)

        except Exception as exc:
            log(f"Error: {exc}")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
