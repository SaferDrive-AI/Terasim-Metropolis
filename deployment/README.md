# Deployment

| Folder | What | Start with |
| --- | --- | --- |
| `brev/` | VSS and TeraSim on one NVIDIA Brev GPU instance (Docker Compose), the reference deployment with VSS 3.3.0, SUMO and MTR modes | `brev/README.md` |
| `helm/` | Helm chart `terasim-phase1-0.6.0.tgz` (Kubernetes, Red Hat OpenShift `restricted-v2`) and example OpenShift values | `helm upgrade --install terasim-phase1 helm/terasim-phase1-0.6.0.tgz -n <namespace> -f helm/values-openshift.example.yaml` |
| `compose/` | the repository's Docker Compose stack on one host | `compose/README.md` |
| `vss-integration/` | VSS agent configuration snippet, TMC collector settings, detector labels and behavior analytics | `vss-integration/labels-and-analytics.md` |
| `corridor-example-32-cameras/` | files the camera-inventory generator writes for a synthetic eight-signal, 32-camera corridor | `corridor-example-32-cameras/README.md` |
