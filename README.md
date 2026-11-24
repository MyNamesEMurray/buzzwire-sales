# Buzzwire Sales

Self-hosted Steam sales dashboard. Python stdlib, SQLite, one container.

## Setup

1. Steamworks > Users & Permissions > Manage Groups > Create Financial API Group. Copy the key.
2. Copy `.env.example` to `.env`, paste the key. Optionally set `APP_IDS=123456` (Buzzwire app ID).
3. `docker compose up -d` (pulls `ghcr.io/mynamesemurray/buzzwire-sales:latest`, built by GitHub Actions on every push to `main`)
4. Open `http://<host>:8080`

## Config

| Var | Default | Meaning |
| --- | --- | --- |
| `STEAM_FINANCIAL_KEY` | required | Financial API key |
| `APP_IDS` | all | Comma list of app IDs to keep |
| `POLL_SECONDS` | 120 | Steam sales poll interval |
| `PLAYER_POLL_SECONDS` | 300 | Current players sample interval (reviews every 3rd, wishlist every 6th tick) |

Players, reviews and wishlist need one app ID. Set `APP_IDS`, or leave it empty if your sales contain exactly one app.

## Notes

- Uses `GetChangedDatesForPartner` + `GetDetailedSales`. Changed dates are deleted and re-fetched, as Steam recommends.
- Revenue is Steam's reported figure: before refund adjustments, taxes, Steam cut.
- No auth. Put it behind your reverse proxy / SSO. Never expose it raw.
- Financial key can be IP-restricted in Steamworks.
- Chart.js loads from jsdelivr CDN.
