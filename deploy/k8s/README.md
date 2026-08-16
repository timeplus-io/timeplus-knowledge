# Deploying tpk on Kubernetes

Manifests for running the Timeplus knowledge agent (chat agent + web UI +
ingest + MCP) on Kubernetes, in three modes:

| Mode | File | What runs | Use it for |
|------|------|-----------|------------|
| **All-in-one** | [`allinone.yaml`](allinone.yaml) | OSS **proton** + tpk in one pod (`timeplus/tpk` image, `TPK_DB_BACKEND=proton`) | demos, evaluations, single-node clusters — fully open-source, one object to manage |
| **DB + App** | [`enterprise.yaml`](enterprise.yaml) | stock **Timeplus Enterprise timeplusd** StatefulSet + a separate stateless tpk `app` Deployment | production, when tpk should also bring up the DB |
| **App only** | [`app-only.yaml`](app-only.yaml) | just the tpk `app` Deployment, pointed at an **already-running** Timeplus Enterprise | production, when Timeplus Enterprise is already deployed and managed separately |

All reference one Secret, [`secret.example.yaml`](secret.example.yaml)
(`tpk-secrets`), and share the `timeplus-knowledge` namespace. Pick **one** mode —
`enterprise.yaml` and `app-only.yaml` both define the `tpk-app` Deployment/Service,
so they're alternatives, not layers.

The images are the ones published by [`.github/workflows/docker-publish.yml`](../../.github/workflows/docker-publish.yml):
`timeplus/tpk-app` (app-only) and `timeplus/tpk` (all-in-one). Pin `:latest` to a
released tag (e.g. `timeplus/tpk-app:1.2.3`) for reproducible rollouts.

## Prerequisites

- A Kubernetes cluster and `kubectl` context pointing at it.
- A default StorageClass (the manifests use `PersistentVolumeClaim`s without
  naming one). Check with `kubectl get storageclass`; add `storageClassName:`
  to the claim specs if your cluster has no default.
- For **DB + App** mode: pull access to `docker.timeplus.com/timeplus/timeplusd`.
  If your cluster needs credentials, create a pull secret and reference it:

  ```sh
  kubectl -n timeplus-knowledge create secret docker-registry timeplus-registry \
    --docker-server=docker.timeplus.com \
    --docker-username=<user> --docker-password=<token>
  ```

  then uncomment `imagePullSecrets` on the `tpk-db` StatefulSet in `enterprise.yaml`.

## 1. Create the secret

Either edit `secret.example.yaml` and apply it, or create it imperatively so no
secret value is written to a file:

```sh
kubectl create namespace timeplus-knowledge
kubectl -n timeplus-knowledge create secret generic tpk-secrets \
  --from-literal=TIMEPLUS_PASSWORD='change-this-db-password' \
  --from-literal=OPENAI_API_KEY='sk-...' \
  --from-literal=GITHUB_TOKEN='ghp-...'      # omit keys you don't use
```

`TIMEPLUS_PASSWORD` is required — it provisions the `tpk` DB user and locks the
`default` user. Add `ANTHROPIC_API_KEY`, custom-gateway `*_BASE_URL`/`*_MODEL`
variables, etc. as needed; the key names are the environment variables tpk reads.

## 2a. All-in-one

```sh
kubectl apply -f allinone.yaml
kubectl -n timeplus-knowledge rollout status statefulset/tpk-allinone
```

## 2b. DB + App

```sh
kubectl apply -f enterprise.yaml
kubectl -n timeplus-knowledge rollout status statefulset/tpk-db
kubectl -n timeplus-knowledge rollout status deployment/tpk-app
```

## 2c. App only (existing Timeplus Enterprise)

Use this when timeplusd is **already deployed** (its own Helm chart / operator /
cluster) and you only want to add the agent. First edit `TIMEPLUS_HOST` and
`TIMEPLUS_USER` in `app-only.yaml` to point at your DB, and make sure
`TIMEPLUS_PASSWORD` in the Secret is that user's password. Then:

```sh
kubectl apply -f app-only.yaml
kubectl -n timeplus-knowledge rollout status deployment/tpk-app
```

The app connects to timeplusd's SQL-over-HTTP port, which is **fixed at 8123**.
If your DB is reachable at a different port, or lives outside the cluster, put a
Service in front of it that exposes 8123 and point `TIMEPLUS_HOST` at that
Service. For an out-of-cluster host, an `ExternalName` Service works:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: timeplusd
  namespace: timeplus-knowledge
spec:
  type: ExternalName
  externalName: timeplusd.your-db-host.example.com   # then TIMEPLUS_HOST: timeplusd
```

Or, to remap the port, a headless Service + an `EndpointSlice` pointing at the
DB's IP on its real port, exposed as `port: 8123`. The DB user in `TIMEPLUS_USER`
must already exist on your timeplusd — `app-only.yaml` does not provision users.

## 3. Build the knowledge graph (ingest)

The corpus is defined in the `repos.toml` baked into the image. Run ingest once
(and after corpus changes) via `exec` — the equivalent of `docker compose exec`:

```sh
# all-in-one
kubectl -n timeplus-knowledge exec -it tpk-allinone-0 -- tpk ingest
kubectl -n timeplus-knowledge exec -it tpk-allinone-0 -- tpk status

# DB + App, or App only
kubectl -n timeplus-knowledge exec deploy/tpk-app -- tpk ingest
kubectl -n timeplus-knowledge exec deploy/tpk-app -- tpk status
```

Ingest of the full Timeplus corpus is long-running and needs `GITHUB_TOKEN` (for
private repos) and an LLM key (for `semantic` repos). For a repeatable,
restart-surviving job, run it as a `Job` instead of `exec` — see
[Running ingest as a Job](#running-ingest-as-a-job).

## 4. Reach the web UI / chat agent

Quickest, no ingress needed:

```sh
# all-in-one
kubectl -n timeplus-knowledge port-forward svc/tpk-allinone 8000:8000
# DB + App, or App only
kubectl -n timeplus-knowledge port-forward svc/tpk-app 8000:8000
```

then open <http://localhost:8000>. For real access, point an Ingress (or switch
the access Service to `type: LoadBalancer`) at the `tpk-allinone` / `tpk-app`
Service on port 8000. Example Ingress:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: tpk
  namespace: timeplus-knowledge
spec:
  rules:
    - host: tpk.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: tpk-app        # or tpk-allinone
                port:
                  number: 8000
```

The chat endpoint streams over SSE — if you terminate TLS at the ingress, make
sure response buffering is disabled (e.g. nginx `proxy_buffering off`).

## Configuration

Every non-secret setting is settable by environment variable (and has a
`repos.toml` equivalent — see the repo README's **Configuration** section). Set
them in the container `env:` blocks. Common ones (some already set in the
manifests, others stubbed as commented-out examples):

| Env var | Purpose |
|---------|---------|
| `TPK_AGENT_PROVIDER` / `TPK_AGENT_MODEL` | chat-agent backend + model |
| `TPK_EXTRACTION_BACKEND` | `tpk ingest` semantic backend (`openai`\|`claude`\|`auto`) |
| `TPK_DB_BACKEND` | `timeplusd` (Enterprise, mutable streams) or `proton` |
| `TPK_DB_WAIT_SECONDS` | how long the app waits for the DB on boot |

### Custom corpus (`repos.toml`) via ConfigMap

To change which repos are indexed without rebuilding the image, put your own
`repos.toml` in a ConfigMap, mount it, and point `TPK_CONFIG` at it:

```sh
kubectl -n timeplus-knowledge create configmap tpk-corpus --from-file=repos.toml=./repos.toml
```

```yaml
# on the app / all-in-one container:
    env:
      - name: TPK_CONFIG
        value: /etc/tpk/repos.toml
    volumeMounts:
      - name: corpus
        mountPath: /etc/tpk/repos.toml
        subPath: repos.toml
# ...and in the pod spec:
    volumes:
      - name: corpus
        configMap:
          name: tpk-corpus
```

`TPK_CONFIG` is honored by `serve`, `ingest`, and the MCP server alike.

## Running ingest as a Job

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: tpk-ingest
  namespace: timeplus-knowledge
spec:
  backoffLimit: 1
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: ingest
          image: timeplus/tpk-app:latest
          command: ["tpk", "ingest"]
          env:
            - name: TIMEPLUS_HOST
              value: "tpk-db"          # or point at the all-in-one pod's service
            - name: TIMEPLUS_USER
              value: "tpk"
          envFrom:
            - secretRef:
                name: tpk-secrets
```

## Notes & caveats

- **Single-instance DB.** Both the `tpk-db` and `tpk-allinone` StatefulSets are
  `replicas: 1` — timeplusd/proton here is a single instance, not a cluster.
  Don't scale them; scale the corpus/graph vertically (bigger PVC, more memory).
- **Scaling the app.** The `tpk-app` Deployment mounts the `tpk-checkouts` PVC
  `ReadWriteOnce`, so it's pinned to one replica (`strategy: Recreate`). To run
  multiple app replicas, give each its own checkout cache (a
  `volumeClaimTemplates`-backed StatefulSet) or use a `ReadWriteMany` volume, and
  make sure only one ingester writes at a time.
- **Resources.** The `requests`/`limits` are modest starting points. timeplusd
  memory use grows with the corpus — raise the DB limits for the full Timeplus
  corpus (hundreds of thousands of nodes).
- **Storage sizing.** Data PVCs default to 10–20Gi and the checkout cache to
  5–10Gi. Git checkouts of the pinned corpus repos and the ingested graph can
  exceed these for large corpora; size up before ingesting.
- **Backups.** Snapshot the DB data PVC (`/var/lib/proton` or
  `/var/lib/timeplusd`), or use `tpk export` to write a portable bundle you can
  `tpk import` elsewhere.
- **All-in-one disk guard (dev tuning).** The `timeplus/tpk` image bakes in the
  dev override `deploy/timeplusd-dev/small-segments.yaml`, which — besides
  disabling nativelog preallocation — raises proton's disk-usage write guard to
  0.98 (default 0.9). That's a dev convenience, not production-safe. For a
  hardened all-in-one deployment, rebuild the image without that override (the
  DB + App mode's `enterprise.yaml` deliberately ships only the production-safe
  `preallocate: false` half and leaves the guard at its default).
