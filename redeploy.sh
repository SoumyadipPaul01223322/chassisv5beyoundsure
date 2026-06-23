#!/bin/bash
# Fetch and hard reset to match main remote branch
git fetch origin
git reset --hard origin/main

# Rebuild and run the chassis-v5 docker container on port 5005
docker rm -f chassis-v5 2>/dev/null && \
docker build -t chassis-v5 . && \
docker run -d \
  --name chassis-v5 \
  --network dokploy-network \
  -p 5005:5005 \
  --env-file .env \
  --label traefik.enable=true \
  --label 'traefik.http.routers.chassis-v5.rule=Host("chassiss-yound-jjjks9mmskmksfjkjjhsfkjJjhjjjsfuii9934.unknownbatter.online")' \
  --label 'traefik.http.routers.chassis-v5.entrypoints=web,websecure' \
  --label 'traefik.http.routers.chassis-v5.tls=true' \
  --label 'traefik.http.services.chassis-v5.loadbalancer.server.port=5005' \
  chassis-v5
