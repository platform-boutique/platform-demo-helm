# user-db

PostgreSQL 17 for the user-service; the service creates its schema itself on start-up..

Built on the shared [`platform-common`](../platform-common) library chart: hardened pod spec, probes,
resources, NetworkPolicy, ServiceAccount.

```bash
helm dependency update            # vendors ../platform-common into charts/
helm upgrade --install user-db . -n platform-demo --create-namespace \
  --set auth.password=<alphanumeric> \
  --set image.tag=17-alpine
```

* Service DNS name: `user-db` (port `5432`). Names are fixed to the chart name on purpose; other services are configured with them.
* All settings and their defaults are documented inline in [`values.yaml`](values.yaml); `values.schema.json` validates the important ones.
* More context for this service: its repository README and the master `README.md` of platform-demo.
