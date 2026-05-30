# Snapmaker U1 Offline Filament Inventory

A lightweight, self-hosted filament inventory dashboard designed to run locally on a Snapmaker U1 running Paxx12/Klipper/Moonraker. The goal is to keep spool inventory, location, remaining weight, reorder links, notes, QR labels, and Snapmaker U1 slot assignments available directly from the printer’s local web interface without requiring cloud services.

> Status: Work in progress / maker project. Core inventory storage works through Moonraker’s database API. Snapmaker U1 slot-load detection works through exposed Moonraker objects. True RFID UID auto-import depends on whether the firmware exposes the tag ID through Moonraker.

---

## Features

- Offline/local filament inventory system
- Runs from the Snapmaker U1 local webserver
- Stores spool inventory in Moonraker database
- Add, edit, delete, import, and export spool records
- Track:
  - Brand
  - Material
  - Color
  - Location
  - Remaining weight
  - Spool capacity
  - Price
  - Reorder URL
  - Print profile notes
- Material-grouped inventory views
- Search and sort tools
- Low-stock dashboard for spools at or below 200 g
- QR label generation for spool IDs
- Dark mode
- Local snapshot / restore system
- Optional Klipper `save_variables` bridge for:
  - Pending filament usage
  - Manual RFID/NFC test values
- Snapmaker U1 slot-load detection using Moonraker objects

---

## Project Goal

This project was built to replace cloud or app-dependent filament inventory systems with a local, printer-hosted inventory page. The ideal workflow is:

1. Insert or attach filament to a Snapmaker U1 slot.
2. The printer detects the slot state.
3. The inventory page sees which slot changed.
4. The user assigns an existing spool or adds a new one.
5. The spool location updates to the correct Snapmaker U1 slot.
6. Print usage can later be deducted from the assigned spool.

---

## System Requirements

- Snapmaker U1
- Paxx12 firmware / Klipper-based environment
- Moonraker API available from the printer web interface
- SSH/root access to the printer
- Nginx or compatible local webserver
- Browser access to the printer IP address

Tested assumptions from the working printer environment:

```text
Printer user/home: /home/lava
Persistent printer data: /home/lava/printer_data
G-code path: /home/lava/printer_data/gcodes
Config path: /home/lava/printer_data/config
Klipper variables file: ~/printer_data/config/variables.cfg
```

---

## Recommended Install Location

Do not store the web app directly in `/home/lava`. Some Snapmaker/Paxx12 update or cleanup behavior may remove custom folders placed there.

Recommended path:

```text
/home/lava/printer_data/config/filament_inventory/
```

Main HTML file:

```text
/home/lava/printer_data/config/filament_inventory/index.html
```

Create the folder:

```bash
mkdir -p /home/lava/printer_data/config/filament_inventory
chmod -R 755 /home/lava/printer_data/config/filament_inventory
```

---

## Webserver / Nginx Setup

Add an alias to the active Nginx server block:

```nginx
location /filament_inventory/ {
    alias /home/lava/printer_data/config/filament_inventory/;
    index index.html;
    try_files $uri $uri/ /filament_inventory/index.html;
}
```

Then test and reload Nginx.

On Snapmaker U1, `systemctl` may not exist. Use one of these instead:

```bash
nginx -t
nginx -s reload
```

If that does not work:

```bash
service nginx reload
```

or:

```bash
/etc/init.d/nginx reload
```

Open the inventory page at:

```text
http://YOUR-PRINTER-IP/filament_inventory/
```

---

## Moonraker Database Storage

The inventory is stored through Moonraker’s database API using:

```javascript
namespace: "fluidd"
key: "custom_filament_spools"
```

This means the inventory data is not only stored inside the HTML page. If the web folder is lost and the Moonraker database remains intact, the page can be restored by replacing `index.html`.

Snapshot storage uses:

```javascript
namespace: "fluidd"
key: "custom_spools_snapshot"
```

---

## Safe Klipper Helper Macros

Add these only to a working full printer configuration. Do not replace the entire printer configuration with only these macros.

Your original config should already contain:

```ini
[save_variables]
filename: ~/printer_data/config/variables.cfg
```

If it already exists, do not add a second `[save_variables]` section.

Add this block near the bottom of `printer.cfg`:

```ini
#################################################################
###    FILAMENT INVENTORY HELPERS - SAFE VERSION
#################################################################

[gcode_macro CLEAR_FILAMENT_USAGE]
description: Clear pending filament amount after inventory deducts it
gcode:
    SAVE_VARIABLE VARIABLE=pending_filament_mm VALUE=0


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
        SAVE_VARIABLE VARIABLE=pending_filament_mm VALUE={printer.print_stats.filament_used | float}
    {% endif %}

    UPDATE_DELAYED_GCODE ID=TRACK_FILAMENT_USAGE DURATION=60
```

Restart Klipper after saving.

---

## Testing the Save Variable Bridge

Run this in the printer console:

```gcode
SET_SCANNED_NFC UID=TEST1234
```

Then open:

```text
http://YOUR-PRINTER-IP/printer/objects/query?save_variables
```

Expected result:

```json
{
  "result": {
    "status": {
      "save_variables": {
        "variables": {
          "pending_filament_mm": 0.0,
          "scanned_nfc": "TEST1234"
        }
      }
    }
  }
}
```

Clear it with:

```gcode
CLEAR_SCANNED_NFC
```

---

## Snapmaker U1 Slot Detection

The following Moonraker objects have been observed on the Snapmaker U1:

```text
filament_feed left
filament_feed right
gcode_macro _FILAMENT_FEED_VARIABLE
```

Example query URLs:

```text
http://YOUR-PRINTER-IP/printer/objects/query?filament_feed%20left
http://YOUR-PRINTER-IP/printer/objects/query?filament_feed%20right
http://YOUR-PRINTER-IP/printer/objects/query?gcode_macro%20_FILAMENT_FEED_VARIABLE
```

Observed slot mapping:

| Inventory Location | Moonraker Object |
|---|---|
| Snapmaker U1 - Slot 1 | `filament_feed left.extruder1` |
| Snapmaker U1 - Slot 2 | `filament_feed left.extruder0` |
| Snapmaker U1 - Slot 3 | `filament_feed right.extruder2` |
| Snapmaker U1 - Slot 4 | `filament_feed right.extruder3` |

Example state:

```json
{
  "filament_feed left": {
    "extruder1": {
      "filament_detected": false,
      "channel_state": "wait_insert"
    },
    "extruder0": {
      "filament_detected": true,
      "channel_state": "load_finish"
    }
  }
}
```

A `false` to `true` transition on `filament_detected` can be used to detect that a spool was loaded into a specific U1 slot.

---

## RFID / NFC Notes

The Snapmaker U1 may detect RFID/NFC internally and update the printer screen, but the actual RFID UID is not necessarily exposed through Moonraker.

Objects checked so far:

- `save_variables`
- `filament_feed left`
- `filament_feed right`
- `gcode_macro _FILAMENT_FEED_VARIABLE`
- `filament_parameters`
- `configfile`

Known findings:

- `filament_feed left/right` exposes slot load state.
- `filament_parameters` exposes material/profile settings.
- `save_variables` can store custom variables.
- The exposed objects checked so far do not show the raw RFID UID.

Current practical solution:

- Use slot-load detection.
- Prompt the user to assign an existing spool or add a new spool.
- Update the spool location to the loaded U1 slot.

Future improvement:

- If Paxx12 exposes a real RFID/NFC object or variable later, the inventory page can poll that key directly and auto-match the spool by UID.

---

## Suggested Inventory Workflow

1. Open the inventory dashboard.
2. Insert filament into a Snapmaker U1 slot.
3. The dashboard detects the slot change.
4. Choose the matching spool from the prompt.
5. The spool location changes to:

```text
Snapmaker U1 - Slot 1
Snapmaker U1 - Slot 2
Snapmaker U1 - Slot 3
Snapmaker U1 - Slot 4
```

6. If the spool is new, choose `NEW` and add it to inventory.
7. After printing, filament usage can be deducted from the assigned spool.

---

## Troubleshooting

### The inventory folder disappeared

Do not store the app directly under:

```text
/home/lava/
```

Move it to:

```text
/home/lava/printer_data/config/filament_inventory/
```

or, as an alternate option:

```text
/home/lava/printer_data/gcodes/filament_inventory/
```

---

### `systemctl: command not found`

The U1 environment may not use `systemd`. Use:

```bash
nginx -t
nginx -s reload
```

or:

```bash
service nginx reload
```

or:

```bash
/etc/init.d/nginx reload
```

---

### LED controls stopped working

This usually means the full Snapmaker/Paxx12 printer configuration was removed or Klipper entered an error state.

Do not run only the filament tracker block by itself. Restore the full working `printer.cfg` and add only the safe helper macros at the bottom.

The LED depends on config sections such as:

```ini
[led cavity_led]
```

If that section is missing, the printer will not know the LED exists.

---

### RFID stopped updating

The RFID reader depends on the full Snapmaker/Paxx12 configuration. Do not remove core config sections.

Look for sections such as:

```ini
[fm175xx_reader]
[filament_detect]
```

If these are removed or disabled, the printer screen and/or RFID behavior may stop working.

---

### Inventory page cannot connect to Moonraker

Check that the printer is ready and Moonraker is responding:

```text
http://YOUR-PRINTER-IP/printer/objects/list
```

Check save variables:

```text
http://YOUR-PRINTER-IP/printer/objects/query?save_variables
```

Expected variables include:

```json
{
  "pending_filament_mm": 0.0,
  "scanned_nfc": 0
}
```

---

### Nginx gives 403 or cannot access the folder

Fix permissions:

```bash
chmod 755 /home/lava
chmod 755 /home/lava/printer_data
chmod 755 /home/lava/printer_data/config
chmod -R 755 /home/lava/printer_data/config/filament_inventory
```

Then reload Nginx.

---

## Development Notes

The application is intentionally a single-file HTML/CSS/JavaScript dashboard for easier hosting on embedded printer environments.

Primary integration points:

```javascript
// Load inventory
GET /server/database/item?namespace=fluidd&key=custom_filament_spools

// Save inventory
POST /server/database/item

// Query Klipper variables
GET /printer/objects/query?save_variables

// Query U1 slot states
GET /printer/objects/query?filament_feed%20left&filament_feed%20right

// Run helper macros
POST /printer/gcode/script
```

---

## Roadmap

- [ ] Add polished slot assignment modal instead of browser `prompt()`
- [ ] Add dedicated Snapmaker U1 slot view
- [ ] Add automatic slot conflict handling
- [ ] Add material/color presets
- [ ] Add spool history log
- [ ] Add print usage history per spool
- [ ] Add backup/restore file versioning
- [ ] Add support for multiple printers
- [ ] Add true RFID auto-match if Paxx12 exposes UID through Moonraker
- [ ] Add optional Spoolman import/export compatibility

---

## Safety Notes

Editing `printer.cfg` can put Klipper into an error state. Always keep a backup of the working printer configuration before adding macros.

Recommended backup:

```bash
cp /home/lava/printer_data/config/printer.cfg /home/lava/printer_data/config/printer.cfg.backup
```

Do not remove core Snapmaker/Paxx12 configuration sections unless you know exactly what they do.

---

## License

Choose a license before publishing. MIT is a good default for a small open-source utility.

Example:

```text
MIT License
```

---

## Credits

Built for a Snapmaker U1 running Paxx12/Klipper/Moonraker as a local-first filament inventory and slot-tracking dashboard.
