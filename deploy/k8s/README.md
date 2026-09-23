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
Both are multi-arch (`linux/amd64` + `linux/arm64`), so they schedule onto
x86 and ARM (e.g. Graviton) nodes alike without a `nodeSelector`.

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

**The `tpk` database.** All tpk streams live under a dedicated database (`tpk`
by default, `TIMEPLUS_DATABASE`), which the app creates on startup (`CREATE
DATABASE IF NOT EXISTS`). Against a shared, externally-managed Timeplus this is
the one extra grant to check: the `TIMEPLUS_USER` needs **CREATE DATABASE** (the
first time) plus read/write on that database. If that user isn't allowed to
create databases, pre-create it and grant access, then keep `TIMEPLUS_DATABASE`
pointed at it:

```sql
CREATE DATABASE IF NOT EXISTS tpk;
-- grant your tpk user read/write on tpk (per your Timeplus access model)
```

This only applies to app-only: `enterprise.yaml` and `allinone.yaml` provision a
`tpk` user with full privileges, so database creation just works there.

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

## Connect a coding agent (MCP)

Members need a role with the `explore` capability to see the **API tokens**
page and connect (plus `source:view` for the `read_source` tool); `admin` has
both.

`/mcp` (streamable HTTP, authenticated with a per-user API token) is served
by the same app container on the same port/Service as the web UI in all
three modes (`tpk-allinone` or `tpk-app`, port 8000) — no manifest change
needed. TLS terminates wherever it already does for the UI (in the app-only
reference deployment: NLB + ACM, no ALB/Ingress controller); if you do front
tpk with a path-based Ingress, make sure its rules don't exclude `/mcp` (the
example Ingress above uses `path: /` with `pathType: Prefix`, which already
covers it — `grep -rn "path" deploy/k8s/*.yaml` confirms every manifest here
uses that same unrestricted `/` prefix, on probes and the example Ingress
alike, so nothing path-filters `/mcp`).

1. In the web UI open **API tokens → New token**, name it, pick an expiry,
   and copy the token (shown once).
2. Register it:

       claude mcp add --transport http timeplus-knowledge https://<your-host>/mcp \
         --header "Authorization: Bearer tpk_…"

3. `claude mcp list` should show `timeplus-knowledge ✔ Connected`.

Denied `/mcp` requests are logged at WARNING on the `tpk.mcp` logger (tokens
are never logged). See the repo README's "Use from Claude Code (MCP)" section
for the full token lifecycle (revoke, expiry, admin revoke-on-disable).

## Upgrading

1. **Make sure the image exists before you touch the manifest.** A GitHub
   release does not guarantee an image: check that the `Docker` workflow run for
   the tag succeeded and the tag is on Docker Hub, e.g.
   `docker buildx imagetools inspect timeplus/tpk-app:<version>`. The `tpk-app`
   Deployment uses `strategy: Recreate` (single `ReadWriteOnce` checkout
   volume), so the old pod is stopped **first** — a missing image is an outage,
   not a no-op.
2. Pin the new tag (`image: timeplus/tpk-app:<version>`) and apply:

       kubectl apply -f app-only.yaml          # or enterprise.yaml / allinone.yaml
       kubectl -n timeplus-knowledge rollout status deployment/tpk-app

   Expect a short gap while the pod restarts. Schema changes need no manual
   step: `tpk serve` creates any new streams on startup (`CREATE … IF NOT
   EXISTS`), and the corpus, users, roles and API tokens live in the database,
   not the pod.
3. Verify: `curl -s -o /dev/null -w '%{http_code}\n' https://<host>/healthz`
   → `200`, and an unauthenticated `POST https://<host>/mcp` → `401`.
4. Roll back by re-applying the previous tag.

**Locked out of every admin account?** Run the break-glass command in the pod
(see the main README → Users & roles):

    kubectl -n timeplus-knowledge exec -it deploy/tpk-app -- tpk auth reset-admin

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
| `TIMEPLUS_DATABASE` | database all tpk streams live under (default `tpk`; app-only: user needs CREATE DATABASE) |
| `TPK_DB_WAIT_SECONDS` | how long the app waits for the DB on boot |
| `TPK_ANONYMOUS_ACCESS` / `TPK_ANONYMOUS_DAILY_TOKEN_LIMIT` | unauthenticated chat over the `public` corpus entries (off by default) and its shared daily token budget |
| `TPK_MCP_HTTP_ENABLED` / `TPK_MCP_ALLOWED_HOSTS` | the remote MCP endpoint (`/mcp`, on by default) and its optional `Host` allow-list |

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
  corpus (hundreds of thousands of nodes). The **app** pod also builds each repo's
  graph in memory during `tpk ingest`, so large repos need real headroom: ingesting
  `proton-enterprise` (code-only) is OOM-killed (exit 137) at the default `1Gi` app
  limit. Raise the `tpk-app` container's memory `limits` to **≥8Gi** before ingesting
  the full corpus — an OOM leaves that repo absent from the graph with no error row
  (the run log is written only on completion), so it silently fails to index.
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
