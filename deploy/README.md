# Deploying

Secrets never go in git. Put them in `.env.prod` on the server only:

    gcloud compute scp .env.prod accountable:~/AccountableAI/.env.prod --zone=us-central1-a
    ssh in, then: chmod 600 ~/AccountableAI/.env.prod

Rebuild and restart:

    cd ~/AccountableAI/deploy
    sudo DOMAIN=tryaccountableai.com docker compose -f docker-compose.prod.yml up -d --build
