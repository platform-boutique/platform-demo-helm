# kafka

Single-node Apache Kafka (KRaft, broker and controller in one pod) for the `orders.placed` events of checkout-service, order-service and email-service. Test-grade: one broker, replication factor 1, no authentication. It is replaced by a managed Kafka (MSK) when the shop moves to one..

Built on the shared [`platform-common`](../platform-common) library chart: hardened pod spec, probes,
resources, NetworkPolicy, ServiceAccount.

```bash
helm dependency update            # vendors ../platform-common into charts/
helm upgrade --install kafka . -n platform-demo --create-namespace \
  --set image.tag=latest
```

* Service DNS name: `kafka` (port `9092`). Names are fixed to the chart name on purpose; other services are configured with them.
* All settings and their defaults are documented inline in [`values.yaml`](values.yaml); `values.schema.json` validates the important ones.
* More context for this service: its repository README and the master `README.md` of platform-demo.
