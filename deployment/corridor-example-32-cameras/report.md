# Corridor arterial_blvd: generated configuration

8 intersections, 32 cameras (26 counting), 8 signals bound in the SUMO map. Ready.

- Inventory: `arterial_32.yaml`
- TeraSim map (service path): `/data/output/store/maps/ed0947a56fee/base.sumocfg`
- Map extract: `arterial_8.osm`, map id `ed0947a56fee` (register-map.sh)
- Road graph for Headings: `arterial_8.osm`

## Intersections

| Intersection | Signal (SUMO) | Approaches (OSM) | Approaches (SUMO) | Headings edges N/E/S/W | Cameras |
|---|---|---|---|---|---|
| oak | `1001` | NB/EB/SB/WB | NB/EB/SB/WB | 4/2/4/2 | cam5=SB, cam6=WB, cam7=NB, cam8=EB |
| elm | `1002` | NB/EB/WB | NB/EB/WB | 2/2/2/2 | cam9=none, cam10=WB, cam11=NB, cam12=EB |
| pine | `1003` | NB/EB/SB/WB | NB/EB/SB/WB | 4/2/4/2 | cam1=SB, cam2=WB, cam3=NB, cam4=EB |
| cedar | `1004` | NB/EB/WB | NB/EB/WB | 4/2/0/2 | cam13=none, cam14=WB, cam15=NB, cam16=EB |
| maple | `1005` | EB/SB/WB | EB/SB/WB | 0/2/4/2 | cam17=SB, cam18=WB, cam19=none, cam20=EB |
| birch | `1006` | NB/EB/WB | NB/EB/WB | 4/2/0/2 | cam21=none, cam22=WB, cam23=NB, cam24=EB |
| ash | `1007` | EB/SB/WB | EB/SB/WB | 0/2/4/2 | cam25=SB, cam26=WB, cam27=none, cam28=EB |
| walnut | `1008` | EB/SB/WB | EB/SB/WB | 2/2/2/2 | cam29=SB, cam30=WB, cam31=none, cam32=EB |

## Calibration

| Camera | Intersection | Status | Source | Note |
|---|---|---|---|---|
| cam5 | oak | copied from another intersection | calibration.sample.json:cam1 | image<->geo points still describe pine: detections are placed at pine, not at oak |
| cam6 | oak | copied from another intersection | calibration.sample.json:cam2 | image<->geo points still describe pine: detections are placed at pine, not at oak |
| cam7 | oak | copied from another intersection | calibration.sample.json:cam3 | image<->geo points still describe pine: detections are placed at pine, not at oak |
| cam8 | oak | copied from another intersection | calibration.sample.json:cam4 | image<->geo points still describe pine: detections are placed at pine, not at oak |
| cam9 | elm | copied from another intersection | calibration.sample.json:cam1 | image<->geo points still describe pine: detections are placed at pine, not at elm |
| cam10 | elm | copied from another intersection | calibration.sample.json:cam2 | image<->geo points still describe pine: detections are placed at pine, not at elm |
| cam11 | elm | copied from another intersection | calibration.sample.json:cam3 | image<->geo points still describe pine: detections are placed at pine, not at elm |
| cam12 | elm | copied from another intersection | calibration.sample.json:cam4 | image<->geo points still describe pine: detections are placed at pine, not at elm |
| cam1 | pine | copied | calibration.sample.json:cam1 |  |
| cam2 | pine | copied | calibration.sample.json:cam2 |  |
| cam3 | pine | copied | calibration.sample.json:cam3 |  |
| cam4 | pine | copied | calibration.sample.json:cam4 |  |
| cam13 | cedar | copied from another intersection | calibration.sample.json:cam1 | image<->geo points still describe pine: detections are placed at pine, not at cedar |
| cam14 | cedar | copied from another intersection | calibration.sample.json:cam2 | image<->geo points still describe pine: detections are placed at pine, not at cedar |
| cam15 | cedar | copied from another intersection | calibration.sample.json:cam3 | image<->geo points still describe pine: detections are placed at pine, not at cedar |
| cam16 | cedar | copied from another intersection | calibration.sample.json:cam4 | image<->geo points still describe pine: detections are placed at pine, not at cedar |
| cam17 | maple | copied from another intersection | calibration.sample.json:cam1 | image<->geo points still describe pine: detections are placed at pine, not at maple |
| cam18 | maple | copied from another intersection | calibration.sample.json:cam2 | image<->geo points still describe pine: detections are placed at pine, not at maple |
| cam19 | maple | copied from another intersection | calibration.sample.json:cam3 | image<->geo points still describe pine: detections are placed at pine, not at maple |
| cam20 | maple | copied from another intersection | calibration.sample.json:cam4 | image<->geo points still describe pine: detections are placed at pine, not at maple |
| cam21 | birch | copied from another intersection | calibration.sample.json:cam1 | image<->geo points still describe pine: detections are placed at pine, not at birch |
| cam22 | birch | copied from another intersection | calibration.sample.json:cam2 | image<->geo points still describe pine: detections are placed at pine, not at birch |
| cam23 | birch | copied from another intersection | calibration.sample.json:cam3 | image<->geo points still describe pine: detections are placed at pine, not at birch |
| cam24 | birch | copied from another intersection | calibration.sample.json:cam4 | image<->geo points still describe pine: detections are placed at pine, not at birch |
| cam25 | ash | copied from another intersection | calibration.sample.json:cam1 | image<->geo points still describe pine: detections are placed at pine, not at ash |
| cam26 | ash | copied from another intersection | calibration.sample.json:cam2 | image<->geo points still describe pine: detections are placed at pine, not at ash |
| cam27 | ash | copied from another intersection | calibration.sample.json:cam3 | image<->geo points still describe pine: detections are placed at pine, not at ash |
| cam28 | ash | copied from another intersection | calibration.sample.json:cam4 | image<->geo points still describe pine: detections are placed at pine, not at ash |
| cam29 | walnut | copied from another intersection | calibration.sample.json:cam1 | image<->geo points still describe pine: detections are placed at pine, not at walnut |
| cam30 | walnut | copied from another intersection | calibration.sample.json:cam2 | image<->geo points still describe pine: detections are placed at pine, not at walnut |
| cam31 | walnut | copied from another intersection | calibration.sample.json:cam3 | image<->geo points still describe pine: detections are placed at pine, not at walnut |
| cam32 | walnut | copied from another intersection | calibration.sample.json:cam4 | image<->geo points still describe pine: detections are placed at pine, not at walnut |

## Issues

None.

## Files

- `report.json`
- `report.md`
- `terasim/Metadata.json`
- `terasim/corridor-configmap.yaml`
- `terasim/register-intersections.sh`
- `terasim/register-map.sh`
- `terasim/scheduled_config.yaml`
- `terasim/values-corridor.yaml`
- `vss/calibration.json`
- `vss/register-vst-streams.sh`
- `vss/rtsp-fanout.yaml`
- `vss/vst-streams.json`
