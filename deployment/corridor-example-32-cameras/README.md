# Example: files generated for a 32-camera corridor

Output of `python3 apps/deploy/city/camera-config/camcfg.py generate apps/deploy/city/camera-config/examples/arterial_32.yaml -o corridor-example-32-cameras`
(synthetic eight-signal corridor, 32 camera streams). Order of use: documentation site,
Deployment, "Corridor and cameras".

| File | Use |
| --- | --- |
| `report.md`, `report.json` | signal of each intersection, derived tables, issues (none) |
| `terasim/Metadata.json`, `terasim/scheduled_config.yaml` | TMC spec and corridor configuration |
| `terasim/values-corridor.yaml`, `terasim/corridor-configmap.yaml` | the same as Helm values or as a ConfigMap |
| `terasim/register-map.sh`, `terasim/register-intersections.sh` | store the map, register the intersections |
| `vss/vst-streams.json`, `vss/register-vst-streams.sh`, `vss/rtsp-fanout.yaml` | the 32 VST stream registrations and the test-feed RTSP server |
| `vss/calibration.json` | calibration entries for the VA-API upload |
