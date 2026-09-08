# Javis OS - Quick start

*[Vietnamese](QUICKSTART.md) · **English***

## Option 1 - Hostinger VPS

1. Open hPanel, choose **VPS**, then **Docker Manager**, **Compose**, and **Compose from URL**.
2. Paste:
   ```
   https://raw.githubusercontent.com/ThuanNgcodelor/javis-os/main/deploy/docker/docker-compose.hostinger.yml
   ```
3. Deploy, then create the administrator account when the app first opens.

## Option 2 - Docker on any machine or VPS

```bash
docker compose -f deploy/docker/docker-compose.yml up -d
```

Open <http://localhost:7777>. Runtime state remains in Docker volumes; do not remove those volumes while updating.

## Option 3 - Windows without Docker

Run [`deploy/windows/setup.bat`](deploy/windows/setup.bat) once, then open [`JAVIS OS.bat`](deploy/windows/JAVIS OS.bat).

For detailed deployment and recovery instructions, read [DEPLOY.md](DEPLOY.md).
