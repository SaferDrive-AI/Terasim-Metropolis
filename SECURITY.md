# Security policy

## Supported versions

| Version | Supported |
| --- | --- |
| 0.1.x | yes |

## Reporting a vulnerability

Please do not report security vulnerabilities through public issues. Report them privately
through GitHub's private vulnerability reporting for this repository ("Security" tab,
"Report a vulnerability").

Include a description of the issue, the affected version and component, steps to reproduce,
and the impact you expect. We acknowledge reports within five business days and coordinate a
fix and disclosure timeline with the reporter.

## Deployment hardening

The deployment guides describe the security-relevant settings: the container image runs as
a non-root user with an arbitrary uid and a read-only root filesystem, the Helm chart enables
Redis authentication and per-component NetworkPolicies, the MCP server is reachable only from
the namespaces you allow, and `TERASIM_CITY_OSM_DOWNLOAD=0` disables the only outbound call
the service makes.
