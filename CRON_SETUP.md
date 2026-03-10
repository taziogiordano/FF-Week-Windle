# Daily Data Rebuild (Cron)

Run this once to install a daily 3:10 AM local cron job:

```bash
(crontab -l 2>/dev/null; echo "10 3 * * * cd /Users/taziogiordano/Documents/HTML && ./scripts/rebuild_data.sh >> /Users/taziogiordano/Documents/HTML/data/rebuild.log 2>&1") | crontab -
```

Check the latest log:

```bash
tail -n 80 /Users/taziogiordano/Documents/HTML/data/rebuild.log
```

Remove the cron job later:

```bash
crontab -l | grep -v "scripts/rebuild_data.sh" | crontab -
```
