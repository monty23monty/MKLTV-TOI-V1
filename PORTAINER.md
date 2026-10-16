# Deploy Lightning TV TOI on Linux with Portainer

This stack targets a **Linux Docker Standalone environment** in Portainer CE or BE. It runs nginx/React, one FastAPI worker and PostgreSQL 17. Only the dashboard port is published. Match data, shifts and history live in a persistent PostgreSQL named volume.

Use [compose.portainer.yaml](compose.portainer.yaml) in **Stacks → Add stack → Web editor** after building the two application images below. The application images are local images, not images published to Docker Hub. Portainer documents limitations around building repository source during stack deployment, so image building is a separate, explicit step ([Portainer build FAQ](https://docs.portainer.io/faqs/troubleshooting/can-i-build-an-image-while-deploying-a-stack-application-from-git)).

## 1. Build the images in Portainer

Select the **Docker environment that will run the stack**. If Portainer manages a remote host through an agent, build on that remote host, not on the Portainer server's own Docker engine.

Open **Images → Build a new image**, use the URL build option/URL field, and build these two images separately:

| Field | Backend build | Frontend build |
| --- | --- | --- |
| Image name/tag | `mkltoi-backend:portainer-v1` | `mkltoi-frontend:portainer-v1` |
| Git URL | `https://github.com/monty23monty/MKLTV-TOI-V1.git#codex/ipad-operator` | Same URL |
| Dockerfile path | `Dockerfile.backend` | `Dockerfile.frontend` |

In the URL form, **Dockerfile path is a separate input below the URL**. Replace its `Dockerfile` value with the exact filename above for each build. The repository deliberately has no root file named `Dockerfile`: an error ending in `Cannot locate specified Dockerfile: Dockerfile` means the build is still searching for that default name. If the error keeps naming `Dockerfile` after you enter `Dockerfile.backend` or `Dockerfile.frontend`, use the SSH method below to pass the filename directly to Docker.

Use the Git repository URL, including the branch fragment, so the build has the complete source tree for `COPY`. A raw Dockerfile URL alone is insufficient. Leave the frontend build argument `VITE_API_BASE_URL` empty: nginx supplies the same-origin API/WebSocket proxy.

Wait for each build to finish successfully and confirm that both tags appear in **Images**. Docker downloads the base images and dependencies during these builds. The target host needs outbound access to GitHub, Docker Hub, PyPI and the npm registry. No registry login is needed for this public repository.

These steps follow [Portainer's image build workflow](https://docs.portainer.io/user/docker/images/build) and [Docker's Git-context branch syntax](https://docs.docker.com/build/concepts/context/#url-fragments). If your Portainer/Docker combination does not support the URL builder, use the SSH method below.

## 2. Create the stack

1. Open **Stacks → Add stack** and name it **`lightning-toi`**.
2. Select **Web editor** and paste the complete [compose.portainer.yaml](compose.portainer.yaml).
3. In **Environment variables**, add the values below. Use two different long random values for the secrets; do not commit your real values to Git.

| Variable | Value |
| --- | --- |
| `POSTGRES_PASSWORD` | Your database password; required |
| `TOI_AUTH_TOKEN` | Your operator access token; required |
| `TOI_IMAGE_TAG` | `portainer-v1` |
| `TOI_FRONTEND_PORT` | `8080`, or another unused host port |
| `TOI_CORS_ORIGINS` | The exact dashboard origin, e.g. `http://192.168.1.50:8080`, without a trailing slash |

Replace the example address with **your Linux Docker host's LAN IP**. For HTTPS use the external HTTPS origin instead, such as `https://toi.example.com`. Multiple allowed origins can be separated with commas. The optional defaults are in [portainer.env.example](portainer.env.example); you can also use that file as a template when preparing environment values.

4. Leave **Re-pull image / Pull latest image** disabled for the two local application images. Their `pull_policy: never` means they must already exist on this Docker host. PostgreSQL is a public image and is pulled normally if missing.
5. Click **Deploy the stack** and wait for `db`, `backend` and `frontend` to become healthy.

The backend applies database migrations at startup and creates a demonstration match only if it is absent. One worker is intentional: live WebSocket notifications currently use an in-process bus. Keep the backend at one replica.

A first deployment creates a new server database. Your local development SQLite file is not copied into the images; existing local training sessions are not automatically transferred to PostgreSQL.

The stack uses a named volume, so it does not require Portainer's Business Edition relative bind-mount feature. This file is for Docker Standalone, not a Swarm or Kubernetes deployment.

## 3. Open it on the iPad

On the same network, open:

```text
http://YOUR-LINUX-HOST-IP:8080
```

Use the port chosen above. `localhost` on the iPad points to the iPad, not the Linux host. The frontend binds to the host's LAN interfaces by default; the database and backend have no published host ports. If the host has a firewall, permit the selected dashboard port from the operator network.

At the initial operator-token prompt, enter the same `TOI_AUTH_TOKEN` configured in the stack and connect. The token is retained in that browser tab's session storage. Load/import your match, then use **Operator Full Screen**. Safari uses native fullscreen where permitted and the explicitly labelled focus layout otherwise.

Quick checks:

- `http://YOUR-LINUX-HOST-IP:8080/health` should return JSON with `"status":"ok"`.
- All three containers should be healthy in Portainer.
- The operator controls should enable after the live WebSocket connects.
- On a training match, try a substitution and Undo, then reload and confirm that the match resumes.

## Alternative: build over SSH on the Docker host

This produces exactly the same two local images. Run it on the Docker endpoint that Portainer will deploy to, with Git and Docker installed:

```bash
git clone --branch codex/ipad-operator --single-branch https://github.com/monty23monty/MKLTV-TOI-V1.git
cd MKLTV-TOI-V1
docker build --pull -f Dockerfile.backend -t mkltoi-backend:portainer-v1 .
docker build --pull -f Dockerfile.frontend -t mkltoi-frontend:portainer-v1 .
```

Then deploy through Portainer's Web editor as above. Alternatively, deploy with Docker Compose v2 directly:

```bash
cp portainer.env.example .env.portainer
chmod 600 .env.portainer
# Edit .env.portainer: set both secrets and your actual dashboard origin.
docker compose --env-file .env.portainer -p lightning-toi -f compose.portainer.yaml config --quiet
docker compose --env-file .env.portainer -p lightning-toi -f compose.portainer.yaml up -d --wait
```

Use one management method for a running stack. A stack created through the Docker CLI may appear in Portainer with limited editing capabilities; create it through Portainer if you want Portainer to own its definition.

## HTTPS / an existing reverse proxy

Point your existing reverse proxy at the frontend port and enable WebSocket upgrade forwarding. Set `TOI_CORS_ORIGINS` to the exact external HTTPS origin. The explicit origin matters when HTTPS terminates before the container's HTTP listener. Keep database/backend ports private. A public deployment should use HTTPS so the operator token is transported over TLS.

The frontend uses relative `/api` URLs; do not enter a backend container name, `localhost:8000`, or a separate API URL into Safari. Docker service DNS names are internal to the containers. nginx re-resolves the backend through Docker DNS after container recreation.

## Updates and rollback

Pause tracking and take a database backup before an application upgrade.

1. Build both application images again from the chosen source revision using a new tag, for example `mkltoi-backend:portainer-v2` and `mkltoi-frontend:portainer-v2`.
2. Edit the existing stack's `TOI_IMAGE_TAG` to `portainer-v2` and update/redeploy. Keep re-pulling local application images disabled.
3. Keep the stack name, PostgreSQL volume, database name/user/password and PostgreSQL major version unchanged.
4. Check container health and the resumed match before starting the clock.

A Git source update alone does not rebuild these local images. Rebuilding with a new tag makes the deployed revision explicit and preserves the previous images. For an app-image rollback, select the previous tag; if a schema migration is incompatible with the older app, restore the matching database backup as well. Do not downgrade PostgreSQL by changing its image tag against the same data directory.

PostgreSQL's initialization environment is applied when its data volume is empty. Changing `POSTGRES_PASSWORD` in an existing stack does **not** rotate the stored database password. Perform a deliberate database password change before updating the environment, otherwise the backend will no longer connect ([PostgreSQL image documentation](https://hub.docker.com/_/postgres)).

## Back up and restore match data

The named volume normally appears as `lightning-toi_postgres_data`; Portainer shows its actual name under **Volumes**. Container replacement and ordinary stack updates retain it. Do not delete the volume or select a volume-removal option when removing the stack if you want to retain matches. Changing the stack name can create a different empty volume.

For a logical backup, find the actual `db` container name in Portainer and run this on the Linux Docker host:

```bash
docker exec YOUR-DB-CONTAINER pg_dump -U toi -d lightning_toi -Fc > lightning-toi-backup.dump
```

Adjust the database/user if you changed their defaults. Keep the dump outside the Docker host too. A Portainer configuration backup does not by itself back up this PostgreSQL data.

To restore, stop the backend and frontend, keep PostgreSQL running, and restore the intended dump into the target database:

```bash
docker exec -i YOUR-DB-CONTAINER pg_restore -U toi -d lightning_toi --clean --if-exists --no-owner < lightning-toi-backup.dump
```

Restoring replaces the target database's application data. Start the backend and frontend again and verify the restored match before operating.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `pull access denied` for an application image | Both local images must exist on the selected Docker host with the exact tag. Disable re-pulling them. |
| Compose reports a missing password/token | Set the two required variables in the stack's Environment variables section. |
| Backend is unhealthy | Read backend logs for migration/DB connectivity errors; check PostgreSQL health and retained-volume credentials. |
| iPad cannot reach the page | Use the Linux host's LAN IP and chosen port, not localhost; check host firewall and Wi-Fi routing. |
| Page loads but controls stay disabled | Check the WebSocket connection, operator token and exact `TOI_CORS_ORIGINS`, particularly behind HTTPS. |
| Backend was recreated and nginx briefly returns 502 | Allow its Docker DNS cache to refresh (10 seconds), then check backend health. |
| Rosters appear empty after redeployment | Check that the same stack name and PostgreSQL volume are attached; do not overwrite the old volume. |
| Port 8080 is already used | Choose another `TOI_FRONTEND_PORT` and update the dashboard URL/origin. |

The repository's **Linux container smoke test** builds both images, deploys this image-only stack with PostgreSQL, exercises nginx/API/WebSocket/authentication and tracking, and checks persistence across container recreation. It publishes no container images. The workflow's result is available under the repository's **Actions** tab; deployment on your own Portainer host still needs the image-build steps above.

The initial Linux run passed: [container stack smoke test](https://github.com/monty23monty/MKLTV-TOI-V1/actions/runs/38946708889).
