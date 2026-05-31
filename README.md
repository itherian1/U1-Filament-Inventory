# Snapmaker U1 Offline Filament Inventory with OpenRFID Integration

A self-hosted, offline filament inventory system designed to run directly on a Snapmaker U1 using Paxx12 / extended firmware, Moonraker, Klipper-style `SAVE_VARIABLE` storage, and OpenRFID tag reads.

This project lets the Snapmaker U1 become the inventory source of truth for loaded filament. When a supported RFID tag is read, the inventory page can automatically assign the spool to the correct U1 slot, pull useful filament metadata, and later subtract filament usage from the correct spool after printing.

The project is intentionally local-first. It does not require cloud services, external databases, Bambu Cloud, Snapmaker Cloud, or an internet connection after installation.

---

## What This Project Does

This system adds a browser-based filament inventory app to the Snapmaker U1 and integrates it with the printer through Moonraker and OpenRFID.

Core features:

- Offline filament inventory hosted on the printer.
- Inventory data stored in Moonraker's database.
- RFID UID tracking through OpenRFID logs.
- Automatic assignment of RFID spools to Snapmaker U1 slots.
- Ignores unreadable / unsupported RFID tags instead of cluttering inventory.
- Friendly color names, such as `Yellow`, while still preserving the hex color.
- Basic and Advanced card views.
- Basic and Advanced edit views.
- Drying recommendations and print setting fields.
- Slot-specific filament deduction.
- Per-spool weight tracking.
- Low-spool reorder dashboard.
- JSON import/export backup.
- Local snapshot restore.
- Printable QR labels.

---

## Current Confirmed Functionality

The following behavior has been tested and confirmed in this setup:

- OpenRFID reads Snapmaker-compatible tags.
- The bridge script reads OpenRFID log events.
- Decoded RFID tags are written to Moonraker database key `u1_last_rfid_tag`.
- The inventory page detects the new tag.
- The inventory page adds or updates the matching spool.
- Slot 1 deduction works.
- Slot 2 deduction works.
- Generic / unreadable RFID tags, including tags OpenRFID cannot decrypt, can be ignored.
- Slot-specific deduction uses separate variables for each U1 slot.

---

## High-Level Architecture

```text
Snapmaker U1 / Paxx12 firmware
        |
        | OpenRFID reads RFID tag
        v
/oem/printer_data/logs/openrfid.log
        |
        | Python bridge tails log
        v
Moonraker database key:
fluidd / u1_last_rfid_tag
        |
        | Inventory page polls Moonraker
        v
Filament Inventory UI
        |
        | Saves inventory list
        v
Moonraker database key:
fluidd / custom_filament_spools
```

Filament usage tracking uses a separate path:

```text
Klipper / printer.cfg tracker
        |
        | Writes per-slot usage variables
        v
save_variables:
pending_filament_slot1_mm
pending_filament_slot2_mm
pending_filament_slot3_mm
pending_filament_slot4_mm
        |
        | Inventory page polls variables
        v
Subtracts grams from matching inventory slot
```

---

## Important Design Decision

This project does **not** modify OpenRFID's config to export data directly.

Early testing showed that editing `openrfid_user.cfg` could cause OpenRFID/RFID behavior to break on this Snapmaker U1 setup. Instead, this project uses a safer **sidecar bridge**:

- OpenRFID remains untouched.
- OpenRFID continues doing what it already does.
- The bridge reads OpenRFID's log file.
- The bridge only forwards successfully decoded tag reads.
- Failed or unreadable RFID tags are ignored.

This keeps the printer's RFID system stable.

---

## Repository Layout

Recommended GitHub repository structure:

```text
snapmaker-u1-filament-inventory/
├── README.md
├── web/
│   └── index.html
├── bridge/
│   └── openrfid_inventory_bridge.py
├── klipper/
│   └── filament_tracker_slot_specific_block.cfg
├── nginx/
│   └── filament_inventory_nginx_location.conf
└── init.d/
    └── S98inventorybridge
```

Recommended installed locations on the printer:

```text
/home/lava/printer_data/config/filament_inventory/index.html
/oem/printer_data/config/filament_inventory/openrfid_inventory_bridge.py
/etc/init.d/S98inventorybridge
```

---

## Requirements

This setup assumes:

- Snapmaker U1.
- Paxx12 / extended firmware or similar firmware exposing Moonraker/OpenRFID.
- SSH/root access to the printer.
- Moonraker HTTP API available locally.
- OpenRFID installed and working.
- Nginx or equivalent web server available on the printer.
- Existing `[save_variables]` section in `printer.cfg`.

Your `printer.cfg` should already include something like:

```ini
[save_variables]
filename: ~/printer_data/config/variables.cfg
```

Do not add a second `[save_variables]` block if one already exists.

---

## Key Files and Paths

### Web UI

Recommended location:

```text
/home/lava/printer_data/config/filament_inventory/index.html
```

This is the main browser app.

### Python bridge

Recommended location:

```text
/oem/printer_data/config/filament_inventory/openrfid_inventory_bridge.py
```

This tails the OpenRFID log and writes decoded tag data to Moonraker.

### OpenRFID log

```text
/oem/printer_data/logs/openrfid.log
```

The bridge watches this file.

### Bridge log

```text
/oem/printer_data/logs/openrfid_inventory_bridge.log
```

This is where the bridge should write its own status output when run in the background.

### Moonraker database keys

The inventory app uses these Moonraker database keys:

```text
namespace: fluidd
key: custom_filament_spools
```

Stores the full inventory list.

```text
namespace: fluidd
key: custom_spools_snapshot
```

Stores the local snapshot backup.

```text
namespace: fluidd
key: u1_last_rfid_tag
```

Stores the most recent decoded RFID tag read from the bridge.

---

## Installation Overview

Installation has four main parts:

1. Install the web UI.
2. Point the printer web server to the UI.
3. Install the OpenRFID bridge script.
4. Add the slot-specific filament tracking block to `printer.cfg`.

---

# Part 1 — Install the Web UI

Create a persistent folder for the app:

```bash
mkdir -p /home/lava/printer_data/config/filament_inventory
```

Copy the inventory HTML into that folder:

```bash
cp index.html /home/lava/printer_data/config/filament_inventory/index.html
```

Set permissions:

```bash
chmod -R 755 /home/lava/printer_data/config/filament_inventory
chmod 755 /home/lava
chmod 755 /home/lava/printer_data
chmod 755 /home/lava/printer_data/config
```

Do not host the app directly under `/home/lava/filament_inventory`. That location may be cleaned or regenerated by the printer firmware.

Recommended persistent path:

```text
/home/lava/printer_data/config/filament_inventory/
```

---

# Part 2 — Configure the Web Server

The printer may not use `systemd`, so `systemctl` may not exist. Use standard nginx commands instead.

Find nginx config files:

```bash
find /etc -iname '*nginx*' -o -iname '*moonraker*' 2>/dev/null
```

Check if nginx is running:

```bash
ps | grep nginx
```

Add a location block inside the active nginx `server { ... }` block:

```nginx
location /filament_inventory/ {
    alias /home/lava/printer_data/config/filament_inventory/;
    index index.html;
    try_files $uri $uri/ /filament_inventory/index.html;
}
```

Test nginx config:

```bash
nginx -t
```

Reload nginx:

```bash
nginx -s reload
```

If needed, start nginx:

```bash
nginx
```

Then open:

```text
http://YOUR-PRINTER-IP/filament_inventory/
```

---

# Part 3 — Install the OpenRFID Inventory Bridge

Create a persistent bridge folder:

```bash
mkdir -p /oem/printer_data/config/filament_inventory
```

Copy the bridge script:

```bash
cp openrfid_inventory_bridge.py /oem/printer_data/config/filament_inventory/openrfid_inventory_bridge.py
```

Make it executable:

```bash
chmod +x /oem/printer_data/config/filament_inventory/openrfid_inventory_bridge.py
```

Test in the foreground:

```bash
python3 /oem/printer_data/config/filament_inventory/openrfid_inventory_bridge.py
```

Now scan or load a supported RFID spool.

Expected bridge output for a decoded tag:

```text
[inventory-bridge] Sent decoded UID=81F17BE3 slot=1 manufacturer='Polymaker' type='PLA' subtype='SnapSpeed'
```

Expected bridge output for an unreadable tag:

```text
[inventory-bridge] Ignored unreadable UID=440955F6 slot=0
```

Stop foreground mode with:

```text
Ctrl+C
```

Run in the background:

```bash
nohup python3 /oem/printer_data/config/filament_inventory/openrfid_inventory_bridge.py \
  > /oem/printer_data/logs/openrfid_inventory_bridge.log 2>&1 &
```

Watch the bridge log:

```bash
tail -f /oem/printer_data/logs/openrfid_inventory_bridge.log
```

---

# Part 4 — Make the Bridge Start on Boot

Create an init script:

```bash
vi /etc/init.d/S98inventorybridge
```

Paste:

```sh
#!/bin/sh

SCRIPT="/oem/printer_data/config/filament_inventory/openrfid_inventory_bridge.py"
LOG="/oem/printer_data/logs/openrfid_inventory_bridge.log"
PID="/run/openrfid_inventory_bridge.pid"

case "$1" in
  start)
    echo "Starting OpenRFID inventory bridge"
    start-stop-daemon -S -b -m -p "$PID" -x /usr/bin/python3 -- "$SCRIPT" >> "$LOG" 2>&1
    ;;
  stop)
    echo "Stopping OpenRFID inventory bridge"
    start-stop-daemon -K -p "$PID"
    rm -f "$PID"
    ;;
  restart)
    "$0" stop
    sleep 1
    "$0" start
    ;;
  *)
    echo "Usage: $0 {start|stop|restart}"
    exit 1
    ;;
esac

exit 0
```

Make it executable:

```bash
chmod +x /etc/init.d/S98inventorybridge
```

Start it:

```bash
/etc/init.d/S98inventorybridge start
```

Check process:

```bash
ps | grep inventory
```

Check log:

```bash
tail -f /oem/printer_data/logs/openrfid_inventory_bridge.log
```

---

# Part 5 — Add Slot-Specific Filament Tracking to `printer.cfg`

Back up your current config first:

```bash
cp /home/lava/printer_data/config/printer.cfg \
   /home/lava/printer_data/config/printer.cfg.bak-filament-inventory
```

Find your old filament inventory block. It likely starts with:

```ini
#################################################################
###    FILAMENT INVENTORY HELPERS - SAFE VERSION
#################################################################
```

Replace that entire old block with the slot-specific version below.

Important: do not leave two sections named:

```ini
[delayed_gcode TRACK_FILAMENT_USAGE]
```

Klipper cannot have duplicate sections with the same name.

## Slot-Specific Tracker Block

```ini
#################################################################
###    FILAMENT INVENTORY HELPERS - SLOT SPECIFIC VERSION
#################################################################

[gcode_macro CLEAR_FILAMENT_USAGE]
description: Clear all pending filament usage after inventory deducts it
gcode:
    SAVE_VARIABLE VARIABLE=pending_filament_mm VALUE=0
    SAVE_VARIABLE VARIABLE=pending_filament_slot1_mm VALUE=0
    SAVE_VARIABLE VARIABLE=pending_filament_slot2_mm VALUE=0
    SAVE_VARIABLE VARIABLE=pending_filament_slot3_mm VALUE=0
    SAVE_VARIABLE VARIABLE=pending_filament_slot4_mm VALUE=0


[gcode_macro CLEAR_FILAMENT_SLOT_USAGE]
description: Clear pending filament usage for one slot. Usage: CLEAR_FILAMENT_SLOT_USAGE SLOT=1
gcode:
    {% set SLOT = params.SLOT | default(0) | int %}

    {% if SLOT == 1 %}
        SAVE_VARIABLE VARIABLE=pending_filament_slot1_mm VALUE=0
    {% elif SLOT == 2 %}
        SAVE_VARIABLE VARIABLE=pending_filament_slot2_mm VALUE=0
    {% elif SLOT == 3 %}
        SAVE_VARIABLE VARIABLE=pending_filament_slot3_mm VALUE=0
    {% elif SLOT == 4 %}
        SAVE_VARIABLE VARIABLE=pending_filament_slot4_mm VALUE=0
    {% else %}
        RESPOND TYPE=error MSG="Missing or invalid SLOT. Usage: CLEAR_FILAMENT_SLOT_USAGE SLOT=1"
    {% endif %}


[gcode_macro SET_SCANNED_NFC]
description: Manually store scanned RFID/NFC tag for inventory testing
gcode:
    {% set UID = params.UID | default("") | string %}
    {% if UID != "" %}
        SAVE_VARIABLE VARIABLE=scanned_nfc VALUE='"{UID}"'
        RESPOND TYPE=command MSG="Saved scanned NFC UID: {UID}"
    {% else %}
        RESPOND TYPE=error MSG="Missing UID. Usage: SET_SCANNED_NFC UID=123456"
    {% endif %}


[gcode_macro CLEAR_SCANNED_NFC]
description: Clear scanned RFID/NFC tag after inventory processes it
gcode:
    SAVE_VARIABLE VARIABLE=scanned_nfc VALUE=0


[delayed_gcode TRACK_FILAMENT_USAGE]
initial_duration: 5.0
gcode:
    {% set status = printer.print_stats.state | default("standby") | lower %}

    {% if status == "printing" %}
        # Read each physical extruder's cumulative extrusion.
        {% set e0 = printer['extruder'].printing_e_pos | default(0) | float | abs %}
        {% set e1 = printer['extruder1'].printing_e_pos | default(0) | float | abs %}
        {% set e2 = printer['extruder2'].printing_e_pos | default(0) | float | abs %}
        {% set e3 = printer['extruder3'].printing_e_pos | default(0) | float | abs %}

        # Last saved values.
        {% set last_e0 = printer.save_variables.variables.last_e0_mm | default(e0) | float %}
        {% set last_e1 = printer.save_variables.variables.last_e1_mm | default(e1) | float %}
        {% set last_e2 = printer.save_variables.variables.last_e2_mm | default(e2) | float %}
        {% set last_e3 = printer.save_variables.variables.last_e3_mm | default(e3) | float %}

        # Positive deltas only.
        {% set d0 = e0 - last_e0 if e0 > last_e0 else 0 %}
        {% set d1 = e1 - last_e1 if e1 > last_e1 else 0 %}
        {% set d2 = e2 - last_e2 if e2 > last_e2 else 0 %}
        {% set d3 = e3 - last_e3 if e3 > last_e3 else 0 %}

        # Existing pending amounts.
        {% set p1 = printer.save_variables.variables.pending_filament_slot1_mm | default(0) | float %}
        {% set p2 = printer.save_variables.variables.pending_filament_slot2_mm | default(0) | float %}
        {% set p3 = printer.save_variables.variables.pending_filament_slot3_mm | default(0) | float %}
        {% set p4 = printer.save_variables.variables.pending_filament_slot4_mm | default(0) | float %}

        # Snapmaker U1 mapping:
        # T0 / extruder  = Snapmaker U1 - Slot 2
        # T1 / extruder1 = Snapmaker U1 - Slot 1
        # T2 / extruder2 = Snapmaker U1 - Slot 3
        # T3 / extruder3 = Snapmaker U1 - Slot 4

        SAVE_VARIABLE VARIABLE=pending_filament_slot1_mm VALUE={p1 + d1}
        SAVE_VARIABLE VARIABLE=pending_filament_slot2_mm VALUE={p2 + d0}
        SAVE_VARIABLE VARIABLE=pending_filament_slot3_mm VALUE={p3 + d2}
        SAVE_VARIABLE VARIABLE=pending_filament_slot4_mm VALUE={p4 + d3}

        SAVE_VARIABLE VARIABLE=last_e0_mm VALUE={e0}
        SAVE_VARIABLE VARIABLE=last_e1_mm VALUE={e1}
        SAVE_VARIABLE VARIABLE=last_e2_mm VALUE={e2}
        SAVE_VARIABLE VARIABLE=last_e3_mm VALUE={e3}

    {% else %}
        # Reset baselines while idle so the next print starts clean.
        SAVE_VARIABLE VARIABLE=last_e0_mm VALUE=0
        SAVE_VARIABLE VARIABLE=last_e1_mm VALUE=0
        SAVE_VARIABLE VARIABLE=last_e2_mm VALUE=0
        SAVE_VARIABLE VARIABLE=last_e3_mm VALUE=0
    {% endif %}

    UPDATE_DELAYED_GCODE ID=TRACK_FILAMENT_USAGE DURATION=10
```

Restart Klipper after editing.

---

## Snapmaker U1 Slot Mapping

This project uses the observed Snapmaker U1 mapping:

```text
T1 / extruder1 -> Snapmaker U1 - Slot 1
T0 / extruder  -> Snapmaker U1 - Slot 2
T2 / extruder2 -> Snapmaker U1 - Slot 3
T3 / extruder3 -> Snapmaker U1 - Slot 4
```

This means:

- A print using T1 should deduct from Slot 1.
- A print using T0 should deduct from Slot 2.
- A print using T2 should deduct from Slot 3.
- A print using T3 should deduct from Slot 4.

---

## Manual Deduction Tests

Open the inventory page before testing. The page must be open because the browser performs the deduction from Moonraker variables into the inventory database.

### Test Slot 1

```gcode
SAVE_VARIABLE VARIABLE=pending_filament_slot1_mm VALUE=10000
```

Expected result:

- Deducts about 30 g from the spool assigned to `Snapmaker U1 - Slot 1`.

### Test Slot 2

```gcode
SAVE_VARIABLE VARIABLE=pending_filament_slot2_mm VALUE=10000
```

Expected result:

- Deducts about 30 g from the spool assigned to `Snapmaker U1 - Slot 2`.

### Test Slot 3

```gcode
SAVE_VARIABLE VARIABLE=pending_filament_slot3_mm VALUE=10000
```

Expected result:

- Deducts about 30 g from the spool assigned to `Snapmaker U1 - Slot 3`.

### Test Slot 4

```gcode
SAVE_VARIABLE VARIABLE=pending_filament_slot4_mm VALUE=10000
```

Expected result:

- Deducts about 30 g from the spool assigned to `Snapmaker U1 - Slot 4`.

After successful deduction, the inventory page resets the corresponding pending slot variable back to `0`.

---

## Why 10,000 mm Is About 30 g

For 1.75 mm filament, the app estimates grams from filament length using material density.

Approximate formula:

```text
volume_mm3 = pi * radius_mm^2 * length_mm
volume_cm3 = volume_mm3 / 1000
grams = volume_cm3 * density
```

For PLA:

```text
radius = 0.875 mm
length = 10000 mm
density = about 1.24 g/cm3
result = about 30 g
```

Material density defaults:

```text
PLA       1.24 g/cm3
PLA-CF    1.27 g/cm3
PLA Silk  1.24 g/cm3
PETG      1.27 g/cm3
PETG-CF   1.29 g/cm3
ABS       1.04 g/cm3
ASA       1.07 g/cm3
TPU       1.21 g/cm3
PC        1.20 g/cm3
Nylon     1.14 g/cm3
PA-CF     1.14 g/cm3
```

---

## RFID Behavior

### Decoded Tags

When OpenRFID successfully decodes a tag, the bridge writes data like this to Moonraker:

```json
{
  "slot": 1,
  "card_uid": "81F17BE3",
  "vendor": "Snapmaker",
  "manufacturer": "Polymaker",
  "type": "PLA",
  "subtype": "SnapSpeed",
  "color": "F4C032",
  "color_name": "Yellow",
  "weight": 500,
  "diameter": 1.75,
  "drying_temp": 55,
  "drying_time": 6,
  "hotend_min_temp": 190,
  "hotend_max_temp": 230,
  "bed_temp": 60,
  "first_layer_temp": 230,
  "other_layer_temp": 220,
  "sku": "900003"
}
```

The inventory page then:

- Looks for a matching spool by RFID UID.
- If found, updates the spool location.
- If not found, opens the Add Spool screen.
- Pre-fills useful data from the tag.
- Uses the UID as the spool ID if saved.

### Unreadable Tags

Unreadable tags are ignored by the bridge.

Examples:

- Bambu Lab tags that OpenRFID can detect but cannot decode.
- Generic MIFARE cards with unknown keys.
- Tags that produce only UID but no decoded metadata.

This prevents the inventory from filling up with unusable tags like:

```text
Unknown / PLA / Unknown / 1000g
```

---

## Inventory UI Behavior

### Main Card View

The main card view is intentionally clean. It shows everyday information like:

- Brand / manufacturer.
- Friendly color name.
- Material.
- Remaining weight.
- Location.
- Low-stock status.
- Quick weight adjustment buttons.

### Advanced Card View

Each spool card has an Advanced button that can show:

- RFID UID.
- Vendor.
- Manufacturer.
- Subtype.
- Color name.
- Color hex.
- Recommended hotend settings.
- Bed temp.
- First layer temp.
- Other layer temp.
- Drying temp.
- Drying time.
- SKU.
- Diameter.
- Manufactured date.
- Raw OpenRFID metadata if preserved.

### Edit Modal

The Edit modal also has Basic and Advanced modes.

Basic edit fields:

- Brand / manufacturer.
- Material.
- Color name.
- Location.
- Spool capacity.
- Current weight.
- Price.
- Notes.
- Reorder link.

Advanced edit fields:

- Vendor.
- RFID UID.
- Color hex.
- SKU.
- Diameter.
- Hotend min/max.
- Bed temp.
- First layer temp.
- Other layer temp.
- Drying temp/time.
- Manufactured date.

---

## Data Model

A typical spool entry may look like:

```json
{
  "id": "81F17BE3",
  "rfidUid": "81F17BE3",
  "vendor": "Snapmaker",
  "brand": "Polymaker",
  "manufacturer": "Polymaker",
  "material": "PLA",
  "subtype": "SnapSpeed",
  "color": "Yellow",
  "colorHex": "#F4C032",
  "location": "Snapmaker U1 - Slot 2",
  "weight": 500,
  "maxWeight": 500,
  "diameter": 1.75,
  "sku": "900003",
  "hotendMinTemp": 190,
  "hotendMaxTemp": 230,
  "bedTemp": 60,
  "firstLayerTemp": 230,
  "otherLayerTemp": 220,
  "dryingTemp": 55,
  "dryingTime": 6,
  "manufacturedDate": "2026-01-01",
  "price": 0,
  "notes": "",
  "url": "",
  "openRfid": {}
}
```

---

## Backup and Restore

The web UI includes:

- Export inventory to JSON.
- Import inventory from JSON.
- Create local snapshot.
- Restore previous snapshot.

Manual backup from Moonraker:

```bash
curl -s "http://localhost/server/database/item?namespace=fluidd&key=custom_filament_spools" \
  > filament_inventory_backup.json
```

Manual restore is usually easier through the web UI import button.

---

## Useful Commands

### Check OpenRFID log

```bash
tail -f /oem/printer_data/logs/openrfid.log
```

### Check bridge log

```bash
tail -f /oem/printer_data/logs/openrfid_inventory_bridge.log
```

### Check latest RFID tag in Moonraker

```bash
curl -s "http://localhost/server/database/item?namespace=fluidd&key=u1_last_rfid_tag"
```

### Clear latest RFID tag

```bash
curl -s -X POST http://localhost/server/database/item \
  -H "Content-Type: application/json" \
  -d '{"namespace":"fluidd","key":"u1_last_rfid_tag","value":{"cleared":true,"card_uid":"","slot":null}}'
```

### Check save variables

```bash
curl -s "http://localhost/printer/objects/query?save_variables"
```

### Restart OpenRFID

```bash
/etc/init.d/S99openrfid restart
```

### Restart inventory bridge

```bash
/etc/init.d/S98inventorybridge restart
```

### Restart nginx

```bash
nginx -s reload
```

---

## Troubleshooting

### `systemctl: command not found`

The printer OS may not use systemd. Use:

```bash
nginx -s reload
/etc/init.d/S99openrfid restart
/etc/init.d/S98inventorybridge restart
```

instead of `systemctl`.

---

### LED or RFID stopped working after editing config

Restore your printer config or OpenRFID config from backup.

Avoid editing OpenRFID base files.

Do not delete important sections from `printer.cfg`, such as:

```ini
[mcu]
[mcu host]
[fm175xx_reader]
[filament_detect]
[led cavity_led]
```

Removing those can break LED control, RFID, and printer hardware behavior.

---

### RFID works only when `openrfid_user.cfg` is untouched

Use the sidecar bridge method from this project.

Do not add webhook exporters directly to OpenRFID if your firmware build is sensitive to those config changes.

---

### OpenRFID detects a tag but inventory does nothing

Check OpenRFID log:

```bash
tail -n 80 /oem/printer_data/logs/openrfid.log
```

If you see:

```text
Successfully read tag with UID ...
```

then check bridge log:

```bash
tail -n 80 /oem/printer_data/logs/openrfid_inventory_bridge.log
```

If you see only:

```text
M1 AUTH ERROR
Failed to read MIFARE Classic card data
```

then the tag was detected but not decoded. The bridge intentionally ignores it.

---

### Bambu Lab tag is ignored

This is expected unless OpenRFID can decrypt and decode the Bambu tag.

The bridge ignores UID-only tags so the inventory does not fill with incomplete entries.

If Bambu tag decoding is later added, the bridge can be extended to parse Bambu metadata and send it to the same Moonraker key.

---

### Deduction goes to the wrong spool

Check the spool location in the inventory.

The location must exactly match one of:

```text
Snapmaker U1 - Slot 1
Snapmaker U1 - Slot 2
Snapmaker U1 - Slot 3
Snapmaker U1 - Slot 4
```

Then confirm the pending slot variable:

```bash
curl -s "http://localhost/printer/objects/query?save_variables"
```

Check for:

```text
pending_filament_slot1_mm
pending_filament_slot2_mm
pending_filament_slot3_mm
pending_filament_slot4_mm
```

---

### Deduction does nothing

Make sure:

1. The inventory page is open.
2. A spool is assigned to the matching slot.
3. The pending slot variable is greater than zero.
4. The browser can reach Moonraker.
5. The new slot-specific HTML is installed.

---

### Nginx reload fails

Test config:

```bash
nginx -t
```

Then reload:

```bash
nginx -s reload
```

If nginx is not running:

```bash
nginx
```

---

## Safety Notes

- Back up `printer.cfg` before editing.
- Do not replace the entire Snapmaker config with only the filament tracker block.
- Do not edit `/tmp/openrfid.cfg`; it is likely generated at runtime.
- Avoid editing OpenRFID base files unless you understand the firmware overlay behavior.
- Keep custom files in persistent paths under `/home/lava/printer_data` or `/oem/printer_data`.
- Test manually before running a long print.

---

## Known Limitations

- The browser page must be open for automatic deduction from pending variables into inventory.
- Bambu Lab tags may be detected but ignored if OpenRFID cannot decode them.
- Filament usage is estimated from filament length and density.
- Slot mapping is based on observed Snapmaker U1 behavior and may need adjustment if firmware changes.
- Multi-material prints depend on correct `printing_e_pos` reporting for each physical extruder.

---

## Future Improvements

Possible future additions:

- Background server-side deduction so the browser does not need to stay open.
- Native Bambu Lab RFID decoding if keys/processor support are available.
- Better color-name mapping from hex values.
- Filament brand database.
- Spool history and print history.
- Per-print usage logs.
- Estimated remaining print hours.
- Auto-reorder thresholds by material.
- QR/RFID pairing workflow for non-RFID spools.
- Better mobile layout.
- Dedicated setup script.

---

## License

Choose a license before publishing. Recommended options:

- MIT License for simple open-source sharing.
- GPLv3 if you want derivative projects to remain open source.

---

## Disclaimer

This project modifies printer-side configuration and adds custom scripts. Use at your own risk. Always back up your printer configuration before making changes. This project is not affiliated with Snapmaker, Paxx12, OpenRFID, Bambu Lab, Polymaker, Fluidd, Moonraker, or Klipper.
