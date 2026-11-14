# Home Twin for Home Assistant

Backend integration for a **digital twin of your home**: it stores the floor plan used by the
[floor plan card](https://github.com/SysriX/floor_plan_ha_card) in MariaDB/MySQL with full revision history, and it adds
**BLE indoor tracking** – beacons, tags and phones (via IRK) are located per room using ESPHome Bluetooth proxies and can be
assigned to Home Assistant persons.

![Home Twin preview](docs/images/preview.png)
*Floor plan stored by Home Twin, shown with the [Floor Plan Card](https://github.com/SysriX/floor_plan_ha_card), including BLE proxies and located tags (sample apartment, UI in German)*

![Home Twin BLE persons](docs/images/ble-persons.png)
*Assigning BLE devices to persons with the [Home Twin BLE Cards](https://github.com/SysriX/home_twin_ble_ha_card) (UI in German)*

- Domain: `home_twin`
- Version: 0.6.0
- UI language of entity names and messages: German

> Built for the author's own home. It works generically, but some parts are tailored to it – see
> [Limitations](#limitations).

## Features

### Floor plan storage
- Plan (floors, rooms, devices, windows, doors, furniture …) stored as JSON in the database; every save creates a revision.
- Optimistic locking: saving with an outdated `base_revision` returns `conflict`.
- Relational views of the plan are written on each save (`ht_floor`, `ht_room`, `ht_item`, `ht_view`) for SQL/Grafana.
- On first start without a plan, the plan is imported once from an existing Lovelace dashboard containing a
  `custom:haus-grundriss` / `custom:home-twin-card` card (default dashboard: `haus-grundriss`).
- If the database is unavailable, a read-only local copy (`.storage/home_twin.cache`) is served; reconnect every 60 s.

### BLE tracking
- Listens to all BLE advertisements received by Home Assistant's Bluetooth integration (local adapters and ESPHome proxies).
- Decodes iBeacon, AltBeacon, Eddystone (UID/URL/TLM/EID) and KKM/BlueCharm frames (KSensor, system data); resolves
  random addresses of phones with their IRK.
- Added tags become HA devices with:
  - `device_tracker` (home / not home, received by any proxy within 120 s)
  - sensors **Raum** (room = area of the strongest proxy, or position in the floor plan), **Nächster Proxy**, **Signal** (dBm),
    **Zuletzt gesehen** (last seen)
  - one sensor per value the tag actually sends (battery, voltage, temperature, humidity, acceleration, button, motion, lux, CO₂ …)
- Tags can be assigned to HA persons (flags *presence* and *locate*). The first assigned tag creates:
  - `device_tracker.haus_sys_ble_<person>` (home/not_home, attributes room, floor, x, y, tag, proxy, rssi)
  - `sensor.haus_sys_ble_<person>_room`
- Generates ESPHome configs for new BLE proxies (`/config/esphome/<room>-ble-<nr>.yaml`, own API key and passwords) that
  include the shared package [`esphome/common/ble-proxy.yaml`](esphome/common/ble-proxy.yaml).
- Custom icons/images per tag.

## Services

| Service | Description |
|---|---|
| `home_twin.ble_status` | Returns all added BLE devices (room, nearest proxy, RSSI, seconds since last seen) and per person: home, room, floor, x/y. Response only (`response_variable`). Useful e.g. to open a door only if a phone is nearby. |

WebSocket API used by the cards: `home_twin/status`, `home_twin/plan/get|subscribe|save|import_lovelace`,
`home_twin/revisions/list|get`, `home_twin/ble/subscribe`, `home_twin/ble/tag/set|image|set_irk|list|remove|assign`,
`home_twin/ble/proxy/list|create`, `home_twin/ble/persons`. Saving plans and managing tags requires an admin user.

## Requirements

- Home Assistant 2024.6 or newer
- MariaDB or MySQL (the Home Assistant container already ships the `mysqlclient` driver for `mysql://` URLs);
  SQLite also works for testing.
- For BLE: the Bluetooth integration with at least one adapter or ESPHome Bluetooth proxy (active scanning recommended).

## Installation

### HACS (custom repository)

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/SysriX/home_twin_ha_integration`, category **Integration**.
2. Install **Home Twin** and restart Home Assistant.

### Manual

Copy `custom_components/home_twin` to `/config/custom_components/home_twin` and restart Home Assistant.

## Configuration

1. Create the database once with [`setup-db.sql`](setup-db.sql) (replace `CHANGE_ME`). Tables are created automatically.
2. `secrets.yaml`:
   ```yaml
   home_twin_db_url: mysql://home_twin:YOUR_PASSWORD@db.example.lan/home_twin?charset=utf8mb4
   ```
3. `configuration.yaml`:
   ```yaml
   home_twin:
     db_url: !secret home_twin_db_url
     # import_dashboard: haus-grundriss   # dashboard to import the initial plan from
     # keep_revisions: 200                # revisions to keep (1–10000)
   ```
4. Restart. The config entry "Home Twin" is created automatically (it only hosts the BLE devices/entities);
   adding it via *Settings → Devices & services* is not supported – YAML is the only setup path.

For BLE proxies created by the card, copy [`esphome/common/ble-proxy.yaml`](esphome/common/ble-proxy.yaml) to
`/config/esphome/common/ble-proxy.yaml` and define `wifi_ssid` / `wifi_password` in your ESPHome `secrets.yaml`.

## Companion cards

- [floor_plan_ha_card](https://github.com/SysriX/floor_plan_ha_card) – floor plan editor/viewer (`custom:haus-grundriss`), 2D/3D
- [home_twin_ble_ha_card](https://github.com/SysriX/home_twin_ble_ha_card) – live list of all BLE devices seen by your proxies; add tags, manage proxies
- [ble_pairing_ha_card](https://github.com/SysriX/ble_pairing_ha_card) – pair phones/watches via their IRK (ESP32 IRK reader)
- [home_twin_app_ha_card](https://github.com/SysriX/home_twin_app_ha_card) – Home Twin dashboard app with navigation

## Limitations

- The proxy generator uses a fixed list of room codes and German area names
  (`fl` Flur, `bz` Badezimmer, `ku` Küche, `sz` Schlafzimmer, `bu` Büro, `wz` Wohnzimmer, `ke` Keller, `ba` Balkon) in
  `ble_proxies.py` (`ROOMS`). Edit it for your home.
- Person entities follow the author's naming scheme (`haus_sys_ble_<person>`).

## Credits

KKM/BlueCharm frame formats are taken from the official SDK
[kkmhogen/android_kbeaconlib2](https://github.com/kkmhogen/android_kbeaconlib2) (MIT).

## License

MIT – see [LICENSE](LICENSE).
